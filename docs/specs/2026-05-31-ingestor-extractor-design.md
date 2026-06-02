# Bellwether — Ingestor + Extractor Design

| | |
|---|---|
| **Status** | Draft — approved in brainstorm, pending spec review |
| **Date** | 2026-05-31 |
| **Owner** | Dylan |
| **Scope** | The ingestion + extraction core (the first build epic). Research layer, entity resolution, and the structured market feed are out of scope here. |
| **Related** | `README.md` (platform design doc) — §4 Architecture, §5.2 v0 spine, §6.4 provenance |

> **Amended 2026-06-01** by `docs/specs/2026-06-01-producer-orchestrator-design.md` (the producer-orchestrator epic): **D1 evolves** — Bellwether now *orchestrates* external collector scripts (still unprivileged) — and the **structured-feed path that §10 deferred is now being built**. Inline amendments are flagged below.

---

## 1. Goal

Build the **source-agnostic ingestion + extraction core** of Bellwether: a front
door that any external producer can push data to, an immutable raw store, a
durable processing queue, and a pluggable extraction stage that lands signals in
silver/gold tables. GDELT ships as the first reference producer and the first
extractor.

The deliverable proves the **spine** (§5.2 of the README) with a generalized
front door — not just a GDELT-specific poller.

---

## 2. Key decisions (from brainstorm)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Scraping *logic* is external to Bellwether.** The core never crawls or runs LLM agents. Collection lives in external scripts that push to the HTTP ingestion point. **(Amended 2026-06-01: Bellwether now *orchestrates* those scripts — scheduling them and calling them with parameters; they stay unprivileged, holding only the ingest URL. This adds a second coupling, the template manifest. See `docs/specs/2026-06-01-producer-orchestrator-design.md`.)** | Keeps scraping concerns (auth, rate limits, navigation) out of the platform. The ingestion contract is the *data* coupling; the template manifest is the *orchestration* coupling. |
| D2 | **Generic ingestor first, GDELT as the first collector.** | Matches README v0 spine but generalizes the front door so scrapers / custom-data loads are just more producers. |
| D3 | **Ingestion entry = HTTP API (`POST /ingest`) + a thin Python client.** | Universal: any language/agent, local or remote, can produce. Validation happens at submit time. |
| D4 | **Processing = durable Postgres-backed queue + a separate worker process.** | Decoupled + replayable (README §2, §6.4) without standing up Kafka. The queue table is the swap-point for Redpanda/Pub/Sub later. |
| D5 | **Postgres is the transactional spine; GCS holds raw bytes.** Postgres covers raw-record index, work queue, silver (tags/entities), and gold (time series). | Consolidation: one paid datastore instead of Kafka + ClickHouse + a doc store. Cheap and portable. |
| D6 | **Silver + gold in Postgres from day one** (not BigQuery yet). | Simplicity at enthusiast scale. The seam is drawn so gold can graduate to BigQuery/ClickHouse and research to DuckDB later without touching ingestion. |
| D7 | **Infra weight = "middle": managed Postgres + GCS object store + in-process/worker queue.** No streaming stack, no MinIO (use GCS directly on GCP). | "Lean but real" within a ~$40/mo enthusiast budget on GCP. |
| D8 | **v0 ships exactly one extractor (`gdelt-gkg`)**, behind a registry ready for more. | README §7: no bespoke NLP before the GDELT spine works end to end. |

---

## 3. Architecture

Everything inside the box is Bellwether; everything before `/ingest` is external.

```
external producers ──HTTP──▶ │ Ingestion API │ ──▶ GCS bronze (raw bytes)
(GDELT poller, scrapers,     │  POST /ingest │ ──▶ raw_records (PG, metadata)
 LLM agents, file loads)     └───────────────┘ ──▶ work_queue (PG)
                                                        │
                                          ┌─────────────▼─────────────┐
                                          │  Worker (bellwether worker)│
                                          │  lease job → route by      │
                                          │  content_type:             │
                                          │   ├─ unstructured ▶ Extractor registry
                                          │   └─ structured   ▶ Normalizer (built: 2026-06-01 orchestrator epic)
                                          └─────────────┬─────────────┘
                                                        ▼
                               silver (PG: tags, entities)  +  gold (PG: tracked_symbols, observations)
```

### 3.1 Units (each independently testable)

