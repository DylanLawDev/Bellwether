# T26 — Schedules UI page + `web.data` backends

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§8 Control-plane API + UI; §5 schedule registry; §13 D3 run controls).
**Depends on:** T16 (live web backend + `web.data` seam), T25 (control-plane API: `/api/schedules`, `/api/templates`, `/api/templates/{name}/preview`, `/api/schedules/{id}/force`, `/api/orchestrator/run`, `/api/runs`). **Branch:** `ticket/T26-schedules-ui`. **PR, do not merge without approval.**

## Goal
Add the operator **Schedules** control-plane page and the `web.data` backend functions behind it, following the existing seam: the page imports only from `bellweather.web.data`; `mock.py` serves in-memory shapes for offline use; `live.py` issues `httpx` calls to the T25 `/api/...` endpoints with **identical shapes**. This is the first UI write-path (POST/PATCH/DELETE). Per T16, the tests cover only the backends + column contracts, not Streamlit screen internals.

## Files
- Create: `src/bellweather/web/pages/5_Schedules.py` — list usages, an "Add usage" form generated from a template's params schema, per-row **Force Run** toggle (reads back off after a run), **Run now** button, **Preview** (dry-run), and recent-run history. Imports only from `bellweather.web.data`.
- Modify: `src/bellweather/web/data/source.py` — add `SCHEDULE_COLUMNS`, `RUN_COLUMNS`, and document the new functions in the module docstring.
- Modify: `src/bellweather/web/data/mock.py` — add `get_schedules/get_templates/get_runs` + `create_schedule/update_schedule/delete_schedule/force_schedule/run_orchestrator_now/preview_template` (in-memory, deterministic).
- Modify: `src/bellweather/web/data/live.py` — add the same nine functions, issuing `httpx` calls to `/api/...`.
- Modify: `src/bellweather/web/data/__init__.py` — re-export the nine new functions through the seam (`get_schedules`, `get_templates`, `get_runs`, `create_schedule`, `update_schedule`, `delete_schedule`, `force_schedule`, `run_orchestrator_now`, `preview_template`).
- Test: `tests/test_web_schedules.py` — `live.*` against a fake API via `pytest-httpserver` (mirrors `tests/test_web_live.py`); `mock.*` returns in-memory shapes; both match the `source` column constants. No DB.

## Interface
Column contracts (locked, `web/data/source.py`):
```python
SCHEDULE_COLUMNS = ["id", "name", "template", "interval_seconds", "enabled", "force_run", "last_run_at"]
RUN_COLUMNS = ["id", "schedule_id", "template", "started_at", "finished_at", "status", "submitted", "error"]
```
Backend functions (locked, mock + live identical shapes):
```python
get_schedules()                          -> DataFrame[SCHEDULE_COLUMNS]
get_templates()                          -> list[dict]   # {name, description, default_interval_seconds, params:[{name,type,required,default,choices,help}]}
get_runs(schedule_id=None)               -> DataFrame[RUN_COLUMNS]
create_schedule(name, template, params, interval_seconds, enabled=True) -> int   # new schedule id
update_schedule(id, **fields)            -> None         # name|params|interval_seconds|enabled
delete_schedule(id)                      -> None
force_schedule(id)                       -> None         # sets the one-shot force_run flag
run_orchestrator_now()                   -> dict         # {started_run_ids: [...]}
preview_template(name, params)           -> dict         # {symbols:[...], sample:[...]}  (dry-run, commits nothing)
```
T25 API surface these map to (prefix `/api`):
`GET /templates`, `POST /templates/{name}/preview`, `GET /schedules`, `POST /schedules`, `PATCH /schedules/{id}`, `DELETE /schedules/{id}`, `POST /schedules/{id}/force`, `POST /orchestrator/run`, `GET /runs`.

## Steps

