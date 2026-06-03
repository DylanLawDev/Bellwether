# Bellwether — Scrape-Spec UI Redesign (master/detail Edit · Preview)

| | |
|---|---|
| **Status** | Draft — approved in brainstorm, pending spec review |
| **Date** | 2026-06-02 |
| **Owner** | Dylan |
| **Scope** | Rework the operator **Scrape specs** page (`src/bellweather/web/pages/6_Scrape.py`) from a create-only flat list into a **master/detail** surface that can fully **edit** an existing spec and **preview any of its sites**, closing the gaps left after the T39–T41 data-import epic. UI + `web.data` seam only, plus one small read-only API endpoint to enumerate fetch adapters. Mock-data first. |
| **Related** | `docs/specs/2026-06-01-llm-scrape-engine-design.md` (the scrape engine + spec control plane this fronts), `docs/specs/2026-05-31-ui-prototype-design.md` (the UI prototype + `web.data` seam), `docs/specs/2026-06-01-producer-orchestrator-design.md` (the source-agnostic scheduling surface this page deliberately does **not** duplicate) |

---

## 1. Goal

After T39 (scrape-spec control-plane API), T40 (scrape collector), and T41 (scrape-specs UI page), an operator can **create**, **toggle**, **delete**, and **first-site-preview** a scrape spec — but **cannot edit one** (sites, output schema, binding, model, description are set once at creation and never changed except by delete-and-recreate), cannot **preview a site other than the first**, and never sees the **tags** a preview would emit.

This redesign closes those gaps by turning `6_Scrape.py` into a master/detail page:

- A **spec selector** (with a "➕ New spec…" entry) drives a **detail panel**.
- An **Edit** tab is a single **unified create/edit form** — the same form authors a new spec or edits an existing one.
- A **Preview** tab runs a **per-site** dry-run and renders the full `ScrapePreviewResult` — extracted JSON, sample observations, **and tags**.

The backing API and `web.data` seam already expose everything the core redesign needs; the only backend addition is a tiny endpoint so the adapter field can be a real dropdown.

## 2. Non-goals

- **No scheduling or run-health UI on this page.** Scheduling is source-agnostic and already lives on the Schedules page (`template="scrape", params={"spec": <name>}`). This page carries only a one-line pointer/link to it. The orchestrator/schedules/templates machinery is **not** touched.
- **No rename of an existing spec.** `name` is the URL path key (`/api/scrape-specs/{name}`) and the selector identity; it is read-only when editing. Only the New-spec path takes a fresh name.
- **No server-side `fetch_adapter` validation** added to create/update in this work. The live-mode dropdown (sourced from the registry) already prevents the UI from saving an unknown adapter; tightening the API is noted as a future item (§9).
- **No `st.session_state` / `st.dialog`.** Neither is used anywhere in `web/` today; the page stays on the established stateless-rerun idiom.

## 3. Key decisions (from brainstorm)

