# T05 — Durable work queue (lease/ack/fail)

**Spec:** §5 Processing model (durable Postgres queue, `FOR UPDATE SKIP LOCKED`), D4.
**Depends on:** T02. **Branch:** `ticket/T05-work-queue`. **PR, do not merge without approval.**

## Goal
A durable, retryable job queue over the `work_queue` table. Concurrency-safe leasing so multiple workers never grab the same job.

## Files
- Create: `src/bellweather/queue.py`
- Test: `tests/test_queue.py` (requires `make up` + migrations)

## Interface (referenced by exact name in T06, T11)
```python
# queue.py
@dataclass
class Job:
    id: int
    raw_record_id: int
    attempts: int

def enqueue(conn, raw_record_id: int) -> int: ...                 # returns work_queue.id
def lease(conn, limit: int = 10, lease_seconds: int = 60) -> list[Job]: ...
def ack(conn, job_id: int) -> None: ...                           # state -> done
def fail(conn, job_id: int, error: str, max_attempts: int = 5) -> None:
    """increment attempts; back to 'pending' (retry) or 'failed' (dead-letter) if exhausted."""
```
All functions take an open `conn` and do NOT commit — the caller controls the transaction (so enqueue can share the ingest transaction). `lease` commits internally is NOT allowed; caller commits after leasing.

## Steps

- [ ] **Step 1: Failing tests** `tests/test_queue.py`
```python
import pytest
from bellweather.migrate import apply_migrations
from bellweather.db import get_conn
from bellweather import queue

@pytest.fixture(autouse=True)
def _migrated():
    apply_migrations()

def _insert_raw(conn) -> int:
    return conn.execute(
        "insert into raw_records(source,kind,content_type,idempotency_key,payload_uri,fetched_at)"
        " values('s','unstructured','c',%s,'gs://b/x',now()) returning id",
        (f"k-{conn.execute('select gen_random_uuid()').fetchone()[0]}",),
    ).fetchone()[0]

def test_enqueue_then_lease_then_ack():
    with get_conn() as conn:
        rid = _insert_raw(conn)
        jid = queue.enqueue(conn, rid)
        conn.commit()
        jobs = queue.lease(conn, limit=10)
        conn.commit()
        assert any(j.id == jid for j in jobs)
        queue.ack(conn, jid); conn.commit()
        again = queue.lease(conn, limit=10); conn.commit()
        assert all(j.id != jid for j in again)

def test_lease_skips_already_leased():
    with get_conn() as c1, get_conn() as c2:
        rid = _insert_raw(c1); jid = queue.enqueue(c1, rid); c1.commit()
        leased1 = queue.lease(c1, limit=10); c1.commit()
        leased2 = queue.lease(c2, limit=10); c2.commit()
        ids1 = {j.id for j in leased1}; ids2 = {j.id for j in leased2}
        assert jid in ids1 and jid not in ids2   # no double-lease

def test_fail_retries_then_dead_letters():
    with get_conn() as conn:
        rid = _insert_raw(conn); jid = queue.enqueue(conn, rid); conn.commit()
        for _ in range(5):
            queue.lease(conn, limit=10); conn.commit()
            queue.fail(conn, jid, "boom", max_attempts=5); conn.commit()
        state = conn.execute("select state, attempts from work_queue where id=%s", (jid,)).fetchone()
        assert state[0] == "failed" and state[1] >= 5
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `queue.py`**
```python
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
          where state='pending' and lease_until < now()
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
```
> Note: `lease` increments `attempts` at lease time, so `fail` only flips state based on the already-incremented count. The dead-letter test leases then fails 5×.
- [ ] **Step 4: Run** → PASS. Commit (`feat: add durable work queue with SKIP LOCKED leasing`).

## Acceptance criteria
- Two concurrent `lease` calls never return the same job (SKIP LOCKED proven by test).
- A job that fails `max_attempts` times lands in `failed` (dead-letter), otherwise returns to `pending`.
- Functions never commit internally; caller owns the transaction.