| Unit | Responsibility | Depends on |
|---|---|---|
| `ingestion-api` (FastAPI) | Validate submission, write bronze, dedup, enqueue. Returns 202. | GCS, PG |
| `ingestion-client` (thin Python lib) | `ingest(record)` / `ingest_batch()` over HTTP. | httpx |
| `bronze-store` | Write/read immutable payloads, provenance-addressed, in GCS. | GCS |
| `work-queue` | Enqueue / lease / ack / retry jobs. | PG |
| `worker` | Lease job → route → call extractor/normalizer → write silver+gold. | queue, stores, registry |
| `extractor-registry` | Map `content_type` → extractor. v0 ships one: `gdelt-gkg`. | — |
| `gold-store` | tracked_symbols + time-bucketed observations. | PG |
| `gdelt-producer` (reference) | External: pull GDELT, POST to `/ingest`. The worked example. | ingestion-client |

**Isolation contract:** a producer breaking, an extractor throwing, or the worker
being down must never lose data — bronze is written before any fallible step, and
jobs are durable and retryable.

---

## 4. The ingestion contract (the central seam)

A submission sent to `POST /ingest` (single or batched):

```jsonc
{
  "source": "gdelt.gkg",          // who produced it (namespaced)
  "kind": "unstructured",          // "unstructured" | "structured" — routing hint
  "content_type": "gdelt-gkg-v2",  // selects the extractor / normalizer
  "fetched_at": "2026-05-31T14:15:00Z",
  "idempotency_key": "gdelt-gkg-20260531141500-row42",  // unique per logical record
  "payload": { } | "<raw text>",  // inline small payloads...
  "payload_uri": "gs://...",       // ...or a pointer for large blobs
  "provenance": { "url": "...", "collector_version": "..." }  // free-form, stamped into bronze
}
```

**Rules**
- **Idempotency:** `UNIQUE(source, idempotency_key)`. Re-submission is a no-op that
  returns the existing record id — producers are safely retryable (§6.4).
- **Bronze-first:** the immutable capture (GCS write + `raw_records` row) is the
  first durable side effect; only then is a job enqueued. Nothing fallible runs
  before the data is safe.
- **Routing:** `kind` + `content_type` decide the path and the extractor. Unknown
  `content_type` → record still lands in bronze; job parked as `unroutable` for
  replay once an extractor exists. **No data is ever dropped for lack of a parser.**
- **Inline vs pointer:** small payloads inline; large blobs uploaded to GCS by the
  producer (or by the API) and referenced via `payload_uri`.

---

## 5. Processing model

1. `POST /ingest` → validate → dedup check → write payload to GCS bronze → insert
   `raw_records` row (`status=received`) → insert `work_queue` row → return `202`
   with the record id.
2. **Worker** (`bellwether worker`, separate process) loops:
   - Lease a batch of jobs: `SELECT … FROM work_queue WHERE state='pending'
     AND lease_until < now() FOR UPDATE SKIP LOCKED LIMIT n`.
   - Route by `content_type` → extractor (unstructured) or normalizer (structured).
   - Write silver (`tags`, possibly `entities`) and gold (`observations`).
   - Mark `raw_records.status=processed`, ack the job. On error: increment
     `attempts`, set `last_error`, back off; after N attempts → `failed` (dead-letter,
     replayable).
3. **Replay:** because bronze is immutable and extraction is idempotent + versioned,
   any record can be re-processed later with a better extractor by re-enqueuing.

The worker runs as a Cloud Run Job on a schedule, or alongside Postgres on a small
VM — see §8.

---

## 6. Data model

### 6.1 GCS (bronze — raw bytes)
Immutable, partitioned objects: `gs://<bucket>/<source>/<yyyy>/<mm>/<dd>/<idempotency_key>`.
Never mutated. Holds raw GDELT files, scraped HTML/text, large JSON blobs.

### 6.2 Postgres (the structured spine)

```sql
-- bronze metadata (one row per captured record; payload bytes live in GCS)
raw_records(
  id, source, kind, content_type,
  idempotency_key,            -- UNIQUE(source, idempotency_key)
  payload_uri,                -- pointer into GCS
  fetched_at, ingested_at,
  provenance JSONB,
  status                      -- received | processed | unroutable | failed
)

-- durable work queue
work_queue(id, raw_record_id, state, attempts, lease_until, last_error, enqueued_at)

-- silver: extraction output
tags(
  id, raw_record_id, source, observed_at,
  tag_type,                   -- theme | person | org | location | tone
  raw_value,                  -- e.g. "TAX_FNCACT" or "Joe Biden"
  canonical_entity_id,        -- nullable until entity resolution runs
  score JSONB                 -- tone numbers, counts, confidence
)

-- canonical entities (the §6.1 two-tier model + tag→symbol promotion land here)
entities(id, canonical_name, entity_type, aliases JSONB, is_tracked_symbol bool)

-- gold: tracked symbols + time series
tracked_symbols(id, key, kind, entity_id, unit, description)
observations(tracked_symbol_id, ts_bucket, value, sample_count)  -- PK(symbol, ts_bucket)
```

