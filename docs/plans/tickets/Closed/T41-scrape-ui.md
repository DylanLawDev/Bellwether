# T41 — Scrape-specs UI page + `web.data` backends

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§8 control-plane + UI; §4 scrape-spec contract; §K10 dry-run preview).
**Depends on:** T16 (live web backend + `web.data` seam, the `_get`/`_request` httpx helpers), T39 (scrape-spec control-plane API: `/api/scrape-specs` read/CRUD/preview). **Branch:** `ticket/T41-scrape-ui`. **PR, do not merge without approval.**

## Goal
Add the operator **Scrape** control-plane page and the `web.data` backend functions behind it, following the existing seam already used by the Schedules page (T26): the page imports only from `bellweather.web.data`; `mock.py` serves deterministic in-memory shapes for offline use; `live.py` issues `httpx` calls to the T39 `/api/scrape-specs` endpoints with **identical shapes**. This adds a second control-plane write-path (POST/PATCH/DELETE) plus a **dry-run preview** that commits nothing (K10). Per T16, the tests cover only the backends + column contracts, not Streamlit screen internals — **no DB, no network** (live runs against `pytest-httpserver`, mock is in-memory).

## Files
- Create: `src/bellweather/web/pages/6_Scrape.py` — list scrape specs, an authoring form (name, sites as one-URL-per-line text area, `output_schema` + `binding` as JSON text areas, `fetch_adapter`, `llm_model`), a **Preview (dry-run)** button showing extracted JSON + sample observations, and delete. Imports only from `bellweather.web.data`.
- Modify: `src/bellweather/web/data/source.py` — add `SCRAPE_SPEC_COLUMNS` and document the six new functions in the module docstring.
- Modify: `src/bellweather/web/data/mock.py` — add `get_scrape_specs/get_scrape_spec/create_scrape_spec/update_scrape_spec/delete_scrape_spec/preview_scrape_spec` (in-memory, deterministic, round-tripping).
- Modify: `src/bellweather/web/data/live.py` — add the same six functions, issuing `httpx` calls to `/api/scrape-specs...` via the existing `_get`/`_request` helpers.
- Modify: `src/bellweather/web/data/__init__.py` — re-export the six new functions through the seam and extend `__all__`.
- Test: `tests/test_web_scrape.py` — `live.*` against a fake API via `pytest-httpserver` (mirrors `tests/test_web_schedules.py`); `mock.*` returns in-memory shapes; both match the `source` column constant. No DB, no network.

## Interface
Column contract (locked, `web/data/source.py`):
```python
SCRAPE_SPEC_COLUMNS = ["id", "name", "description", "fetch_adapter", "llm_model", "enabled"]
# sites/output_schema/binding are nested JSON, carried per-spec (not flat columns) like params on schedules.
```
Backend functions (locked, mock + live identical shapes):
```python
get_scrape_specs()                  -> DataFrame[SCRAPE_SPEC_COLUMNS]
get_scrape_spec(name)               -> dict   # full spec incl sites/output_schema/binding
create_scrape_spec(name, sites, output_schema, binding, *, description=None,
                   fetch_adapter="httpx", llm_model=None) -> int
update_scrape_spec(name, **fields)  -> None
delete_scrape_spec(name)            -> None
preview_scrape_spec(name, url=None) -> dict   # {extracted, symbols, sample, tags}
```
T39 API surface these map to (prefix `/api`):
```
GET    /scrape-specs                 -> list[ScrapeSpecRow]
GET    /scrape-specs/{name}          -> ScrapeSpecRow         (404 if unknown)
POST   /scrape-specs                 -> ScrapeSpecRow         (body ScrapeSpecCreate)
PATCH  /scrape-specs/{name}          -> ScrapeSpecRow         (body ScrapeSpecPatch; 404 if unknown)
DELETE /scrape-specs/{name}          -> {"status":"deleted"}  (404 if unknown)
POST   /scrape-specs/{name}/preview  -> ScrapePreviewResult   (body {"url": str | None}; default = first site)
```
`ScrapePreviewResult = {extracted: dict, symbols: list[str], sample: list[{symbol_key, ts, value}], tags: list[{tag_type, raw_value}]}` — sample/symbols capped to the first ~N; commits nothing (no bronze, no DB, no `/ingest`).

