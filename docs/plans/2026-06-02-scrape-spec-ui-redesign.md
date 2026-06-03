# Scrape-Spec UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the create-only Scrape specs page into a master/detail page that can fully **edit** a spec and **preview any of its sites**, with a real fetch-adapter dropdown and comprehensive offline mock data.

**Architecture:** A single-file rewrite of `web/pages/6_Scrape.py` (selector + `st.tabs(["Edit","Preview"])`), backed by the existing `web.data` scrape seam plus one new seam helper (`get_fetch_adapter_choices`) and one new read-only API endpoint (`GET /api/fetch-adapters`). All form logic stays in the pure, Streamlit-free `_scrape_form.py` so it is unit-tested; the page itself is covered by import/compile + manual smoke. Scheduling is untouched (source-agnostic, on the Schedules page).

**Tech Stack:** Python 3.12, Streamlit, FastAPI, pandas, pytest + pytest-httpserver. Spec: `docs/specs/2026-06-02-scrape-spec-ui-redesign-design.md`.

---

### Task 1: `GET /api/fetch-adapters` endpoint

**Files:**
- Modify: `src/bellweather/api.py:11` (import) and add an endpoint near the other `/api` reads.
- Test: covered indirectly by the live seam test in Task 2 (the endpoint is a 2-line passthrough over the already-tested `known_fetchers()`); no dedicated API test needed.

- [ ] **Step 1: Widen the fetch import**

`src/bellweather/api.py:11` — change:
```python
from bellweather.fetch import get_fetcher
```
to:
```python
from bellweather.fetch import get_fetcher, known_fetchers
```
(`bellweather.fetch.httpx_fetch` is already imported at line 10, so the registry has `"httpx"`.)

- [ ] **Step 2: Add the endpoint**

Add after `api_config` (`src/bellweather/api.py`, near line 410):
```python
@api_router.get("/fetch-adapters")
def api_fetch_adapters():
    return {"adapters": sorted(known_fetchers())}
```

- [ ] **Step 3: Smoke-check it imports and lists httpx**

Run: `uv run python -c "from bellweather.api import app; import bellweather.fetch as f; print(sorted(f.known_fetchers()))"`
Expected: `['httpx']`

- [ ] **Step 4: Commit**
```bash
git add src/bellweather/api.py
git commit -m "feat: add GET /api/fetch-adapters to enumerate registered fetch adapters"
```

---

### Task 2: `get_fetch_adapter_choices()` seam helper

