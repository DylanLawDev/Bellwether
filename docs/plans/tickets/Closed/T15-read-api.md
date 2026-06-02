# T15 — Read API: GET endpoints for the web UI

**Spec:** `docs/specs/2026-05-31-ui-prototype-design.md` (the "endpoints after" seam + return-shape contract); README §4 (research surface).
**Depends on:** T07 (FastAPI app), T11 (gold `observations`). **Branch:** `ticket/T15-read-api`. **PR, do not merge without approval.**

## Goal
Give the FastAPI app a **read surface** that returns exactly the shapes the web UI's
data contract (`bellweather.web.data.source`) expects, so `web.data.live` (T16) can be a
thin HTTP client. The ingestion API is write-only today; this adds query endpoints over
the existing Postgres spine. **Reads only — no schema changes to the six tables.**

## Files
- Create: `src/bellweather/reads.py` — pure query functions (take a conn, return plain
  dicts/lists; **never commit**, caller owns the transaction, per repo convention).
- Modify: `src/bellweather/api.py` — add a `/api` router exposing the reads as GET routes
  with Pydantic response models.
- Test: `tests/test_reads.py` (query functions against a seeded DB), `tests/test_api_reads.py`
  (endpoints via `TestClient`). Both require `make up` + `apply_migrations()`.

## Endpoints (all under `/api`, all GET)
| Route | Query params | Returns (JSON) |
|---|---|---|
| `/api/symbols` | — | `[{id, key, tag_type, raw_value, kind, latest_value, total_samples}]` |
| `/api/observations` | `keys` (repeatable), `start?`, `end?` | `[{ts_bucket, key, value, sample_count}]` |
| `/api/records` | `source?`, `content_type?`, `status?`, `search?`, `start?`, `end?`, `limit=100`, `offset=0` | `[{id, source, kind, content_type, idempotency_key, status, fetched_at, payload_uri}]` |
| `/api/tags` | `tag_type?`, `search?`, `start?`, `end?`, `limit=100`, `offset=0` | `[{id, raw_record_id, source, tag_type, raw_value, observed_at, score}]` |
| `/api/queue` | — | `{pending, leased, done, failed}` |
| `/api/ingestion-rate` | `hours=48` | `[{hour, records}]` (raw_records bucketed by hour) |
| `/api/config` | — | `[{key, value, note}]` — **redacted** view of `Settings` (mask `database_url`; never return secrets) |

Derivations:
- `symbols.latest_value` / `total_samples`: join `tracked_symbols` → `observations`
  (latest bucket value; `sum(value)`). `key`’s `tag_type`/`raw_value` split on the first `:`.
- `observations.key`: from `tracked_symbols.key` via the join.
- `queue`: `select state, count(*) from work_queue group by state` (zero-fill missing states).
- `ingestion-rate`: `date_trunc('hour', fetched_at)` count over the window.

> **Out of scope / known gap:** the mock UI has a *worker-runs* table that has **no backing
> table** in the schema. T15 does **not** invent one. The live Pipeline screen (T16) shows
> queue + ingestion-rate; worker-run history is deferred (would need a small `worker_runs`
> migration — call it out, don't stub it).

## Steps
- [ ] **Step 1: Failing tests** `tests/test_reads.py` — seed a couple `raw_records`,
  `tracked_symbols`, `observations`, `tags`, `work_queue` rows; assert each `reads.*`
  function returns the contracted keys, correct filtering, and `limit/offset` paging.
- [ ] **Step 2:** `tests/test_api_reads.py` — hit each route via `TestClient`; assert status
  200, JSON shape matches, and `/api/config` masks `database_url`.
- [ ] **Step 3: Run → FAIL.**
- [ ] **Step 4: Implement `reads.py`** — one function per endpoint, parameterized SQL
  (psycopg, `%s` placeholders), returning dicts via `row_factory=dict_row`.
- [ ] **Step 5: Implement the `/api` router** in `api.py` with Pydantic response models
  (`SymbolRow`, `ObservationRow`, …). Open a conn with `get_conn()` per request.
- [ ] **Step 6: Run → PASS.** Commit (`feat: add read API for the web UI`).

## Acceptance criteria
- Every route returns the exact keys in the contract table; filters + paging work.
- `/api/config` never leaks `database_url` or other secrets.
- No migrations; no changes to write paths. `make check` green (DB/GCS tests via `make up`).
- Shapes verified to match `bellweather.web.data.source` column constants (cross-check in a test).
