# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Bellwether is a source-agnostic **observational signal pipeline** (full product vision in `README.md`). It is being built incrementally, one ticket at a time, from the tickets under `docs/plans/tickets/` (T00–T14). Each ticket file is self-contained — spec ref, prerequisites, exact files, interfaces, tests, acceptance criteria — and is the source of truth for its task.

**Currently on `main` (T00–T05 merged):** the ingestion core through the durable work queue — `config.py`, `db.py`, migrations + the 6-table schema, the GCS bronze store, the `Submission`/`IngestResult` contracts, and `enqueue/lease/ack/fail`. **Not yet built (T06–T14):** `ingest.py` (bronze-first ingest + dedup), `api.py` (FastAPI front door), `client.py`, `extractors/` (registry + GDELT GKG), `worker.py` + `gold.py`, the reference `producers/gdelt/`, the Terraform `infra/`, and the Dockerfile/deploy pipeline. New feature work happens on a **ticket branch**, not directly on `main`.

**The README's §8 "Candidate Tech" table is aspirational/long-term (Kafka, ClickHouse, Dagster, MinIO). Do not reach for those.** The v0 stack actually in use is deliberately consolidated and cheap:

- **Python 3.12 + `uv`**, FastAPI, `psycopg` v3 (sync) + `psycopg_pool`, `pydantic` v2, `google-cloud-storage`, `httpx`, `typer`.
- **Postgres is the transactional spine** — it holds the raw-record index, the work queue, silver `tags`/`entities`, and gold `observations`. **GCS holds the immutable raw bytes (bronze).** One paid datastore, by design.
- **Deploy:** Terraform → GCS + Cloud SQL (Postgres `db-f1-micro`) + Cloud Run service (API) + Cloud Run Job (worker) + Cloud Scheduler. Target cost ≈ **<$40/mo** (Cloud Run scales to zero; Cloud SQL micro dominates). Keep changes within that envelope.

Authoritative design: `docs/specs/2026-05-31-ingestor-extractor-design.md`. Build plan, locked module layout, and dependency graph: `docs/plans/2026-05-31-ingestor-extractor.md`. Conventions for autonomous workers: `AGENTS.md`.

## Architecture (the big picture)

Everything that *collects* data (scrapers, LLM agents, file loads, the reference GDELT producer) is **external** to Bellwether — the only coupling is the HTTP ingestion contract. The end-to-end flow:

```
external producer ──POST /ingest──▶ ingest_record()            [T06/T07]
        │                               │
        │            bronze-first: write raw envelope to GCS (immutable)
        │                               │
        │            index + dedup in Postgres raw_records (UNIQUE(source, idempotency_key))
        │                               │
        │            enqueue in Postgres work_queue
        ▼                               ▼
   (nothing internal)           worker (Cloud Run Job, scheduled)     [T11]
                                        │  lease FOR UPDATE SKIP LOCKED
                                        │  route by content_type → Extractor (registry)
                                        ▼
                          silver tags  +  gold observations
```

Invariants that span multiple files — internalize these before editing:

- **Bronze-first immutability.** The raw payload is written to GCS *before* anything else and never mutated; extraction is replayable from bronze. GCS writes use `if_generation_match=0` (idempotent re-capture).
- **Two ingestion paths, one gold layer.** Unstructured content → extraction → tags → time series. Structured numeric feeds → validate/normalize → time series directly (no NLP). Both converge on a shared time + entity key. v0 implements only the unstructured/GDELT path.
- **Durable Postgres work queue, not Kafka.** `queue.py`'s `enqueue/lease/ack/fail` use `SELECT … FOR UPDATE SKIP LOCKED`; jobs dead-letter to `failed` after `max_attempts`. This is the deliberate swap-point if a real broker is ever needed.
- **"Borrowed extraction" for GDELT.** v0 does no bespoke NLP — it parses GDELT GKG's already-extracted themes/persons/orgs/locations/tone into tags. Extractors live behind a registry keyed by `content_type`.

Six-table Postgres spine (`src/bellweather/migrations/0001_initial.sql`): `raw_records`, `work_queue`, `entities`, `tags`, `tracked_symbols`, `observations`. Medallion shape: bronze (GCS + `raw_records`) → silver (`tags`/`entities`) → gold (`observations`). The locked file-ownership map (which module/ticket owns what) is the **"Module layout" section of `docs/plans/2026-05-31-ingestor-extractor.md`** — consult it instead of guessing where a responsibility belongs.

## Commands

Everything goes through `make` (which wraps `uv`):

```bash
make dev      # uv sync — install deps (incl. dev group)
make up       # docker compose up -d → Postgres 16 + fake-gcs-server (REQUIRED before DB/GCS tests)
make down     # docker compose down -v → tears down AND wipes volumes (clean DB)
make migrate  # uv run bellweather migrate → apply forward-only SQL migrations
make lint     # ruff check .
make fmt      # ruff format .
make test     # uv run pytest
make check    # ruff check . && ruff format --check . && pytest  ← the CI gate; a ticket is "done" only when this is green
```

Run a single test (`make up` must be running for anything touching Postgres or GCS):

```bash
uv run pytest tests/test_queue.py::test_lease_skips_already_leased -v
```

Local config: copy `.env.example` to `.env`. `STORAGE_EMULATOR_HOST` points tests at fake-gcs locally; **unset it in prod** to use real GCS. GCS tests use a `requires_gcs` marker and **auto-skip** when the emulator is unreachable, so CI stays green without fake-gcs.

## Conventions (enforced in `AGENTS.md`)

- **One ticket per branch** `ticket/T<NN>-<slug>`, **one PR**, **never merge to `main`** without explicit approval. Branches are stacked in dependency order.
- **TDD is required:** failing test first → minimal code to pass → commit. Small, conventional commits (`feat:`/`test:`/`chore:`/`fix:`).
- **`make check` must be green before a ticket is done** — and before stacking the next ticket on top, since failures propagate down a stacked chain.
- **Only `config.py` reads the environment.** Everything else imports `get_settings()`. Don't read `os.environ` elsewhere.
- **DB helpers never commit** (`enqueue/lease/ack/fail` and friends). The caller owns the transaction boundary.

## Gotchas discovered in practice

- **Spelling split is intentional and load-bearing.** The Python package, DB names, and identifiers use `bellweather` (the original misspelling). The product/prose name is **Bellwether** (correct). Don't "fix" the package name — imports, the Docker image, and Cloud SQL all key off `bellweather`.
- **`get_settings()` / `get_pool()` are process-wide `@lru_cache`.** Any test that monkeypatches the environment MUST clear the cache *before and after* — a throwaway `DATABASE_URL` leaking into a later test once caused every DB test to time out resolving a bogus host. An autouse fixture in `tests/conftest.py` now resets the settings cache around every test; keep it.
- **`queue.lease()` does not yet reclaim expired leases** — it only selects `state='pending'`, so a worker that crashes mid-job orphans its row (`lease_until` expiry is currently a no-op). Widen the predicate to also pick up `state='leased' AND lease_until < now()` when building the worker (T11), before shipping it.
- **Doc-path drift:** the plan and ticket files internally reference `docs/superpowers/plans/...` and `docs/superpowers/specs/...`, but the real locations are `docs/plans/...` and `docs/specs/...`. Use the real paths.
- **CI intentionally omits fake-gcs** (the service-container entrypoint override is finicky in Actions), so the GCS round-trip — and later the worker end-to-end test — *skip* in CI rather than run. Before the worker ticket (T11) lands, start the emulator in-session so those paths get real coverage.
