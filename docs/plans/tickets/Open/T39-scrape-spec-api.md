# T39 — Scrape-spec control-plane API (read/CRUD/preview)

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§8 Testing / control-plane + UI; K10 trusted dry-run preview). **Depends on:** T15 (read API + `api_router`), T33 (fetch seam `get_fetcher` + the `httpx` default adapter), T34 (`scrape_specs` migration + `scrape/specs.py`), T35 (`apply_binding`), T36 (`llm.LlmExtractor`). **Branch:** `ticket/T39-scrape-spec-api`. **PR, do not merge without approval.**

## Goal
Add the scrape-spec control-plane HTTP surface to the existing `/api` router: read (`GET`), CRUD (`POST`/`PATCH`/`DELETE`), and an **in-process dry-run preview** (`POST .../preview`). The GET endpoints are what the **unprivileged collector** (T40) reads to resolve a spec's `sites`/`fetch_adapter` — the split read-path from the design (collector reads via API, worker reads via `scrape.specs.get_spec` directly). CRUD maps to `scrape.specs.*` and **commits in the endpoint** (those helpers never commit — caller owns the txn), mirroring the schedules endpoints including 404s. Preview honors K10: the API is trusted and holds the LLM key, so it fetches **one** URL, LLM-extracts against the spec's `output_schema`, applies the spec's `binding`, and returns the extracted JSON + would-be observations — **committing nothing** (no bronze, no `/ingest`, no DB write).

## Files
- Modify: `src/bellweather/api.py` — add `ScrapeSpecRow` / `ScrapeSpecCreate` / `ScrapeSpecPatch` / `ScrapePreviewResult` models and the six `/scrape-specs` routes to the existing `api_router` (prefix `/api`); add imports for `scrape.specs`, `fetch.get_fetcher`, the `fetch.httpx_fetch` default-adapter registration, `llm.LlmExtractor`, `scrape.binding.apply_binding`.
- Test: `tests/test_api_scrape.py` — `fastapi.testclient` + Postgres (DB tests: `make up` + `make migrate`). CRUD round-trips against real Postgres; preview monkeypatches `get_fetcher` + `LlmExtractor` to fakes so no network/LLM call is made.

## Interface
From the build plan **Locked interfaces** (`docs/plans/2026-06-02-llm-scrape-engine.md`), verbatim.

`api.py` — add to `api_router` (prefix `/api`). The collector reads its spec via the GET endpoints; preview runs **in-process** (the API is trusted and holds the LLM key — it fetches one URL + LLM-extracts + binds, committing nothing):
```
GET    /scrape-specs                 -> list[ScrapeSpecRow]
GET    /scrape-specs/{name}          -> ScrapeSpecRow         (404 if unknown)   # collector uses sites+fetch_adapter
POST   /scrape-specs                 -> ScrapeSpecRow         (body ScrapeSpecCreate)
PATCH  /scrape-specs/{name}          -> ScrapeSpecRow         (body ScrapeSpecPatch; 404 if unknown)
DELETE /scrape-specs/{name}          -> {"status":"deleted"}  (404 if unknown)
POST   /scrape-specs/{name}/preview  -> ScrapePreviewResult   (body {"url": str | None}; default = first site)
```
`ScrapePreviewResult = {extracted: dict, symbols: list[str], sample: list[{symbol_key, ts, value}], tags: list[{tag_type, raw_value}]}` (sample/symbols capped to first ~N; commits nothing, no bronze, no /ingest). Preview reuses `get_fetcher`, `LlmExtractor`, `apply_binding`, `scrape.specs.get_spec`.

`scrape/specs.py` (helpers **never commit** — the API must `conn.commit()` after writes; `sites`/`output_schema`/`binding` come back as Python `list`/`dict`):
```python
def list_specs(conn) -> list[dict]: ...
def get_spec(conn, name: str) -> dict | None: ...
def create_spec(conn, *, name: str, sites: list, output_schema: dict, binding: dict,
                description: str | None = None, fetch_adapter: str = "httpx",
                llm_model: str | None = None, enabled: bool = True) -> int: ...   # returns id
def update_spec(conn, name: str, **fields) -> None: ...   # name|description|sites|output_schema|
                                                          # binding|fetch_adapter|llm_model|enabled; bumps updated_at
def delete_spec(conn, name: str) -> None: ...
```

