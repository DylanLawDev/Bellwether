# Bellwether — Scrape/Extract Split (two entities, two pages)

| | |
|---|---|
| **Status** | Draft — approved in brainstorm, pending spec review |
| **Date** | 2026-06-03 |
| **Owner** | Dylan |
| **Scope** | Split the bundled scrape-spec concept into **two entities** — *scrape sources* (what to fetch) and *extraction specs* (how to parse) — related **many-to-many**, and reflect that split in the UI as **two pages**: `6_Scrape.py` (sources + raw captures) and a new `7_Extract.py` (extraction specs + test-against-capture). **Staged UI-first:** this spec's build is the two pages + `web.data` seam on **mock data** (current `ui/scrape-spec-redesign` branch); the backend (migration, API, collector, worker) is decomposed into **T43+ tickets** that land behind it. `live.py` is written against the *planned* endpoints now (tested via `pytest-httpserver`), locking the API contract those tickets must implement. |
| **Related** | `docs/specs/2026-06-02-scrape-spec-ui-redesign-design.md` (the master/detail page this supersedes), `docs/specs/2026-06-01-llm-scrape-engine-design.md` (the engine being split), `docs/specs/2026-05-31-ui-prototype-design.md` (the mock/live seam) |

---

## 1. Goal

The architecture already separates **scraping** (collector fetches raw pages → immutable bronze) from **parsing** (worker LLM-extracts from bronze), but the config and the UI bundle both halves into one `scrape_specs` row and one form. This work makes the separation literal:

- A **scrape source** declares *what to fetch*: `name`, `description`, `sites`, `fetch_adapter`, `enabled`. Its product is **raw captures** (HTML/markdown/JSON text).
- An **extraction spec** declares *how to parse*: `name`, `description`, `output_schema`, `binding`, `llm_model`. It is **reusable** and applies to sources via a **many-to-many** link — one page's captures can feed several extractors (prices *and* sentiment); one extractor can apply across many site sets.
- A source with no extractors still captures bronze (*scrape now, parse later*) — which is the future entry point for the **extraction playground / fine-tuning** vision (explicitly deferred; see §9).

## 2. Non-goals

- **No playground in this build.** The Extract page's Test tab (run an extractor against an existing capture) is its future slot, but free-form paste-HTML/iterate/save-examples/fine-tune is later work.
- **No backend in this build.** Migration `0004`, the new API endpoints, collector/worker rewiring, and the `scrape` template's `spec`→`source` param rename are **T43+ tickets** (§8). Until they land, the two pages are fully functional on **mock** and live-mode scrape pages would 404 — acceptable per the staging decision.
- **No scheduling/run-health UI** on either page (unchanged from the previous spec) — only the Schedules-page pointer.
- **No rename** of an existing source/extractor (name immutable on edit, create-only — same rule as before).

## 3. Key decisions (from brainstorm)

| # | Decision | Rationale |
|---|---|---|
| K1 | **Two pages**: `6_Scrape.py` = sources (master/detail: Edit · Captures), new `7_Extract.py` = extraction specs (master/detail: Edit · Test). | Mirrors the pipeline stages one-to-one; playground later slots into Extract → Test. |
| K2 | **Two entities, many-to-many.** `scrape_sources` ⟷ `extraction_specs` via a junction. | User decision. One capture, many parsers; one parser, many sources. Worker consequence (each capture runs *every* linked extractor) lands in the T43+ tickets. |
| K3 | **Links are edited on the Extract page only** ("Applies to sources" multiselect); the Scrape page shows read-only "Parsed by" chips. | One writing side avoids two-way-sync confusion; a parser declaring where it applies matches the reuse mental model. |
| K4 | **Captures are first-class in the UI.** Scrape → Captures tab: pick a site, see the latest raw capture (content, captured-at, content-type, size) + a "Fetch now (test)" action. Extract → Test tab consumes a chosen capture — **no fetching during extraction tests**. | Makes "scraping produces raw content, full stop" visible, and the Test tab demonstrates parse-without-fetch — the architectural point of the split. |
| K5 | **UI-first on mock; `live.py` written against planned endpoints now.** Live functions are implemented and tested with `pytest-httpserver` against the §7 endpoint contract even though `api.py` doesn't serve those routes yet. | Keeps the seam's mock/live parity invariant, and turns the live tests into an executable contract for the T43+ API ticket. |
| K6 | **The unified spec page and its seam functions are cut over, not kept alongside.** `get_scrape_specs`/`get_scrape_spec`/`create/update/delete_scrape_spec`/`preview_scrape_spec` are **removed** from the seam (mock, live, `__init__`, contract, tests) and replaced by the source/extractor functions. The existing backend `/api/scrape-specs*` endpoints stay in `api.py` untouched (the T43+ tickets migrate them). | Two parallel models in the UI would be confusing. Nothing else imports the old functions (`6_Scrape.py` was their only consumer). `get_fetch_adapter_choices()` and `GET /api/fetch-adapters` are kept as-is. |
| K7 | **Stateless master/detail idiom carries over** from the previous spec: `st.selectbox` + sentinel (`➕ New source…` / `➕ New extractor…`), unified create/edit form, name immutable on edit, errors collected via pure helpers, `st.rerun()` after mutations, no `st.session_state`. | Established pattern, already validated by AppTest on this branch. |