- [ ] **Step 1: Column contracts + docstring** in `src/bellweather/web/data/source.py`. Append after `QUEUE_STATES`:
```python
# Producer orchestrator control plane (T26). `last_run_at`/`started_at`/`finished_at`
# are timestamps (None until set); `params` is carried per-template, not a column here.
SCHEDULE_COLUMNS = [
    "id",
    "name",
    "template",
    "interval_seconds",
    "enabled",
    "force_run",
    "last_run_at",
]
RUN_COLUMNS = [
    "id",
    "schedule_id",
    "template",
    "started_at",
    "finished_at",
    "status",
    "submitted",
    "error",
]
```
  And add to the module docstring's function list (after `get_settings_view()`):
```
    get_schedules()                    -> DataFrame[id, name, template, interval_seconds,
                                                    enabled, force_run, last_run_at]
    get_templates()                    -> list[dict[name, description,
                                                    default_interval_seconds, params]]
    get_runs(schedule_id=None)         -> DataFrame[id, schedule_id, template, started_at,
                                                    finished_at, status, submitted, error]
    create_schedule(name, template, params, interval_seconds, enabled=True) -> int
    update_schedule(id, **fields)      -> None   # name|params|interval_seconds|enabled
    delete_schedule(id)                -> None
    force_schedule(id)                 -> None   # one-shot force_run flag
    run_orchestrator_now()             -> dict[started_run_ids]
    preview_template(name, params)     -> dict[symbols, sample]   # dry-run, commits nothing
```

- [ ] **Step 2: Failing test** `tests/test_web_schedules.py`. Two halves: `live.*` against `pytest-httpserver` (mirroring `tests/test_web_live.py`), and `mock.*` in-memory. No DB, no network.
```python
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
            {"name": "url", "type": "str", "required": True, "default": None,
             "choices": None, "help": "GKG file URL"},
            {"name": "backfill", "type": "str", "required": False, "default": "all",
             "choices": ["all", "recent"], "help": None},
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
    httpserver.expect_request("/api/schedules/1/force", method="POST").respond_with_json({"ok": True})
    httpserver.expect_request("/api/templates", method="GET").respond_with_json(_TEMPLATES)
    httpserver.expect_request(
        "/api/templates/gdelt/preview", method="POST"
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
```

