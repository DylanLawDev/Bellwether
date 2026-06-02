# LLM Scrape Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A **generic, schema-driven scrape engine**. A user declares **{a set of sites, a JSON output schema, a binding}** once; Bellwether fetches each page through a **pluggable HTTP adapter**, stores the raw page immutably (bronze), **LLM-extracts** it to the declared JSON, and a **binding** lands the result as gold observations + tags. Generalizes the producer model so a new source needs *config, not a bespoke Python parser*. Built as reusable infrastructure, testable with fixtures + a fake LLM, before any real site.

**Architecture:** A scrape spec rides the existing producer orchestrator as the generic `scrape` **template**: the orchestrator fires a thin **collector** (`producers/scrape/`) which fetches each site via a `FetchProvider` adapter and `POST`s the **raw page** to `/ingest` as `kind="unstructured"`, `content_type="scrape-llm-v1"`, `provenance.scrape_spec=<name>`. The worker routes that record to a generic **`LlmScrapeExtractor`** which loads the spec from the `scrape_specs` table, calls Claude with the spec's `output_schema` as a tool `input_schema` (guaranteed-valid JSON, `temperature=0`), and applies the spec's `binding` → **observations** (`gold.upsert_value`, set-semantics) **+ tags**. Bronze keeps the raw page, so extraction is replayable with a better model later.

**Tech Stack:** Python 3.12 + `uv`, FastAPI, `psycopg` v3 (sync), `pydantic` v2, `httpx` (default fetch adapter), `anthropic` (**new runtime dep** — the first paid runtime dependency; cheap model by default), Streamlit (UI), Terraform (secret wiring only — no new Cloud Run job). Rides the orchestrator's Job + Scheduler.

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md`.

**Builds on (on `main`):** the producer-orchestrator epic (T18–T27 — code merged to `main` via PRs #28–#33; the ticket files are mid-move from `In Progress/` to `Closed/`): `gold.upsert_value`, the extractor/normalizer registries, `worker.process_job` `kind`-routing, `templates.py`/`schedules.py`/`orchestrator.py`, the control-plane API + Schedules UI, and the orchestrator Cloud Run Job that already bakes `producers/`.

---

## How to run a ticket (lifecycle)

Tickets live in `docs/plans/tickets/{Open, In Progress, Closed}/`. To work one: move it `Open → In Progress`, branch `ticket/T<NN>-<slug>`, follow TDD, get `make check` green, open one PR. **Merge gate:** a ticket's contents may merge to `main` only when it is in `In Progress/` (work underway) or `Closed/` (done) — never from `Open/`. Move it to `Closed/` when merged. (Mirrors `CLAUDE.md` Conventions.)

Each ticket is self-contained — spec ref, prerequisites, exact files, interfaces, tests, acceptance criteria.

---

## Module layout (locked — new + modified for this epic)

```
src/bellweather/
├── config.py            # MODIFY: + anthropic_api_key, scrape_llm_model         [T36]
├── llm.py               # CREATE: LlmExtractor (schema-constrained, lazy client) [T36]
├── fetch/
│   ├── __init__.py      # CREATE: FetchResult, FetchProvider, registry           [T33]
│   └── httpx_fetch.py   # CREATE: HttpxFetcher ("httpx") + register at import     [T33]
├── scrape/
│   ├── __init__.py      # CREATE: package marker                                  [T34]
│   ├── specs.py         # CREATE: scrape_specs CRUD (never commits)               [T34]
│   └── binding.py       # CREATE: apply_binding (pure, stdlib-only)               [T35]
├── extractors/
│   ├── __init__.py      # MODIFY: + ExtractionResult dataclass                    [T37]
│   └── scrape_llm.py    # CREATE: LlmScrapeExtractor ("scrape-llm-v1")            [T38]
├── worker.py            # MODIFY: accept ExtractionResult; write observations     [T37]
│                        #         + import scrape_llm (registers)                 [T38]
├── ingest.py            # MODIFY: KNOWN_CONTENT_TYPES += "scrape-llm-v1"          [T38]
├── migrations/
│   └── 0003_scrape_specs.sql  # CREATE: scrape_specs table                        [T34]
├── api.py               # MODIFY: + /api/scrape-specs (read/CRUD/preview)         [T39]
└── web/
    ├── data/source.py   # MODIFY: + SCRAPE_SPEC_COLUMNS + docstring               [T41]
    ├── data/mock.py     # MODIFY: + scrape-spec backends (in-memory)              [T41]
    ├── data/live.py     # MODIFY: + scrape-spec backends (httpx)                  [T41]
    ├── data/__init__.py # MODIFY: re-export scrape-spec functions                 [T41]
    └── pages/6_Scrape.py# CREATE: Scrape-specs control-plane page                 [T41]