**Files:**
- Modify: `src/bellweather/web/data/source.py` (contract docstring), `mock.py`, `live.py`, `__init__.py`.
- Test: `tests/test_web_scrape.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_scrape.py` (mock test near the other mock tests; live test inside the `_api` fixture's coverage):
```python
def test_mock_fetch_adapter_choices():
    assert mock.get_fetch_adapter_choices() == ["httpx"]


def test_live_fetch_adapter_choices(_api):
    assert live.get_fetch_adapter_choices() == ["httpx"]
```
And register the live route in the `_api` fixture (add alongside the other `expect_request` lines):
```python
    httpserver.expect_request("/api/fetch-adapters", method="GET").respond_with_json(
        {"adapters": ["httpx"]}
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_scrape.py -k fetch_adapter_choices -v`
Expected: FAIL — `AttributeError: module 'bellweather.web.data.mock' has no attribute 'get_fetch_adapter_choices'`

- [ ] **Step 3: Implement in mock.py**

Add to `src/bellweather/web/data/mock.py` (near the scrape section):
```python
# Registered fetch adapters offered in the Edit form's dropdown. The live
# backend reads these from GET /api/fetch-adapters; offline we mirror the one
# adapter the registry ships with.
_FETCH_ADAPTERS = ["httpx"]


def get_fetch_adapter_choices() -> list[str]:
    return list(_FETCH_ADAPTERS)
```

- [ ] **Step 4: Implement in live.py**

Add to `src/bellweather/web/data/live.py` (near the scrape functions):
```python
def get_fetch_adapter_choices() -> list[str]:
    return _get("/api/fetch-adapters")["adapters"]
```

- [ ] **Step 5: Re-export from the seam**

In `src/bellweather/web/data/__init__.py`, add after `preview_scrape_spec = _b.preview_scrape_spec`:
```python
get_fetch_adapter_choices = _b.get_fetch_adapter_choices
```
and add `"get_fetch_adapter_choices",` to `__all__`.

- [ ] **Step 6: Document the contract**

In `src/bellweather/web/data/source.py`, add to the contract docstring block (after the `preview_scrape_spec` line, ~line 45):
```
    get_fetch_adapter_choices()        -> list[str]  # registered fetch adapters (Edit dropdown)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_scrape.py -k fetch_adapter_choices -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**
```bash
git add src/bellweather/web/data/ tests/test_web_scrape.py
git commit -m "feat: add get_fetch_adapter_choices seam helper (mock + live)"
```

---

### Task 3: Mock preview varies by URL + comprehensive fixture specs

**Files:**
- Modify: `src/bellweather/web/data/mock.py`.
- Test: `tests/test_web_scrape.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_scrape.py`:
```python
def test_mock_preview_varies_by_url():
    name = mock.get_scrape_specs().iloc[0]["name"]
    a = mock.preview_scrape_spec(name, url="https://example.com/products/a")
    b = mock.preview_scrape_spec(name, url="https://example.com/products/b")
    assert a["symbols"] != b["symbols"]
    assert a["sample"][0]["value"] != b["sample"][0]["value"]
    # deterministic: same url → same result
    assert mock.preview_scrape_spec(name, url="https://example.com/products/a") == a


def test_mock_preview_url_none_uses_first_site():
    name = mock.get_scrape_specs().iloc[0]["name"]
    first_site = mock.get_scrape_spec(name)["sites"][0]
    assert mock.preview_scrape_spec(name) == mock.preview_scrape_spec(name, url=first_site)


def test_mock_has_several_fixture_specs():
    # comprehensive offline data: the selector should have plenty to browse
    assert len(mock.get_scrape_specs()) >= 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_scrape.py -k "preview_varies or url_none or several_fixture" -v`
Expected: FAIL (current preview ignores url → symbols equal; only 1 fixture spec)

- [ ] **Step 3: Add `hashlib` import**

In `src/bellweather/web/data/mock.py` top imports, add:
```python
import hashlib
```

- [ ] **Step 4: Expand the fixture specs**

Replace the `_SCRAPE_SPECS_STATE = [...]` block and the `_NEXT_SCRAPE_ID` line in `src/bellweather/web/data/mock.py` with (keep `demo-prices` first so existing `iloc[0]` tests still target it):
```python
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
    },
    {
        "id": 2,
        "name": "fed-speeches",
        "description": "Hawkish/dovish tone of FOMC member remarks.",
        "sites": [
            "https://www.federalreserve.gov/newsevents/speeches.htm",
            "https://www.federalreserve.gov/newsevents/testimony.htm",
        ],
        "output_schema": {
            "type": "object",
            "properties": {
                "speaker": {"type": "string"},
                "tone": {"type": "number"},
                "topic": {"type": "string"},
            },
        },
        "binding": {
            "symbol_key": "scrape:fed-tone:{speaker}",
            "symbol_kind": "sentiment",
            "value": "$.tone",
            "ts": "fetched_at",
            "unit": "score",
            "tags": ["speaker", "topic"],
        },
        "fetch_adapter": "httpx",
        "llm_model": "claude-haiku-4-5-20251001",
        "enabled": True,
    },
    {
        "id": 3,
        "name": "weather-alerts",
        "description": "Active NWS severe-weather alert counts by region.",
        "sites": [
            "https://www.weather.gov/alerts/west",
            "https://www.weather.gov/alerts/central",
            "https://www.weather.gov/alerts/east",
        ],
        "output_schema": {
            "type": "object",
            "properties": {
                "region": {"type": "string"},
                "active_alerts": {"type": "number"},
            },
        },
        "binding": {
            "symbol_key": "scrape:wx-alerts:{region}",
            "symbol_kind": "count",
            "value": "$.active_alerts",
            "ts": "fetched_at",
            "unit": "alerts",
            "tags": ["region"],
        },
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": True,
    },
    {
        "id": 4,
        "name": "crypto-funding",
        "description": "Perp funding rates (disabled until rate-limit cleared).",
        "sites": ["https://example-exchange.test/funding/btc-perp"],
        "output_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "funding_rate": {"type": "number"},
            },
        },
        "binding": {
            "symbol_key": "scrape:funding:{symbol}",
            "symbol_kind": "rate",
            "value": "$.funding_rate",
            "ts": "fetched_at",
            "unit": "bps",
            "tags": ["symbol"],
        },
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": False,
    },
    {
        "id": 5,
        "name": "job-postings",
        "description": "Open-req counts on a few careers pages.",
        "sites": [
            "https://example-co.test/careers",
            "https://another-co.test/jobs",
        ],
        "output_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string"},
                "open_roles": {"type": "number"},
            },
        },
        "binding": {
            "symbol_key": "scrape:hiring:{company}",
            "symbol_kind": "count",
            "value": "$.open_roles",
            "ts": "fetched_at",
            "unit": "roles",
            "tags": ["company"],
        },
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": True,
    },
]
_NEXT_SCRAPE_ID = {"spec": 6}
```

- [ ] **Step 5: Rewrite `preview_scrape_spec` to honor `url`**

Replace the `preview_scrape_spec` function in `src/bellweather/web/data/mock.py` with:
```python
def preview_scrape_spec(name, url=None) -> dict:
    # Deterministic dry-run (commits nothing) that mirrors the live API's
    # ScrapePreviewResult AND varies by the chosen site, so the Preview tab's
    # per-site selector visibly does something offline. url=None falls back to
    # the spec's first site, matching the API (_scrape_preview uses sites[0]).
    spec = get_scrape_spec(name)
    sites = (spec or {}).get("sites") or []
    site = url or (sites[0] if sites else "https://example.com/")
    seed = int(hashlib.sha1(site.encode()).hexdigest()[:6], 16)
    value = round(5 + (seed % 1000) / 100.0, 2)  # stable 5.00–14.99 per url
    slug = site.rstrip("/").rsplit("/", 1)[-1] or "root"
    symbol = f"scrape:prices:{slug}"
    return {
        "extracted": {"price": value, "title": slug, "source_url": site},
        "symbols": [symbol],
        "sample": [
            {"symbol_key": symbol, "ts": _now_hour().isoformat(), "value": value}
        ],
        "tags": [{"tag_type": "title", "raw_value": slug}],
    }
