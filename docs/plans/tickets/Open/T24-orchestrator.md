# T24 — Orchestrator tick + `bellweather orchestrate`

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§7 The orchestrator; K4/K5 isolation; §13 D2/D3).
**Depends on:** T21 (`schedules.py` + migration `0002`), T23 (`run-template` CLI + `DryRunClient`).
**Branch:** `ticket/T24-orchestrator`. **PR, do not merge without approval.**

## Goal
Build the thin orchestrator that turns due schedules into runs. `tick(conn)` reads
`due_schedules`, claims each (committing the claim so a crash can't double-fire it),
opens a `producer_runs` row, spawns `bellweather run-template` as a **subprocess with a
minimal env** (`BELLWEATHER_API_URL` + `PATH` only — never DB/bucket creds, per K4), and
records `ok`/`error`. `run_orchestrator(once)` loops `tick()` over `get_conn()`, mirroring
the worker-drain pattern; `bellweather orchestrate --once` is the Cloud Run Job entrypoint.

## Files
- Create: `src/bellweather/orchestrator.py` — `tick`, `_run_subprocess`, `run_orchestrator`.
- Modify: `src/bellweather/cli.py` — add the `orchestrate` command.
- Test: `tests/test_orchestrator.py` — DB test (needs `make up` + `make migrate`) for the
  tick state machine, plus a pure env-isolation test that captures `subprocess.run` kwargs.

## Interface
Copied verbatim from the build plan ("Locked interfaces" → `orchestrator.py` / `cli.py`):
```python
# orchestrator.py
def tick(conn) -> list[int]: ...   # for s in due_schedules: claim+commit; rid=start_run+commit;
                                   #   try: summary=_run_subprocess(...); finish_run('ok', submitted)
                                   #   except: finish_run('error', error); commit
def _run_subprocess(template: str, params: dict, *, timeout: int = 600) -> dict: ...
    # subprocess.run(["bellweather","run-template","--template",template,"--params",json.dumps(params)],
    #   env={"BELLWEATHER_API_URL": get_settings().bellweather_api_url, "PATH": os.environ["PATH"]},  # NO db/bucket
    #   capture_output=True, text=True, timeout=timeout) ; json.loads(last stdout line)
def run_orchestrator(once: bool = False) -> None: ...   # loop tick() with get_conn(); sleep when idle
```
```python
# cli.py
@app.command()
def orchestrate(once: bool = False) -> None:
    from bellweather.orchestrator import run_orchestrator; run_orchestrator(once=once)
```
These `schedules.py` helpers (locked, from T21) are the ones `tick()` calls — never commit
inside them; the orchestrator owns the txn boundary:
```python
def due_schedules(conn) -> list[dict]: ...                 # rows: id, template, params, ...
def claim(conn, schedule_id: int) -> None: ...             # last_run_at=now(), force_run=false
def start_run(conn, *, schedule_id: int, template: str, params: dict) -> int: ...
def finish_run(conn, run_id: int, *, status: str, submitted: int | None = None, error: str | None = None) -> None: ...
```

## Steps

- [ ] **Step 1: Env-isolation test (no DB needed)** — add to `tests/test_orchestrator.py`. This
  is the load-bearing K4 invariant: spawned scripts get the ingest URL but **no** DB/bucket creds.
```python
import json
from unittest import mock

import pytest

from bellweather import orchestrator


def test_run_subprocess_passes_only_api_url_in_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/should_not_leak")
    monkeypatch.setenv("BELLWEATHER_BUCKET", "should-not-leak")
    monkeypatch.setenv("BELLWEATHER_API_URL", "http://api.example:8000")

    fake = mock.Mock()
    fake.stdout = '{"submitted": 5}\n'
    fake.returncode = 0
    with mock.patch.object(orchestrator.subprocess, "run", return_value=fake) as run:
        out = orchestrator._run_subprocess("echo-template", {"k": "v"})

    assert out == {"submitted": 5}
    args, kwargs = run.call_args
    assert args[0] == [
        "bellweather", "run-template",
        "--template", "echo-template",
        "--params", json.dumps({"k": "v"}),
    ]
    env = kwargs["env"]
    assert env["BELLWEATHER_API_URL"] == "http://api.example:8000"
    assert "PATH" in env
    assert "DATABASE_URL" not in env
    assert "BELLWEATHER_BUCKET" not in env
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 600
```
- [ ] **Step 2: Run → FAIL** (`orchestrator` module does not exist yet):
  `uv run pytest tests/test_orchestrator.py::test_run_subprocess_passes_only_api_url_in_env -v`

- [ ] **Step 3: DB tick test** — add to the same file. `make up` + `make migrate` must have run
  (this needs the `producer_schedules`/`producer_runs` tables from migration `0002`). Seed a due
  schedule, monkeypatch `_run_subprocess` to skip the real child process, and assert the run row
  + schedule state. The autouse settings-cache fixture in `tests/conftest.py` handles cache resets.
```python
from bellweather.db import get_conn


def _cleanup(conn, name):
    conn.execute(
        "delete from producer_runs where schedule_id in"
        " (select id from producer_schedules where name=%s)",
        (name,),
    )
    conn.execute("delete from producer_schedules where name=%s", (name,))


def test_tick_runs_due_schedule_records_ok_and_clears_force(monkeypatch):
    name = "t24-tick-ok"
    monkeypatch.setattr(orchestrator, "_run_subprocess", lambda *a, **k: {"submitted": 3})
    with get_conn() as conn:
        _cleanup(conn, name)
        sid = conn.execute(
            "insert into producer_schedules(name, template, params, interval_seconds,"
            " enabled, force_run, last_run_at) values"
            " (%s, 'echo-template', '{}'::jsonb, 3600, true, true, null) returning id",
            (name,),
        ).fetchone()[0]
        conn.commit()

        started = orchestrator.tick(conn)
        conn.commit()

        sched = conn.execute(
            "select last_run_at, force_run from producer_schedules where id=%s", (sid,)
        ).fetchone()
        runs = conn.execute(
            "select id, status, submitted, finished_at from producer_runs"
            " where schedule_id=%s",
            (sid,),
        ).fetchall()
        _cleanup(conn, name)
        conn.commit()

    assert sched[0] is not None          # last_run_at set by claim()
    assert sched[1] is False             # force_run consumed by claim()
    assert len(runs) == 1
    rid, status, submitted, finished_at = runs[0]
    assert rid in started
    assert status == "ok"
    assert submitted == 3
    assert finished_at is not None


def test_tick_records_error_when_subprocess_raises(monkeypatch):
    name = "t24-tick-err"

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_run_subprocess", boom)
    with get_conn() as conn:
        _cleanup(conn, name)
        sid = conn.execute(
            "insert into producer_schedules(name, template, params, interval_seconds,"
            " enabled, force_run, last_run_at) values"
            " (%s, 'echo-template', '{}'::jsonb, 3600, true, true, null) returning id",
            (name,),
        ).fetchone()[0]
        conn.commit()

        orchestrator.tick(conn)
        conn.commit()

        status, error = conn.execute(
            "select status, error from producer_runs where schedule_id=%s", (sid,)
        ).fetchone()
        _cleanup(conn, name)
        conn.commit()

    assert status == "error"
    assert "boom" in error
```
- [ ] **Step 4: Run → FAIL** (`make up` running): `uv run pytest tests/test_orchestrator.py -v`

- [ ] **Step 5: Implement `src/bellweather/orchestrator.py`** with the locked signatures:
```python
import json
import os
import subprocess
import time

from bellweather import schedules
from bellweather.config import get_settings
from bellweather.db import get_conn


def _run_subprocess(template: str, params: dict, *, timeout: int = 600) -> dict:
    proc = subprocess.run(
        [
            "bellweather", "run-template",
            "--template", template,
            "--params", json.dumps(params),
        ],
        env={
            "BELLWEATHER_API_URL": get_settings().bellweather_api_url,
            "PATH": os.environ["PATH"],
        },  # K4: ingest URL only — never DATABASE_URL / BELLWEATHER_BUCKET
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    proc.check_returncode()
    last = [line for line in proc.stdout.splitlines() if line.strip()][-1]
    return json.loads(last)


def tick(conn) -> list[int]:
    """Run every due schedule once; return the started producer_runs ids."""
    started: list[int] = []
    for s in schedules.due_schedules(conn):
        schedules.claim(conn, s["id"])
        conn.commit()
        run_id = schedules.start_run(
            conn, schedule_id=s["id"], template=s["template"], params=s["params"]
        )
        conn.commit()
        try:
            summary = _run_subprocess(s["template"], s["params"])
            schedules.finish_run(
                conn, run_id, status="ok", submitted=summary.get("submitted")
            )
        except Exception as e:  # noqa: BLE001
            schedules.finish_run(conn, run_id, status="error", error=str(e))
        conn.commit()
        started.append(run_id)
    return started


def run_orchestrator(once: bool = False) -> None:
    while True:
        with get_conn() as conn:
            started = tick(conn)
            conn.commit()
        if once:
            return
        if not started:
            time.sleep(2)
```
- [ ] **Step 6: Add the CLI command** in `src/bellweather/cli.py` (alongside `worker`/`ui`):
```python
@app.command()
def orchestrate(once: bool = False):
    from bellweather.orchestrator import run_orchestrator

    run_orchestrator(once=once)
```
- [ ] **Step 7: Run → PASS** (`make up` running): `uv run pytest tests/test_orchestrator.py -v`.
  Smoke the CLI wiring: `uv run bellweather orchestrate --help` lists the `--once` flag.
- [ ] **Step 8: `make check`** → green.
- [ ] **Step 9: Commit** (`feat: orchestrator tick + bellweather orchestrate`).

## Acceptance criteria
- `_run_subprocess` invokes `["bellweather","run-template","--template",t,"--params",json.dumps(p)]`
  with `env` containing exactly `BELLWEATHER_API_URL` + `PATH` — **no** `DATABASE_URL` or
  `BELLWEATHER_BUCKET` (K4 isolation), `capture_output=True`, `text=True`, `timeout=600` default —
  and parses the last non-blank stdout line as the JSON summary.
- `tick(conn)` claims each due schedule (committing the claim, which sets `last_run_at` and clears
  `force_run`) before spawning; on success records a `producer_runs` row `status="ok"` with
  `submitted` from the summary; on exception records `status="error"` with the message; returns the
  started run ids.
- `run_orchestrator(once=True)` runs one tick and returns; `once=False` loops, sleeping when no
  schedules were due (worker-drain pattern).
- `bellweather orchestrate --once` is wired and lazily imports `run_orchestrator`.
- `make check` green (the DB tests run under `make up` + `make migrate`; the env-isolation test
  needs neither — it mocks `subprocess.run`).