- [ ] **Step 3: Run → FAIL** (the `source` constants and the nine backend functions don't exist yet):
```
uv run pytest tests/test_web_schedules.py -q
```

- [ ] **Step 4: Implement `mock.py`** — append an in-memory registry + the nine functions (no `_build()` rewrite). The mock keeps state in a module-level list so the page's create/update/delete/force round-trip in an offline session:
```python
# --- producer orchestrator control plane (T26) ------------------------------
# Two fixture templates so the UI's "Add usage" form + preview have a schema to
# render offline. `echo` exercises the structured (numeric-series-v1) path.
_TEMPLATES = [
    {
        "name": "gdelt",
        "description": "GDELT GKG collector (unstructured).",
        "default_interval_seconds": 1800,
        "params": [
            {"name": "url", "type": "str", "required": True, "default": None,
             "choices": None, "help": "GKG file URL or local path."},
            {"name": "backfill", "type": "str", "required": False, "default": "all",
             "choices": ["all", "recent"], "help": "How far back to fetch."},
        ],
    },
    {
        "name": "echo",
        "description": "Fixture numeric-series-v1 producer (Phase-1 demo).",
        "default_interval_seconds": 3600,
        "params": [
            {"name": "n", "type": "int", "required": False, "default": 1,
             "choices": None, "help": "How many points to emit."},
        ],
    },
]

_SCHEDULES_STATE: list[dict] = [
    {
        "id": 1,
        "name": "gdelt-hourly",
        "template": "gdelt",
        "params": {"url": "http://data.gdeltproject.org/...", "backfill": "all"},
        "interval_seconds": 3600,
        "enabled": True,
        "force_run": False,
        "last_run_at": _now_hour() - timedelta(minutes=20),
    }
]
_RUNS_STATE: list[dict] = [
    {
        "id": 1,
        "schedule_id": 1,
        "template": "gdelt",
        "started_at": _now_hour() - timedelta(minutes=20),
        "finished_at": _now_hour() - timedelta(minutes=19),
        "status": "ok",
        "submitted": 412,
        "error": None,
    }
]
_NEXT_ID = {"schedule": 2, "run": 2}


def _schedules_frame() -> pd.DataFrame:
    rows = [{c: s[c] for c in contract.SCHEDULE_COLUMNS} for s in _SCHEDULES_STATE]
    return pd.DataFrame(rows, columns=contract.SCHEDULE_COLUMNS)


def get_schedules() -> pd.DataFrame:
    return _schedules_frame()


def get_templates() -> list[dict]:
    return [dict(t, params=[dict(p) for p in t["params"]]) for t in _TEMPLATES]


def get_runs(schedule_id=None) -> pd.DataFrame:
    rows = [r for r in _RUNS_STATE if schedule_id is None or r["schedule_id"] == schedule_id]
    rows = [{c: r[c] for c in contract.RUN_COLUMNS} for r in rows]
    df = pd.DataFrame(rows, columns=contract.RUN_COLUMNS)
    return df.sort_values("started_at", ascending=False, ignore_index=True) if not df.empty else df


def create_schedule(name, template, params, interval_seconds, enabled=True) -> int:
    sid = _NEXT_ID["schedule"]
    _NEXT_ID["schedule"] += 1
    _SCHEDULES_STATE.append(
        {
            "id": sid,
            "name": name,
            "template": template,
            "params": dict(params),
            "interval_seconds": int(interval_seconds),
            "enabled": bool(enabled),
            "force_run": False,
            "last_run_at": None,
        }
    )
    return sid


def update_schedule(id, **fields) -> None:
    allowed = {"name", "params", "interval_seconds", "enabled"}
    for s in _SCHEDULES_STATE:
        if s["id"] == id:
            s.update({k: v for k, v in fields.items() if k in allowed})


def delete_schedule(id) -> None:
    _SCHEDULES_STATE[:] = [s for s in _SCHEDULES_STATE if s["id"] != id]


def force_schedule(id) -> None:
    for s in _SCHEDULES_STATE:
        if s["id"] == id:
            s["force_run"] = True


def run_orchestrator_now() -> dict:
    # Mimic the orchestrator tick: any enabled schedule that is forced (or never
    # run) gets a recorded run; the claim consumes force_run (resets to False).
    started = []
    now = _now_hour()
    for s in _SCHEDULES_STATE:
        if not s["enabled"]:
            continue
        if s["force_run"] or s["last_run_at"] is None:
            s["force_run"] = False
            s["last_run_at"] = now
            rid = _NEXT_ID["run"]
            _NEXT_ID["run"] += 1
            _RUNS_STATE.append(
                {
                    "id": rid,
                    "schedule_id": s["id"],
                    "template": s["template"],
                    "started_at": now,
                    "finished_at": now,
                    "status": "ok",
                    "submitted": 1,
                    "error": None,
                }
            )
            started.append(rid)
    return {"started_run_ids": started}


def preview_template(name, params) -> dict:
    # Deterministic dry-run shape: one fictitious symbol + a single sample point.
    return {
        "symbols": [f"{name}:demo"],
        "sample": [{"symbol_key": f"{name}:demo", "ts": _now_hour().isoformat(), "value": 0.5}],
    }
```

- [ ] **Step 5: Implement `live.py`** — add a `_request(method, path, json=None, **params)` helper (the existing `_get` only does GET) and the nine functions. Reuse the existing `_frame` for the two tabular results:
```python
def _request(method: str, path: str, json: dict | None = None, **params) -> object:
    """Issue ``method {bellweather_api_url}{path}`` and return parsed JSON.

    Mirrors ``_get`` but covers the write verbs (POST/PATCH/DELETE) the Schedules
    control plane needs. ``None`` query params are dropped; the base URL is read
    at call time via ``UISettings`` (no DB/GCS secrets needed)."""
    clean = {k: v for k, v in params.items() if v is not None}
    base = get_ui_settings().bellweather_api_url
    with httpx.Client(base_url=base, timeout=_TIMEOUT) as client:
        resp = client.request(method, path, json=json, params=clean)
        resp.raise_for_status()
        return resp.json()


def get_schedules() -> pd.DataFrame:
    return _frame(_get("/api/schedules"), contract.SCHEDULE_COLUMNS, ts_cols=("last_run_at",))


def get_templates() -> list[dict]:
    return _get("/api/templates")


def get_runs(schedule_id=None) -> pd.DataFrame:
    rows = _get("/api/runs", schedule_id=schedule_id)
    return _frame(rows, contract.RUN_COLUMNS, ts_cols=("started_at", "finished_at"))


def create_schedule(name, template, params, interval_seconds, enabled=True) -> int:
    body = {
        "name": name,
        "template": template,
        "params": params,
        "interval_seconds": int(interval_seconds),
        "enabled": bool(enabled),
    }
    return _request("POST", "/api/schedules", json=body)["id"]


def update_schedule(id, **fields) -> None:
    _request("PATCH", f"/api/schedules/{id}", json=fields)


def delete_schedule(id) -> None:
    _request("DELETE", f"/api/schedules/{id}")


def force_schedule(id) -> None:
    _request("POST", f"/api/schedules/{id}/force")


def run_orchestrator_now() -> dict:
    return _request("POST", "/api/orchestrator/run")


def preview_template(name, params) -> dict:
    return _request("POST", f"/api/templates/{name}/preview", json={"params": params})
```

- [ ] **Step 6: Re-export through the seam** in `src/bellweather/web/data/__init__.py` — add nine bindings after `get_settings_view = _b.get_settings_view` and extend `__all__`:
```python
get_schedules = _b.get_schedules
get_templates = _b.get_templates
get_runs = _b.get_runs
create_schedule = _b.create_schedule
update_schedule = _b.update_schedule
delete_schedule = _b.delete_schedule
force_schedule = _b.force_schedule
run_orchestrator_now = _b.run_orchestrator_now
preview_template = _b.preview_template
```
  And append those nine names to the `__all__` list.

- [ ] **Step 7: Run → PASS:**
```
uv run pytest tests/test_web_schedules.py -q
```

- [ ] **Step 8: Build the page** `src/bellweather/web/pages/5_Schedules.py` — imports only from `bellweather.web.data`:
```python
"""Schedules — producer orchestrator control plane.

List usages, add one from a template's params schema, force/run/preview, and
view recent runs. Reads/writes only through bellweather.web.data (mock or live).
"""

import streamlit as st

from bellweather.web import data

st.title("⏱️ Schedules")
st.caption("Bind a template to parameters + an interval; force, run-now, preview, and review runs.")

templates = {t["name"]: t for t in data.get_templates()}

# --- run controls -----------------------------------------------------------
if st.button("▶️ Run orchestrator now", help="Trigger an immediate tick instead of waiting."):
    result = data.run_orchestrator_now()
    started = result.get("started_run_ids", [])
    st.success(f"Started run(s): {started}" if started else "No schedules were due.")

# --- existing usages --------------------------------------------------------
st.subheader("Usages")
schedules = data.get_schedules()
if schedules.empty:
    st.info("No schedules yet. Add one below.")
else:
    for row in schedules.to_dict("records"):
        sid = row["id"]
        cols = st.columns([3, 2, 2, 2, 2])
        cols[0].markdown(f"**{row['name']}**  \n`{row['template']}`")
        cols[1].metric("Interval (s)", row["interval_seconds"])
        enabled = cols[2].toggle("Enabled", value=bool(row["enabled"]), key=f"en_{sid}")
        if enabled != bool(row["enabled"]):
            data.update_schedule(sid, enabled=enabled)
            st.rerun()
        # Force Run is one-shot: the orchestrator consumes it (reads off after a run).
        forced = cols[3].toggle("Force Run", value=bool(row["force_run"]), key=f"fr_{sid}")
        if forced and not bool(row["force_run"]):
            data.force_schedule(sid)
            st.rerun()
        if cols[4].button("🗑️ Delete", key=f"del_{sid}"):
            data.delete_schedule(sid)
            st.rerun()

# --- add usage (form generated from a template's params schema) -------------
st.subheader("Add usage")
if not templates:
    st.warning("No templates discovered.")
else:
    tpl_name = st.selectbox("Template", list(templates))
    tpl = templates[tpl_name]
    st.caption(tpl.get("description", ""))
    with st.form("add_usage"):
        name = st.text_input("Usage name", value=f"{tpl_name}-usage")
        interval = st.number_input(
            "Interval (seconds)",
            min_value=1,
            value=int(tpl.get("default_interval_seconds") or 3600),
            step=60,
        )
        params: dict = {}
        for p in tpl.get("params", []):
            label = f"{p['name']}{' *' if p.get('required') else ''}"
            if p.get("choices"):
                params[p["name"]] = st.selectbox(
                    label, p["choices"], help=p.get("help"), key=f"p_{p['name']}"
                )
            elif p.get("type") == "int":
                params[p["name"]] = st.number_input(
                    label, value=int(p.get("default") or 0), step=1, help=p.get("help"),
                    key=f"p_{p['name']}",
                )
            else:
                params[p["name"]] = st.text_input(
                    label, value=str(p.get("default") or ""), help=p.get("help"),
                    key=f"p_{p['name']}",
                )
        c_prev, c_add = st.columns(2)
        previewed = c_prev.form_submit_button("🔍 Preview (dry-run)")
        added = c_add.form_submit_button("➕ Add")
    if previewed:
        out = data.preview_template(tpl_name, {k: v for k, v in params.items() if v != ""})
        st.success(f"Would emit {len(out['sample'])} sample point(s) across {len(out['symbols'])} symbol(s).")
        st.json(out)
    if added:
        sid = data.create_schedule(
            name, tpl_name, {k: v for k, v in params.items() if v != ""}, int(interval)
        )
        st.success(f"Created schedule #{sid}.")
        st.rerun()

# --- recent runs ------------------------------------------------------------
st.subheader("Recent runs")
st.dataframe(data.get_runs(), hide_index=True, width="stretch")
```

- [ ] **Step 9: Manual smoke (offline)** — the page renders against the mock backend without an API or DB:
```
BELLWEATHER_UI_SOURCE=mock uv run bellweather ui
```
  Open the **Schedules** page; add a usage from `echo`, toggle Force Run, click Run now (the toggle reads off after), Preview shows the dry-run JSON, the new run appears under Recent runs.

- [ ] **Step 10: `make check`** → green:
```
make check
```

- [ ] **Step 11: Commit** (`feat: schedules UI page + web.data control-plane backends`).

## Acceptance criteria
- `source.SCHEDULE_COLUMNS` and `source.RUN_COLUMNS` exist and match the locked contract; the module docstring documents the nine new functions.
- `mock.*` and `live.*` expose the same nine functions with byte-identical signatures; `get_schedules`/`get_runs` return frames whose columns equal the `source` constants, and `get_templates` returns dicts carrying a `params` schema (`name`/`type`/`required`/`default`/`choices`/`help`).
- `live.*` issues the correct verb+path per the T25 API surface (POST/PATCH/DELETE/force/preview/orchestrator-run); `live` reads the base URL via `UISettings` at call time, so the UI needs no DB/GCS secrets.
- `mock.*` round-trips create → get → update → delete in-memory, and `force_schedule` + `run_orchestrator_now` consume the one-shot `force_run` (it reads off after a run).
- `5_Schedules.py` imports **only** from `bellweather.web.data`; no screen reads `mock`/`live` directly, and no other page changes.
- Tests use `pytest-httpserver` (live) + in-memory (mock); **no DB, no network**. `make check` green.
