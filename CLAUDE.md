# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Bellwether is a source-agnostic **observational signal pipeline** (full product vision in `README.md`). It is being built incrementally, one ticket at a time, from the tickets under `docs/plans/tickets/`, organized into `Open/` → `In Progress/` → `Closed/` lifecycle folders (see Conventions). **T00–T17 are complete (in `Closed/`); T18+ build the producer-orchestrator epic** (`docs/specs/2026-06-01-producer-orchestrator-design.md`). Each ticket file is self-contained — spec ref, prerequisites, exact files, interfaces, tests, acceptance criteria — and is the source of truth for its task.

**Currently on `main` (T00–T17 merged):** the full v0 unstructured spine end to end — `config.py`, `db.py`, migrations + the 6-table schema, the GCS bronze store, the `Submission`/`IngestResult` contracts, `enqueue/lease/ack/fail`, `ingest.py` (bronze-first ingest + dedup), `api.py` (FastAPI front door + read API under `/api`), `client.py`, `extractors/` (registry + GDELT GKG), `worker.py` + `gold.py`, the reference `producers/gdelt/`, the live web UI (`web/data/live.py`, `bellweather ui`), the Terraform `infra/` (Neon Postgres + GCS + Cloud Run service/job + Scheduler), the `Dockerfile`/`Caddyfile` single-app packaging, and the GitHub Actions deploy. **Not yet built (T18+):** the producer orchestrator + structured-feed path (this epic). New feature work happens on a **ticket branch**, not directly on `main`.

A **Streamlit web UI** is packaged with the backend at `src/bellweather/web/` (run `make ui`). It is an operator/research surface — view/query data, pipeline status, config — that runs on **mock data** today behind a swappable data-access seam (`bellweather.web.data`), so real read-endpoints can be wired in later without changing the screens. The live-UI build (read API, `live.py`, single-app GCP packaging) is tracked in tickets **T15–T17**. Design: `docs/specs/2026-05-31-ui-prototype-design.md`.

**The README's §8 "Candidate Tech" table is aspirational/long-term (Kafka, ClickHouse, Dagster, MinIO). Do not reach for those.** The v0 stack actually in use is deliberately consolidated and cheap:

- **Python 3.12 + `uv`**, FastAPI, `psycopg` v3 (sync) + `psycopg_pool`, `pydantic` v2, `google-cloud-storage`, `httpx`, `typer`.
- **Postgres is the transactional spine** — it holds the raw-record index, the work queue, silver `tags`/`entities`, and gold `observations`. **GCS holds the immutable raw bytes (bronze).** One paid datastore, by design.
- **Deploy:** Terraform → GCS + **Neon (serverless Postgres)** + Cloud Run service (API + UI) + Cloud Run Job (worker) + Cloud Scheduler. Target cost ≈ **<$40/mo** (Cloud Run scales to zero; Neon free/low tier). Keep changes within that envelope.

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
- **Two ingestion paths, one gold layer.** Unstructured content → extraction → tags → time series. Structured numeric feeds → validate/normalize → time series directly (no NLP). Both converge on a shared time + entity key. v0 on `main` implements only the unstructured/GDELT path; **the structured path + a producer orchestrator are specced and ticketed in the T18+ epic (`docs/specs/2026-06-01-producer-orchestrator-design.md`) — not yet on `main`.**
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

Local config: copy `.env.example` to `.env`. `STORAGE_EMULATOR_HOST` points tests at fake-gcs locally; **unset it in prod** to use real GCS. GCS tests use a `requires_gcs` marker and **auto-skip** when the emulator is unreachable (e.g. you ran `pytest` without `make up`). CI starts fake-gcs explicitly, so those tests run there.

## Conventions (enforced in `AGENTS.md`)

- **One ticket per branch** `ticket/T<NN>-<slug>`, **one PR**, **never merge to `main`** without explicit approval. Branches are stacked in dependency order.
  - **Keep the PR stack stacked.** A ticket PR's **base must be its parent ticket branch**, not `main` (only the bottom of the remaining stack targets `main`, once its parent has merged). `gh pr create` defaults `--base` to `main` and silently flattens the stack — always pass `--base ticket/T<parent>-<slug>`. To retarget an existing PR, `gh pr edit --base` has silently no-op'd here; use REST: `gh api -X PATCH repos/{owner}/{repo}/pulls/{n} -f base="<branch>"`. When merging, delete the merged branch via the **PR page's "Delete branch" button** so GitHub auto-retargets the child onto `main`; deleting it from the branch list instead **closes** the child PR.
- **Ticket lifecycle folders.** Tickets live in `docs/plans/tickets/{Open, In Progress, Closed}/`: `Open/` = specified but not started; `In Progress/` = actively being worked; `Closed/` = complete + merged. **Move the file as the state changes** (`Open → In Progress` when you pick it up, `→ Closed` when done). **Merge gate:** a ticket's contents may be merged to `main` only when it is in `In Progress/` (work underway, not yet done) or `Closed/` (done) — never while it still sits in `Open/`.
- **TDD is required:** failing test first → minimal code to pass → commit. Small, conventional commits (`feat:`/`test:`/`chore:`/`fix:`).
- **`make check` must be green before a ticket is done** — and before stacking the next ticket on top, since failures propagate down a stacked chain.
- **Only `config.py` reads the environment.** Everything else imports `get_settings()`. Don't read `os.environ` elsewhere.
- **DB helpers never commit** (`enqueue/lease/ack/fail` and friends). The caller owns the transaction boundary.

## Gotchas discovered in practice

- **Spelling split is intentional and load-bearing.** The Python package, DB names, and identifiers use `bellweather` (the original misspelling). The product/prose name is **Bellwether** (correct). Don't "fix" the package name — imports, the Docker image, and Cloud SQL all key off `bellweather`.
- **`get_settings()` / `get_pool()` are process-wide `@lru_cache`.** Any test that monkeypatches the environment MUST clear the cache *before and after* — a throwaway `DATABASE_URL` leaking into a later test once caused every DB test to time out resolving a bogus host. An autouse fixture in `tests/conftest.py` now resets the settings cache around every test; keep it.
- **`queue.lease()` reclaims expired leases.** It selects `state in ('pending','leased') and lease_until < now()`, so a job whose worker crashed before `ack`/`fail` is re-leased once its lease window elapses rather than being orphaned. `attempts` increments on every (re)lease, so a job that keeps killing its worker still dead-letters via `fail()`/`max_attempts`. Corollary: the worker's extraction (T11) must be **idempotent**, since a job can legitimately be processed more than once after an expired lease.
- **Canonical doc paths** are `docs/specs/` (design specs) and `docs/plans/` (the build plan + `docs/plans/tickets/`). **Never use a `superpowers/` path prefix or a `superpowers:` skill prefix in repo docs** — plans go in `docs/plans/`, specs in `docs/specs/`. (Plugin skills may default to `docs/superpowers/...` and namespace themselves `superpowers:…`; both are cosmetic plugin defaults this repo overrides — strip them on sight.)
- **CI runs fake-gcs as a `docker run` step**, not a service container — the image needs CLI args (`-scheme http`, `-public-host`) the `services:` block can't pass (it would default to https and be unreachable). The GCS round-trip tests run in CI as a result; the T11 worker end-to-end test will too. If GCS tests start *skipping* in CI, check that the "Start fake GCS server" step is healthy and `STORAGE_EMULATOR_HOST` is set.