producers/scrape/
├── __init__.py          # CREATE: package marker                                  [T40]
├── collector.py         # CREATE: run(params, client) -> {"submitted": int}       [T40]
└── template.toml        # CREATE: manifest (entrypoint producers.scrape.collector:run) [T40]
pyproject.toml           # MODIFY: dependencies += "anthropic>=0.40"               [T36]
tests/conftest.py        # MODIFY: + requires_llm marker (mirrors requires_gcs)    [T36]
infra/                   # MODIFY: ANTHROPIC_API_KEY secret → worker Job + api svc  [T42]
```

**No two tickets create the same file** — with one benign exception: the empty package marker `scrape/__init__.py` is owned by **T34**, but T35 (binding) is DAG-independent of T34 and may land first, so T35 also creates it **empty if absent**. Identical empty content → no merge conflict whichever order they stack. `worker.py` is touched by T37 (shim) then T38 (one import line) — sequential (T38 depends on T37). `api.py` only by T39. `config.py` only by T36. `extractors/__init__.py` only by T37. `ingest.py` only by T38.

---

## Locked interfaces (use these exact names/signatures across tickets)

**config.py** — add to `Settings` (only `config.py` reads env):
```python
anthropic_api_key: str | None = None
scrape_llm_model: str = "claude-haiku-4-5-20251001"   # cheap default; per-spec override wins
```

**fetch/__init__.py**:
```python
@dataclass
class FetchResult:
    content: str                 # raw page text (HTML / JSON / text)
    status: int
    content_type: str | None = None
    final_url: str | None = None

@runtime_checkable
class FetchProvider(Protocol):
    name: str
    def fetch(self, url: str, **opts) -> FetchResult: ...

def register(provider: FetchProvider) -> None: ...
def get_fetcher(name: str) -> FetchProvider | None: ...
def known_fetchers() -> set[str]: ...
```
**fetch/httpx_fetch.py** — `HttpxFetcher.name = "httpx"`; `fetch` does `httpx.get(url, follow_redirects=True, timeout=30)`, returns `FetchResult(resp.text, resp.status_code, resp.headers.get("content-type"), str(resp.url))`. Calls `register(HttpxFetcher())` at import. No secret.

**migrations/0003_scrape_specs.sql** (next after the orchestrator's `0002`; the runner auto-discovers `*.sql` in order):
```sql
create table if not exists scrape_specs (
  id            bigserial primary key,
  name          text not null unique,        -- referenced by a record's provenance.scrape_spec
  description   text,
  sites         jsonb not null default '[]'::jsonb,   -- list of URLs
  output_schema jsonb not null,              -- JSON Schema → LLM tool input_schema
  binding       jsonb not null,              -- see binding contract below
  fetch_adapter text not null default 'httpx',
  llm_model     text,                        -- per-spec model override; null → settings default
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
```

**scrape/specs.py** — never commit (caller owns txn); `dict_row` shapes; `sites`/`output_schema`/`binding` come back as Python `list`/`dict` (psycopg jsonb adaption):
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

**scrape/binding.py** — pure, stdlib-only (NO jsonpath dependency); reuses the gold-value point shape and the tag shape so the worker writes them with the existing helpers:
```python
from bellweather.extractors import ExtractedTag
from bellweather.normalizers import NormalizedPoint

def apply_binding(instance: dict, binding: dict, *, fetched_at: datetime
                  ) -> tuple[list[NormalizedPoint], list[ExtractedTag]]: ...
```
Binding contract (jsonb on the spec; minimal field-reference resolver — flat fields only, enrich later):
```jsonc
{
  "records_path": "$.items",          // absent/None → the whole instance is ONE record;
                                      //   "$.key"   → instance["key"] must be a list of records
  "symbol_key":   "scrape:prices:{category}:{name}",  // str.format over a record's fields
  "symbol_kind":  "scraped-metric",   // literal → NormalizedPoint.symbol_kind
  "value":        "$.price",          // field ref → float(record["price"])
  "ts":           "fetched_at",       // the literal "fetched_at" → the param; else "$.field" parsed ISO
  "unit":         "usd",              // literal, OR "$.field" ref (a value starting "$." is a ref)
  "description":  "$.title",          // optional; literal or "$.field" ref
  "tags":         ["category", "in_stock"]   // field names → ExtractedTag(tag_type=name, raw_value=str(val), score={})
}
```
Resolver rules (locked): a string starting `"$."` is a **field reference** into the current record (top-level key only); the literal `"fetched_at"` in `ts` resolves to the `fetched_at` arg; any other string is a **literal**. `symbol_key` is `template.format(**record)` (missing key → that record is skipped, not crashed — log/count is the extractor's concern). One `NormalizedPoint` per record; `unit`/`description` resolved per-record (ref or literal). Missing/duplicate handling: a record missing `value` or `symbol_key` fields is skipped.

**llm.py** — thin Anthropic wrapper; **lazy client** so importing the module needs no key (the worker imports extractors unconditionally):
```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
        # store overrides; DO NOT construct the anthropic client here
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
        # lazy: build anthropic.Anthropic(api_key=api_key or get_settings().anthropic_api_key)
        #       on first use; raise RuntimeError if no key.
        # tools=[{"name":"emit","description":"Emit the extracted record(s).","input_schema":output_schema}]
        # tool_choice={"type":"tool","name":"emit"}, temperature=0, max_tokens=4096,
        # model = model or self._model or get_settings().scrape_llm_model
        # content is truncated to a sane cap (e.g. 200_000 chars) — log truncation, never silently drop.
        # return the tool_use block's .input dict.
```

**tests/conftest.py** — add after `requires_gcs`:
```python
requires_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)
```
Unit tests inject a fake LLM (`class _FakeLlm: def extract(self, content, output_schema, *, model=None): return {...}`); only one opt-in live test carries `@requires_llm`.

**extractors/__init__.py** — add (worker accepts BOTH the legacy `list[ExtractedTag]` and this):
```python
from dataclasses import dataclass, field
from bellweather.normalizers import NormalizedPoint   # reuse the gold-value point shape