```

- [ ] **Step 6: Run the new + existing scrape tests**

Run: `uv run pytest tests/test_web_scrape.py -v`
Expected: PASS (all, including the unchanged `test_mock_preview_scrape_spec_shape`)

- [ ] **Step 7: Commit**
```bash
git add src/bellweather/web/data/mock.py tests/test_web_scrape.py
git commit -m "feat: mock preview varies by url + richer fixture scrape specs"
```

---

### Task 4: `build_spec_payload` pure form helper

**Files:**
- Modify: `src/bellweather/web/pages/_scrape_form.py`.
- Test: `tests/test_web_scrape_form.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_scrape_form.py`:
```python
_OK_SCHEMA = '{"type": "object", "properties": {"price": {"type": "number"}}}'
_OK_BINDING = '{"symbol_key": "s:{x}", "symbol_kind": "k", "value": "$.price", "ts": "fetched_at"}'


def _kw(**over):
    base = dict(
        name="my-spec",
        description="",
        sites_raw="https://a\n  \nhttps://b\n",
        output_schema_raw=_OK_SCHEMA,
        binding_raw=_OK_BINDING,
        fetch_adapter="httpx",
        llm_model="",
    )
    base.update(over)
    return base


def test_build_payload_happy_path():
    payload, errors = form.build_spec_payload(**_kw())
    assert errors == []
    assert payload["name"] == "my-spec"
    assert payload["sites"] == ["https://a", "https://b"]  # blanks stripped
    assert payload["description"] is None  # blank → None
    assert payload["llm_model"] is None
    assert payload["output_schema"] == {"type": "object", "properties": {"price": {"type": "number"}}}


