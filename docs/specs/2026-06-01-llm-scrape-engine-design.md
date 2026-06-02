# Bellwether — LLM Scrape Engine (schema-driven extraction) Design

| | |
|---|---|
| **Status** | Draft — approved in brainstorm, high-level plan (NOT yet ticketed) |
| **Date** | 2026-06-01 |
| **Owner** | Dylan |
| **Scope** | A **generic, schema-driven scrape engine**: a user declares **{a set of sites, a JSON output schema, a binding}** once; Bellwether fetches each page through a **pluggable HTTP adapter**, stores the raw page immutably, **LLM-extracts** it to the declared JSON, and lands the result as **gold observations + tags**. Generalizes the producer model so a new source needs *config, not a bespoke Python parser*. **Builds on top of the producer-orchestrator epic** (rides it as the generic template). This document is a high-level plan; ticket decomposition is deferred. |
| **Related** | `docs/specs/2026-06-01-producer-orchestrator-design.md` (the orchestrator + structured path this rides on), `docs/specs/2026-05-31-ingestor-extractor-design.md` (the v0 spine), `README.md` §3–§5 (Collector, two ingestion paths) |

---

## 1. Goal

Today, adding a source means hand-writing a bespoke parser — the GDELT producer painstakingly indexes TSV columns; the planned Polymarket producer calls the Gamma + CLOB APIs and shapes `numeric-series-v1`. The scrape engine replaces *that per-source code* with *per-source config* for the large class of sources that expose data on a web page but have no clean feed:

> The user declares a **set of sites** and the **structured JSON they want out of them**, plus a small **binding** that says how that JSON maps onto Bellwether's time series. An LLM does the messy-page → clean-JSON step. Nothing source-specific is written in Python.

This is the Oxylabs-style model — declarative extraction — folded into Bellwether's invariants: **bronze-first immutability**, **extraction replayable from bronze**, and the orchestrator's **"collection logic is external/unprivileged"** stance.

The output format is **JSON only for now**; bronze stores the **raw page (any content type)**.

---

## 2. Key decisions (from brainstorm)

| # | Decision | Rationale |
|---|---|---|
| K1 | **Worker-side extraction; bronze stores the raw page, not the LLM output.** A thin collector lands the raw page (HTML/JSON/text) in bronze as `kind="unstructured"`; a new worker-side LLM extractor turns raw → JSON later. | Preserves the spine's load-bearing invariant — *extraction is replayable from bronze*. The page can be re-extracted with a better model/prompt without re-fetching. The LLM key lives in the **trusted worker** (which already holds DB/bucket creds), not in an external script. |
| K2 | **Arbitrary user JSON + a declarative binding.** The user declares any JSON output schema *and* a small binding (`value`/`ts`/`symbol_key`/`tags`) that maps the extracted JSON onto `(symbol, ts, value)` + tags. | A signal pipeline's gold layer is an opinionated time series. The binding is the price of arbitrariness, and it keeps per-source mapping out of code — it is UI-editable data. |
| K3 | **One generic `LlmScrapeExtractor`, parameterized by a DB-stored spec.** It is registered for `content_type="scrape-llm-v1"` and loads the spec (schema + binding) from a `scrape_specs` table by the `scrape_spec` name carried in the record's provenance. | The worker is trusted and has DB access. Specs stay *data* (UI-editable, versionable in a table), not generated code. One extractor serves all scrape specs. |
| K4 | **Extraction produces gold *values* (and tags) off the unstructured path.** The extractor return is widened from "tags only" to `ExtractionResult(tags, observations)`; the worker writes tags **and** `gold.upsert_value()`. Backward-compatible — GDELT keeps returning tags only. | The LLM extractor is the legitimate bridge from unstructured input to numeric output. Avoids a messy producer-side re-`POST` of a structured record. |
| K5 | **Pluggable fetch adapter; httpx default, no Oxylabs.** A `FetchProvider` seam (mirrors the extractor/normalizer registries) with a plain-`httpx` default. Rendered/anti-bot adapters (Oxylabs, Bright Data, …) are drop-in later, selected per spec. | Keeps the first cut at **$0** and within the `<$40/mo` envelope — no paid dependency yet. The escalation path exists without committing to a vendor. |
| K6 | **Rides the producer orchestrator as the generic template.** A scrape spec becomes a schedule; the orchestrator fires the scrape collector; the worker does the LLM extraction. Scheduling, run history, and the Schedules UI come for free. | The orchestrator was *built* to host declarative templates. The scrape engine is the most general one. No duplicated scheduling/UI infra. |
| K7 | **Schema-constrained LLM output via tool-use.** The user's `output_schema` *is* the LLM tool's `input_schema`, so the model is forced to emit JSON valid against it; `temperature=0`. | Guarantees parseable, schema-valid output without bespoke parsing/repair. Determinism is maximized (see K8). |
| K8 | **Non-determinism is absorbed by set-semantics gold.** `gold.upsert_value` *sets* (last-value-wins); re-extraction of the same bronze page **converges**. | The worker is at-least-once (expired-lease re-processing); LLM output may vary slightly. Set-semantics + `temperature=0` keep this idempotent in effect. Raw page remains the immutable replayable source. |
| K9 | **Anthropic Claude, cheap model by default, configurable per spec.** `llm.py` is a thin Anthropic wrapper; default model is a low-cost tier (Haiku); each spec may override via `llm_model`. A general multi-provider seam is YAGNI for now. | "Use LLMs" with cost discipline. The per-spec override and the thin wrapper leave room for a provider seam later without building one now. |
| K10 | **Dry-run preview reuses the orchestrator's trusted-preview model (K9 there).** Fetch one URL + run the LLM against the spec, show the extracted JSON and the would-be observations, **commit nothing** (no bronze, no DB, no `/ingest`). | Gives the "does my schema + binding actually work?" feedback loop in the UI without side effects, on *trusted* spec config (no pasted code). |

