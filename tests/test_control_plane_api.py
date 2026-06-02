"""Control-plane API endpoints via TestClient (DB tests require `make up` + `make migrate`).

Templates are discovered from a fixture dir (BELLWEATHER_TEMPLATES_DIR). Preview and
orchestrator/run monkeypatch the subprocess/tick layer so no real script is spawned.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient

import bellweather.api as api
from bellweather.api import app
from bellweather.config import get_settings
from bellweather.db import get_conn

client = TestClient(app)

TEMPLATES_DIR = str(pathlib.Path(__file__).parent / "fixtures" / "templates")
_NAMES = ("t25-sched-a", "t25-sched-b")


@pytest.fixture(autouse=True)
def _templates_dir(monkeypatch):
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", TEMPLATES_DIR)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_schedules():
    def _wipe(c):
        c.execute(
            "delete from producer_runs where schedule_id in"
            " (select id from producer_schedules where name = any(%s))",
            (list(_NAMES),),
        )
        c.execute("delete from producer_schedules where name = any(%s)", (list(_NAMES),))
        c.commit()

    with get_conn() as c:
        _wipe(c)
    yield
    with get_conn() as c:
        _wipe(c)


def _create(name="t25-sched-a", interval_seconds=1800, enabled=True):
    body = {
        "name": name,
        "template": "echo",
        "params": {"url": "https://example.com", "backfill": "all"},
        "interval_seconds": interval_seconds,
        "enabled": enabled,
    }
    r = client.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_then_list_schedule():
    created = _create()
    assert created["id"] > 0
    assert created["name"] == "t25-sched-a"
    assert created["template"] == "echo"
    assert created["enabled"] is True
    assert created["force_run"] is False
    rows = client.get("/api/schedules").json()
    assert any(r["id"] == created["id"] and r["name"] == "t25-sched-a" for r in rows)
    assert all(
        {
            "id",
            "name",
            "template",
            "params",
            "interval_seconds",
            "enabled",
            "force_run",
            "last_run_at",
        }
        <= set(r)
        for r in rows
    )


def test_patch_toggles_enabled():
    created = _create(enabled=True)
    r = client.patch(f"/api/schedules/{created['id']}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    rows = {x["id"]: x for x in client.get("/api/schedules").json()}
    assert rows[created["id"]]["enabled"] is False


def test_force_sets_force_run_true():
    created = _create()
    assert created["force_run"] is False
    r = client.post(f"/api/schedules/{created['id']}/force")
    assert r.status_code == 200
    assert r.json()["force_run"] is True
    rows = {x["id"]: x for x in client.get("/api/schedules").json()}
    assert rows[created["id"]]["force_run"] is True


def test_delete_removes_schedule():
    created = _create()
    r = client.delete(f"/api/schedules/{created['id']}")
    assert r.status_code == 200
    rows = client.get("/api/schedules").json()
    assert all(x["id"] != created["id"] for x in rows)


def test_patch_missing_schedule_is_404():
    assert client.patch("/api/schedules/99999999", json={"enabled": False}).status_code == 404


def test_templates_lists_fixture_template():
    rows = client.get("/api/templates").json()
    echo = next(t for t in rows if t["name"] == "echo")
    assert echo["entrypoint"] == "tests.fixtures.templates.echo.producer:run"
    assert echo["default_interval_seconds"] == 1800
    names = {p["name"] for p in echo["params"]}
    assert {"url", "backfill"} <= names
    url = next(p for p in echo["params"] if p["name"] == "url")
    assert url["required"] is True


def test_preview_spawns_minimal_env_subprocess(monkeypatch):
    captured = {}

    def fake_preview(template, params):
        captured["template"] = template
        captured["params"] = params
        return {"submitted": 2, "sample": [{"symbol_key": "demo:x", "value": 0.5}]}

    monkeypatch.setattr(api, "_preview_subprocess", fake_preview)
    r = client.post("/api/templates/echo/preview", json={"url": "https://example.com"})
    assert r.status_code == 200
    body = r.json()
    assert captured["template"] == "echo"
    assert captured["params"] == {"url": "https://example.com"}
    assert body["submitted"] == 2
    assert body["sample"][0]["symbol_key"] == "demo:x"


def test_preview_unknown_template_is_404():
    assert client.post("/api/templates/nope/preview", json={}).status_code == 404


def test_preview_shape_flattens_submission_points_to_symbols_and_sample():
    summary = {
        "submitted": 1,
        "sample": [
            {
                "payload": {
                    "symbol_key": "echo:url",
                    "points": [
                        {"ts": "2026-06-01T12:00:00Z", "value": 0.5},
                        {"ts": "2026-06-01T13:00:00Z", "value": 0.6},
                    ],
                }
            },
            {"payload": {}},  # unstructured-style: no symbol_key → ignored
        ],
    }
    out = api._preview_shape(summary)
    assert out == {
        "submitted": 1,
        "symbols": ["echo:url"],
        "sample": [
            {"symbol_key": "echo:url", "ts": "2026-06-01T12:00:00Z", "value": 0.5},
            {"symbol_key": "echo:url", "ts": "2026-06-01T13:00:00Z", "value": 0.6},
        ],
    }


def test_preview_subprocess_real_dry_run_smoke():
    """Non-mocked: spawn the real `bellweather run-template --dry-run` against the
    echo fixture and assert the reshaped preview contract. This is the test that
    catches the `python -m bellweather.cli` (no __main__) and env-stripping bugs."""
    out = api._preview_subprocess("echo", {"url": "https://example.com"})
    assert out["submitted"] == 1
    assert out["symbols"] == ["echo:url"]
    assert out["sample"] == [{"symbol_key": "echo:url", "ts": "2026-06-01T12:00:00Z", "value": 0.5}]


def test_preview_endpoint_returns_reshaped_contract():
    """End-to-end through the route: real subprocess + reshape, unwrapped body."""
    r = client.post("/api/templates/echo/preview", json={"url": "https://example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"submitted", "symbols", "sample"}
    assert body["symbols"] == ["echo:url"]
    assert body["sample"][0] == {
        "symbol_key": "echo:url",
        "ts": "2026-06-01T12:00:00Z",
        "value": 0.5,
    }


def test_orchestrator_run_triggers_tick(monkeypatch):
    monkeypatch.setattr(api, "tick", lambda conn: [101, 102])
    r = client.post("/api/orchestrator/run")
    assert r.status_code == 200
    assert r.json() == {"started_run_ids": [101, 102]}


def test_runs_endpoint_lists_recent_runs():
    created = _create()
    with get_conn() as c:
        from bellweather import schedules

        rid = schedules.start_run(c, schedule_id=created["id"], template="echo", params={})
        schedules.finish_run(c, rid, status="ok", submitted=7)
        c.commit()
    rows = client.get("/api/runs").json()
    mine = next(x for x in rows if x["id"] == rid)
    assert mine["schedule_id"] == created["id"]
    assert mine["template"] == "echo"
    assert mine["status"] == "ok"
    assert mine["submitted"] == 7
    assert {
        "id",
        "schedule_id",
        "template",
        "started_at",
        "finished_at",
        "status",
        "submitted",
        "error",
    } <= set(mine)
    filtered = client.get("/api/runs", params={"schedule_id": created["id"]}).json()
    assert filtered and all(x["schedule_id"] == created["id"] for x in filtered)
