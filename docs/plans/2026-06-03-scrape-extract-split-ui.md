# Scrape/Extract Split UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the bundled scrape-spec UI into two pages backed by two mock entities — **scrape sources** (sites/adapter → raw captures) and **extraction specs** (schema/binding/model, many-to-many onto sources) — per `docs/specs/2026-06-03-scrape-extract-split-design.md`.

**Architecture:** UI-first on the `ui/scrape-spec-redesign` branch. The `web.data` seam swaps the six `*_scrape_spec` functions for source/extractor/capture/preview functions (mock fully functional; live written against the **planned** §7 endpoints and tested with `pytest-httpserver` as an executable contract for the T43+ backend tickets). `api.py` is untouched. Captures in mock are **derived deterministically** from (source, url) — no stored capture state.

**Tech Stack:** Python 3.12, Streamlit, pandas, pytest + pytest-httpserver.

---

### Task 1: Contract constants + mock entities (TDD)

**Files:**
- Modify: `src/bellweather/web/data/source.py` (replace scrape-spec contract block + `SCRAPE_SPEC_COLUMNS`)
- Modify: `src/bellweather/web/data/mock.py` (replace `_SCRAPE_SPECS_STATE`/six functions with sources/extractors/links/captures)
- Modify: `src/bellweather/web/data/__init__.py` (re-export swap)
- Test: `tests/test_web_scrape.py` (full rewrite, mock half)

- [ ] **Step 1: Rewrite `tests/test_web_scrape.py` — mock tests first** (live half comes in Task 2; keep the file header + fetch-adapter mock test). Key tests:

```python
def test_mock_sources_frame_shape():
    df = mock.get_scrape_sources()
    assert list(df.columns) == contract.SCRAPE_SOURCE_COLUMNS
    assert len(df) >= 4  # comprehensive fixtures

def test_mock_source_full_includes_sites_and_parsed_by():
    src = mock.get_scrape_source("demo-prices")
    assert src["sites"] and isinstance(src["sites"], list)
    assert src["parsed_by"] == ["page-sentiment", "product-prices"]  # M2M visible

def test_mock_source_crud_roundtrip():
    sid = mock.create_scrape_source("rt-src", ["https://x"], description="rt")
    assert sid in mock.get_scrape_sources()["id"].tolist()
    mock.update_scrape_source("rt-src", enabled=False, sites=["https://y"])
    src = mock.get_scrape_source("rt-src")
    assert src["enabled"] is False and src["sites"] == ["https://y"]
    mock.delete_scrape_source("rt-src")
    assert mock.get_scrape_source("rt-src") is None

def test_mock_extractors_frame_shape():
    df = mock.get_extraction_specs()
    assert list(df.columns) == contract.EXTRACTION_SPEC_COLUMNS
    assert "page-sentiment" in df["name"].tolist()

def test_mock_extractor_full_includes_links():
    spec = mock.get_extraction_spec("page-sentiment")
    assert spec["sources"] == ["demo-prices", "fed-speeches"]  # one parser, many sources
    assert isinstance(spec["output_schema"], dict) and isinstance(spec["binding"], dict)

def test_mock_extractor_sources_update_replaces_links():
    mock.create_extraction_spec("rt-ex", {"type": "object"}, {"symbol_key": "s:{x}", "tags": []},
                                sources=["demo-prices"])
    assert "rt-ex" in mock.get_scrape_source("demo-prices")["parsed_by"]
    mock.update_extraction_spec("rt-ex", sources=["job-postings"])
    assert "rt-ex" not in mock.get_scrape_source("demo-prices")["parsed_by"]
    assert "rt-ex" in mock.get_scrape_source("job-postings")["parsed_by"]
    mock.delete_extraction_spec("rt-ex")
    assert "rt-ex" not in mock.get_scrape_source("job-postings")["parsed_by"]

def test_mock_captures_listing_and_content():
    df = mock.get_captures("demo-prices")
    assert list(df.columns) == contract.CAPTURE_COLUMNS
    assert len(df) == len(mock.get_scrape_source("demo-prices")["sites"])
    cap = mock.get_capture("demo-prices", df.iloc[0]["url"])
    assert cap["content"] and cap["size_bytes"] == len(cap["content"])
    assert mock.get_capture("demo-prices", "https://not-a-site") is None

def test_mock_fetch_capture_now_matches_get():
    url = mock.get_scrape_source("demo-prices")["sites"][0]
    assert mock.fetch_capture_now("demo-prices", url) == mock.get_capture("demo-prices", url)

def test_mock_preview_extraction_varies_by_url_and_extractor():
    s = mock.get_scrape_source("demo-prices")["sites"]
    a = mock.preview_extraction("product-prices", "demo-prices", s[0])
    b = mock.preview_extraction("product-prices", "demo-prices", s[1])
    assert set(a) == {"extracted", "symbols", "sample", "tags"}
    assert a["symbols"] != b["symbols"] and a["sample"][0]["value"] != b["sample"][0]["value"]
    c = mock.preview_extraction("page-sentiment", "demo-prices", s[0])
    assert c["symbols"] != a["symbols"]  # different extractor → different symbol space
    assert a == mock.preview_extraction("product-prices", "demo-prices", s[0])  # deterministic
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_web_scrape.py -v` → AttributeErrors (functions missing).