**v0 gold aggregation:** per time bucket (15-min or hourly), per tracked symbol,
compute coverage count and mean tone from `tags`.

| Lives in **GCS** | Lives in **Postgres** |
|---|---|
| Raw GDELT files, scraped HTML/text, large blobs (immutable bronze) | `raw_records`, `work_queue`, `tags`, `entities`, `tracked_symbols`, `observations` |

---

## 7. GDELT reference producer

A standalone script (`producers/gdelt/`) that lives in the repo as the worked
example of an *external* producer — it uses the `ingestion-client`, nothing
privileged:
1. Poll GDELT's update cadence (~15-min GKG batches). **Verify current GDELT data
   products / file URLs against official docs before building** (README §5.2).
2. For each GKG row, build a submission (`source=gdelt.gkg`,
   `content_type=gdelt-gkg-v2`, deterministic `idempotency_key`) and `POST /ingest`.
3. The `gdelt-gkg` **extractor** (inside the worker) parses GDELT's existing
   themes/people/orgs/locations/tone into `tags` rows — **borrowed extraction, no
   bespoke NLP** (§8/D8).

---

## 8. Tech & deployment (GCP, ~$40/mo target)

| Concern | Choice | Approx cost at enthusiast volume |
|---|---|---|
| Language | Python 3.12+ | — |
| API | FastAPI on **Cloud Run** (scales to zero; 2M req/mo free tier) | ~$0 |
| Worker | Cloud Run Job on schedule **or** free-tier `e2-micro` VM | ~$0 |
| Bronze | **GCS** standard (no MinIO) | ~$1–2/mo |
| Postgres | managed serverless (Neon/Supabase free tier) **or** Cloud SQL micro | $0 / $10–15/mo |
| Analytics (later) | BigQuery free tier / DuckDB | ~$0 |

The cost driver is Postgres hosting — see open decision OQ1. Everything else is
≈ free at this scale. Total realistic baseline: **~$2/mo** (serverless PG) to
**~$15/mo** (Cloud SQL micro), comfortably under $40.

**Local dev:** docker-compose with Postgres + a GCS emulator (or fake/local object
store), so dev and prod run the identical code paths.

---

## 9. Testing approach

- **Unit:** each unit tested in isolation against its interface — `bronze-store`,
  `work-queue` (lease/ack/retry semantics), `extractor-registry`, `gdelt-gkg`
  extractor (fixture GKG rows → expected `tags`), gold aggregation.
- **Contract:** `POST /ingest` validation, dedup/idempotency (re-submit → no-op),
  unroutable parking, bronze-first guarantee (payload safe even if enqueue fails).
- **Integration:** end-to-end on local docker-compose — submit a fixture GDELT
  batch via the client → assert rows in `tags` and `observations`.
- **Replay:** re-enqueue a processed record → idempotent re-extraction, no dupes.
- TDD per the project's test-driven-development practice; tests precede
  implementation for each unit.

---

## 10. Out of scope (this epic)

- The **news-vs-price divergence** comparison (README §5.3, v1) remains out of scope.
  **Amended 2026-06-01:** the **structured feed *path*** itself (generic numeric
  normalizer, value time series, `kind`-based worker routing) is **no longer deferred** —
  it is built in the producer-orchestrator epic
  (`docs/specs/2026-06-01-producer-orchestrator-design.md`), with Polymarket as the first
  structured feed. Only the *comparison/divergence analysis* stays out of scope here.
- **Bespoke NLP** beyond borrowed GDELT tags (README §7).
- **Entity resolution / synonym collapse** and the **tag→tracked-symbol promotion
  criterion** (README §6.1) — tables exist; the logic is a later epic.
- **Cross-source entity linking** (README §6.5).
- The **research layer** / experiments and FDR guardrails (README §6.2) — beyond a
  trivial sanity query.
- Streaming stack (Kafka/Redpanda), ClickHouse, OpenSearch/vector index — deferred
  swaps behind existing seams.

---

## 11. Open decisions

- **OQ1 — Postgres hosting:** Neon/Supabase free-tier (~$0, separate vendor,
  portable) vs Cloud SQL micro (~$10–15/mo, GCP-native) vs self-host on free-tier
  VM (~$0, you own ops). *Leaning Neon free-tier for the cheapest start; revisit if
  you want everything GCP-native.*
- **OQ2 — Worker placement:** Cloud Run Job (scales to zero, scheduled) vs always-on
  `e2-micro` VM (free tier, simpler long-running loop).
- **OQ3 — Bucket size:** 15-min (matches GDELT cadence) vs hourly (smaller `observations`).
- **OQ4 — Promotion criterion** for tracked symbols (deferred to a later epic, but
  affects the gold schema): manual, frequency threshold, or analyst-driven.
```