---

## 3. Architecture

Everything inside the box is Bellwether. The **scrape collector** is a generic template the orchestrator invokes; the **LLM extractor** is a new worker-side unit.

```
   orchestrator schedule ─▶ scrape collector ──fetch(adapter, K5)──▶ raw page bytes
     (rides T18+ epic, K6)    (generic template)                        │
                                                  POST /ingest          │
                                                  kind=unstructured      │
                                                  content_type=scrape-llm-v1
                                                  provenance.scrape_spec=<name>
                                                                         │
   ┌─────────────────────────────────────────────────────────────────┐ │
   │  Ingestion API → bronze (raw page, immutable) + raw_records + queue│◀┘
   └───────────────────────────────────┬─────────────────────────────┘
                                        │
   ┌────────────────────────────────────▼────────────────────────────┐
   │  Worker (unstructured route)                                      │
   │    get_extractor("scrape-llm-v1") → LlmScrapeExtractor            │
   │      1. load spec from scrape_specs (DB, by provenance.scrape_spec│
   │      2. Claude(raw page, spec.output_schema as tool schema, K7)   │
   │      3. validate JSON; apply spec.binding (K2) → obs + tags       │
   │      4. ExtractionResult(tags, observations)  (K4)                │
   └────────────────────────────────────┬────────────────────────────┘
                                         ▼
                      gold: upsert_value(obs)  +  tags(silver)
```

Three decoupled scheduled processes already exist from the orchestrator epic (orchestrator drives producers, worker drains the queue); the scrape engine adds **no new process** — it is one template + one extractor + supporting units.

### 3.1 Units (each independently testable)