def test_build_payload_edit_path_skips_name_check():
    # require_name=False: an empty name is fine because the selector owns it
    payload, errors = form.build_spec_payload(**_kw(name="", require_name=False))
    assert errors == []


def test_build_payload_requires_name_on_create():
    _, errors = form.build_spec_payload(**_kw(name="bad/name"))
    assert any("Spec name" in e for e in errors)


def test_build_payload_requires_sites():
    _, errors = form.build_spec_payload(**_kw(sites_raw="   \n  "))
    assert any("site" in e.lower() for e in errors)


def test_build_payload_rejects_non_object_schema():
    _, errors = form.build_spec_payload(**_kw(output_schema_raw="[1, 2]"))
    assert any("Output schema" in e for e in errors)


def test_build_payload_rejects_invalid_json_binding():
    _, errors = form.build_spec_payload(**_kw(binding_raw="{nope}"))
    assert any("Binding" in e for e in errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_web_scrape_form.py -k build_payload -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'build_spec_payload'`

- [ ] **Step 3: Implement the helper**

Add to `src/bellweather/web/pages/_scrape_form.py` (after the existing validators):
```python
def build_spec_payload(
    *,
    name: str,
    description: str,
    sites_raw: str,
    output_schema_raw: str,
    binding_raw: str,
    fetch_adapter: str,
    llm_model: str,
    require_name: bool = True,
) -> tuple[dict | None, list[str]]:
    """Parse + validate the unified create/edit form; return (payload, errors).

    Pure and Streamlit-free so the page's new-vs-existing branch stays testable.
    ``require_name=False`` on the edit path, where the name comes from the
    selector and is immutable. Blank ``description``/``llm_model`` collapse to
    ``None``; blank ``fetch_adapter`` defaults to ``"httpx"``.
    """
    errors: list[str] = []
    sites = [line.strip() for line in sites_raw.splitlines() if line.strip()]

    output_schema, err_schema = parse_json("Output schema", output_schema_raw)
    if err_schema:
        errors.append(err_schema)
    else:
        err = validate_json_object("Output schema", output_schema)
        if err:
            errors.append(err)

    binding, err_binding = parse_json("Binding", binding_raw)
    if err_binding:
        errors.append(err_binding)
    else:
        err = validate_json_object("Binding", binding)
        if err:
            errors.append(err)

    if require_name:
        err = validate_spec_name(name)
        if err:
            errors.append(err)

    if not sites:
        errors.append("At least one site URL is required.")

    if errors:
        return None, errors

    return {
        "name": name.strip(),
        "description": description.strip() or None,
        "sites": sites,
        "output_schema": output_schema,
        "binding": binding,
        "fetch_adapter": fetch_adapter or "httpx",
        "llm_model": llm_model.strip() or None,
    }, []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_web_scrape_form.py -v`
Expected: PASS (all, including the pre-existing validator tests)

- [ ] **Step 5: Commit**
```bash
git add src/bellweather/web/pages/_scrape_form.py tests/test_web_scrape_form.py
git commit -m "feat: add build_spec_payload pure helper for the unified scrape form"
```

---

### Task 5: Rewrite `6_Scrape.py` as master/detail

**Files:**
- Modify (full rewrite): `src/bellweather/web/pages/6_Scrape.py`.
- Test: no unit test (Streamlit page needs a runtime); covered by `py_compile` + ruff + the helper/seam tests + manual smoke.

- [ ] **Step 1: Rewrite the page**

Replace the entire contents of `src/bellweather/web/pages/6_Scrape.py` with:
```python
"""Scrape specs — master/detail control plane.

Select a spec (or "➕ New spec…"), edit it in place, and preview any of its sites
(dry-run; commits nothing). Reads/writes only through bellweather.web.data (mock
or live). Scheduling is source-agnostic and lives on the Schedules page — bind
template "scrape" with params {"spec": <name>} there.
"""

import json

import streamlit as st

from bellweather.web import data
from bellweather.web.pages import _scrape_form as form

NEW = "➕ New spec…"
EXAMPLE_SCHEMA = (
    '{\n  "type": "object",\n  "properties": {\n'
    '    "title": {"type": "string"},\n    "price": {"type": "number"}\n  }\n}'
)
EXAMPLE_BINDING = (
    '{\n  "symbol_key": "scrape:demo:{title}",\n  "symbol_kind": "scraped-metric",\n'
    '  "value": "$.price",\n  "ts": "fetched_at",\n  "unit": "usd",\n  "tags": []\n}'
)

st.title("Scrape specs")
st.caption(
    "Declare {sites, output schema, binding} once; edit in place and preview per-site. "
    "Schedule from the Schedules page with the 'scrape' template."
)

specs = data.get_scrape_specs()
names = list(specs["name"]) if not specs.empty else []
choice = st.selectbox("Spec", [NEW, *names])
is_new = choice == NEW
spec = None if is_new else data.get_scrape_spec(choice)
# The selected spec's name, used by the Preview/Delete controls below.
selected_name = "" if is_new else spec["name"]

st.caption('→ Schedule this spec on the **Schedules** page (template "scrape").')

edit_tab, preview_tab = st.tabs(["Edit", "Preview"])

with edit_tab:
    adapters = data.get_fetch_adapter_choices()
    if is_new:
        defaults = {
            "description": "",
            "sites": "https://example.com/",
            "schema": EXAMPLE_SCHEMA,
            "binding": EXAMPLE_BINDING,
            "model": "",
            "enabled": True,
        }
        adapter_options, adapter_idx = adapters, 0
    else:
        defaults = {
            "description": spec.get("description") or "",
            "sites": "\n".join(spec.get("sites") or []),
            "schema": json.dumps(spec["output_schema"], indent=2),
            "binding": json.dumps(spec["binding"], indent=2),
            "model": spec.get("llm_model") or "",
            "enabled": bool(spec["enabled"]),
        }
        # Keep the spec's current adapter selectable even if the registry no
        # longer lists it, so the selectbox never errors on a stale value.
        adapter_options = sorted(set(adapters) | {spec["fetch_adapter"]})
        adapter_idx = adapter_options.index(spec["fetch_adapter"])

    with st.form("spec_form"):
        if is_new:
            name = st.text_input("Spec name", value="my-spec")
        else:
            st.text_input("Spec name", value=selected_name, disabled=True)
            name = selected_name
        description = st.text_input("Description", value=defaults["description"])
        sites_raw = st.text_area("Sites (one URL per line)", value=defaults["sites"])
        output_schema_raw = st.text_area("Output schema (JSON Schema)", value=defaults["schema"])
        binding_raw = st.text_area("Binding (JSON)", value=defaults["binding"])
        c1, c2 = st.columns(2)
        fetch_adapter = c1.selectbox("Fetch adapter", adapter_options, index=adapter_idx)
        llm_model = c2.text_input("LLM model (blank = default)", value=defaults["model"])
        enabled = st.toggle("Enabled", value=defaults["enabled"])
        submitted = st.form_submit_button("Create spec" if is_new else "Save changes")

    if submitted:
        payload, errors = form.build_spec_payload(
            name=name,
            description=description,
            sites_raw=sites_raw,
            output_schema_raw=output_schema_raw,
            binding_raw=binding_raw,
            fetch_adapter=fetch_adapter,
            llm_model=llm_model,
            require_name=is_new,
        )
        if errors:
            for e in errors:
                st.error(e)
        elif is_new:
            sid = data.create_scrape_spec(
                payload["name"],
                payload["sites"],
                payload["output_schema"],
                payload["binding"],
                description=payload["description"],
                fetch_adapter=payload["fetch_adapter"],
                llm_model=payload["llm_model"],
            )
            # create defaults to enabled=True; honor an unchecked toggle via PATCH.
            if not enabled:
                data.update_scrape_spec(payload["name"], enabled=False)
            st.success(f"Created scrape spec #{sid}.")
            st.rerun()
        else:
            data.update_scrape_spec(
                selected_name,
                description=payload["description"],
                sites=payload["sites"],
                output_schema=payload["output_schema"],
                binding=payload["binding"],
                fetch_adapter=payload["fetch_adapter"],
                llm_model=payload["llm_model"],
                enabled=enabled,
            )
            st.success("Saved changes.")
            st.rerun()

    if not is_new and st.button("Delete spec"):
        data.delete_scrape_spec(selected_name)
        st.rerun()

with preview_tab:
    if is_new:
        st.info("Create the spec first, then preview its sites here.")
    else:
        sites = spec.get("sites") or []
        if not sites:
            st.info("This spec has no sites to preview.")
        else:
            url = st.selectbox("Preview which site?", sites)
            if st.button("Run preview (dry-run)"):
                try:
                    with st.spinner("Fetching + extracting (commits nothing)…"):
                        out = data.preview_scrape_spec(selected_name, url=url)
                except Exception as exc:  # noqa: BLE001 — surface any backend error to the operator
                    st.error(f"Preview failed: {exc}")
                else:
                    st.success(
                        f"Would emit {len(out['sample'])} sample point(s) across "
                        f"{len(out['symbols'])} symbol(s) and {len(out['tags'])} tag(s)."
                    )
                    st.markdown("**Extracted JSON**")
                    st.json(out["extracted"])
                    st.markdown("**Sample observations**")
                    st.dataframe(out["sample"], hide_index=True)
                    st.markdown("**Tags**")
                    st.dataframe(out["tags"], hide_index=True)
```

- [ ] **Step 2: Byte-compile the page**

Run: `uv run python -m py_compile src/bellweather/web/pages/6_Scrape.py`
Expected: no output (exit 0)

- [ ] **Step 3: Lint + format the new code**

Run: `uv run ruff check src/bellweather/web/pages/6_Scrape.py && uv run ruff format src/bellweather/web/pages/6_Scrape.py`
Expected: `All checks passed!` and `1 file reformatted` or `left unchanged`

- [ ] **Step 4: Commit**
```bash
git add src/bellweather/web/pages/6_Scrape.py
git commit -m "feat: master/detail Scrape page with unified edit form + per-site preview"
```

---

### Task 6: Full gate + manual smoke

- [ ] **Step 1: Run the full CI gate**

Run: `make check`
Expected: ruff check passes, ruff format --check passes, **all** pytest tests pass.

- [ ] **Step 2: Manual smoke (mock backend, the default)**

Run: `make ui` and in the browser:
- Selector shows `➕ New spec…` + the 5 fixture specs.
- Select an existing spec → Edit tab is pre-filled; Name is disabled; change Description → Save changes → success.
- Select `➕ New spec…` → fill → Create spec → success; new spec appears in the selector.
- Preview tab → pick different sites → Run preview → extracted/sample/tags render and the sample value changes per site.
- Confirm there is **no** interval/scheduling/run-health UI, only the Schedules-page pointer.

- [ ] **Step 3: Final commit if formatting changed anything**
```bash
git add -A
git commit -m "chore: make check green for scrape UI redesign" || echo "nothing to commit"
```

---

## Self-review notes (coverage vs spec)

- §4 page (selector + Edit/Preview tabs, unified form, immutable name, post-create enable patch, per-site preview with tags, spinner/try-except, Schedules pointer) → Task 5.
- §5 seam (`get_fetch_adapter_choices`, mock preview honors url) → Tasks 2 + 3.
- §6 API (`GET /api/fetch-adapters`) → Task 1.
- §7 pure helper (`build_spec_payload`) → Task 4.
- §8 testing (form unit tests, seam contract tests, no-DB/no-network, `make check`) → Tasks 2–4, 6.
- "Comprehensive mock data" (user requirement) → Task 3 (5 diverse fixture specs).
- Non-goals (no scheduling UI, no rename, no session_state, no server-side adapter validation) → respected in Task 5 / out of scope.