## 4. The pages

### 4.1 `6_Scrape.py` — sources (what to fetch)

```
Scrape sources
  Source: [ ➕ New source… ▾ | demo-prices | fed-speeches | … ]
  → Schedule on the Schedules page (template "scrape", source = <name>).
  [ Edit ] [ Captures ]
  Edit:     Name (ro on edit) · Description · Sites (one/line) ·
            Fetch adapter (dropdown) · Enabled (toggle) ·
            "Parsed by:" read-only chips (linked extractor names, or "none — raw only")
            [Create source | Save changes] [Delete source]
  Captures: Site [ ▾ ] → caption "captured <ts> · <content_type> · <size> bytes"
            raw content in st.code (markdown/html/json) · [Fetch now (test)]
```

- Edit submit branches new-vs-existing exactly like the previous page: `create_scrape_source(...)` (+ follow-up `enabled=False` patch when toggled off) or `update_scrape_source(name, …)`. Links are **not** editable here (K3).
- Captures tab: `get_captures(source)` lists per-site capture metadata; selecting a site shows `get_capture(source, url)["content"]` in `st.code`. **Fetch now (test)** calls `fetch_capture_now(source, url)` inside `st.spinner` + try/except and re-renders the capture. New-source sentinel → info note ("create the source first").

### 4.2 `7_Extract.py` — extraction specs (how to parse)

```
Extraction specs
  Extractor: [ ➕ New extractor… ▾ | product-prices | page-sentiment | … ]
  [ Edit ] [ Test ]
  Edit:  Name (ro on edit) · Description · Output schema (JSON) · Binding (JSON) ·
         LLM model · "Applies to sources" (st.multiselect over source names)
         [Create extractor | Save changes] [Delete extractor]
  Test:  Source [ ▾ linked sources ] · Site [ ▾ that source's sites ] →
         raw capture shown read-only (st.code) · [Run extraction]
         → "Would emit N point(s) across M symbol(s) and K tag(s)."
         → Extracted JSON (st.json) · Sample observations (st.dataframe) · Tags (st.dataframe)
```

- "Applies to sources" is the **M2M edit surface** (K3): a `st.multiselect` defaulting to the extractor's current links; persisted via `update_extraction_spec(name, sources=[...])` on save (and accepted by `create_extraction_spec(..., sources=[...])`).
- Test tab runs `preview_extraction(extractor, source, url)` — extraction over the **existing capture**, no fetch (K4) — with the same spinner/try-except/error pattern, rendering all four result sections (incl. tags). New-extractor sentinel → info note. Extractor with no linked sources → info note ("attach a source on the Edit tab").

## 5. The `web.data` seam (mock + live, identical signatures)

Removed (K6): the six `*_scrape_spec` functions and `SCRAPE_SPEC_COLUMNS`.
Kept: `get_fetch_adapter_choices() -> list[str]`.
Added:

```
SCRAPE_SOURCE_COLUMNS   = ["id", "name", "description", "fetch_adapter", "enabled"]
EXTRACTION_SPEC_COLUMNS = ["id", "name", "description", "llm_model"]
CAPTURE_COLUMNS         = ["url", "captured_at", "content_type", "size_bytes"]

get_scrape_sources()                  -> DataFrame[SCRAPE_SOURCE_COLUMNS]
get_scrape_source(name)               -> dict | None   # + sites: list[str], parsed_by: list[str] (read-only)
create_scrape_source(name, sites, *, description=None,
                     fetch_adapter="httpx") -> int
update_scrape_source(name, **fields)  -> None           # description|sites|fetch_adapter|enabled
delete_scrape_source(name)            -> None            # also drops its links + captures

get_extraction_specs()                -> DataFrame[EXTRACTION_SPEC_COLUMNS]
get_extraction_spec(name)             -> dict | None    # + output_schema: dict, binding: dict, sources: list[str]
create_extraction_spec(name, output_schema, binding, *, description=None,
                       llm_model=None, sources=()) -> int
update_extraction_spec(name, **fields) -> None           # description|output_schema|binding|llm_model|sources
delete_extraction_spec(name)          -> None            # also drops its links

get_captures(source_name)             -> DataFrame[CAPTURE_COLUMNS]   # latest capture per site
get_capture(source_name, url)         -> dict | None    # CAPTURE_COLUMNS + content: str
fetch_capture_now(source_name, url)   -> dict            # re-fetch one site, return fresh capture (with content)
preview_extraction(extractor_name, source_name, url) -> dict  # {extracted, symbols, sample, tags}; commits nothing
```

**Mock specifics:**
- Module-level state: `_SOURCES_STATE`, `_EXTRACTION_SPECS_STATE`, `_LINKS_STATE` (list of `(source_name, extractor_name)` pairs), `_CAPTURES_STATE` (keyed `(source, url)`), with `_NEXT_*` counters — same idiom as today.
- **Comprehensive fixtures** derived from the current five specs, split into halves: sources `demo-prices`(2 sites) / `fed-speeches`(2) / `weather-alerts`(3) / `crypto-funding`(1, disabled) / `job-postings`(2); extractors `product-prices` / `fed-tone`(llm_model set) / `alert-counts` / `funding-rate` / `job-counts` **plus `page-sentiment`** linked to *two* sources (`demo-prices`, `fed-speeches`) so the M2M is visible in the fixtures (demo-prices shows two "parsed by" chips).
- **Fixture captures**: one small, realistic raw snippet per (source, site) — HTML for product/job pages, markdown for speeches, an HTML table for alerts, JSON text for funding — each with deterministic `captured_at` (derived from `_now_hour()`), `content_type`, `size_bytes = len(content)`.
- `fetch_capture_now` refreshes `captured_at` to `_now_hour()` and returns the capture (content unchanged — deterministic).
- `preview_extraction` is deterministic per `(extractor, url)`: numeric schema property ← url-hash value (same sha1 trick as the current mock preview); first string property ← url slug; `symbols`/`sample` built from the extractor's `binding["symbol_key"]` prefix; `tags` from `binding["tags"]`. Unknown names raise `KeyError`-free: return shapes only for known fixtures (page guards selections).

**Live specifics (K5):** thin httpx calls per §7, `_LONG_TIMEOUT` on `preview_extraction` and `fetch_capture_now` (both may fetch/LLM in-process server-side later).

## 6. Pure form helpers (`_scrape_form.py`)

`build_spec_payload` is **replaced** by two narrower builders (same validator core, same `(payload|None, errors)` shape):

```
build_source_payload(*, name, description, sites_raw, fetch_adapter,
                     require_name=True) -> (dict | None, list[str])
    # {name, description|None, sites: list[str], fetch_adapter}
build_extraction_payload(*, name, description, output_schema_raw, binding_raw,
                         llm_model, require_name=True) -> (dict | None, list[str])
    # {name, description|None, output_schema: dict, binding: dict, llm_model|None}
```

Rules carried over: path-safe name (create path), ≥1 site (source), JSON-object schema/binding (extractor), blank optionals → `None`.

## 7. Planned API contract (implemented by T43+, consumed by `live.py` now)

