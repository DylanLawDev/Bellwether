# Ingestor + Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Bellwether's source-agnostic ingestion + extraction core — an HTTP front door, immutable GCS bronze store, durable Postgres work queue, pluggable extraction, and gold time-series aggregation — with GDELT as the first reference producer, deployable to GCP via Terraform.

**Architecture:** External producers `POST /ingest` (FastAPI on Cloud Run). Each submission is written **bronze-first** to GCS, indexed + dedup'd in Postgres, and enqueued in a Postgres `work_queue`. A separate worker (Cloud Run Job, scheduled) leases jobs `FOR UPDATE SKIP LOCKED`, routes by `content_type` to an extractor, and writes silver (`tags`) + gold (`observations`). Postgres is the transactional spine; GCS holds raw bytes. Everything before `/ingest` (scrapers, LLM agents, file loads) is external to Bellwether.

**Tech Stack:** Python 3.12, `uv`, FastAPI, `psycopg` v3 (sync) + `psycopg_pool`, `google-cloud-storage`, `pydantic` v2, `httpx`, `pytest` + `ruff`. Local dev via docker-compose (Postgres + fake-gcs-server). Deploy: Terraform → GCS + Cloud SQL (Postgres) + Cloud Run (service + job) + Cloud Scheduler + Artifact Registry. CI/CD: GitHub Actions.

**Spec:** `docs/specs/2026-05-31-ingestor-extractor-design.md`. **Platform doc:** `README.md`.

---

## How to run a ticket from your phone (web Claude Code)

Each ticket under `docs/plans/tickets/` is **self-contained**: it names the spec, prerequisite tickets, exact files, interfaces, tests, and acceptance criteria. To dispatch one:

1. Open the repo in Claude Code on the web.
2. Tell it: *"Read `AGENTS.md`, then implement ticket `docs/plans/tickets/T03-bronze-store.md` end to end: work on a branch, follow TDD, run `make check`, and open a PR. Do not merge."*
3. CI (GitHub Actions) runs `make check` on the PR. Review the diff + CI result from your phone and merge.

**Conventions (also in `AGENTS.md`, created in T00) that every ticket relies on:**
- **Branch:** `ticket/T<NN>-<slug>`. **One PR per ticket.** Never merge to `main` without your approval.
- **Verify:** `make check` = `ruff check . && ruff format --check . && pytest -q`. A ticket is "done" only when `make check` is green.
- **Commits:** small, conventional (`feat:`, `test:`, `chore:`), frequent — commit after each green test.
- **TDD:** failing test first, minimal code to pass, refactor. No implementation without a test that pins it.
- **Don't invent scope:** if a ticket needs something not yet built, it lists it as a prerequisite — do that ticket first, don't stub past it.

---

## Module layout (locked here so tickets stay type-consistent)

```
bellweather/
├── pyproject.toml            # uv project, deps, ruff/pytest config
├── Makefile                  # make dev / up / down / check / migrate / test
├── docker-compose.yml        # Postgres + fake-gcs-server for local dev
├── AGENTS.md                 # conventions for autonomous workers (T00)
├── .github/workflows/
│   ├── ci.yml                # run make check on PRs (T00)
│   └── deploy.yml            # build+push+deploy on merge to main (T13)
├── infra/                    # Terraform (T12)
│   ├── main.tf  variables.tf  outputs.tf  versions.tf
│   └── README.md
├── Dockerfile                # one image; entrypoint switches api/worker (T11/T13)
├── src/bellweather/
│   ├── config.py             # Settings (env-driven)                 [T01]
│   ├── db.py                 # psycopg pool, get_conn()               [T01]
│   ├── storage.py            # BronzeStore (GCS, emulator-aware)      [T03]
│   ├── migrations/           # 0001_initial.sql, ...; runner         [T02]
│   ├── migrate.py            # apply_migrations()                     [T02]
│   ├── contracts.py          # Submission, Kind, IngestResult         [T04]
│   ├── queue.py              # enqueue/lease/ack/fail                 [T05]
│   ├── ingest.py             # ingest_record() bronze-first+dedup     [T06]
│   ├── api.py                # FastAPI app, POST /ingest, /healthz    [T07]
│   ├── client.py             # BellwetherClient (httpx)               [T08]
│   ├── extractors/
│   │   ├── __init__.py       # registry + Extractor protocol         [T09]
│   │   └── gdelt_gkg.py      # GdeltGkgExtractor                      [T10]
│   ├── worker.py             # lease loop, process_job()             [T11]
│   ├── gold.py               # aggregate_observations()              [T11]
│   └── cli.py                # `bellweather` entry (api/worker/...)   [T07/T11]
├── producers/gdelt/          # reference external producer           [T12-prod]
└── tests/                    # mirrors src/, plus tests/fixtures/
```