## Steps

- [ ] **Step 1: Column contract + docstring** in `src/bellweather/web/data/source.py`. Append after the `RUN_COLUMNS` block:
```python
# Scrape-spec control plane (T41). `sites`/`output_schema`/`binding` are nested JSON
# carried per-spec (like `params` on a schedule), not flat columns here.
SCRAPE_SPEC_COLUMNS = [
    "id",
    "name",
    "description",
    "fetch_adapter",
    "llm_model",
    "enabled",
]
```
  And add to the module docstring's function list (after `preview_template(...)`):
```
    get_scrape_specs()                 -> DataFrame[id, name, description,
                                                    fetch_adapter, llm_model, enabled]
    get_scrape_spec(name)              -> dict   # full spec incl sites/output_schema/binding
    create_scrape_spec(name, sites, output_schema, binding, *, description=None,
                       fetch_adapter="httpx", llm_model=None) -> int
    update_scrape_spec(name, **fields) -> None   # name|description|sites|output_schema|
                                                 # binding|fetch_adapter|llm_model|enabled
    delete_scrape_spec(name)           -> None
    preview_scrape_spec(name, url=None) -> dict  # {extracted, symbols, sample, tags}; commits nothing
```

- [ ] **Step 2: Failing test** `tests/test_web_scrape.py`. Two halves: `live.*` against `pytest-httpserver` (mirroring `tests/test_web_schedules.py` / `tests/test_web_live.py`), and `mock.*` in-memory. No DB, no network.
```python
"""Scrape-spec control-plane backends build matching shapes (mock + live).

live.* is exercised against a fake API via pytest-httpserver (mirrors
tests/test_web_schedules.py); mock.* returns in-memory shapes. Both match the
bellweather.web.data.source.SCRAPE_SPEC_COLUMNS contract. No DB, no network.
"""

import pytest

from bellweather.config import get_ui_settings
from bellweather.web.data import live, mock, source as contract

_SCHEMA = {
    "type": "object",
    "properties": {"price": {"type": "number"}, "title": {"type": "string"}},
}
_BINDING = {
    "symbol_key": "scrape:demo:{title}",
    "symbol_kind": "scraped-metric",
    "value": "$.price",
    "ts": "fetched_at",
    "unit": "usd",
    "tags": ["title"],
}

_SPEC_ROW = {
    "id": 1,
    "name": "demo-prices",
    "description": "Fixture scrape spec.",
    "fetch_adapter": "httpx",
    "llm_model": None,
    "enabled": True,
}
_SPEC_FULL = dict(
    _SPEC_ROW,
    sites=["https://example.com/a", "https://example.com/b"],
    output_schema=_SCHEMA,
    binding=_BINDING,
)
_PREVIEW = {
    "extracted": {"price": 12.5, "title": "Widget"},
    "symbols": ["scrape:demo:Widget"],
    "sample": [
        {"symbol_key": "scrape:demo:Widget", "ts": "2026-06-02T11:00:00+00:00", "value": 12.5}
    ],
    "tags": [{"tag_type": "title", "raw_value": "Widget"}],
}


# --- live: fake API via pytest-httpserver -----------------------------------
@pytest.fixture()
def _api(httpserver, monkeypatch):
    httpserver.expect_request("/api/scrape-specs", method="GET").respond_with_json([_SPEC_ROW])
    httpserver.expect_request("/api/scrape-specs", method="POST").respond_with_json(
        dict(_SPEC_ROW, id=7)
    )
    httpserver.expect_request(
        "/api/scrape-specs/demo-prices", method="GET"
    ).respond_with_json(_SPEC_FULL)
    httpserver.expect_request(
        "/api/scrape-specs/demo-prices", method="PATCH"
    ).respond_with_json(dict(_SPEC_ROW, enabled=False))
    httpserver.expect_request(
        "/api/scrape-specs/demo-prices", method="DELETE"
    ).respond_with_json({"status": "deleted"})
    # The preview body carries the url unwrapped under "url".
    httpserver.expect_request(
        "/api/scrape-specs/demo-prices/preview",
        method="POST",
        json={"url": "https://example.com/a"},
    ).respond_with_json(_PREVIEW)
    monkeypatch.setenv("BELLWEATHER_API_URL", httpserver.url_for("").rstrip("/"))
    get_ui_settings.cache_clear()
    yield
    get_ui_settings.cache_clear()


def test_live_get_scrape_specs(_api):
    df = live.get_scrape_specs()
    assert list(df.columns) == contract.SCRAPE_SPEC_COLUMNS
    assert df.iloc[0]["name"] == "demo-prices"
    assert df.iloc[0]["fetch_adapter"] == "httpx"


def test_live_get_scrape_spec_full(_api):
    spec = live.get_scrape_spec("demo-prices")
    assert spec["sites"] == ["https://example.com/a", "https://example.com/b"]
    assert spec["output_schema"] == _SCHEMA
    assert spec["binding"] == _BINDING


def test_live_create_scrape_spec_returns_id(_api):
    new_id = live.create_scrape_spec(
        "demo-prices", _SPEC_FULL["sites"], _SCHEMA, _BINDING, description="x"
    )
    assert new_id == 7


def test_live_write_paths_do_not_raise(_api):
    live.update_scrape_spec("demo-prices", enabled=False)
    live.delete_scrape_spec("demo-prices")


def test_live_preview_scrape_spec(_api):
    out = live.preview_scrape_spec("demo-prices", url="https://example.com/a")
    assert set(out) == {"extracted", "symbols", "sample", "tags"}
    assert out["extracted"]["price"] == 12.5
    assert out["symbols"] == ["scrape:demo:Widget"]
    assert out["sample"][0]["value"] == 12.5
    assert out["tags"][0]["tag_type"] == "title"


# --- mock: in-memory, no API -------------------------------------------------
def test_mock_get_scrape_specs_shape():
    df = mock.get_scrape_specs()
    assert list(df.columns) == contract.SCRAPE_SPEC_COLUMNS


def test_mock_get_scrape_spec_has_nested_json():
    spec = mock.get_scrape_spec(mock.get_scrape_specs().iloc[0]["name"])
    assert isinstance(spec["sites"], list)
    assert isinstance(spec["output_schema"], dict)
    assert isinstance(spec["binding"], dict)


def test_mock_create_then_get_roundtrip():
    new_id = mock.create_scrape_spec(
        "round-trip", ["https://x"], _SCHEMA, _BINDING, description="rt"
    )
    df = mock.get_scrape_specs()
    assert new_id in df["id"].tolist()
    spec = mock.get_scrape_spec("round-trip")
    assert spec["sites"] == ["https://x"]
    assert spec["output_schema"] == _SCHEMA
    assert spec["binding"] == _BINDING
    assert spec["description"] == "rt"


def test_mock_update_enabled():
    mock.create_scrape_spec("toggle-me", ["https://x"], _SCHEMA, _BINDING)
    mock.update_scrape_spec("toggle-me", enabled=False)
    df = mock.get_scrape_specs().set_index("name")
    assert bool(df.loc["toggle-me", "enabled"]) is False


def test_mock_update_nested_json():
    mock.create_scrape_spec("edit-binding", ["https://x"], _SCHEMA, _BINDING)
    new_binding = dict(_BINDING, unit="eur")
    mock.update_scrape_spec("edit-binding", binding=new_binding)
    assert mock.get_scrape_spec("edit-binding")["binding"]["unit"] == "eur"


def test_mock_delete_removes_row():
    mock.create_scrape_spec("delete-me", ["https://x"], _SCHEMA, _BINDING)
    mock.delete_scrape_spec("delete-me")
    assert "delete-me" not in mock.get_scrape_specs()["name"].tolist()


def test_mock_get_unknown_spec_returns_none():
    assert mock.get_scrape_spec("nope-does-not-exist") is None


def test_mock_preview_scrape_spec_shape():
    spec_name = mock.get_scrape_specs().iloc[0]["name"]
    out = mock.preview_scrape_spec(spec_name)
    assert set(out) == {"extracted", "symbols", "sample", "tags"}
    assert isinstance(out["extracted"], dict)
    assert isinstance(out["symbols"], list)
    assert isinstance(out["sample"], list)
    assert isinstance(out["tags"], list)
```

