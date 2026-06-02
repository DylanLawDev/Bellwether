# T20 — Worker routes by `kind` → normalizer → gold
**Spec:** docs/specs/2026-06-01-producer-orchestrator-design.md (§6.3 Worker routing). **Depends on:** T18 (`gold.upsert_value`), T19 (normalizer registry + `numeric-series-v1`). **Branch:** ticket/T20-worker-structured-routing. **PR, do not merge without approval.**

## Goal
Teach `worker.process_job` to branch on `raw_records.kind`. Structured records (`kind="structured"`) route through the **normalizer** registry to `gold.upsert_value`; unstructured records keep the existing extractor → tags → coverage path **unchanged**. Also add `"numeric-series-v1"` to `ingest.KNOWN_CONTENT_TYPES` so structured records are marked routable and enqueued — otherwise they park as `unroutable` at ingest time and the worker never sees them. The structured path is idempotent by construction (set-semantics `upsert_value`), so a re-leased job re-sets the same values safely.

## Files
- Modify: `src/bellweather/worker.py` — add `kind` to the `raw_records` SELECT; branch structured → `get_normalizer` → `upsert_value`; import `bellweather.normalizers.numeric_series` (registers) + `get_normalizer` + `upsert_value`.
- Modify: `src/bellweather/ingest.py` — `KNOWN_CONTENT_TYPES = {"gdelt-gkg-v2", "numeric-series-v1"}`.
- Test: `tests/test_worker_structured.py`.

## Interface (locked — from the build plan)
`worker.py` — `process_job` selects `kind` too and branches:
```python
source, kind, content_type, payload_uri, fetched_at = row  # add kind to the SELECT
if kind == "structured":
    n = get_normalizer(content_type)
    if n is None: -> status='unroutable'; ack; return
    for pt in n.normalize(get_bronze_store().get(payload_uri)):
        upsert_value(conn, pt.symbol_key, pt.symbol_kind, pt.ts, pt.value,
                     unit=pt.unit, description=pt.description)
    -> status='processed'; ack; return
# else: existing unstructured/extractor path (unchanged)
```
Import `bellweather.normalizers.numeric_series  # noqa: F401` (registers) + `get_normalizer` + `upsert_value`.

`ingest.py`:
```python
KNOWN_CONTENT_TYPES = {"gdelt-gkg-v2", "numeric-series-v1"}
```

