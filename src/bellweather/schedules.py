"""Producer-schedule control plane (the orchestrator's app state).

CRUD over ``producer_schedules`` plus the "which schedules are due" query, the
claim-on-dispatch step, the one-shot ``force_run``, and ``producer_runs``
lifecycle. Mirrors the repo conventions: every helper takes a psycopg
``Connection``, runs parameterized SQL, returns ``dict``/``list`` shapes via
``dict_row``, and **never commits** — the caller owns the transaction
(see ``queue.py`` and ``reads.py``).
"""

from __future__ import annotations

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json

# Fields update_schedule() may set. force_run is included so the API/orchestrator
# can flip it through the same path; set_force_run() is the named convenience.
_UPDATABLE = {"name", "params", "interval_seconds", "enabled", "force_run"}


def _rows(conn: Connection, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _one(conn: Connection, sql: str, params: tuple = ()) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchone()


def list_schedules(conn: Connection) -> list[dict]:
    return _rows(
        conn,
        """
        select id, name, template, params, interval_seconds, enabled,
               force_run, last_run_at, created_at, updated_at
        from producer_schedules
        order by id
        """,
    )


def get_schedule(conn: Connection, schedule_id: int) -> dict | None:
    return _one(
        conn,
        """
        select id, name, template, params, interval_seconds, enabled,
               force_run, last_run_at, created_at, updated_at
        from producer_schedules where id = %s
        """,
        (schedule_id,),
    )


def create_schedule(
    conn: Connection,
    *,
    name,
    template,
    params: dict,
    interval_seconds: int,
    enabled: bool = True,
) -> int:
    return conn.execute(
        """
        insert into producer_schedules(name, template, params, interval_seconds, enabled)
        values (%s, %s, %s, %s, %s) returning id
        """,
        (name, template, Json(params), interval_seconds, enabled),
    ).fetchone()[0]


def update_schedule(conn: Connection, schedule_id: int, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not cols:
        return
    sets = []
    vals: list = []
    for k, v in cols.items():
        sets.append(f"{k} = %s")
        vals.append(Json(v) if k == "params" else v)
    sets.append("updated_at = now()")
    vals.append(schedule_id)
    conn.execute(
        f"update producer_schedules set {', '.join(sets)} where id = %s",
        tuple(vals),
    )


def delete_schedule(conn: Connection, schedule_id: int) -> None:
    conn.execute("delete from producer_runs where schedule_id = %s", (schedule_id,))
    conn.execute("delete from producer_schedules where id = %s", (schedule_id,))


def set_force_run(conn: Connection, schedule_id: int, value: bool = True) -> None:
    conn.execute(
        "update producer_schedules set force_run = %s, updated_at = now() where id = %s",
        (value, schedule_id),
    )


def due_schedules(conn: Connection) -> list[dict]:
    return _rows(
        conn,
        """
        select id, name, template, params, interval_seconds, enabled,
               force_run, last_run_at, created_at, updated_at
        from producer_schedules
        where enabled
          and (
            force_run
            or last_run_at is null
            or last_run_at + (interval_seconds || ' seconds')::interval <= now()
          )
        order by id
        """,
    )


def claim(conn: Connection, schedule_id: int) -> bool:
    """Atomically claim a due schedule. Returns True if claimed, False if already claimed."""
    cur = conn.execute(
        """update producer_schedules
           set last_run_at = now(), force_run = false
           where id = %s
             and enabled
             and (
               force_run
               or last_run_at is null
               or last_run_at + (interval_seconds || ' seconds')::interval <= now()
             )""",
        (schedule_id,),
    )
    return cur.rowcount > 0


def start_run(conn: Connection, *, schedule_id: int, template: str, params: dict) -> int:
    return conn.execute(
        """
        insert into producer_runs(schedule_id, template, params)
        values (%s, %s, %s) returning id
        """,
        (schedule_id, template, Json(params)),
    ).fetchone()[0]


def finish_run(
    conn: Connection,
    run_id: int,
    *,
    status: str,
    submitted: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        update producer_runs
           set status = %s, submitted = %s, error = %s, finished_at = now()
         where id = %s
        """,
        (status, submitted, error, run_id),
    )


def list_runs(conn: Connection, *, schedule_id: int | None = None, limit: int = 50) -> list[dict]:
    where = "where schedule_id = %s" if schedule_id is not None else ""
    params: tuple = (schedule_id, limit) if schedule_id is not None else (limit,)
    return _rows(
        conn,
        f"""
        select id, schedule_id, template, params, started_at, finished_at,
               status, submitted, error
        from producer_runs
        {where}
        order by started_at desc, id desc
        limit %s
        """,
        params,
    )
