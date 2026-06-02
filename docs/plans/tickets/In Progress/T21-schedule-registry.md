# T21 — Schedule registry — migration 0002 + `schedules.py`

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§5 The schedule registry; K3/D3, §13). **Depends on:** T02 (db + migrate). **Branch:** `ticket/T21-schedule-registry`. **PR, do not merge without approval.**

## Goal
Add the orchestrator's control-plane state: a forward-only migration that creates `producer_schedules` and `producer_runs`, and a `schedules.py` helper module with CRUD plus the "which schedules are due" query, the claim-on-dispatch step, the force-run one-shot, and run lifecycle (`start_run`/`finish_run`/`list_runs`). These helpers are the substrate the orchestrator (T24) and the control-plane API (T25) build on. Like `queue.py` and `reads.py`, every helper takes a `conn`, runs parameterized SQL, returns `dict`/`list` shapes (via `dict_row`), and **never commits** — the caller owns the transaction.

## Files
- Create: `src/bellweather/migrations/0002_orchestrator.sql`
- Create: `src/bellweather/schedules.py`
- Test: `tests/test_schedules.py`

## Interface
Migration (`migrations/0002_orchestrator.sql`) — exactly as locked in the build plan:
```sql
create table if not exists producer_schedules (
  id bigserial primary key,
  name text not null,
  template text not null,
  params jsonb not null default '{}'::jsonb,
  interval_seconds int not null check (interval_seconds > 0),
  enabled boolean not null default true,
  force_run boolean not null default false,
  last_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table if not exists producer_runs (
  id bigserial primary key,
  schedule_id bigint references producer_schedules(id),
  template text not null,
  params jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running' check (status in ('running','ok','error')),
  submitted int,
  error text
);
create index if not exists producer_runs_schedule_idx on producer_runs (schedule_id, started_at desc);
```

`schedules.py` — locked signatures (never commit; caller owns the txn; `dict_row` shapes):
```python
def list_schedules(conn) -> list[dict]: ...
def get_schedule(conn, schedule_id: int) -> dict | None: ...
def create_schedule(conn, *, name, template, params: dict, interval_seconds: int, enabled: bool = True) -> int: ...
def update_schedule(conn, schedule_id: int, **fields) -> None: ...   # name|params|interval_seconds|enabled|force_run; bumps updated_at
def delete_schedule(conn, schedule_id: int) -> None: ...
def set_force_run(conn, schedule_id: int, value: bool = True) -> None: ...
def due_schedules(conn) -> list[dict]: ...   # enabled AND (force_run OR last_run_at IS NULL OR last_run_at + interval_seconds*'1s' <= now())
def claim(conn, schedule_id: int) -> None: ...   # set last_run_at=now(), force_run=false
def start_run(conn, *, schedule_id: int, template: str, params: dict) -> int: ...
def finish_run(conn, run_id: int, *, status: str, submitted: int | None = None, error: str | None = None) -> None: ...
def list_runs(conn, *, schedule_id: int | None = None, limit: int = 50) -> list[dict]: ...
```

## Steps

- [ ] **Step 1: Migration** `src/bellweather/migrations/0002_orchestrator.sql` — paste the SQL from the Interface section verbatim (both `create table` blocks plus the `producer_runs_schedule_idx` index). The runner (`bellweather.migrate.apply_migrations`) auto-discovers `*.sql` in sorted order, so the file name (`0002_…`) is what sequences it after `0001_initial.sql`. `params` JSONB must be passed as a JSON string from Python (`psycopg.types.json.Json`), so `schedules.py` will wrap dicts with `Json(...)`.