From T18/T19 (already merged on this branch's base):
```python
# gold.py
def upsert_value(conn, symbol_key: str, symbol_kind: str, ts: datetime, value: float,
                 *, unit: str | None = None, description: str | None = None,
                 sample_count: int = 1) -> int: ...   # set-semantics, idempotent; never commits

# normalizers/numeric_series.py — NumericSeriesNormalizer.content_type = "numeric-series-v1"
#   reads envelope["payload"] keys: symbol_key, symbol_kind, unit?, description?, points:[{ts,value}]
#   yields one NormalizedPoint per point (datetime.fromisoformat(ts), float(value))
```

## Steps

> DB + bronze (GCS) round-trip. **`make up`** (Postgres + fake-gcs) and **`make migrate`** must be running; the e2e tests carry the `requires_gcs` marker and auto-skip if the emulator is down.

- [ ] **Step 1: `ingest.py` failing test first.** Add to `tests/test_worker_structured.py` (real code below, full file) a unit assert that `numeric-series-v1` is now known. Run `uv run pytest tests/test_worker_structured.py::test_numeric_series_is_routable -v` → **FAIL** (`KNOWN_CONTENT_TYPES` is `{"gdelt-gkg-v2"}`).

- [ ] **Step 2: Make `numeric-series-v1` routable in `ingest.py`.**
```python
KNOWN_CONTENT_TYPES: set[str] = {"gdelt-gkg-v2", "numeric-series-v1"}
```
Run the unit test → **PASS**. Commit (`feat: mark numeric-series-v1 routable in ingest`).

- [ ] **Step 3: Write the full failing e2e + regression test.** Create `tests/test_worker_structured.py`:
```python
from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.ingest import KNOWN_CONTENT_TYPES, ingest_record
from bellweather.migrate import apply_migrations
from bellweather.worker import run_worker
from tests.conftest import clear_observations, clear_records, requires_gcs

_KEYS = ("st-num-1", "st-unknown-1", "st-gdelt-1")
_SYMBOLS = ("polymarket:demo:yes", "theme:ECON_STOCKMARKET")


def test_numeric_series_is_routable():
    # Unit-level: structured payloads must be enqueued, not parked as unroutable.
    assert "numeric-series-v1" in KNOWN_CONTENT_TYPES


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    with get_conn() as c:
        clear_records(c, "polymarket.demo", _KEYS)
        clear_records(c, "gdelt.gkg", _KEYS)
        clear_observations(c, _SYMBOLS)
        c.commit()


@requires_gcs
def test_structured_record_lands_an_observation():
    sub = Submission(
        source="polymarket.demo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-num-1",
        payload={
            "symbol_key": "polymarket:demo:yes",
            "symbol_kind": "market-probability",
            "unit": "probability",
            "description": "Will X happen by D? (YES)",
            "points": [
                {"ts": "2026-05-31T14:00:00Z", "value": 0.37},
                {"ts": "2026-05-31T15:00:00Z", "value": 0.41},
            ],
        },
    )
    r = ingest_record(sub)
    assert r.status == "created"

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute(
            "select status from raw_records where id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        rows = c.execute(
            "select o.value from observations o"
            " join tracked_symbols s on s.id=o.tracked_symbol_id"
            " where s.key='polymarket:demo:yes' order by o.ts_bucket"
        ).fetchall()
        kind, unit, descr = c.execute(
            "select kind, unit, description from tracked_symbols"
            " where key='polymarket:demo:yes'"
        ).fetchone()
    assert st == "processed"
    assert [v for (v,) in rows] == [0.37, 0.41]  # two hourly buckets, set-semantics
    assert kind == "market-probability"
    assert unit == "probability"
    assert descr == "Will X happen by D? (YES)"


@requires_gcs
def test_unknown_structured_content_type_is_unroutable():
    # Routable=False at ingest -> parked, never enqueued. Belt-and-braces: even if a
    # structured record reached the worker with no normalizer, it must mark
    # unroutable (no data lost), mirroring the unknown-extractor rule.
    sub = Submission(
        source="polymarket.demo",
        kind="structured",
        content_type="mystery-feed-v9",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-unknown-1",
        payload={"anything": 1},
    )
    r = ingest_record(sub)
    assert r.status == "unroutable"  # ingest parked it (not in KNOWN_CONTENT_TYPES)
    with get_conn() as c:
        st = c.execute(
            "select status from raw_records where id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        nq = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
    assert st == "unroutable"
    assert nq == 0  # never enqueued -> the worker is never asked to route it


@requires_gcs
def test_unstructured_path_still_produces_tags():
    # Regression: the gdelt/unstructured branch is unchanged by kind-routing.
    sub = Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-gdelt-1",
        payload={
            "v2_themes": "ECON_STOCKMARKET",
            "v15_tone": "-2.13,0",
            "date": "2026-05-31T14:15:00Z",
        },
    )
    r = ingest_record(sub)
    assert r.status == "created"

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute(
            "select status from raw_records where id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
    assert st == "processed"
    assert ntags >= 1
```
Run `make up && make migrate && uv run pytest tests/test_worker_structured.py -v` → the structured tests **FAIL** (`process_job` selects no `kind` and always calls `get_extractor`, so a structured record raises / never lands an observation); the gdelt regression already PASSes.

- [ ] **Step 4: Implement `worker.py` kind routing.** Add the imports at the top:
```python
import bellweather.normalizers.numeric_series  # noqa: F401  (registers the normalizer)
from bellweather.normalizers import get_normalizer
from bellweather.gold import upsert_coverage, upsert_value
```
Rewrite `process_job` to select `kind` and branch (the unstructured arm below is byte-for-byte the existing code):
```python
def process_job(conn, job: Job) -> None:
    row = conn.execute(
        "select source, kind, content_type, payload_uri, fetched_at"
        " from raw_records where id=%s",
        (job.raw_record_id,),
    ).fetchone()
    source, kind, content_type, payload_uri, fetched_at = row

    if kind == "structured":
        normalizer = get_normalizer(content_type)
        if normalizer is None:
            conn.execute(
                "update raw_records set status='unroutable' where id=%s", (job.raw_record_id,)
            )
            ack(conn, job.id)
            return
        envelope = get_bronze_store().get(payload_uri)
        for pt in normalizer.normalize(envelope):
            upsert_value(
                conn, pt.symbol_key, pt.symbol_kind, pt.ts, pt.value,
                unit=pt.unit, description=pt.description,
            )
        conn.execute(
            "update raw_records set status='processed' where id=%s", (job.raw_record_id,)
        )
        ack(conn, job.id)
        return

    extractor = get_extractor(content_type)
    if extractor is None:
        conn.execute("update raw_records set status='unroutable' where id=%s", (job.raw_record_id,))
        ack(conn, job.id)
        return
    envelope = get_bronze_store().get(payload_uri)
    for t in extractor.extract(envelope):
        conn.execute(
            "insert into tags(raw_record_id, source, observed_at, tag_type, raw_value, score)"
            " values (%s,%s,%s,%s,%s,%s)",
            (job.raw_record_id, source, fetched_at, t.tag_type, t.raw_value, Jsonb(t.score)),
        )
        if t.tag_type != "tone":
            upsert_coverage(conn, source, t.tag_type, t.raw_value, fetched_at)
    conn.execute("update raw_records set status='processed' where id=%s", (job.raw_record_id,))
    ack(conn, job.id)
```
Run `uv run pytest tests/test_worker_structured.py -v` → **PASS** (all four).

- [ ] **Step 5: Full regression.** Run `make up && make migrate && make check` → green (the existing `tests/test_worker.py` unstructured suite and `tests/test_ingest.py` must still pass).

- [ ] **Step 6: Commit** (`feat: route worker by raw_records.kind to normalizer -> gold`).

## Acceptance criteria
- `ingest.KNOWN_CONTENT_TYPES == {"gdelt-gkg-v2", "numeric-series-v1"}`; a `numeric-series-v1` Submission is `created` (routable + enqueued), not `unroutable`.
- `process_job` selects `kind` and branches: `structured` → `get_normalizer` → `upsert_value` per `NormalizedPoint`, then `status='processed'` + ack; a missing normalizer → `status='unroutable'` + ack (no data lost).
- End-to-end (`requires_gcs`): `ingest_record` a `numeric-series-v1` record, `run_worker(once=True)` lands one `observations` row per hourly bucket keyed to a `tracked_symbol` whose `kind`/`unit`/`description` come from the payload.
- Regression: an unstructured gdelt record still produces tags and ends `processed`; the unstructured branch is unchanged.
- `worker.py` imports `bellweather.normalizers.numeric_series` (`# noqa: F401`) so the normalizer is registered at worker start.
- `make check` is green.