- [ ] **Step 3: Run → FAIL** (the `source` constant and the six backend functions don't exist yet):
```
uv run pytest tests/test_web_scrape.py -q
```
Expect failures like `AttributeError: module 'bellweather.web.data.source' has no attribute 'SCRAPE_SPEC_COLUMNS'` and `AttributeError: module 'bellweather.web.data.mock' has no attribute 'get_scrape_specs'`.

- [ ] **Step 4: Implement `mock.py`** — append an in-memory registry + the six functions after the existing `preview_template` (no `_build()` rewrite). The mock keeps state in a module-level list so the page's create/update/delete round-trip in an offline session:
```python
# --- scrape-spec control plane (T41) ----------------------------------------
# One fixture spec so the Scrape page lists + previews something offline. sites/
# output_schema/binding are nested JSON carried per-spec (not SCRAPE_SPEC_COLUMNS).
_SCRAPE_SPECS_STATE: list[dict] = [
    {
        "id": 1,
        "name": "demo-prices",
        "description": "Fixture scrape spec (offline demo).",
        "sites": ["https://example.com/products/a", "https://example.com/products/b"],
        "output_schema": {
            "type": "object",
            "properties": {
                "price": {"type": "number"},
                "title": {"type": "string"},
                "in_stock": {"type": "boolean"},
            },
        },
        "binding": {
            "symbol_key": "scrape:prices:{title}",
            "symbol_kind": "scraped-metric",
            "value": "$.price",
            "ts": "fetched_at",
            "unit": "usd",
            "tags": ["title", "in_stock"],
        },
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": True,
    }
]
_NEXT_SCRAPE_ID = {"spec": 2}


def _scrape_specs_frame() -> pd.DataFrame:
    rows = [{c: s[c] for c in contract.SCRAPE_SPEC_COLUMNS} for s in _SCRAPE_SPECS_STATE]
    return pd.DataFrame(rows, columns=contract.SCRAPE_SPEC_COLUMNS)


def get_scrape_specs() -> pd.DataFrame:
    return _scrape_specs_frame()


def get_scrape_spec(name) -> dict | None:
    for s in _SCRAPE_SPECS_STATE:
        if s["name"] == name:
            # deep-ish copy so callers can't mutate the fixture in place
            return {
                **s,
                "sites": list(s["sites"]),
                "output_schema": dict(s["output_schema"]),
                "binding": dict(s["binding"]),
            }
    return None


def create_scrape_spec(
    name, sites, output_schema, binding, *, description=None, fetch_adapter="httpx", llm_model=None
) -> int:
    sid = _NEXT_SCRAPE_ID["spec"]
    _NEXT_SCRAPE_ID["spec"] += 1
    _SCRAPE_SPECS_STATE.append(
        {
            "id": sid,
            "name": name,
            "description": description,
            "sites": list(sites),
            "output_schema": dict(output_schema),
            "binding": dict(binding),
            "fetch_adapter": fetch_adapter,
            "llm_model": llm_model,
            "enabled": True,
        }
    )
    return sid


def update_scrape_spec(name, **fields) -> None:
    allowed = {
        "name",
        "description",
        "sites",
        "output_schema",
        "binding",
        "fetch_adapter",
        "llm_model",
        "enabled",
    }
    for s in _SCRAPE_SPECS_STATE:
        if s["name"] == name:
            s.update({k: v for k, v in fields.items() if k in allowed})


def delete_scrape_spec(name) -> None:
    _SCRAPE_SPECS_STATE[:] = [s for s in _SCRAPE_SPECS_STATE if s["name"] != name]


def preview_scrape_spec(name, url=None) -> dict:
    # Deterministic dry-run shape (commits nothing). Mirrors the live API's
    # ScrapePreviewResult: extracted JSON + would-be symbols/sample/tags.
    return {
        "extracted": {"price": 9.99, "title": "demo"},
        "symbols": [f"scrape:prices:demo ({name})"],
        "sample": [
            {
                "symbol_key": f"scrape:prices:demo ({name})",
                "ts": _now_hour().isoformat(),
                "value": 9.99,
            }
        ],
        "tags": [{"tag_type": "title", "raw_value": "demo"}],
    }
```

- [ ] **Step 5: Implement `live.py`** — add the six functions after `preview_template`, reusing the existing `_get` (GET) and `_request(method, path, json=None, *, timeout=_TIMEOUT, **params)` helpers and the existing `_frame`:
```python
def get_scrape_specs() -> pd.DataFrame:
    return _frame(_get("/api/scrape-specs"), contract.SCRAPE_SPEC_COLUMNS)


def get_scrape_spec(name) -> dict:
    return _get(f"/api/scrape-specs/{name}")


def create_scrape_spec(
    name, sites, output_schema, binding, *, description=None, fetch_adapter="httpx", llm_model=None
) -> int:
    body = {
        "name": name,
        "sites": sites,
        "output_schema": output_schema,
        "binding": binding,
        "description": description,
        "fetch_adapter": fetch_adapter,
        "llm_model": llm_model,
    }
    return _request("POST", "/api/scrape-specs", json=body)["id"]


def update_scrape_spec(name, **fields) -> None:
    _request("PATCH", f"/api/scrape-specs/{name}", json=fields)


def delete_scrape_spec(name) -> None:
    _request("DELETE", f"/api/scrape-specs/{name}")


def preview_scrape_spec(name, url=None) -> dict:
    # Trusted in-process dry-run (K10): the API fetches one URL + LLM-extracts +
    # binds, committing nothing. Holds the LLM key, so it can take orchestrator's
    # long timeout. Returns {extracted, symbols, sample, tags}.
    return _request(
        "POST", f"/api/scrape-specs/{name}/preview", json={"url": url}, timeout=_LONG_TIMEOUT
    )
```

- [ ] **Step 6: Re-export through the seam** in `src/bellweather/web/data/__init__.py` — add six bindings after `preview_template = _b.preview_template`:
```python
get_scrape_specs = _b.get_scrape_specs
get_scrape_spec = _b.get_scrape_spec
create_scrape_spec = _b.create_scrape_spec
update_scrape_spec = _b.update_scrape_spec
delete_scrape_spec = _b.delete_scrape_spec
preview_scrape_spec = _b.preview_scrape_spec
```
  And append those six names to the `__all__` list:
```python
    "get_scrape_specs",
    "get_scrape_spec",
    "create_scrape_spec",
    "update_scrape_spec",
    "delete_scrape_spec",
    "preview_scrape_spec",
```

- [ ] **Step 7: Run → PASS:**
```
uv run pytest tests/test_web_scrape.py -q
```

- [ ] **Step 8: Build the page** `src/bellweather/web/pages/6_Scrape.py` — imports only from `bellweather.web.data`:
```python
"""Scrape specs — LLM scrape-engine control plane.

List scrape specs, author one (sites + output schema + binding), dry-run preview
the extraction (commits nothing), and delete. Reads/writes only through
bellweather.web.data (mock or live). Schedule a spec from the Schedules page with
template "scrape" and params {"spec": <name>}.
"""

import json

import streamlit as st

from bellweather.web import data

st.title("Scrape specs")
st.caption(
    "Declare {sites, output schema, binding} once; preview the LLM extraction, "
    "then schedule with the 'scrape' template."
)


def _parse_json(label: str, raw: str) -> tuple[object | None, str | None]:
    """Parse a JSON text area; return (value, error_message)."""
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{label} is not valid JSON: {exc}"


# --- existing specs ---------------------------------------------------------
st.subheader("Specs")
specs = data.get_scrape_specs()
if specs.empty:
    st.info("No scrape specs yet. Author one below.")
else:
    for row in specs.to_dict("records"):
        name = row["name"]
        cols = st.columns([3, 2, 2, 2, 2])
        cols[0].markdown(f"**{name}**  \n`{row['fetch_adapter']}`")
        cols[1].markdown(row.get("description") or "_no description_")
        cols[2].markdown(f"model: `{row['llm_model'] or 'default'}`")
        enabled = cols[3].toggle("Enabled", value=bool(row["enabled"]), key=f"en_{name}")
        if enabled != bool(row["enabled"]):
            data.update_scrape_spec(name, enabled=enabled)
            st.rerun()
        if cols[4].button("Delete", key=f"del_{name}"):
            data.delete_scrape_spec(name)
            st.rerun()

        if st.button("Preview (dry-run)", key=f"prev_{name}"):
            spec = data.get_scrape_spec(name)
            first_url = spec["sites"][0] if spec.get("sites") else None
            out = data.preview_scrape_spec(name, url=first_url)
            st.success(
                f"Would emit {len(out['sample'])} sample point(s) across "
                f"{len(out['symbols'])} symbol(s) and {len(out['tags'])} tag(s)."
            )
            st.markdown("**Extracted JSON**")
            st.json(out["extracted"])
            st.markdown("**Sample observations**")
            st.json(out["sample"])

# --- author a spec ----------------------------------------------------------
st.subheader("Author a spec")
with st.form("add_spec"):
    name = st.text_input("Spec name", value="my-spec")
    description = st.text_input("Description", value="")
    sites_raw = st.text_area("Sites (one URL per line)", value="https://example.com/")
    output_schema_raw = st.text_area(
        "Output schema (JSON Schema)",
        value='{\n  "type": "object",\n  "properties": {"price": {"type": "number"}}\n}',
    )
    binding_raw = st.text_area(
        "Binding (JSON)",
        value=(
            '{\n  "symbol_key": "scrape:demo:{title}",\n  "symbol_kind": "scraped-metric",\n'
            '  "value": "$.price",\n  "ts": "fetched_at",\n  "unit": "usd",\n  "tags": []\n}'
        ),
    )
    fetch_adapter = st.text_input("Fetch adapter", value="httpx")
    llm_model = st.text_input("LLM model (blank = default)", value="")
    added = st.form_submit_button("Create spec")

if added:
    sites = [line.strip() for line in sites_raw.splitlines() if line.strip()]
    output_schema, err_schema = _parse_json("Output schema", output_schema_raw)
    binding, err_binding = _parse_json("Binding", binding_raw)
    errors = [e for e in (err_schema, err_binding) if e]
    if not name.strip():
        errors.append("Spec name is required.")
    if not sites:
        errors.append("At least one site URL is required.")
    if errors:
        for e in errors:
            st.error(e)
    else:
        sid = data.create_scrape_spec(
            name.strip(),
            sites,
            output_schema,
            binding,
            description=description or None,
            fetch_adapter=fetch_adapter or "httpx",
            llm_model=llm_model or None,
        )
        st.success(f"Created scrape spec #{sid}.")
        st.rerun()
```

- [ ] **Step 9: Manual smoke (offline)** — the page renders against the mock backend without an API or DB:
```
BELLWEATHER_UI_SOURCE=mock uv run bellweather ui
```
  Open the **Scrape** page; the `demo-prices` fixture spec lists, **Preview (dry-run)** shows the extracted JSON + sample observations, authoring a new spec (sites + schema + binding) adds it and it re-lists, toggling Enabled and Delete round-trip in the session.

- [ ] **Step 10: `make check`** → green:
```
make check
```

- [ ] **Step 11: Commit** (`feat: scrape-specs UI page + web.data backends`).

## Acceptance criteria
- `source.SCRAPE_SPEC_COLUMNS == ["id", "name", "description", "fetch_adapter", "llm_model", "enabled"]`; the module docstring documents the six new functions.
- `mock.*` and `live.*` expose the same six functions with byte-identical signatures; `get_scrape_specs()` returns a frame whose columns equal `source.SCRAPE_SPEC_COLUMNS`; `get_scrape_spec(name)` returns the full nested spec (`sites` list, `output_schema`/`binding` dicts); `preview_scrape_spec(name, url=None)` returns `{extracted, symbols, sample, tags}`.
- `live.*` issues the correct verb+path per the T39 API surface (GET list/`{name}`, POST create, PATCH update, DELETE, POST `{name}/preview`); the preview body posts `{"url": url}` unwrapped; `live` reads the base URL via `UISettings` at call time, so the UI needs no DB/GCS secrets.
- `mock.*` round-trips create → get → update → delete in-memory (including nested `output_schema`/`binding` edits); `get_scrape_spec` returns `None` for an unknown name.
- `6_Scrape.py` imports **only** from `bellweather.web.data`; no screen reads `mock`/`live` directly, and no other page changes.
- Tests use `pytest-httpserver` (live) + in-memory (mock); **no DB, no network**. `make check` green.