@dataclass
class ExtractionResult:
    tags: list[ExtractedTag] = field(default_factory=list)
    observations: list[NormalizedPoint] = field(default_factory=list)
```
(`normalizers/__init__.py` does not import `extractors`, so this import is acyclic.) The `Extractor` Protocol's `extract` return is widened to `list[ExtractedTag] | ExtractionResult`; existing `GdeltGkgExtractor` is **unchanged** (still returns a list).

**worker.py** — the **unstructured** branch normalizes the extractor return, then writes tags (as today) AND observations:
```python
result = extractor.extract(envelope)
if isinstance(result, ExtractionResult):
    ex_tags, ex_obs = result.tags, result.observations
else:
    ex_tags, ex_obs = result, []        # legacy list[ExtractedTag] — GDELT path unchanged
for t in ex_tags:
    ...                                 # existing tags insert + upsert_coverage (unchanged)
for o in ex_obs:
    upsert_value(conn, o.symbol_key, o.symbol_kind, o.ts, o.value,
                 unit=o.unit, description=o.description)
```
Import `ExtractionResult` from `bellweather.extractors`. (`upsert_value` is already imported.) The structured branch is untouched.

**extractors/scrape_llm.py**:
```python
class LlmScrapeExtractor:
    content_type = "scrape-llm-v1"
    def __init__(self, *, spec_loader=None, llm=None) -> None:
        self._load = spec_loader or _db_spec_loader   # (name) -> spec dict | None
        self._llm = llm or LlmExtractor()
    def extract(self, envelope: dict) -> ExtractionResult:
        spec = self._load(envelope["provenance"]["scrape_spec"])
        if spec is None:
            return ExtractionResult()                  # nothing written; worker still ack/processed
        content = envelope["payload"] if isinstance(envelope["payload"], str) \
                  else json.dumps(envelope["payload"])
        instance = self._llm.extract(content, spec["output_schema"], model=spec.get("llm_model"))
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        obs, tags = apply_binding(instance, spec["binding"], fetched_at=fetched_at)
        return ExtractionResult(tags=tags, observations=obs)

def _db_spec_loader(name: str) -> dict | None:
    with get_conn() as c:                              # read-only spec lookup (trusted worker)
        return get_spec(c, name)

