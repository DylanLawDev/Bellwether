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
    # The child must be able to discover the template and import its entrypoint.
    assert "BELLWEATHER_TEMPLATES_DIR" in env
    assert env["PYTHONPATH"]
    # K4: never the spine's DB/bucket credentials.
    assert "DATABASE_URL" not in env
    assert "BELLWEATHER_BUCKET" not in env
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["timeout"] == 600


def test_child_env_omits_spine_creds_but_keeps_discovery(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/should_not_leak")
    monkeypatch.setenv("BELLWEATHER_BUCKET", "should-not-leak")
    monkeypatch.setenv("BELLWEATHER_API_URL", "http://api.example:8000")
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", "tests/fixtures/templates")

    env = orchestrator._child_env()

    assert env["BELLWEATHER_API_URL"] == "http://api.example:8000"
    assert env["BELLWEATHER_TEMPLATES_DIR"] == "tests/fixtures/templates"
    assert env["PYTHONPATH"]
    assert "PATH" in env
    assert "DATABASE_URL" not in env
    assert "BELLWEATHER_BUCKET" not in env


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


def test_tick_skips_schedule_when_claim_is_lost(monkeypatch):
    """If a concurrent tick already claimed the row, claim() returns False and
    tick() must skip it without spawning a run."""
    name = "t24-tick-claim-lost"
    spawned = []
    monkeypatch.setattr(orchestrator.schedules, "claim", lambda conn, sid: False)
    monkeypatch.setattr(
        orchestrator, "_run_subprocess", lambda *a, **k: spawned.append(1) or {"submitted": 0}
    )
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

        nruns = conn.execute(
            "select count(*) from producer_runs where schedule_id=%s", (sid,)
        ).fetchone()[0]
        _cleanup(conn, name)
        conn.commit()

    assert started == []
    assert nruns == 0
    assert spawned == []  # never spawned the producer subprocess


def test_tick_coerces_non_integer_submitted_to_none(monkeypatch):
    """A template that returns a non-integer ``submitted`` must still record a
    clean ok run (submitted=None) rather than leaving the row stuck running."""
    name = "t24-tick-bad-submitted"
    monkeypatch.setattr(
        orchestrator, "_run_subprocess", lambda *a, **k: {"submitted": "not-a-number"}
    )
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

        status, submitted, finished_at = conn.execute(
            "select status, submitted, finished_at from producer_runs where schedule_id=%s",
            (sid,),
        ).fetchone()
        _cleanup(conn, name)
        conn.commit()

    assert status == "ok"
    assert submitted is None
    assert finished_at is not None