- [ ] **Step 3: `source.py`** — replace the scrape-spec docstring block with the §5 contract text, and replace `SCRAPE_SPEC_COLUMNS` with:

```python
SCRAPE_SOURCE_COLUMNS = ["id", "name", "description", "fetch_adapter", "enabled"]
EXTRACTION_SPEC_COLUMNS = ["id", "name", "description", "llm_model"]
CAPTURE_COLUMNS = ["url", "captured_at", "content_type", "size_bytes"]
```

- [ ] **Step 4: `mock.py`** — delete `_SCRAPE_SPECS_STATE`, `_NEXT_SCRAPE_ID`, `_scrape_specs_frame`, and the six `*_scrape_spec` functions; keep `_FETCH_ADAPTERS`/`get_fetch_adapter_choices`. Add (full fixture data per spec §5 — sources `demo-prices/fed-speeches/weather-alerts(disabled crypto-funding)/job-postings`, extractors `product-prices/fed-tone/alert-counts/funding-rate/job-counts/page-sentiment`, links incl. `page-sentiment → demo-prices + fed-speeches`):

```python
_SOURCES_STATE: list[dict] = [ ... five fixtures ... ]
_EXTRACTION_SPECS_STATE: list[dict] = [ ... six fixtures ... ]
_LINKS_STATE: list[tuple[str, str]] = [ ... seven (source, extractor) pairs ... ]
_NEXT_SPLIT_ID = {"source": 6, "extractor": 7}

def _url_value(url): seed = int(hashlib.sha1(url.encode()).hexdigest()[:6], 16); return round(5 + (seed % 1000) / 100.0, 2)
def _url_slug(url): return url.rstrip("/").rsplit("/", 1)[-1] or "root"

_CAPTURE_TEMPLATES = { per-source (content_type, format-template) snippets; generic html fallback }
def _capture(source_name, url) -> dict   # url/captured_at/content_type/size_bytes/content, fully derived

get_scrape_sources / get_scrape_source (adds sites copy + parsed_by from _LINKS_STATE)
create_scrape_source / update_scrape_source (allowed: description|sites|fetch_adapter|enabled)
delete_scrape_source (drops its links)
get_extraction_specs / get_extraction_spec (adds dict copies + sources from _LINKS_STATE)
create_extraction_spec(..., sources=()) / update_extraction_spec (fields + sources replaces links)
delete_extraction_spec (drops its links)
get_captures / get_capture / fetch_capture_now (all via _capture; unknown source/url → empty/None)
preview_extraction (deterministic: numeric props ← _url_value, string props ← _url_slug,
                    symbol from binding symbol_key prefix, tags from binding["tags"])
```

- [ ] **Step 5: `__init__.py`** — remove the six `*_scrape_spec` re-exports/`__all__` entries; add the twelve new functions (keep `get_fetch_adapter_choices`).

- [ ] **Step 6: Run mock tests** — `uv run pytest tests/test_web_scrape.py -v` → mock half PASS.

- [ ] **Step 7: Commit** — `feat: mock scrape-source + extraction-spec entities with captures (M2M)`

---

### Task 2: Live seam against the planned API (TDD)

**Files:**
- Modify: `src/bellweather/web/data/live.py`
- Test: `tests/test_web_scrape.py` (live half)