register(LlmScrapeExtractor())
```
`worker.py` adds `import bellweather.extractors.scrape_llm  # noqa: F401` (registers). `ingest.py` `KNOWN_CONTENT_TYPES = {"gdelt-gkg-v2", "numeric-series-v1", "scrape-llm-v1"}`.

**api.py** — add to `api_router` (prefix `/api`). The collector (unprivileged) reads its spec via the GET endpoints; preview runs **in-process** (the API is trusted and holds the LLM key — it fetches one URL + LLM-extracts + binds, committing nothing):
```
GET    /scrape-specs                 -> list[ScrapeSpecRow]
GET    /scrape-specs/{name}          -> ScrapeSpecRow         (404 if unknown)   # collector uses sites+fetch_adapter
POST   /scrape-specs                 -> ScrapeSpecRow         (body ScrapeSpecCreate)
PATCH  /scrape-specs/{name}          -> ScrapeSpecRow         (body ScrapeSpecPatch; 404 if unknown)
DELETE /scrape-specs/{name}          -> {"status":"deleted"}  (404 if unknown)
POST   /scrape-specs/{name}/preview  -> ScrapePreviewResult   (body {"url": str | None}; default = first site)
```
`ScrapePreviewResult = {extracted: dict, symbols: list[str], sample: list[{symbol_key, ts, value}], tags: list[{tag_type, raw_value}]}` (sample/symbols capped to first ~N; commits nothing, no bronze, no /ingest). Preview reuses `get_fetcher`, `LlmExtractor`, `apply_binding`, `scrape.specs.get_spec`.

**web/data** — column contract + functions (mock + live identical shapes):
```python
SCRAPE_SPEC_COLUMNS = ["id", "name", "description", "fetch_adapter", "llm_model", "enabled"]
# sites/output_schema/binding are nested JSON, carried per-spec (not flat columns) like params on schedules.
get_scrape_specs()                  -> DataFrame[SCRAPE_SPEC_COLUMNS]
get_scrape_spec(name)               -> dict   # full spec incl sites/output_schema/binding
create_scrape_spec(name, sites, output_schema, binding, *, description=None,
                   fetch_adapter="httpx", llm_model=None) -> int
update_scrape_spec(name, **fields)  -> None
delete_scrape_spec(name)            -> None
preview_scrape_spec(name, url=None) -> dict   # {extracted, symbols, sample, tags}
```

**Scrape collector contract (locked — `producers/scrape/`):**
- `template.toml`: `name = "scrape"`, `entrypoint = "producers.scrape.collector:run"`, `description`, `[params] spec = { type = "str", required = true, help = "scrape_specs.name" }`, `[schedule] default_interval = "6h"`.
- `def run(params: dict, client) -> dict:` reads `params["spec"]`, GETs `{BELLWEATHER_API_URL}/api/scrape-specs/{spec}` (producer reads the API URL from env — the same external-producer exemption `producers/gdelt` uses; it does NOT touch the DB), picks `get_fetcher(spec["fetch_adapter"]) or HttpxFetcher()`, fetches each `spec["sites"]`, and `client.ingest(...)` one raw page per site, returning `{"submitted": n}`.
- **Submission per page (locked):** `source=f"scrape:{spec_name}"`, `kind="unstructured"`, `content_type="scrape-llm-v1"`, `fetched_at=datetime.now(timezone.utc)`, `payload=res.content` (the raw page string), `idempotency_key=f"{spec_name}:{url}:{sha1(res.content)}"` (unchanged page → `duplicate` no-op; changed page → new bronze snapshot → re-extract), `provenance={"scrape_spec": spec_name, "url": url, "final_url": res.final_url, "fetch_status": res.status}`.

**Why the split paths to the spec (locked seam):** the **collector** runs unprivileged (orchestrator minimal-env, K4) so it reads the spec via the **API** (`GET /api/scrape-specs/{name}`); the **worker** is trusted (has DB) so its extractor reads the spec via **`scrape.specs.get_spec`** directly. One authored spec row, two read paths — never duplicated into schedule params.

---

## Build order & dependency graph

```
Phase A — worker-extractor core (the novel part):
  T33 fetch seam (httpx) ───────────────┐
  T34 scrape_specs registry (mig 0003) ─┤
  T35 binding (pure) ───────────────────┤
  T36 llm client (+dep,+config,+marker) ─┤
  T37 ExtractionResult + worker obs ─────┴─▶ T38 LlmScrapeExtractor + ingest type (worker-side complete)

Phase B — control plane → collection → UI:
  T34,T35,T36,T33 ─▶ T39 scrape API (read + CRUD + in-process preview)
  T33,T39 ─────────▶ T40 scrape collector template (rides the orchestrator)
  T39 ─────────────▶ T41 scrape-specs UI page + web.data backends

Phase C — deploy:
  T36,T38,T39 ─────▶ T42 infra: ANTHROPIC_API_KEY secret → worker Job + api service
```