`fetch/__init__.py` (the `httpx` adapter self-registers on `import bellweather.fetch.httpx_fetch`):
```python
@dataclass
class FetchResult:
    content: str
    status: int
    content_type: str | None = None
    final_url: str | None = None

def get_fetcher(name: str) -> FetchProvider | None: ...
```

`llm.py`:
```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
```

`scrape/binding.py` (returns `(observations, tags)` — reuses the gold-value point shape `NormalizedPoint(symbol_key, symbol_kind, ts, value, unit, description)` and `ExtractedTag(tag_type, raw_value, score)`):
```python
def apply_binding(instance: dict, binding: dict, *, fetched_at: datetime
                  ) -> tuple[list[NormalizedPoint], list[ExtractedTag]]: ...
```

## Steps

- [ ] **Step 0 (env):** `make up` (Postgres + fake-gcs) and `make migrate` (applies `0001_initial.sql`, the orchestrator's `0002_orchestrator.sql`, and the T34 `0003_scrape_specs.sql` creating `scrape_specs`). The CRUD tests below are DB-backed and need the `scrape_specs` table.

- [ ] **Step 1: Failing test** `tests/test_api_scrape.py` (write the whole file; DB tests assume `make up` + `make migrate`). CRUD runs against real Postgres; preview injects fakes for `get_fetcher` + `LlmExtractor` so no network or LLM is touched.
```python
"""Scrape-spec control-plane API via TestClient (DB tests require `make up` + `make migrate`).

CRUD (create -> get -> list -> patch -> delete + 404s) round-trips against real Postgres
(scrape_specs from migration 0003). Preview monkeypatches the fetch seam and LlmExtractor
to in-process fakes, so it never hits the network or the LLM and commits nothing.
"""

import pytest
from fastapi.testclient import TestClient

import bellweather.api as api
from bellweather.api import app
from bellweather.db import get_conn
from bellweather.fetch import FetchResult
from bellweather.migrate import apply_migrations

client = TestClient(app)

# Spec names this module owns — wiped before/after so reruns are deterministic.
_NAMES = ("t39-prices", "t39-other")

# A minimal but realistic spec body. output_schema is the LLM tool input_schema;
# binding maps the extracted JSON onto (symbol, ts, value) + tags.
_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                    "in_stock": {"type": "boolean"},
                },
            },
        }
    },
}
_BINDING = {
    "records_path": "$.items",
    "symbol_key": "scrape:prices:{category}:{name}",
    "symbol_kind": "scraped-metric",
    "value": "$.price",
    "ts": "fetched_at",
    "unit": "usd",
    "tags": ["category", "in_stock"],
}


def _body(name="t39-prices", **over):
    body = {
        "name": name,
        "description": "T39 fixture spec",
        "sites": ["https://example.com/a", "https://example.com/b"],
        "output_schema": _SCHEMA,
        "binding": _BINDING,
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": True,
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _clean():
    apply_migrations()

    def _wipe(c):
        c.execute("delete from scrape_specs where name = any(%s)", (list(_NAMES),))
        c.commit()

    with get_conn() as c:
        _wipe(c)
    yield
    with get_conn() as c:
        _wipe(c)


def _create(**over):
    r = client.post("/api/scrape-specs", json=_body(**over))
    assert r.status_code == 200, r.text
    return r.json()


# --- CRUD (DB-backed) -------------------------------------------------------
def test_create_returns_row_with_nested_json():
    created = _create()
    assert created["id"] > 0
    assert created["name"] == "t39-prices"
    assert created["enabled"] is True
    assert created["fetch_adapter"] == "httpx"
    # sites/output_schema/binding round-trip as nested JSON (psycopg jsonb adaption).
    assert created["sites"] == ["https://example.com/a", "https://example.com/b"]
    assert created["binding"]["symbol_key"] == "scrape:prices:{category}:{name}"
    assert created["output_schema"]["type"] == "object"


def test_get_then_list_includes_created():
    created = _create()
    got = client.get(f"/api/scrape-specs/{created['name']}")
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]
    assert got.json()["sites"] == created["sites"]
    rows = client.get("/api/scrape-specs").json()
    assert any(r["id"] == created["id"] and r["name"] == "t39-prices" for r in rows)
    assert all(
        {"id", "name", "description", "sites", "output_schema", "binding",
         "fetch_adapter", "llm_model", "enabled"} <= set(r)
        for r in rows
    )


def test_patch_updates_fields():
    created = _create()
    r = client.patch(
        f"/api/scrape-specs/{created['name']}",
        json={"enabled": False, "sites": ["https://example.com/only"], "llm_model": "claude-haiku-4-5-20251001"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["sites"] == ["https://example.com/only"]
    assert body["llm_model"] == "claude-haiku-4-5-20251001"
    # Persisted: a fresh GET reflects the patch.
    assert client.get(f"/api/scrape-specs/{created['name']}").json()["enabled"] is False


def test_delete_removes_spec():
    created = _create()
    r = client.delete(f"/api/scrape-specs/{created['name']}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}
    assert client.get(f"/api/scrape-specs/{created['name']}").status_code == 404
    rows = client.get("/api/scrape-specs").json()
    assert all(x["id"] != created["id"] for x in rows)


def test_get_unknown_is_404():
    assert client.get("/api/scrape-specs/t39-nope").status_code == 404


def test_patch_unknown_is_404():
    assert client.patch("/api/scrape-specs/t39-nope", json={"enabled": False}).status_code == 404


def test_delete_unknown_is_404():
    assert client.delete("/api/scrape-specs/t39-nope").status_code == 404


# --- in-process preview (fakes; no network, no LLM, no commit) --------------
class _FakeFetcher:
    name = "httpx"

    def __init__(self):
        self.urls = []

    def fetch(self, url, **opts):
        self.urls.append(url)
        return FetchResult(
            content="<html>raw page</html>", status=200,
            content_type="text/html", final_url=url,
        )


class _FakeLlm:
    """Stand-in for LlmExtractor: returns canned JSON, never builds an Anthropic client."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def extract(self, content, output_schema, *, model=None):
        self.calls.append((content, model))
        return {
            "items": [
                {"category": "fruit", "name": "apple", "price": 1.5, "in_stock": True},
                {"category": "fruit", "name": "pear", "price": 2.0, "in_stock": False},
            ]
        }


def test_preview_returns_extracted_observations_and_tags(monkeypatch):
    created = _create()
    fetcher = _FakeFetcher()
    fake_llm = _FakeLlm()
    # Patch the seams used inside the preview endpoint (the api module's references).
    monkeypatch.setattr(api, "get_fetcher", lambda name: fetcher)
    monkeypatch.setattr(api, "LlmExtractor", lambda *a, **k: fake_llm)

    r = client.post(f"/api/scrape-specs/{created['name']}/preview", json={"url": None})
    assert r.status_code == 200, r.text
    body = r.json()

    # Default url = first site; the fetcher saw exactly that one URL (one fetch only).
    assert fetcher.urls == ["https://example.com/a"]
    # The LLM was called with the raw page content + the spec's per-spec model (None here).
    assert fake_llm.calls and fake_llm.calls[0][0] == "<html>raw page</html>"

    # extracted = the raw LLM JSON instance.
    assert body["extracted"] == {
        "items": [
            {"category": "fruit", "name": "apple", "price": 1.5, "in_stock": True},
            {"category": "fruit", "name": "pear", "price": 2.0, "in_stock": False},
        ]
    }
    # symbols = distinct symbol_keys from the binding.
    assert body["symbols"] == [
        "scrape:prices:fruit:apple",
        "scrape:prices:fruit:pear",
    ]
    # sample = flat {symbol_key, ts, value} rows; values came from $.price.
    assert {s["symbol_key"]: s["value"] for s in body["sample"]} == {
        "scrape:prices:fruit:apple": 1.5,
        "scrape:prices:fruit:pear": 2.0,
    }
    assert all({"symbol_key", "ts", "value"} == set(s) for s in body["sample"])
    # tags = {tag_type, raw_value} per bound field.
    tag_pairs = {(t["tag_type"], t["raw_value"]) for t in body["tags"]}
    assert ("category", "fruit") in tag_pairs
    assert all({"tag_type", "raw_value"} == set(t) for t in body["tags"])

    # Preview commits nothing: no scrape-llm-v1 raw_records, no tracked_symbols.
    with get_conn() as c:
        n_recs = c.execute(
            "select count(*) from raw_records where content_type = %s", ("scrape-llm-v1",)
        ).fetchone()[0]
        n_syms = c.execute(
            "select count(*) from tracked_symbols where key like %s", ("scrape:prices:%",)
        ).fetchone()[0]
    assert n_recs == 0
    assert n_syms == 0


def test_preview_explicit_url_overrides_first_site(monkeypatch):
    created = _create()
    fetcher = _FakeFetcher()
    monkeypatch.setattr(api, "get_fetcher", lambda name: fetcher)
    monkeypatch.setattr(api, "LlmExtractor", lambda *a, **k: _FakeLlm())
    r = client.post(
        f"/api/scrape-specs/{created['name']}/preview",
        json={"url": "https://example.com/explicit"},
    )
    assert r.status_code == 200
    assert fetcher.urls == ["https://example.com/explicit"]


def test_preview_unknown_spec_is_404():
    assert client.post("/api/scrape-specs/t39-nope/preview", json={"url": None}).status_code == 404
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_api_scrape.py -v` — the routes don't exist yet, so every CRUD/preview call 404s on the URL (`/api/scrape-specs` unmatched) and the `api.get_fetcher` / `api.LlmExtractor` monkeypatch targets raise `AttributeError`. Expected: errors/failures across the module.

- [ ] **Step 3: Implement in `src/bellweather/api.py`.** Add imports, the four Pydantic models, the `_scrape_preview` helper, and the six routes to the existing `api_router` (do **not** create a second router — `app.include_router(api_router)` at the bottom already mounts it).

New imports near the top (alongside the existing `from bellweather import reads, schedules, templates`):
```python
from bellweather import reads, schedules, templates
import bellweather.fetch.httpx_fetch  # noqa: F401  # registers the default "httpx" adapter
from bellweather.fetch import get_fetcher
from bellweather.llm import LlmExtractor
from bellweather.scrape import specs as scrape_specs
from bellweather.scrape.binding import apply_binding
```
> `get_fetcher` and `LlmExtractor` are bound as module-level names in `api.py` so the test can `monkeypatch.setattr(api, "get_fetcher", ...)` / `monkeypatch.setattr(api, "LlmExtractor", ...)` — the preview endpoint must reference them as bare names (not `fetch.get_fetcher`).
> **Why the `httpx_fetch` import is required:** `bellweather.fetch.__init__` does NOT auto-import its adapters, so nothing in the tree registers the default `httpx` provider until `bellweather.fetch.httpx_fetch` is imported. Without this line a *real* (non-monkeypatched) preview of a `fetch_adapter="httpx"` spec would resolve `get_fetcher("httpx") -> None` and 400 — the unit tests would still pass (they patch `api.get_fetcher`), so this is a latent prod bug the import closes. (T40's collector likewise needs the adapter registered; it imports/falls back to `HttpxFetcher()` on its side.)

Pydantic request/response models (place after the existing control-plane models, before `api_router = APIRouter(...)`):
```python
# --- scrape-spec control plane (read / CRUD / preview) ----------------------
class ScrapeSpecRow(BaseModel):
    id: int
    name: str
    description: str | None = None
    sites: list
    output_schema: dict
    binding: dict
    fetch_adapter: str
    llm_model: str | None = None
    enabled: bool


class ScrapeSpecCreate(BaseModel):
    name: str
    sites: list = []
    output_schema: dict
    binding: dict
    description: str | None = None
    fetch_adapter: str = "httpx"
    llm_model: str | None = None
    enabled: bool = True


class ScrapeSpecPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    sites: list | None = None
    output_schema: dict | None = None
    binding: dict | None = None
    fetch_adapter: str | None = None
    llm_model: str | None = None
    enabled: bool | None = None


class ScrapePreviewRequest(BaseModel):
    url: str | None = None


class ScrapePreviewSampleRow(BaseModel):
    symbol_key: str
    ts: datetime
    value: float


class ScrapePreviewTagRow(BaseModel):
    tag_type: str
    raw_value: str


class ScrapePreviewResult(BaseModel):
    extracted: dict
    symbols: list[str]
    sample: list[ScrapePreviewSampleRow]
    tags: list[ScrapePreviewTagRow]
```
The preview helper — a module-level function so the in-process logic is unit-clear; it fetches one URL, LLM-extracts, applies the binding, and reshapes into `ScrapePreviewResult` without committing anything. Reuses the *bare* module names `get_fetcher`/`LlmExtractor` so they are monkeypatchable:
```python
SCRAPE_PREVIEW_SAMPLE_LIMIT = 50  # cap flat sample/symbol rows a dry-run preview returns


def _scrape_preview(spec: dict, url: str | None) -> dict:
    """In-process K10 dry-run: fetch ONE url, LLM-extract against the spec's
    output_schema, apply the spec's binding, and return the extracted JSON +
    would-be observations/tags. Commits NOTHING — no bronze, no /ingest, no DB.

    The API is the trusted surface that holds the LLM key (the collector does
    not), so this runs in-process rather than spawning a subprocess. Reuses the
    same units the worker path uses: get_fetcher, LlmExtractor, apply_binding.
    """
    target = url or (spec["sites"][0] if spec["sites"] else None)
    if not target:
        raise HTTPException(status_code=400, detail="spec has no sites and no url given")
    fetcher = get_fetcher(spec["fetch_adapter"])
    if fetcher is None:
        raise HTTPException(status_code=400, detail=f"unknown fetch adapter: {spec['fetch_adapter']}")
    fetched = fetcher.fetch(target)
    instance = LlmExtractor().extract(
        fetched.content, spec["output_schema"], model=spec.get("llm_model")
    )
    obs, tags = apply_binding(instance, spec["binding"], fetched_at=datetime.now(timezone.utc))
    symbols: list[str] = []
    for o in obs:
        if o.symbol_key not in symbols:
            symbols.append(o.symbol_key)
    sample = [
        {"symbol_key": o.symbol_key, "ts": o.ts, "value": o.value} for o in obs
    ]
    return {
        "extracted": instance,
        "symbols": symbols[:SCRAPE_PREVIEW_SAMPLE_LIMIT],
        "sample": sample[:SCRAPE_PREVIEW_SAMPLE_LIMIT],
        "tags": [{"tag_type": t.tag_type, "raw_value": t.raw_value} for t in tags],
    }
```
> Add `timezone` to the datetime import at the top of the file: `from datetime import datetime, timezone`.

> **Deliberate `fetch_adapter` asymmetry (documented, not drift):** preview **400s** on an unknown
> `fetch_adapter` (line 413 — it surfaces the misconfiguration to the author), whereas T40's collector
> resolves `get_fetcher(spec["fetch_adapter"]) or HttpxFetcher()` and **falls back to httpx** (a
> scheduled run must never crash on a typo'd adapter). Same field, two intentional behaviors: fail-loud
> at authoring time, fail-safe at collection time. (T41's authoring form should constrain the adapter to
> a known set where practical to avoid the mismatch in the first place.)

The routes (add to `api_router`, e.g. after the schedules block):
```python
@api_router.get("/scrape-specs", response_model=list[ScrapeSpecRow])
def api_scrape_specs():
    with get_conn() as conn:
        return scrape_specs.list_specs(conn)


@api_router.get("/scrape-specs/{name}", response_model=ScrapeSpecRow)
def api_scrape_spec(name: str):
    with get_conn() as conn:
        spec = scrape_specs.get_spec(conn, name)
        if spec is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        return spec


@api_router.post("/scrape-specs", response_model=ScrapeSpecRow)
def api_create_scrape_spec(body: ScrapeSpecCreate):
    with get_conn() as conn:
        scrape_specs.create_spec(
            conn,
            name=body.name,
            sites=body.sites,
            output_schema=body.output_schema,
            binding=body.binding,
            description=body.description,
            fetch_adapter=body.fetch_adapter,
            llm_model=body.llm_model,
            enabled=body.enabled,
        )
        conn.commit()
        return scrape_specs.get_spec(conn, body.name)


@api_router.patch("/scrape-specs/{name}", response_model=ScrapeSpecRow)
def api_update_scrape_spec(name: str, body: ScrapeSpecPatch):
    fields = body.model_dump(exclude_none=True)
    with get_conn() as conn:
        if scrape_specs.get_spec(conn, name) is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        if fields:
            scrape_specs.update_spec(conn, name, **fields)
            conn.commit()
        # A patch may rename the spec; look it up by its (possibly new) name.
        return scrape_specs.get_spec(conn, fields.get("name", name))


@api_router.delete("/scrape-specs/{name}")
def api_delete_scrape_spec(name: str):
    with get_conn() as conn:
        if scrape_specs.get_spec(conn, name) is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        scrape_specs.delete_spec(conn, name)
        conn.commit()
    return {"status": "deleted"}


@api_router.post("/scrape-specs/{name}/preview", response_model=ScrapePreviewResult)
def api_scrape_spec_preview(name: str, body: ScrapePreviewRequest):
    with get_conn() as conn:
        spec = scrape_specs.get_spec(conn, name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown scrape spec")
    return _scrape_preview(spec, body.url)
```
Notes that keep the implementation honest:
- `get_fetcher` and `LlmExtractor` are referenced as **bare module-level names** so the test's `monkeypatch.setattr(api, ...)` lands; do not call them through their packages.
- The 404 guard runs **before** the preview's fetch/LLM work; preview reads the spec inside its own `get_conn()` block, closes it, and does the fetch/LLM/bind outside any transaction — it writes nothing, mirroring the design's "commit nothing" (no bronze, no `/ingest`, no DB).
- Per-request `get_conn()` + `conn.commit()` after every CRUD write (`scrape.specs.*` never commit, per the `queue.py`/`schedules.py` convention); the 404 guards mirror `api_update_schedule`/`api_delete_schedule`.
- `update_spec` may rename; the route re-reads by the new name so the response is the updated row.

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_api_scrape.py -v` (with `make up` + `make migrate`). The CRUD tests exercise real Postgres `scrape_specs`; the preview tests use the injected `_FakeFetcher`/`_FakeLlm` (no network, no LLM) and assert the `ScrapePreviewResult` shape plus that nothing was committed.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green with `make up` running.

- [ ] **Step 6: Commit** (`feat: scrape-spec control-plane API (read/CRUD/in-process preview)`).

## Acceptance criteria
- `POST /api/scrape-specs` creates a `scrape_specs` row (defaults `fetch_adapter="httpx"`, `enabled=true`) and returns a `ScrapeSpecRow` whose `sites`/`output_schema`/`binding` round-trip as nested JSON; `GET /api/scrape-specs` lists rows with keys `id, name, description, sites, output_schema, binding, fetch_adapter, llm_model, enabled`.
- `GET /api/scrape-specs/{name}` returns the full spec (collector reads `sites`+`fetch_adapter` here) and 404s on an unknown name.
- `PATCH /api/scrape-specs/{name}` updates `name|description|sites|output_schema|binding|fetch_adapter|llm_model|enabled` and persists; `DELETE /api/scrape-specs/{name}` removes the row and returns `{"status":"deleted"}`; unknown `{name}` → 404 on GET/PATCH/DELETE/preview.
- `POST /api/scrape-specs/{name}/preview` runs **in-process** (K10): it loads the spec, picks `body.url or spec["sites"][0]`, fetches via `get_fetcher(spec["fetch_adapter"])` (the default `httpx` adapter is registered via the `bellweather.fetch.httpx_fetch` import), LLM-extracts with `LlmExtractor().extract(content, spec["output_schema"], model=spec["llm_model"])`, applies `apply_binding`, and returns `ScrapePreviewResult` = `{extracted, symbols (distinct), sample (first ~N {symbol_key, ts, value}), tags ({tag_type, raw_value})}` — **committing nothing** (no `scrape-llm-v1` raw_records, no `tracked_symbols`, no `/ingest`).
- Preview reuses the worker-path units (`get_fetcher`, `LlmExtractor`, `apply_binding`, `scrape.specs.get_spec`) and the tests assert the shape with fakes (no real network/LLM); `get_fetcher`/`LlmExtractor` are monkeypatchable module names on `api`.
- Routes added to the existing `api_router` (prefix `/api`) only; per-request `get_conn()` + commit after writes; `scrape.specs.*` helpers still never commit. `make check` green.