- [ ] **Step 1: Add the live tests + `_api` httpserver fixture** registering **every §7 route** (list/get/create/patch/delete for both entities; captures list; capture-with-content `GET …/capture` with `url` query param; `POST …/fetch`; `POST …/preview` with body `{"source": …, "url": …}`; `/api/fetch-adapters`). One test per function asserting shape/verb (mirror the deleted live tests' style).

- [ ] **Step 2: Verify failure** → AttributeErrors.

- [ ] **Step 3: Implement in `live.py`** — delete the six old functions; add the twelve thin calls (paths per spec §7; `_LONG_TIMEOUT` on `fetch_capture_now` + `preview_extraction`; `_frame(...)` with the new contract constants for the three frame-returning functions).

- [ ] **Step 4: Run** — full `tests/test_web_scrape.py` PASS.

- [ ] **Step 5: Commit** — `feat: live seam for scrape-sources/extraction-specs (executable contract for T44)`

---

### Task 3: Form payload builders (TDD)

**Files:**
- Modify: `src/bellweather/web/pages/_scrape_form.py` (replace `build_spec_payload`)
- Test: `tests/test_web_scrape_form.py` (replace `build_spec_payload` tests)

- [ ] **Step 1: Replace the `build_spec_payload` test block** with suites for the two new builders: happy path (blank optionals → None, blank-line stripping), `require_name=False` edit path, bad name, no sites (source), non-object schema / invalid JSON binding (extractor).

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement** `build_source_payload(*, name, description, sites_raw, fetch_adapter, require_name=True)` and `build_extraction_payload(*, name, description, output_schema_raw, binding_raw, llm_model, require_name=True)` (spec §6; same validator core; delete `build_spec_payload`).

- [ ] **Step 4: Run** — `tests/test_web_scrape_form.py` PASS. **Step 5: Commit** — `feat: source/extraction payload builders replace build_spec_payload`

---

### Task 4: Rewrite `6_Scrape.py` (sources: Edit · Captures)

**Files:** rewrite `src/bellweather/web/pages/6_Scrape.py` per spec §4.1 — selector + sentinel `➕ New source…`; Edit tab (name ro-on-edit, description, sites, adapter dropdown w/ stale-value union, enabled toggle, post-create `enabled=False` patch, read-only "Parsed by" caption, Delete); Captures tab (site selectbox → `get_capture` caption+`st.code`, `Fetch now (test)` w/ spinner+try/except, language `html`/`markdown` from content_type).

- [ ] **Step 1: Write the page.**  **Step 2:** `py_compile` + `ruff check/format`.  **Step 3: Commit** — `feat: Scrape page = sources + raw captures (fetch side only)`

---

### Task 5: New `7_Extract.py` (extractors: Edit · Test)

**Files:** create `src/bellweather/web/pages/7_Extract.py` per spec §4.2 — selector + sentinel `➕ New extractor…`; Edit tab (schema/binding/model + **`st.multiselect("Applies to sources", all_sources, default=linked)`**, create accepts `sources=`, update sends `sources=`, Delete); Test tab (linked-source selectbox → site selectbox → capture shown via `st.code` → `Run extraction` → success line + extracted `st.json` + sample/tags `st.dataframe`, spinner+try/except; info notes for new-extractor and no-linked-sources states).

- [ ] **Step 1: Write the page.**  **Step 2:** `py_compile` + `ruff check/format`.  **Step 3: Commit** — `feat: Extract page = extraction specs + test-against-capture (parse side)`

---

### Task 6: AppTest smokes + gate

**Files:** rewrite `tests/test_web_scrape_page.py` (still `pytest.importorskip("streamlit")`-guarded):

- [ ] **Step 1: Tests** — Scrape page: renders, selector has sentinel + fixtures, name disabled on edit, Captures tab content renders (`at.code`), blank-sites create → error. Extract page: renders, selector has fixtures incl `page-sentiment`, multiselect present with current links as default, Test tab renders capture + runs extraction without exception.
- [ ] **Step 2:** `uv run --group ui pytest tests/test_web_scrape_page.py -v` → PASS.
- [ ] **Step 3:** `make check` → green.
- [ ] **Step 4: Commit** — `test: AppTest smokes for the split Scrape/Extract pages`
- [ ] **Step 5: Manual smoke** — `make ui`: both pages in nav; source edit/save; chips read-only; captures per site vary; extractor M2M edit moves chips on Scrape page; Test tab extraction varies by site. No PR.

## Self-review notes
- Spec §4.1/§4.2 → Tasks 4/5; §5 → Tasks 1/2; §6 → Task 3; §7 contract → Task 2 fixture; §10 → Tasks 1–3, 6. K6 cutover = deletions in Tasks 1–3. `api.py` untouched ✓. Names consistent: `get_scrape_source(s)`, `get_extraction_spec(s)`, `get_captures/get_capture/fetch_capture_now`, `preview_extraction`, `build_source_payload`/`build_extraction_payload`.