Acyclic; `worker.py` (T37→T38) and `api.py` (T39 only) edits are ordered. T33/T34/T35/T36/T37 are mutually independent and may be authored in parallel; T38 joins them.

## Ticket index

| Ticket | Title | Depends on |
|---|---|---|
| [T33](tickets/Open/T33-fetch-adapter-seam.md) | Fetch adapter seam + httpx default | T00 |
| [T34](tickets/Open/T34-scrape-spec-registry.md) | Scrape-spec registry: migration 0003 + `scrape/specs.py` | T01, T02 |
| [T35](tickets/Open/T35-binding.md) | `apply_binding` — JSON→observations+tags (pure) | T09, T19 |
| [T36](tickets/Open/T36-llm-client.md) | LLM client `llm.py` + config + `anthropic` dep + `requires_llm` | T01 |
| [T37](tickets/Open/T37-extraction-result-worker.md) | `ExtractionResult` + worker writes observations | T09, T11, T18 |
| [T38](tickets/Open/T38-scrape-extractor.md) | `LlmScrapeExtractor` + `scrape-llm-v1` routing | T34, T35, T36, T37 |
| [T39](tickets/Open/T39-scrape-spec-api.md) | Scrape-spec control-plane API (read/CRUD/preview) | T15, T33, T34, T35, T36 |
| [T40](tickets/Open/T40-scrape-collector.md) | Scrape collector template (`producers/scrape/`) | T08, T22, T33, T39 |
| [T41](tickets/Open/T41-scrape-ui.md) | Scrape-specs UI page + `web.data` backends | T16, T39 |
| [T42](tickets/Open/T42-scrape-infra.md) | Infra: `ANTHROPIC_API_KEY` secret (worker + api) | T14, T36, T38, T39 |

**Phase-A end-to-end done:** seed a `scrape_specs` row + ingest a fixture raw page (`content_type="scrape-llm-v1"`, `provenance.scrape_spec`), inject a fake LLM returning canned JSON → run the worker → an `observations` row keyed to a `tracked_symbol` (+ any tags); `make check` green.

**Epic end-to-end done:** author a spec in the UI (sites + output schema + binding); Preview shows extracted JSON + would-be observations (commits nothing); schedule it (`template="scrape"`, `params={"spec": name}`); `bellweather orchestrate --once` → collector fetches (httpx) → raw page to bronze → worker LLM-extracts → `observations` + tags; the run shows in history. `make check` green.

---

## Self-review notes

- **Spec coverage (`2026-06-01-llm-scrape-engine-design.md`):** fetch seam K5 (T33); scrape-spec registry K2/K3 §5 (T34); binding K2 §4.3 (T35); LLM K7/K9 (T36); worker-side values K4 D-c (T37); generic extractor K3 §6.3 (T38); control-plane + preview K10 §8 (T39); collector-rides-orchestrator K6 §3 (T40); UI (T41); cost/secret D-b §infra (T42). Phase D (paid adapters, caching, HTML pre-clean, non-JSON out, URL templating) is **out of scope** per spec §9 — not ticketed.
- **Type consistency:** all shared symbols (`FetchResult`/`FetchProvider`, `scrape_specs`, `scrape.specs.*`, `apply_binding`, `LlmExtractor`, `ExtractionResult`, `NormalizedPoint` reuse, `LlmScrapeExtractor`/`"scrape-llm-v1"`, the collector submission, the API surface, the `web.data` functions) are defined once here and referenced by exact name in the tickets.
- **Invariants honored:** bronze-first/replayable (raw page stored; re-extract converges via set-semantics `upsert_value` + `temperature=0`, K8/D-d); collector unprivileged (API-URL only, reads spec via API, K4/D-e — first cut httpx, no secret); only `config.py` reads env (producer exemption noted); DB helpers never commit; worker idempotent (at-least-once safe).
- **No placeholders:** SQL, signatures, the binding resolver rules, the LLM tool-use call, the worker shim, and the collector submission are given in full; tickets supply the TDD test code + steps.