```
GET    /api/scrape-sources                         -> [SourceRow]            # id,name,description,fetch_adapter,enabled
GET    /api/scrape-sources/{name}                  -> SourceRow + sites + parsed_by
POST   /api/scrape-sources                         -> SourceRow              # body: name,sites,description?,fetch_adapter?
PATCH  /api/scrape-sources/{name}                  -> SourceRow
DELETE /api/scrape-sources/{name}                  -> {"status":"deleted"}
GET    /api/scrape-sources/{name}/captures         -> [CaptureRow]           # url,captured_at,content_type,size_bytes
GET    /api/scrape-sources/{name}/capture?url=…    -> CaptureRow + content
POST   /api/scrape-sources/{name}/fetch            -> CaptureRow + content   # body {"url": str}; fetches + (later) lands bronze
GET    /api/extraction-specs                       -> [ExtractionSpecRow]    # id,name,description,llm_model
GET    /api/extraction-specs/{name}                -> ExtractionSpecRow + output_schema + binding + sources
POST   /api/extraction-specs                       -> ExtractionSpecRow      # body incl sources: [str]
PATCH  /api/extraction-specs/{name}                -> ExtractionSpecRow      # sources list replaces links when present
DELETE /api/extraction-specs/{name}                -> {"status":"deleted"}
POST   /api/extraction-specs/{name}/preview        -> {extracted,symbols,sample,tags}  # body {"source": str, "url": str}; extracts from the stored capture, no fetch
GET    /api/fetch-adapters                         -> {"adapters":[str]}     # exists already
```

## 8. Backend decomposition (T43+ tickets — not in this build)

1. **T43 — migration 0004 + `scrape/sources.py`/`scrape/extraction_specs.py`**: tables `scrape_sources`, `extraction_specs`, junction `source_extraction_links`; migrate each existing `scrape_specs` row into one source + one extraction spec + one link; captures read model (latest bronze per (source, url)).
2. **T44 — control-plane API**: §7 endpoints (replacing `/api/scrape-specs*`), preview-from-capture (no fetch), fetch-now (fetch + return; optionally land bronze).
3. **T45 — collector + template**: `producers/scrape` reads a *source* (`params={"source": name}`); provenance carries `scrape_source`; template.toml param rename.
4. **T46 — worker**: `LlmScrapeExtractor` resolves *all* extraction specs linked to the capture's source and applies each (idempotent per spec); `ExtractionResult` merge semantics.
5. **T47 — live cutover check**: flip a staging UI to `BELLWEATHER_UI_SOURCE=live` against T44 and reconcile any contract drift with the seam tests.

## 9. Future: extraction playground (deferred)

The Extract → Test tab is the anchor. Later work adds: paste/upload arbitrary raw content (HTML/markdown), run any extractor against it, diff expected-vs-extracted, save `(raw content, expected JSON)` pairs as labeled examples, and export them as a fine-tuning dataset. Nothing in this build precludes it; `preview_extraction`'s capture-based signature gains a content-based sibling then.

## 10. Testing

- **Form helpers** (`tests/test_web_scrape_form.py`): rewrite `build_spec_payload` tests into `build_source_payload` + `build_extraction_payload` suites (happy path, edit path, name/sites/JSON failures).
- **Seam** (`tests/test_web_scrape.py`): rewrite around the new functions — mock round-trips (source CRUD incl. enabled toggle; extractor CRUD incl. `sources` link replacement; `parsed_by` reflects links from both directions; captures list/get/fetch-now shapes; `preview_extraction` deterministic and varies by url) and live calls against `pytest-httpserver` for **every §7 endpoint** (the executable contract). Contract constants imported from `source.py`.
- **Pages** (`tests/test_web_scrape_page.py`): guarded AppTest smokes for both pages — render with fixtures, selector contents, name-immutable-on-edit, M2M multiselect present on Extract, Captures tab renders content, Test tab renders all four result sections, validation error surfaces.
- `make check` green; no DB, no network, no Streamlit in the default gate (importorskip).

## 11. Files touched (this build)

| File | Change |
|---|---|
| `src/bellweather/web/pages/6_Scrape.py` | Rewrite: sources master/detail (Edit · Captures). |
| `src/bellweather/web/pages/7_Extract.py` | New: extraction specs master/detail (Edit · Test). |
| `src/bellweather/web/pages/_scrape_form.py` | Replace `build_spec_payload` with the two §6 builders. |
| `src/bellweather/web/data/source.py` | Replace scrape-spec contract block + columns with §5. |
| `src/bellweather/web/data/mock.py` | Replace spec state/functions with sources/extractors/links/captures fixtures + functions. |
| `src/bellweather/web/data/live.py` | Replace spec functions with §7 calls. |
| `src/bellweather/web/data/__init__.py` | Re-export swap. |
| `tests/test_web_scrape.py`, `tests/test_web_scrape_form.py`, `tests/test_web_scrape_page.py` | Rewritten per §10. |
| `src/bellweather/api.py` | **Untouched** (existing `/api/scrape-specs*` stays for the backend stack; `/api/fetch-adapters` already present). |