- [ ] **Step 2: Failing test** `tests/test_schedules.py` — start the local Postgres (`make up`), then `make migrate` so `0002` is applied. Write real test code:
```python
import time

import pytest

from bellweather import schedules
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations


@pytest.fixture(autouse=True)
def _migrated():
    # Applies forward-only migrations (incl. 0002). Each test inserts its own
    # schedules and asserts only on the rows it created, so no global cleanup is
    # needed — but delete this test's rows on teardown to keep due_schedules()
    # assertions in other tests deterministic across local re-runs.
    apply_migrations()
    yield
    with get_conn() as conn:
        conn.execute(
            "delete from producer_runs where template like 't21-%'"
        )
        conn.execute(
            "delete from producer_schedules where template like 't21-%'"
        )
        conn.commit()


def test_create_get_list_roundtrip():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="echo usage", template="t21-echo",
            params={"url": "http://x"}, interval_seconds=300,
        )
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["id"] == sid
        assert row["name"] == "echo usage"
        assert row["template"] == "t21-echo"
        assert row["params"] == {"url": "http://x"}
        assert row["interval_seconds"] == 300
        assert row["enabled"] is True
        assert row["force_run"] is False
        assert row["last_run_at"] is None
        listed = schedules.list_schedules(conn)
        assert any(s["id"] == sid for s in listed)


def test_get_missing_returns_none():
    with get_conn() as conn:
        assert schedules.get_schedule(conn, -1) is None


def test_interval_seconds_must_be_positive():
    import psycopg

    with get_conn() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            schedules.create_schedule(
                conn, name="bad", template="t21-bad",
                params={}, interval_seconds=0,
            )
        conn.rollback()


def test_update_schedule_changes_fields_and_bumps_updated_at():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="x", template="t21-upd", params={}, interval_seconds=60,
        )
        conn.commit()
        before = schedules.get_schedule(conn, sid)["updated_at"]
        time.sleep(0.01)
        schedules.update_schedule(
            conn, sid, name="renamed", interval_seconds=120,
            params={"a": 1}, enabled=False,
        )
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["name"] == "renamed"
        assert row["interval_seconds"] == 120
        assert row["params"] == {"a": 1}
        assert row["enabled"] is False
        assert row["updated_at"] > before


def test_update_with_no_fields_is_noop():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="x", template="t21-noop", params={}, interval_seconds=60,
        )
        conn.commit()
        schedules.update_schedule(conn, sid)  # no fields -> no-op, no error
        conn.commit()
        assert schedules.get_schedule(conn, sid)["name"] == "x"


def test_delete_schedule():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="x", template="t21-del", params={}, interval_seconds=60,
        )
        conn.commit()
        schedules.delete_schedule(conn, sid)
        conn.commit()
        assert schedules.get_schedule(conn, sid) is None


def test_due_includes_never_run_and_elapsed_excludes_disabled_and_not_yet_due():
    with get_conn() as conn:
        # never run -> due
        never = schedules.create_schedule(
            conn, name="never", template="t21-never", params={}, interval_seconds=60,
        )
        # ran long ago -> interval elapsed -> due
        elapsed = schedules.create_schedule(
            conn, name="elapsed", template="t21-elapsed", params={}, interval_seconds=60,
        )
        # ran just now, long interval -> NOT due
        fresh = schedules.create_schedule(
            conn, name="fresh", template="t21-fresh", params={}, interval_seconds=3600,
        )
        # disabled, never run -> NOT due
        off = schedules.create_schedule(
            conn, name="off", template="t21-off", params={}, interval_seconds=60,
            enabled=False,
        )
        conn.execute(
            "update producer_schedules set last_run_at = now() - interval '1 hour' where id=%s",
            (elapsed,),
        )
        conn.execute(
            "update producer_schedules set last_run_at = now() where id=%s",
            (fresh,),
        )
        conn.commit()
        due_ids = {s["id"] for s in schedules.due_schedules(conn)}
        assert never in due_ids
        assert elapsed in due_ids
        assert fresh not in due_ids
        assert off not in due_ids


def test_force_run_makes_a_fresh_schedule_due():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="forced", template="t21-force", params={}, interval_seconds=3600,
        )
        conn.execute(
            "update producer_schedules set last_run_at = now() where id=%s", (sid,)
        )
        conn.commit()
        assert sid not in {s["id"] for s in schedules.due_schedules(conn)}
        schedules.set_force_run(conn, sid, True)
        conn.commit()
        assert schedules.get_schedule(conn, sid)["force_run"] is True
        assert sid in {s["id"] for s in schedules.due_schedules(conn)}


def test_claim_sets_last_run_at_and_clears_force_run():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="claimed", template="t21-claim", params={}, interval_seconds=3600,
        )
        schedules.set_force_run(conn, sid, True)
        conn.commit()
        schedules.claim(conn, sid)
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["last_run_at"] is not None
        assert row["force_run"] is False
        # consuming the force AND advancing last_run_at -> no longer due
        assert sid not in {s["id"] for s in schedules.due_schedules(conn)}


def test_start_then_finish_run_transitions_running_to_ok_with_submitted():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="runs", template="t21-runs", params={"k": "v"}, interval_seconds=60,
        )
        conn.commit()
        rid = schedules.start_run(
            conn, schedule_id=sid, template="t21-runs", params={"k": "v"},
        )
        conn.commit()
        runs = schedules.list_runs(conn, schedule_id=sid)
        running = next(r for r in runs if r["id"] == rid)
        assert running["status"] == "running"
        assert running["template"] == "t21-runs"
        assert running["params"] == {"k": "v"}
        assert running["finished_at"] is None
        assert running["submitted"] is None
        schedules.finish_run(conn, rid, status="ok", submitted=7)
        conn.commit()
        done = next(r for r in schedules.list_runs(conn, schedule_id=sid) if r["id"] == rid)
        assert done["status"] == "ok"
        assert done["submitted"] == 7
        assert done["finished_at"] is not None
        assert done["error"] is None


def test_finish_run_records_error():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="err", template="t21-err", params={}, interval_seconds=60,
        )
        rid = schedules.start_run(conn, schedule_id=sid, template="t21-err", params={})
        conn.commit()
        schedules.finish_run(conn, rid, status="error", error="boom")
        conn.commit()
        row = next(r for r in schedules.list_runs(conn, schedule_id=sid) if r["id"] == rid)
        assert row["status"] == "error"
        assert row["error"] == "boom"
        assert row["submitted"] is None


def test_list_runs_orders_newest_first_and_honors_limit():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name="ord", template="t21-order", params={}, interval_seconds=60,
        )
        conn.commit()
        rids = []
        for _ in range(3):
            rids.append(
                schedules.start_run(conn, schedule_id=sid, template="t21-order", params={})
            )
            conn.commit()
            time.sleep(0.01)
        runs = schedules.list_runs(conn, schedule_id=sid)
        ordered = [r["id"] for r in runs]
        assert ordered == sorted(rids, reverse=True)  # newest started_at first
        assert len(schedules.list_runs(conn, schedule_id=sid, limit=1)) == 1
```

