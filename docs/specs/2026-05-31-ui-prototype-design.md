# Bellwether UI Prototype — Design

| | |
|---|---|
| **Status** | Approved (prototype) |
| **Date** | 2026-05-31 |
| **Owner** | Dylan |

## Purpose

A local, browser-based front end for Bellwether that lets a user **view/query data**,
inspect **pipeline status**, and review **configuration**. This is a **prototype**: it
runs against **mock data** today and is structured so that real FastAPI read-endpoints
can be wired in later **without changing any screen code**.

This is research/operator surface, separate from the shipped ingestion pipeline. It is
not part of the API service or the worker job.

## Non-goals

- No real database or API access in the prototype (mock data only).
- No persistence of configuration changes (the Settings "Save" echoes the would-be
  payload; real config writes are later work).
- No authentication, deployment, or multi-user concerns.
- Streamlit pages are not unit-tested (they are a prototype surface).

## Stack

- **Streamlit** + **pandas** (pandas pulls numpy). Added as an optional `ui` dependency
  group (pandas is also in `dev` because the tests import it), not a runtime dependency
  of the ingestion API/worker.
- Launched via `make ui` → `uv run --group ui streamlit run src/bellweather/web/app.py`.

## Location

The UI is **packaged with the backend** at `src/bellweather/web/`, so it ships inside
the installable `bellweather` package and can later be deployed as a single GCP app
(tracked in T17). Because `bellweather` is importable, pages use ordinary
`from bellweather.web import …` imports — no `sys.path` manipulation.

## The "endpoints after" seam

The screens must never know whether data is mock or live. One data-access module is the
only seam:

```
src/bellweather/web/
  __init__.py
  app.py                # entrypoint + landing/overview (streamlit run …/web/app.py)
  pages/                # Streamlit multipage convention (auto sidebar nav)
    1_Dashboard.py
    2_Explorer.py
    3_Pipeline.py
    4_Settings.py
  data/
    __init__.py         # exports the data API; selects mock vs live by env flag
    source.py           # the interface: function signatures + return-shape contract
    mock.py             # deterministic, schema-accurate fixtures (GDELT-flavored)
    live.py             # STUB: same signatures, httpx calls to /api/... (T15/T16)
  analysis.py           # pure helpers (anomaly flagging) — unit-tested
```

- Every page imports **only** from `bellweather.web.data` (which re-exports the active backend).
- Backend selected by `BELLWEATHER_UI_SOURCE=mock|live` (default `mock`).
- `live.py` ships as a stub raising `NotImplementedError`; filling it in + the matching
  read-endpoints is the **T15/T16** work. Flipping the flag is the only change at that point.

### Return-shape contract

Tabular results are pandas `DataFrame`s with fixed columns (so `live.py` can build the
same frames from API JSON); aggregates are plain dicts/lists.

| Function | Returns | Columns / keys |
|---|---|---|
| `get_tracked_symbols()` | DataFrame | `id, key, tag_type, raw_value, kind, latest_value, total_samples` |
| `get_observations(keys, start, end)` | DataFrame | `ts_bucket, key, value, sample_count` |
| `query_raw_records(filters)` | DataFrame | `id, source, kind, content_type, idempotency_key, status, fetched_at, payload_uri` |
| `query_tags(filters)` | DataFrame | `id, raw_record_id, source, tag_type, raw_value, observed_at, score` |
| `get_queue_stats()` | dict | `pending, leased, done, failed` |
| `get_worker_runs()` | DataFrame | `run_at, leased, processed, failed, duration_s` |
| `get_ingestion_rate()` | DataFrame | `hour, records` |
| `get_settings_view()` | list[dict] | `key, value, note` (mirrors `bellweather.config.Settings` fields) |

## Mock data

Deterministic (seeded) so the prototype looks identical on every launch:

- **Tracked symbols** keyed `theme:ECON_STOCKMARKET`, `person:jerome powell`,
  `org:federal reserve`, `location:Ukraine`, etc.; `kind='coverage'`.
- **Observations**: ~14 days of hourly buckets per symbol with a couple of injected
  spikes so anomaly flags have something to catch.
- **Raw records**: `source='gdelt.gkg'`, `kind='unstructured'`, `content_type='gdelt-gkg-v2'`
  (the values the real producer/extractor write, so live mode shows the same rows).
- **Tags**: `tag_type ∈ {theme, person, org, location, tone}`.
- **Queue stats / worker runs / ingestion rate**: representative operational numbers.

Shapes match the six-table Postgres schema (`migrations/0001_initial.sql`) so swapping in
real data is a backend change only.

## Screens

- **Dashboard** — select tracked symbols → time-series line chart of coverage; top-movers
  table; anomaly markers (bucket value > mean + 3σ via `analysis.flag_anomalies`).
- **Explorer** — tabs over raw_records / tags / observations; filters (source, tag_type,
  value substring, time range); paginated table + row detail.
- **Pipeline** — queue-state metric cards, recent worker-run table, ingestion rate chart.
- **Settings** — config fields rendered as a form; "Save" echoes the payload (no write).

## Testing

- `tests/test_web_mock.py` — asserts each mock function returns the contracted columns/keys
  and non-empty, well-typed data.
- `tests/test_web_analysis.py` — asserts `flag_anomalies` flags injected spikes and not flat
  series.
- Pages are not imported by `pytest`; they are smoke-tested with Streamlit's `AppTest`
  (run manually under the `ui` group, kept out of `make check` so the gate has no Streamlit
  dependency). `make check` stays green.

## From prototype to live (tracked work)

This spec covers the mock prototype. Turning it into a live, single-deployable app is
three follow-on tickets:

- **T15 — Read API**: GET endpoints on the FastAPI app returning the shapes in the contract
  table above, backed by Postgres.
- **T16 — Live backend**: implement `bellweather.web.data.live` against those endpoints; a
  `bellweather ui` CLI command to serve the UI; flip `BELLWEATHER_UI_SOURCE=live` for prod.
- **T17 — Single-app packaging**: one Cloud Run service serving both the UI and the API
  (extends the T14 Dockerfile + T13 Terraform).

## Documentation updates (done with this work)

1. `CLAUDE.md` — correct the stale "Currently on `main`" status (T06–T12 are merged) and
   note the `src/bellweather/web/` surface + T15–T17.
2. `README.md` — add the operator/research surface to the architecture description.
3. This spec at `docs/specs/2026-05-31-ui-prototype-design.md`.
