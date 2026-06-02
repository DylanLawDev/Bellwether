# T11 — Worker lease loop + gold aggregation

**Spec:** §5 processing model, §6.2 gold aggregation, D6.
**Depends on:** T05, T10 (and uses T03, T09). **Branch:** `ticket/T11-worker-gold`.
**PR, do not merge without approval.**

## Goal
The worker that turns queued raw records into silver `tags` and gold `observations`. Closes the end-to-end loop: ingest → bronze → queue → **worker** → tags + observations.

## Files
- Create: `src/bellweather/worker.py`, `src/bellweather/gold.py`
- Test: `tests/test_worker.py`, `tests/test_gold.py`
- Import side-effect: `worker.py` must `import bellweather.extractors.gdelt_gkg` so it registers.

## Interfaces (referenced by exact name in T07 CLI)
```python
# gold.py
def bucket_ts(when: datetime, granularity: str) -> datetime: ...   # floor to 'hour' or '15min'
def upsert_coverage(conn, source: str, tag_type: str, raw_value: str, observed_at: datetime) -> None:
    """Ensure a tracked_symbol exists for this tag and increment its coverage count for the bucket."""

# worker.py
def process_job(conn, job: Job) -> None: ...    # extract, write tags, update gold, set raw_records.status
def run_worker(once: bool = False) -> None: ... # lease loop; once=True drains one batch and returns
```

## Gold model for v0
- A tracked symbol is auto-created per `(tag_type, raw_value)` with `key = f"{tag_type}:{raw_value}"`, `kind="coverage"`.
- `observations.value` = count of tags in that bucket; `sample_count` mirrors it. Bucket granularity from `Settings.bellweather_obs_bucket`.
- (Tone aggregation/mean is deferred — v0 ships coverage counts. The `tone` tag is still written to `tags`, just not aggregated yet. Note this in the PR.)

## Steps

- [ ] **Step 1: Failing test for bucketing** `tests/test_gold.py`
```python
from datetime import datetime, timezone
from bellweather.gold import bucket_ts

def test_bucket_hour():
    t = datetime(2026,5,31,14,47,12,tzinfo=timezone.utc)
    assert bucket_ts(t, "hour") == datetime(2026,5,31,14,0,tzinfo=timezone.utc)

def test_bucket_15min():
    t = datetime(2026,5,31,14,47,12,tzinfo=timezone.utc)
    assert bucket_ts(t, "15min") == datetime(2026,5,31,14,45,tzinfo=timezone.utc)
```
- [ ] **Step 2: Run** → FAIL. Implement `bucket_ts` + `upsert_coverage` in `gold.py`:
```python
from datetime import datetime, timedelta

def bucket_ts(when: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return when.replace(minute=0, second=0, microsecond=0)
    if granularity == "15min":
        minute = (when.minute // 15) * 15
        return when.replace(minute=minute, second=0, microsecond=0)
    raise ValueError(f"unknown granularity {granularity}")

def upsert_coverage(conn, source, tag_type, raw_value, observed_at):
    from bellweather.config import get_settings
    key = f"{tag_type}:{raw_value}"
    sym_id = conn.execute(
        """insert into tracked_symbols(key, kind) values(%s,'coverage')
           on conflict (key) do update set key=excluded.key returning id""",
        (key,),
    ).fetchone()[0]
    bucket = bucket_ts(observed_at, get_settings().bellweather_obs_bucket)
    conn.execute(
        """insert into observations(tracked_symbol_id, ts_bucket, value, sample_count)
           values (%s,%s,1,1)
           on conflict (tracked_symbol_id, ts_bucket)
           do update set value = observations.value + 1,
                         sample_count = observations.sample_count + 1""",
        (sym_id, bucket),
    )
```
- [ ] **Step 3: Run** → PASS for gold. Commit (`feat: add gold bucketing and coverage upsert`).

- [ ] **Step 4: Failing end-to-end test** `tests/test_worker.py` (requires `make up` + GCS)
```python
from datetime import datetime, timezone
import pytest
from bellweather.migrate import apply_migrations
from bellweather.contracts import Submission
from bellweather.ingest import ingest_record
from bellweather.worker import run_worker
from bellweather.db import get_conn
from tests.conftest import requires_gcs

@pytest.fixture(autouse=True)
def _m(): apply_migrations()

@requires_gcs
def test_ingest_then_worker_creates_tags_and_observations():
    sub = Submission(source="gdelt.gkg", kind="unstructured", content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026,5,31,14,15,tzinfo=timezone.utc), idempotency_key="wk-1",
        payload={"v2_themes":"ECON_STOCKMARKET;TAX_FNCACT","v2_persons":"Jerome Powell",
                 "v2_organizations":"","v2_locations":"","v15_tone":"-2.13,0",
                 "date":"2026-05-31T14:15:00Z"})
    r = ingest_record(sub); assert r.status == "created"
    run_worker(once=True)
    with get_conn() as c:
        ntags = c.execute("select count(*) from tags where raw_record_id=%s", (r.raw_record_id,)).fetchone()[0]
        nobs = c.execute(
            "select count(*) from observations o join tracked_symbols s on s.id=o.tracked_symbol_id "
            "where s.key='theme:ECON_STOCKMARKET'").fetchone()[0]
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[0]
    assert ntags >= 3 and nobs == 1 and st == "processed"
```
- [ ] **Step 5: Run** → FAIL. Implement `worker.py`:
```python
import time
import bellweather.extractors.gdelt_gkg  # noqa: F401  (registers the extractor)
from bellweather.db import get_conn
from bellweather.queue import Job, lease, ack, fail
from bellweather.storage import get_bronze_store
from bellweather.extractors import get_extractor
from bellweather.gold import upsert_coverage

def process_job(conn, job: Job) -> None:
    row = conn.execute(
        "select source, content_type, payload_uri, fetched_at from raw_records where id=%s",
        (job.raw_record_id,),
    ).fetchone()
    source, content_type, payload_uri, fetched_at = row
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

def run_worker(once: bool = False) -> None:
    while True:
        with get_conn() as conn:
            jobs = lease(conn, limit=20)
            conn.commit()
            for job in jobs:
                try:
                    process_job(conn, job)
                    conn.commit()
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    with get_conn() as c2:
                        fail(c2, job.id, str(e)); c2.commit()
        if once:
            return
        if not jobs:
            time.sleep(2)
```
> Add `from psycopg.types.json import Jsonb` at the top of `worker.py`.
- [ ] **Step 6: Run** → PASS. Commit (`feat: add worker loop and end-to-end extraction`).

## Acceptance criteria
- `ingest_record` → `run_worker(once=True)` produces `tags` and coverage `observations`; `raw_records.status='processed'`.
- A throwing extractor routes the job through `fail()` (retry/dead-letter), never crashes the loop.
- Worker registers `gdelt-gkg-v2` via import side-effect.
- **Wiring check:** confirm T06 `KNOWN_CONTENT_TYPES` still routes `gdelt-gkg-v2`. If you prefer, switch `KNOWN_CONTENT_TYPES` to call `known_content_types()` after importing the extractors package — note this in the PR if you change it.
