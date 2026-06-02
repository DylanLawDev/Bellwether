import json
from unittest import mock

from bellweather import orchestrator
from bellweather.db import get_conn


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
        "bellweather",
        "run-template",
        "--template",
        "echo-template",
        "--params",
        json.dumps({"k": "v"}),
    ]
    env = kwargs["env"]
    assert env["BELLWEATHER_API_URL"] == "http://api.example:8000"
    assert "PATH" in env
    assert "DATABASE_URL" not in env
    assert "BELLWEATHER_BUCKET" not in env
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 600


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
            "select id, status, submitted, finished_at from producer_runs where schedule_id=%s",
            (sid,),
        ).fetchall()
        _cleanup(conn, name)
        conn.commit()

    assert sched[0] is not None  # last_run_at set by claim()
    assert sched[1] is False  # force_run consumed by claim()
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
