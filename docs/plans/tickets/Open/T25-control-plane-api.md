# T25 — Control-plane API (schedules/templates/runs/preview/force/run)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§8 Control-plane API + UI; D2/D3, K4/K9). **Depends on:** T15 (read API + `api_router`), T24 (orchestrator `tick`/`run_orchestrator`; also brings T21 `schedules.py`, T22 `templates.py`). **Branch:** `ticket/T25-control-plane-api`. **PR, do not merge without approval.**

## Goal
Add the control-plane HTTP surface on top of the existing `/api` router so the UI (T26) can list/create/edit/delete schedules, list templates, preview a template (dry-run), force-run a schedule, trigger an orchestrator tick now, and read run history. Templates are **discovered without executing code** (`templates.discover_templates`); **preview spawns `bellweather run-template --dry-run` as a minimal-env subprocess** (K4/K9 isolation — never in-process, which would hand the script the API's DB/bucket creds); the per-request DB pattern (`get_conn()` + helpers from `schedules.py`) and pydantic response models follow the T15 read endpoints exactly.

## Files
- Modify: `src/bellweather/api.py` — add the control-plane routes to the existing `api_router` (prefix `/api`).
- Test: `tests/test_control_plane_api.py` — `fastapi.testclient` + Postgres (DB tests: `make up` + `make migrate`); templates from a fixture dir via `BELLWEATHER_TEMPLATES_DIR`; preview + orchestrator/run monkeypatch the subprocess/tick layer (no real work spawned).

## Interface
From the build plan **Locked interfaces** (`docs/plans/2026-06-01-producer-orchestrator.md`), the symbols this ticket consumes verbatim:

`api.py` — add to `api_router` (prefix `/api`):
`GET /templates`, `POST /templates/{name}/preview` (spawns `run-template --dry-run` minimal-env subprocess; returns sample), `GET /schedules`, `POST /schedules`, `PATCH /schedules/{id}`, `DELETE /schedules/{id}`, `POST /schedules/{id}/force`, `POST /orchestrator/run` (trigger a tick now), `GET /runs`. Pydantic row models mirror `schedules.py`/`templates.py` dict shapes.

`schedules.py` (caller owns the txn — the API must `conn.commit()` after writes):
```python
def list_schedules(conn) -> list[dict]: ...
def get_schedule(conn, schedule_id: int) -> dict | None: ...
def create_schedule(conn, *, name, template, params: dict, interval_seconds: int, enabled: bool = True) -> int: ...
def update_schedule(conn, schedule_id: int, **fields) -> None: ...   # name|params|interval_seconds|enabled|force_run; bumps updated_at
def delete_schedule(conn, schedule_id: int) -> None: ...
def set_force_run(conn, schedule_id: int, value: bool = True) -> None: ...
def start_run(conn, *, schedule_id: int, template: str, params: dict) -> int: ...
def finish_run(conn, run_id: int, *, status: str, submitted: int | None = None, error: str | None = None) -> None: ...
def list_runs(conn, *, schedule_id: int | None = None, limit: int = 50) -> list[dict]: ...
```

`templates.py`:
```python
@dataclass
class TemplateParam:
    name: str; type: str = "str"; required: bool = False
    default: object | None = None; choices: list | None = None; help: str | None = None

@dataclass
class Template:
    name: str; entrypoint: str; description: str = ""
    params: list[TemplateParam] = field(default_factory=list)
    default_interval_seconds: int | None = None

def discover_templates(templates_dir: str | None = None) -> dict[str, Template]: ...  # scan */template.toml via tomllib; DO NOT import entrypoints
```

`orchestrator.py`:
```python
def tick(conn) -> list[int]: ...   # returns started run ids
def _run_subprocess(template: str, params: dict, *, timeout: int = 600) -> dict: ...
    # subprocess.run(["bellweather","run-template","--template",template,"--params",json.dumps(params)],
    #   env={"BELLWEATHER_API_URL": ..., "PATH": ...}, capture_output=True, text=True, timeout=timeout)
```

## Steps

- [ ] **Step 0 (env):** `make up` (Postgres + fake-gcs) and `make migrate` (applies `0001_initial.sql` + the T21 `0002_orchestrator.sql` creating `producer_schedules`/`producer_runs`). The DB tests below need both.

- [ ] **Step 1: Templates fixture dir.** Create `tests/fixtures/templates/echo/template.toml` (a discoverable, code-free manifest — discovery must not import its entrypoint):
```toml
name        = "echo"
entrypoint  = "tests.fixtures.templates.echo.producer:run"
description = "Fixture echo template for control-plane API tests"

[params]
url      = { type = "str", required = true, help = "A URL to echo" }
backfill = { type = "str", default = "all", choices = ["all", "recent"] }

[schedule]
default_interval = "30m"
```
(No `producer.py` is needed for this ticket — discovery and preview/run-now never import it here. T22 owns `discover_templates`; T23/T24 own the real subprocess path. Preview and orchestrator/run are monkeypatched below.)

- [ ] **Step 2: Failing test** `tests/test_control_plane_api.py` (write the whole file; DB tests assume `make up` + `make migrate`):
```python
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
    # Only config.py reads the environment; point Settings at the fixture templates dir.
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", TEMPLATES_DIR)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_schedules():
    # Remove rows this module creates (and their runs) before and after, so reruns
    # are deterministic. This helper owns its own transaction.
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
    assert all({"id", "name", "template", "params", "interval_seconds",
                "enabled", "force_run", "last_run_at"} <= set(r) for r in rows)


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
    # Preview must NOT run in-process. Stub _run_subprocess (the K4 minimal-env spawn)
    # and assert the route hands it --dry-run via the dry-run path and returns its sample.
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


def test_orchestrator_run_triggers_tick(monkeypatch):
    # Run-now triggers one tick; stub it so no subprocess is spawned.
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
    assert {"id", "schedule_id", "template", "started_at", "finished_at",
            "status", "submitted", "error"} <= set(mine)
    # filter by schedule_id
    filtered = client.get("/api/runs", params={"schedule_id": created["id"]}).json()
    assert filtered and all(x["schedule_id"] == created["id"] for x in filtered)
```

- [ ] **Step 3: Run → FAIL.** `uv run pytest tests/test_control_plane_api.py -v` — the routes don't exist yet (404s / `AttributeError` on `api._preview_subprocess`, `api.tick`).

- [ ] **Step 4: Implement in `src/bellweather/api.py`.** Add imports and the control-plane models + routes to the existing `api_router` (do not create a second router; `app.include_router(api_router)` at the bottom already mounts it). New imports near the top:
```python
import json
import os
import subprocess
import sys

from fastapi import HTTPException

from bellweather import schedules, templates
from bellweather.config import get_settings
from bellweather.orchestrator import tick
```
Pydantic request/response models (place after the read models, before `api_router`):
```python
# --- control-plane API (schedules / templates / runs) -----------------------
class ScheduleRow(BaseModel):
    id: int
    name: str
    template: str
    params: dict
    interval_seconds: int
    enabled: bool
    force_run: bool
    last_run_at: datetime | None


class ScheduleCreate(BaseModel):
    name: str
    template: str
    params: dict = {}
    interval_seconds: int
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    params: dict | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    force_run: bool | None = None


class TemplateParamRow(BaseModel):
    name: str
    type: str
    required: bool
    default: object | None = None
    choices: list | None = None
    help: str | None = None


class TemplateRow(BaseModel):
    name: str
    entrypoint: str
    description: str
    params: list[TemplateParamRow]
    default_interval_seconds: int | None = None


class RunRow(BaseModel):
    id: int
    schedule_id: int | None
    template: str
    params: dict
    started_at: datetime
    finished_at: datetime | None
    status: str
    submitted: int | None
    error: str | None


class TickResult(BaseModel):
    started_run_ids: list[int]
```
The preview helper — a module-level function so tests can monkeypatch it; it spawns the **same minimal-env subprocess** as the orchestrator (K4), never in-process:
```python
def _preview_subprocess(template: str, params: dict) -> dict:
    """Spawn `bellweather run-template --dry-run` with a minimal env (K4/K9).

    Never runs the template in-process: an in-process import would hand the
    customer script the API's DB/bucket credentials. The subprocess gets only
    BELLWEATHER_API_URL + PATH; its last stdout line is a JSON summary
    ({"submitted": int, "sample": [...]}).
    """
    proc = subprocess.run(
        [
            sys.executable, "-m", "bellweather.cli", "run-template",
            "--template", template, "--params", json.dumps(params), "--dry-run",
        ],
        env={
            "BELLWEATHER_API_URL": get_settings().bellweather_api_url,
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip() or "preview failed")
    return json.loads(proc.stdout.strip().splitlines()[-1])
```
The routes (add to `api_router`):
```python
@api_router.get("/templates", response_model=list[TemplateRow])
def api_templates():
    out = []
    for t in templates.discover_templates().values():
        out.append(TemplateRow(
            name=t.name, entrypoint=t.entrypoint, description=t.description,
            default_interval_seconds=t.default_interval_seconds,
            params=[TemplateParamRow(
                name=p.name, type=p.type, required=p.required,
                default=p.default, choices=p.choices, help=p.help,
            ) for p in t.params],
        ))
    return out


@api_router.post("/templates/{name}/preview")
def api_template_preview(name: str, params: dict):
    if name not in templates.discover_templates():
        raise HTTPException(status_code=404, detail="unknown template")
    return _preview_subprocess(name, params)


@api_router.get("/schedules", response_model=list[ScheduleRow])
def api_schedules():
    with get_conn() as conn:
        return schedules.list_schedules(conn)


@api_router.post("/schedules", response_model=ScheduleRow)
def api_create_schedule(body: ScheduleCreate):
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn, name=body.name, template=body.template, params=body.params,
            interval_seconds=body.interval_seconds, enabled=body.enabled,
        )
        conn.commit()
        return schedules.get_schedule(conn, sid)


@api_router.patch("/schedules/{schedule_id}", response_model=ScheduleRow)
def api_update_schedule(schedule_id: int, body: SchedulePatch):
    fields = body.model_dump(exclude_none=True)
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        if fields:
            schedules.update_schedule(conn, schedule_id, **fields)
            conn.commit()
        return schedules.get_schedule(conn, schedule_id)


@api_router.delete("/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: int):
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        schedules.delete_schedule(conn, schedule_id)
        conn.commit()
    return {"status": "deleted"}


@api_router.post("/schedules/{schedule_id}/force", response_model=ScheduleRow)
def api_force_schedule(schedule_id: int):
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        schedules.set_force_run(conn, schedule_id, True)
        conn.commit()
        return schedules.get_schedule(conn, schedule_id)


@api_router.post("/orchestrator/run", response_model=TickResult)
def api_orchestrator_run():
    with get_conn() as conn:
        return TickResult(started_run_ids=tick(conn))


@api_router.get("/runs", response_model=list[RunRow])
def api_runs(schedule_id: int | None = None, limit: int = Query(50, ge=1, le=500)):
    with get_conn() as conn:
        return schedules.list_runs(conn, schedule_id=schedule_id, limit=limit)
```
Notes that keep the implementation honest:
- The `tick` and `_preview_subprocess` names are referenced from the test as `api.tick` / `api._preview_subprocess`, so they must be module-level in `api.py` (imported `tick`, defined `_preview_subprocess`).
- `_preview_subprocess` runs `python -m bellweather.cli run-template ... --dry-run` (T23's `run-template`); preview is **never in-process** — D2/K4/K9. The 404 guard on the template name runs before the subprocess.
- Per-request DB and `conn.commit()` after every write (schedules.py helpers never commit). `get_schedule` guards each `{id}` route → 404, matching the read-endpoint shape conventions.

- [ ] **Step 5: Run → PASS.** `uv run pytest tests/test_control_plane_api.py -v` (with `make up` + `make migrate`). The preview/orchestrator tests pass without spawning anything (monkeypatched); the schedule/template/runs tests exercise real Postgres + the fixture templates dir.

- [ ] **Step 6: Full gate.** `make check` (ruff check + ruff format --check + pytest) green.

- [ ] **Step 7: Commit** (`feat: control-plane API for schedules/templates/runs/preview/force/run`).

## Acceptance criteria
- `POST /api/schedules` creates a `producer_schedules` row and returns it (`force_run` false, `enabled` true by default); `GET /api/schedules` lists rows whose keys are exactly `id, name, template, params, interval_seconds, enabled, force_run, last_run_at`.
- `PATCH /api/schedules/{id}` toggles `enabled` (and accepts `name|params|interval_seconds|force_run`); `POST /api/schedules/{id}/force` sets `force_run` true; `DELETE /api/schedules/{id}` removes the row; unknown `{id}` → 404 on PATCH/DELETE/force.
- `GET /api/templates` lists the fixture template(s) from `BELLWEATHER_TEMPLATES_DIR` with their param schemas, **without importing entrypoints**; `default_interval` is surfaced as `default_interval_seconds`.
- `POST /api/templates/{name}/preview` spawns a **minimal-env (`BELLWEATHER_API_URL` + `PATH`) `run-template --dry-run` subprocess** (never in-process) and returns its JSON sample; unknown template → 404.
- `POST /api/orchestrator/run` triggers exactly one `tick(conn)` and returns its started run ids; `GET /api/runs` returns recent `producer_runs` (filterable by `schedule_id`) with the `RUN_COLUMNS` shape.
- Routes added to the existing `api_router` (prefix `/api`) only; per-request `get_conn()` + commit after writes; schedules helpers still never commit. `make check` green.