| # | Decision | Rationale |
|---|---|---|
| K1 | **Master/detail with a stateless selector.** Selection derives from the `st.selectbox` return value each rerun — no `st.session_state`. | Matches every other page in the repo (`5_Schedules.py`, `2_Explorer.py`). Avoids introducing a brand-new state pattern (and its test-isolation hazards) for no need. |
| K2 | **One unified create/edit form.** A `➕ New spec…` sentinel at index 0 of the selector opens the Edit tab blank (with example defaults) + a **Create** button; selecting an existing spec pre-fills it + **Save**/**Delete**. Submit branches new-vs-existing. | DRY: one form, one set of validators. The form assembly/validation is a pure helper so the branch logic stays unit-testable. |
| K3 | **`name` immutable on edit; create-only.** | It is the live path key and selector identity (§2). |
| K4 | **`enabled` is shown in both modes but persisted asymmetrically.** Existing spec: via `update_scrape_spec(name, enabled=…)`. New spec created disabled: create (defaults enabled) then a follow-up `update_scrape_spec(name, enabled=False)`. | `create_scrape_spec` has no `enabled` kwarg; rather than widen four layers (seam/mock/live/API body) for a rare case, the API's existing PATCH covers it. |
| K5 | **Per-site Preview.** A `st.selectbox` over `spec["sites"]` feeds `preview_scrape_spec(name, url=chosen)`. The full result renders: extracted JSON, sample observations, **and tags** (tags are in the contract but unrendered today). Wrapped in `st.spinner` + try/except. | Closes the first-site-only gap and the dropped-tags gap. Live preview fetches + calls the LLM in-process (up to `_LONG_TIMEOUT = 600s`), so progress + error feedback are required. |
| K6 | **`fetch_adapter` is a real dropdown.** New read-only `GET /api/fetch-adapters → {"adapters": sorted(known_fetchers())}`; new seam helper `get_fetch_adapter_choices() -> list[str]` (mock returns `["httpx"]`, live calls the endpoint); Edit form uses `st.selectbox`. | `known_fetchers()` already exists (`fetch/__init__.py`) but is unreachable from `web/`. A dropdown prevents typo'd adapters that would only fail at preview/collect time, and is accurate in live mode. |
| K7 | **Mock preview honors `url`.** `mock.preview_scrape_spec(name, url=None)` deterministically reflects the chosen `url` (e.g. derives a symbol/value from it) instead of returning identical output for every site. | The per-site selector is the point; an offline demo that ignores the site undersells it and makes a "preview reflects the chosen site" test impossible. Kept deterministic so tests stay stable. |
| K8 | **Rewrite `6_Scrape.py` in place.** | The user named that file; Streamlit auto-numbers pages by filename; keeping the slot avoids nav churn and a dangling duplicate. |

## 4. The page (`6_Scrape.py`)

```
Scrape specs
┌────────────────────────────────────────────────────────────────┐
│ Spec:  [ ➕ New spec… ▾ ]   (options: New · demo-prices · …)      │
│ → Schedule this spec on the Schedules page (template "scrape").  │
│                                                                  │
│ [ Edit ] [ Preview ]                                             │
│ ┌── Edit ─────────────────────────────────────────────────────┐ │
│ │ Name        [ demo-prices ]  (read-only when editing)        │ │
│ │ Description [ … ]                                            │ │
│ │ Sites       [ one URL per line … ]            (textarea)     │ │
│ │ Output schema (JSON Schema)  [ … ]            (textarea)     │ │
│ │ Binding (JSON)               [ … ]            (textarea)     │ │
│ │ Fetch adapter [ httpx ▾ ]    LLM model [ (blank=default) ]   │ │
│ │ ( ) Enabled                                                  │ │
│ │ [ Save changes ]  [ Delete spec ]   (Create for New spec)    │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ┌── Preview ───────────────────────────────────────────────────┐ │
│ │ Preview which site? [ https://…/a ▾ ]   [ Run preview ⟳ ]     │ │
│ │ "Would emit N point(s) across M symbol(s) and K tag(s)."      │ │
│ │ Extracted JSON {…}                                            │ │
│ │ Sample observations  | symbol_key | ts | value |              │ │
│ │ Tags                 | tag_type | raw_value |   (NEW)         │ │
│ └──────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

**Selector.** `names = list(get_scrape_specs()["name"])`; options = `["➕ New spec…", *names]`. The selected value (or the sentinel) is read each rerun.

**Edit tab.** When the sentinel is selected → blank form with the existing example defaults (the self-consistent `{title, price}` schema + `scrape:demo:{title}` binding already in the current page) and a **Create** button. When an existing name is selected → fields pre-filled from `get_scrape_spec(name)`; **Name** rendered disabled; **Save changes** + **Delete spec** buttons. On submit:
- assemble + validate the payload via the pure helper (below); on errors, render them all with `st.error` and stop;
- **create path:** `create_scrape_spec(name, sites, output_schema, binding, description=…, fetch_adapter=…, llm_model=…)`, then if the Enabled toggle is off, `update_scrape_spec(name, enabled=False)`; `st.rerun()`;
- **edit path:** `update_scrape_spec(name, description=…, sites=…, output_schema=…, binding=…, fetch_adapter=…, llm_model=…, enabled=…)` (name excluded); `st.rerun()`.

**Preview tab.** Disabled/with an info note when the New-spec sentinel is selected (nothing to preview until saved). For an existing spec: a `st.selectbox` over `spec["sites"]`; **Run preview** calls `preview_scrape_spec(name, url=chosen)` inside `st.spinner`, wrapped in try/except → `st.error`. Render `extracted` (`st.json`), `sample` (`st.dataframe`/`st.json`), and `tags` (`st.dataframe`/`st.json`), plus the existing summary line extended to include the tag count.

**Scheduling pointer.** One `st.caption` linking to the Schedules page. No interval/run-health controls.

## 5. The `web.data` seam

Existing scrape functions are unchanged and already sufficient for CRUD + per-site preview:

```
get_scrape_specs()                -> DataFrame[SCRAPE_SPEC_COLUMNS]
get_scrape_spec(name)             -> dict | None      # full: sites/output_schema/binding/…
create_scrape_spec(name, sites, output_schema, binding, *, description=None,
                   fetch_adapter="httpx", llm_model=None) -> int
update_scrape_spec(name, **fields) -> None            # name|description|sites|output_schema|
                                                      #   binding|fetch_adapter|llm_model|enabled
delete_scrape_spec(name)          -> None
preview_scrape_spec(name, url=None) -> dict           # {extracted, symbols, sample, tags}
```

**Additions:**

- **`get_fetch_adapter_choices() -> list[str]`** — documented in `source.py`; re-exported from `web/data/__init__.py`.
  - `mock`: returns `["httpx"]` (a module constant, deterministic).
  - `live`: `GET /api/fetch-adapters` → `data["adapters"]` (a plain list).
- **`mock.preview_scrape_spec(name, url=None)`** — change to make the result a deterministic function of `url`: e.g. fold a short hash of `url` into a symbol suffix and the sample `value`, and echo `url` somewhere in `extracted`. When `url is None`, fall back to the spec's first site so behaviour matches the API (`api.py` `_scrape_preview` uses `spec["sites"][0]` when no url is given).

No change to `create_scrape_spec`/`update_scrape_spec` signatures (K4).

## 6. The API addition

```python
# api.py — read-only, no auth, mirrors the other control-plane reads
@app.get("/api/fetch-adapters")
def api_fetch_adapters() -> dict:
    return {"adapters": sorted(known_fetchers())}
```

`known_fetchers()` is imported from `bellweather.fetch` (already importable in `api.py`; `get_fetcher` is imported there today). Returns `["httpx"]` until more adapters register. This is the only backend change.

## 7. Validation & the pure form helper (`_scrape_form.py`)

Keep all form logic Streamlit-free and unit-testable, extending the existing `parse_json` / `validate_spec_name` / `validate_json_object`:

```python
def build_spec_payload(*, name, description, sites_raw, output_schema_raw,
                       binding_raw, fetch_adapter, llm_model,
                       require_name=True) -> tuple[dict | None, list[str]]:
    """Parse + validate the form fields; return (payload, errors).
    payload keys: name, description|None, sites(list), output_schema(dict),
    binding(dict), fetch_adapter, llm_model|None. require_name=False on the edit
    path (name comes from the selector and is immutable)."""
```

Rules (carried over + consolidated): name required & path-safe (`^[A-Za-z0-9._-]+$`) on the create path; `sites` non-empty after stripping blank lines; `output_schema` and `binding` must parse as JSON **and** be objects; blank `description`/`llm_model` → `None`. The page wires the returned `errors` into an `st.error` loop, exactly as today.

## 8. Testing

The gate is `make check` (ruff check + ruff format --check + pytest). Web tests run with **no DB and no network** (mock in-memory; live via `pytest-httpserver`).

- **`tests/test_web_scrape_form.py`** — unit-test `build_spec_payload`: valid create payload; valid edit payload (`require_name=False`); missing/invalid name; empty sites; non-object schema/binding; blanks → `None`.
- **`tests/test_web_scrape.py`** —
  - `get_fetch_adapter_choices`: mock returns `["httpx"]`; live hits `GET /api/fetch-adapters` via the `_api` `pytest-httpserver` fixture and returns its `adapters` list.
  - `preview_scrape_spec` (mock) now varies by `url`: two different urls yield distinguishable results; `url=None` falls back to the first site; shape still `{extracted, symbols, sample, tags}`.
  - Existing CRUD/preview contract tests remain green; new seam functions assert against constants imported from `source.py`.
- Page module itself isn't unit-tested (Streamlit runtime); coverage comes from the pure helper + the seam contract tests. Manual smoke via `make ui` (mock default): New→fill→Create, select→edit→Save, Preview→pick site→Run; confirm no scheduling UI and the Schedules link is present.

## 9. Risks & future items

- **Live preview latency** (≤600s): mitigated by `st.spinner` + try/except → `st.error` (K5).
- **Self-consistency footgun** (a `symbol_key` `{placeholder}` absent from `output_schema.properties` makes `apply_binding` silently skip every record): **not** guarded in this work (the warning was de-scoped); noted so a later pass can add a soft warning in `build_spec_payload`.
- **Asymmetric adapter validation**: create/update accept any `fetch_adapter` string server-side while preview/collect reject unknown ones. The live dropdown prevents bad values from the UI; tightening the API is a future item.
- **`mock` state is module-global** (existing prototype property) — fine for single-session operator use; the live backend is transactional.

## 10. Files touched

| File | Change |
|---|---|
| `src/bellweather/web/pages/6_Scrape.py` | Rewrite as master/detail (selector + Edit/Preview tabs). |
| `src/bellweather/web/pages/_scrape_form.py` | Add `build_spec_payload(...)` pure helper. |
| `src/bellweather/web/data/source.py` | Document `get_fetch_adapter_choices()`; note mock-preview honours `url`. |
| `src/bellweather/web/data/mock.py` | Add `get_fetch_adapter_choices()`; make `preview_scrape_spec` vary by `url`; richer fixture specs (see build plan). |
| `src/bellweather/web/data/live.py` | Add `get_fetch_adapter_choices()` → `GET /api/fetch-adapters`. |
| `src/bellweather/web/data/__init__.py` | Re-export `get_fetch_adapter_choices`. |
| `src/bellweather/api.py` | Add `GET /api/fetch-adapters`. |
| `tests/test_web_scrape.py` | Tests for the new seam fn + url-varying mock preview. |
| `tests/test_web_scrape_form.py` | Tests for `build_spec_payload`. |
