from dataclasses import dataclass


@dataclass
class Job:
    id: int
    raw_record_id: int
    attempts: int


def enqueue(conn, raw_record_id: int) -> int:
    return conn.execute(
        "insert into work_queue(raw_record_id) values(%s) returning id", (raw_record_id,)
    ).fetchone()[0]


def lease(conn, limit: int = 10, lease_seconds: int = 60) -> list[Job]:
    rows = conn.execute(
        """
        with picked as (
          select id from work_queue
          -- pending jobs (lease_until defaults to now() at enqueue, so eligible
          -- immediately) AND leased jobs whose lease has expired -> a crashed
          -- worker's job is reclaimed instead of being orphaned forever.
          where state in ('pending', 'leased') and lease_until < now()
          order by id
          for update skip locked
          limit %s
        )
        update work_queue w
           set state='leased',
               lease_until = now() + (%s || ' seconds')::interval,
               attempts = w.attempts + 1
          from picked
         where w.id = picked.id
        returning w.id, w.raw_record_id, w.attempts
        """,
        (limit, lease_seconds),
    ).fetchall()
    return [Job(*r) for r in rows]


def ack(conn, job_id: int) -> None:
    conn.execute("update work_queue set state='done' where id=%s", (job_id,))


def fail(conn, job_id: int, error: str, max_attempts: int = 5) -> None:
    conn.execute(
        """update work_queue
              set last_error=%s,
                  state = case when attempts >= %s then 'failed' else 'pending' end,
                  lease_until = now()
            where id=%s""",
        (error, max_attempts, job_id),
    )