- [ ] **Step 3: Run → FAIL** (no `0002` applied / no `schedules.py`):
  `uv run pytest tests/test_schedules.py -v`. Expect `ImportError`/`UndefinedTable` errors.

- [ ] **Step 4: Implement** `src/bellweather/schedules.py` — mirror `queue.py` (helpers take `conn`, never commit) and `reads.py` (`dict_row` via a private `_rows`/`_one` helper). Wrap `params` dicts with `psycopg.types.json.Json` so JSONB binds correctly; `update_schedule` builds a whitelisted dynamic SET clause and always bumps `updated_at`:
```python
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


def claim(conn: Connection, schedule_id: int) -> None:
    conn.execute(
        "update producer_schedules set last_run_at = now(), force_run = false where id = %s",
        (schedule_id,),
    )


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
```

- [ ] **Step 5: Run → PASS.** With `make up` running and `0002` applied (`make migrate`):
  `uv run pytest tests/test_schedules.py -v`. All cases green.

- [ ] **Step 6:** `make check` → green (`ruff check`, `ruff format --check`, full `pytest`). Keep `make up` running so the DB tests execute rather than erroring.

- [ ] **Step 7: Commit** (`feat: schedule registry — migration 0002 + schedules.py`).

## Acceptance criteria
- `migrations/0002_orchestrator.sql` creates `producer_schedules` (with the `interval_seconds > 0` check and `force_run` default false) and `producer_runs` (with the `status in ('running','ok','error')` check) and the `producer_runs_schedule_idx` index on `(schedule_id, started_at desc)`, exactly as locked in the build plan; it is auto-applied in order by `apply_migrations()`.
- `schedules.py` exposes every locked signature; helpers take a `conn`, return `dict`/`list` shapes via `dict_row`, and **never commit** (caller owns the txn), mirroring `queue.py`/`reads.py`.
- `create_schedule` → `get_schedule` → `list_schedules` round-trips `params` as a real dict (JSONB) and defaults `enabled=true`, `force_run=false`, `last_run_at=null`.
- `due_schedules` returns enabled schedules that are never-run, force-run, or past their interval; it excludes disabled and not-yet-due schedules.
- `claim` sets `last_run_at = now()` AND clears `force_run`, so a just-claimed schedule is no longer due.
- `set_force_run` makes an otherwise-fresh schedule due; `update_schedule` whitelists fields, no-ops on empty, and bumps `updated_at`.
- `start_run` inserts a `running` row; `finish_run` transitions it to `ok` (with `submitted`) or `error` (with `error`); `list_runs` orders newest-`started_at` first and honors `limit`.
- `make check` is green (DB tests require `make up` + the `0002` migration).
