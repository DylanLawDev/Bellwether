"""Schedules control-plane backends build matching shapes (mock + live).

live.* is exercised against a fake API via pytest-httpserver (mirrors
tests/test_web_live.py); mock.* returns in-memory shapes. Both match the
bellweather.web.data.source column constants. No DB, no network.
"""

import pandas as pd
import pytest

from bellweather.config import get_ui_settings
from bellweather.web.data import live, mock, source as contract

_TS = "2026-06-01T11:00:00+00:00"

_SCHEDULES = [
    {
        "id": 1,
        "name": "gdelt-hourly",
        "template": "gdelt",
        "interval_seconds": 3600,
        "enabled": True,
        "force_run": False,
        "last_run_at": _TS,
    }
]
_TEMPLATES = [
    {
        "name": "gdelt",
        "description": "GDELT GKG collector",
        "default_interval_seconds": 1800,
        "params": [
            {
                "name": "url",
                "type": "str",
                "required": True,
                "default": None,
                "choices": None,
                "help": "GKG file URL",
            },
            {
                "name": "backfill",
                "type": "str",
                "required": False,
                "default": "all",
                "choices": ["all", "recent"],
                "help": None,
            },
        ],
    }
]
_RUNS = [
    {
        "id": 9,
        "schedule_id": 1,
        "template": "gdelt",
        "started_at": _TS,
        "finished_at": _TS,
        "status": "ok",
        "submitted": 412,
        "error": None,
    }
]
_PREVIEW = {
    "symbols": ["theme:ECON_STOCKMARKET"],
    "sample": [{"symbol_key": "theme:ECON_STOCKMARKET", "ts": _TS, "value": 0.37}],
}


# --- live: fake API via pytest-httpserver -----------------------------------
@pytest.fixture()
def _api(httpserver, monkeypatch):
    httpserver.expect_request("/api/schedules", method="GET").respond_with_json(_SCHEDULES)
    httpserver.expect_request("/api/schedules", method="POST").respond_with_json({"id": 7})
    httpserver.expect_request("/api/schedules/1", method="PATCH").respond_with_json({"ok": True})
    httpserver.expect_request("/api/schedules/1", method="DELETE").respond_with_json({"ok": True})
    httpserver.expect_request("/api/schedules/1/force", method="POST").respond_with_json(
        {"ok": True}
    )
    httpserver.expect_request("/api/templates", method="GET").respond_with_json(_TEMPLATES)
    # Match the unwrapped params body — a regression to {"params": {...}} would
    # not match this handler and the test would fail.
    httpserver.expect_request(
        "/api/templates/gdelt/preview", method="POST", json={"url": "x"}
    ).respond_with_json(_PREVIEW)
    httpserver.expect_request("/api/orchestrator/run", method="POST").respond_with_json(
        {"started_run_ids": [9]}
    )
    httpserver.expect_request("/api/runs", method="GET").respond_with_json(_RUNS)
    monkeypatch.setenv("BELLWEATHER_API_URL", httpserver.url_for("").rstrip("/"))
    get_ui_settings.cache_clear()
    yield
    get_ui_settings.cache_clear()


def test_live_get_schedules(_api):
    df = live.get_schedules()
    assert list(df.columns) == contract.SCHEDULE_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["last_run_at"])
    assert df.iloc[0]["template"] == "gdelt"


def test_live_get_templates(_api):
    tpls = live.get_templates()
    assert tpls[0]["name"] == "gdelt"
    assert tpls[0]["params"][0]["name"] == "url"


def test_live_get_runs_parses_timestamps(_api):
    df = live.get_runs()
    assert list(df.columns) == contract.RUN_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["started_at"])
    assert df.iloc[0]["submitted"] == 412


def test_live_create_schedule_returns_id(_api):
    assert live.create_schedule("nightly", "gdelt", {"url": "x"}, 86400) == 7


def test_live_write_paths_do_not_raise(_api):
    live.update_schedule(1, enabled=False)
    live.delete_schedule(1)
    live.force_schedule(1)


def test_live_run_orchestrator_now(_api):
    assert live.run_orchestrator_now() == {"started_run_ids": [9]}


def test_live_preview_template(_api):
    out = live.preview_template("gdelt", {"url": "x"})
    assert out["symbols"] == ["theme:ECON_STOCKMARKET"]
    assert out["sample"][0]["value"] == 0.37


# --- mock: in-memory, no API -------------------------------------------------
def test_mock_get_schedules_shape():
    df = mock.get_schedules()
    assert list(df.columns) == contract.SCHEDULE_COLUMNS


def test_mock_get_runs_shape():
    df = mock.get_runs()
    assert list(df.columns) == contract.RUN_COLUMNS


def test_mock_get_templates_has_params_schema():
    tpls = mock.get_templates()
    assert tpls and "params" in tpls[0]
    assert {"name", "type", "required", "default", "choices", "help"} <= set(tpls[0]["params"][0])


def test_mock_create_then_get_roundtrip():
    new_id = mock.create_schedule("nightly", "echo", {"n": 3}, 86400)
    df = mock.get_schedules()
    assert new_id in df["id"].tolist()
    assert df[df["id"] == new_id].iloc[0]["interval_seconds"] == 86400


def test_mock_update_enabled():
    sid = mock.create_schedule("toggle-me", "echo", {}, 600)
    mock.update_schedule(sid, enabled=False)
    df = mock.get_schedules()
    assert bool(df[df["id"] == sid].iloc[0]["enabled"]) is False


def test_mock_force_then_consume():
    sid = mock.create_schedule("force-me", "echo", {}, 600)
    mock.force_schedule(sid)
    assert bool(mock.get_schedules().set_index("id").loc[sid, "force_run"]) is True
    # run_orchestrator_now consumes the one-shot flag (reads off after the run)
    mock.run_orchestrator_now()
    assert bool(mock.get_schedules().set_index("id").loc[sid, "force_run"]) is False


def test_mock_delete_removes_row():
    sid = mock.create_schedule("delete-me", "echo", {}, 600)
    mock.delete_schedule(sid)
    assert sid not in mock.get_schedules()["id"].tolist()


def test_mock_preview_template_shape():
    out = mock.preview_template("echo", {"n": 2})
    assert set(out) == {"symbols", "sample"}
    assert isinstance(out["symbols"], list) and isinstance(out["sample"], list)