---

## Build order & dependency graph

```
T00 scaffold ─┬─▶ T01 config/db ─┬─▶ T02 migrations ─┬─▶ T03 bronze ─┐
              │                  │                    ├─▶ T05 queue ──┤
              │                  └─▶ T04 contracts ───┘               ├─▶ T06 ingest ─▶ T07 api ─▶ T08 client
              │                                                       │
              ├─▶ T09 registry ─▶ T10 gdelt-extractor ────────────────┴─▶ T11 worker+gold ─▶ T12 gdelt-producer
              │
              └─▶ T13 terraform/GCP infra  (INDEPENDENT — can be done anytime, in parallel)
                  └─▶ T14 CI/CD deploy wiring (needs T13 + a runnable image from T07/T11)
```

**Recommended sequence:** T00 → T01 → T02 → (T03, T04 in parallel) → T05 → T06 → T07 → T08 → (T09 → T10) → T11 → T12. Run **T13 (Terraform)** early/in-parallel since it's independent — it's the "plug into GCP" piece you asked for. T14 wires deploy once an image exists.

**End-to-end "done" for this epic:** submit a fixture GDELT GKG batch through `BellwetherClient` → rows appear in `tags` and `observations`; `make check` green; deployed stack reachable on Cloud Run with `terraform apply`.

---

## Ticket index

| Ticket | Title | Depends on |
|---|---|---|
| [T00](tickets/T00-scaffold.md) | Repo scaffold, tooling, CI, local docker-compose, `AGENTS.md` | — |
| [T01](tickets/T01-config-db.md) | Settings + Postgres connection pool | T00 |
| [T02](tickets/T02-migrations.md) | Migration runner + initial schema (6 tables) | T01 |
| [T03](tickets/T03-bronze-store.md) | GCS bronze store (emulator-aware) | T01 |
| [T04](tickets/T04-contracts.md) | Ingestion contract models (`Submission`) | T00 |
| [T05](tickets/T05-work-queue.md) | Durable work queue (lease/ack/fail) | T02 |
| [T06](tickets/T06-ingest-core.md) | `ingest_record()` — bronze-first + dedup + enqueue | T03, T04, T05 |
| [T07](tickets/T07-ingestion-api.md) | FastAPI `POST /ingest` (+ batch), `/healthz`, CLI | T06 |
| [T08](tickets/T08-client.md) | Thin `BellwetherClient` (httpx) | T04, T07 |
| [T09](tickets/T09-extractor-registry.md) | Extractor registry + `Extractor` protocol | T04 |
| [T10](tickets/T10-gdelt-extractor.md) | GDELT GKG v2 extractor → `tags` | T09 |
| [T11](tickets/T11-worker-gold.md) | Worker lease loop + gold aggregation | T05, T10 |
| [T12](tickets/T12-gdelt-producer.md) | Reference GDELT producer (external script) | T08, T11 |
| [T13](tickets/T13-terraform-gcp.md) | Terraform: GCS + Cloud SQL + Cloud Run + Scheduler | — (parallel) |
| [T14](tickets/T14-cicd-deploy.md) | Dockerfile + GitHub Actions build/deploy | T07, T11, T13 |

---

## Self-review notes

- **Spec coverage:** ingestion contract (T04), HTTP front door (T07), bronze-first immutability (T06), durable queue (T05), one extractor behind a registry (T09/T10), gold time series (T11), GDELT reference producer (T12), GCP deploy + cost (T13/T14), testing (every ticket TDD). Out-of-scope items (market feed, entity resolution, research layer) are intentionally absent.
- **Type consistency:** shared signatures (`Submission`, `IngestResult`, `BronzeStore.put/get`, `enqueue/lease/ack/fail`, `Extractor.extract`, `aggregate_observations`) are defined once in their owning ticket and referenced by exact name elsewhere.
- **No placeholders:** load-bearing code (schemas, SQL, queue semantics, extractor parsing, Terraform, CI) is given in full inside the tickets; routine glue is specified by signature + test.