| Unit | Module | New? | Responsibility |
|---|---|---|---|
| Fetch adapter seam | `fetch/__init__.py` | NEW | `FetchProvider` Protocol `fetch(url, opts) -> FetchResult(content, content_type, status, final_url)` + `register`/`get_fetcher`. |
| httpx fetcher | `fetch/httpx_fetch.py` | NEW | Default adapter. No secret, no paid dep. Paid/rendered adapters drop in later. |
| Scrape-spec registry | `scrape/specs.py` | NEW | CRUD over `scrape_specs` (helpers never commit — caller owns the txn, per `queue.py`). |
| Scrape-spec schema | `migrations/0003_scrape_specs.sql` | NEW | `scrape_specs` table (after the orchestrator's `0002`). |
| LLM client | `llm.py` | NEW | Thin Anthropic wrapper; schema-constrained extraction via tool-use; `temperature=0`; model from spec or default. |
| Binding | `scrape/binding.py` | NEW | **Pure** `apply_binding(json_instance, binding, fetched_at) -> (observations, tags)`. No I/O. Highest-TDD-value unit. |
| LLM scrape extractor | `extractors/scrape_llm.py` | NEW | Registered for `scrape-llm-v1`: load spec → LLM → validate → `apply_binding` → `ExtractionResult`. |
| Extraction result | `extractors/__init__.py` | MODIFY | Widen extractor return to `ExtractionResult(tags=[...], observations=[...])`; default `observations=[]` keeps GDELT unchanged. |
| Worker integration | `worker.py` | MODIFY | Unstructured branch writes `result.observations` via `gold.upsert_value` in addition to tags. |
| Scrape collector | `producers/scrape/` (template) | NEW | Generic orchestrator template: resolve spec → fetch each site via adapter → `POST /ingest` the raw page. Needs only `BELLWEATHER_API_URL` + httpx. |
| Control plane + UI | `api.py`, `web/pages/*`, `web/data/*` | NEW | Author/edit specs; **dry-run preview** (K10); list specs alongside schedules. |

**Isolation contract.** The collector still runs unprivileged (orchestrator's minimal-env model): for the first cut it needs only `BELLWEATHER_API_URL` + the no-secret httpx adapter. The **LLM key lives only in the worker** (trusted spine). When a *paid* fetch adapter is added (Phase D), the scrape collector — *trusted first-class Bellwether code, not an arbitrary customer script* — may hold that adapter's key; called out in §6 D-e.

---

## 4. The scrape-spec contract

A **scrape spec** is the unit a user authors. It has three parts:

### 4.1 Sites
A list of target URLs (or URL templates). The collector fetches each per run. (URL *templating from params* — e.g. a date or page number — is a natural extension; the first cut takes literal URLs.)

### 4.2 Output schema
A JSON Schema describing the structured JSON the user wants out of each page. This schema is passed verbatim as the LLM tool's `input_schema` (K7), so the model is constrained to emit JSON valid against it.

### 4.3 Binding (arbitrary JSON → gold/silver)
A small declarative map (stored as `jsonb`) from the extracted JSON onto Bellwether's `(symbol, ts, value)` + tags. Strawman:

```jsonc
{
  "records_path": "$.items",                          // JSONPath to the record array;
                                                      //   absent → treat the whole doc as one record
  "symbol_key":   "scrape:prices:{category}:{name}",  // template over extracted fields
  "symbol_kind":  "scraped-metric",                   // tracked_symbols.kind
  "value":        "$.price",                          // numeric field → observation value
  "ts":           "fetched_at",                       // a field path, or the literal "fetched_at"
  "unit":         "usd",                              // literal or field path
  "description":  "$.title",                          // optional, literal or field path
  "tags":         ["category", "in_stock"]            // fields → tags (silver), key = field name
}
```

`apply_binding` walks `records_path`, builds each `symbol_key` from the template, and emits **one observation per record** (`value`, `ts` resolved to the record/`fetched_at`, `unit`, `symbol_kind`, `description`) plus a tag per field listed in `tags`. It is a **pure function** — fixture-tested with no network or LLM. Expressiveness starts at JSONPath-ish field references; richer transforms (casts, arithmetic, conditionals) are a later enrichment, not in the first cut (§7 open questions).

---

## 5. The scrape-spec registry

`migrations/0003_scrape_specs.sql` (next after the orchestrator's `0002`; the runner auto-discovers `*.sql` in order):

```sql
create table if not exists scrape_specs (
  id            bigserial primary key,
  name          text not null unique,         -- referenced from a record's provenance.scrape_spec
  description   text,
  sites         jsonb not null default '[]'::jsonb,   -- list of URLs / URL templates
  output_schema jsonb not null,               -- JSON Schema → LLM tool input_schema
  binding       jsonb not null,               -- §4.3
  fetch_adapter text not null default 'httpx', -- FetchProvider name (K5)
  llm_model     text,                          -- per-spec model override (K9); null → default
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
```

`scrape/specs.py` (helpers never commit — caller owns the txn, per the `queue.py` convention):

```python
def list_specs(conn) -> list[dict]: ...
def get_spec(conn, name) -> dict | None: ...
def create_spec(conn, name, sites, output_schema, binding, *,
                fetch_adapter="httpx", llm_model=None, description=None) -> int: ...
def update_spec(conn, name, **fields) -> None: ...
def delete_spec(conn, name) -> None: ...
```

A scrape spec is **scheduled** by creating an orchestrator `producer_schedules` row whose template is the scrape collector and whose params carry `{"spec": "<name>"}` (K6) — scheduling/run-history is entirely the orchestrator's.

---

## 6. Relationship to existing design — deltas

Each item is a deliberate evolution, called out (the house style of the orchestrator spec):

| # | Existing statement | This design | Resolution |
|---|---|---|---|
| **D-a** | v0 stance / README §5.1: "**borrowed extraction** — no bespoke NLP in v0." | We run **LLM extraction** of arbitrary pages. | **Evolution.** It is LLM-as-a-service behind the *same extractor registry*, opt-in per `content_type` (`scrape-llm-v1`). The GDELT borrowed-extraction path is unchanged. |
| **D-b** | The v0 stack is "one paid datastore, by design"; target `<$40/mo`. | Adds the **first paid *runtime* dependency** — the Anthropic API (per-call cost) + a new secret in the worker. | **Cost flag.** Stays in budget only with a cheap default model (Haiku, K9), small pages, and low cadence. Explicitly within the envelope's intent, but the first per-request cost — monitor it. |
| **D-c** | `worker.process_job`'s **unstructured** branch writes **tags only**; structured writes values. | Unstructured extraction can now also write **gold values** (`ExtractionResult.observations` → `upsert_value`). | **Bridge, not contradiction.** The "unstructured→tags / structured→values" split was NLP-vs-numeric; the LLM extractor legitimately turns unstructured input into numeric output. Structured-path normalizers untouched. |
| **D-d** | "Extraction is **replayable from bronze**" assumes a deterministic transform. | The LLM step is **non-deterministic**. | **Preserved in effect.** The raw page stays the immutable replayable source; `temperature=0` + set-semantics `upsert_value` make re-extraction *converge* (K8). Optional later: cache the extracted JSON as a derived artifact so replays don't re-bill the LLM. |
| **D-e** | Orchestrator **K4**: templates run with the ingest URL only — no other creds. | A *paid* fetch adapter (Phase D) needs an adapter key in the collector. | **Scoped exception, deferred.** First cut = httpx (no secret), so K4 is untouched. When a paid adapter lands, the scrape collector is *trusted first-class code* (not arbitrary customer script), so holding a fetch key is acceptable — gated to Phase D. |

> **Doc upkeep when this is built:** add a one-line pointer from README §5.1 (borrowed-extraction) and the 2026-05-31 spec's "no bespoke NLP" note to this spec, the same way the orchestrator epic amended the root docs.

---

## 7. Phasing (high level — not yet ticketed)

Each phase is TDD-able with fixtures and builds on the producer-orchestrator epic.

- **Phase A — worker-extractor core (the novel part).** Fetch seam + httpx adapter; `scrape_specs` migration + `specs.py`; `llm.py`; the **pure binding**; `LlmScrapeExtractor` (tests inject a fake LLM client returning canned JSON); widen `ExtractionResult`; worker writes observations. **Depends on the orchestrator epic's `gold.upsert_value` (T18).** *Done* = seed a spec + ingest a fixture raw page (`content_type=scrape-llm-v1`) → worker → an `observations` row keyed to a `tracked_symbol`; `make check` green.
- **Phase B — collection.** Scrape collector as an orchestrator template; schedule a spec; full end-to-end run under the orchestrator with the httpx adapter. **Depends on orchestrator T21–T24.**
- **Phase C — control plane + UI.** Spec authoring (sites + output schema + binding) and **dry-run preview** (K10). **Depends on orchestrator T25–T26** (extends the Schedules surface).
- **Phase D — later / optional.** Paid/rendered fetch adapters (Oxylabs, Bright Data); derived-extraction caching (avoid re-billing the LLM on replay); HTML pre-cleaning to shrink LLM input; non-JSON output formats; URL templating from params; richer binding transforms.

---

## 8. Testing

- `scrape/binding.py` — **pure unit tests** over fixtures (no network, no LLM); the core correctness surface.
- `LlmScrapeExtractor` — inject a **stub LLM client** returning canned JSON; assert tags + observations; assert spec-not-found / schema-invalid handling (unroutable, no data lost — same rule as an unknown extractor).
- Fetch seam — httpx adapter against a local fixture / mocked transport.
- Worker — extend the existing extractor end-to-end test for the observations path.
- A **`requires_llm` marker** that auto-skips when `ANTHROPIC_API_KEY` is unset (mirrors `requires_gcs`), plus **one opt-in live test** — so CI needs no real key.

---

## 9. Out of scope (this epic)

- **In-UI authoring of *Python* scrapers** — the scrape engine is *declarative config*, not pasted code; arbitrary-code authoring remains the orchestrator's out-of-scope, gated epic.
- **Multi-provider LLM abstraction** — Anthropic only, model configurable per spec (K9). A provider seam is a later option.
- **Non-JSON extraction output** (CSV, etc.) — JSON only for now.
- **Rendered/anti-bot fetching, geo, proxies** — deferred to Phase D adapters; httpx first.
- **Derived-extraction caching** — re-extract on replay for now (K8/D-d); cache later.
- **Richer binding transforms** (casts, arithmetic, conditionals) and **URL templating from params** — field-reference binding + literal URLs first.

---

## 10. Open questions / tuning (decided in tickets, not blocking)

- **Binding expressiveness** — start at JSONPath-ish field references (§4.3); enrich only when a real spec needs it.
- **LLM input size** — large pages cost tokens; Phase D HTML pre-cleaning trims them. First cut sends the raw page (cap length, log truncation rather than silently drop — per the "no silent caps" principle).
- **Preview cap** — a dry-run can extract many records; the UI shows the first ~N per spec (mirrors the orchestrator's preview cap).
- **Per-spec model/cost guardrails** — default Haiku; surface per-run token/cost in run history once Phase C lands.
