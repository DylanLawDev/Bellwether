# T06 — `ingest_record()` — bronze-first + dedup + enqueue

**Spec:** §4 (bronze-first, idempotency, routing/unroutable), §5 step 1.
**Depends on:** T03, T04, T05. **Branch:** `ticket/T06-ingest-core`. **PR, do not merge without approval.**

## Goal
The core ingestion function used by the API. Enforces the spec's central guarantee: **the immutable bronze capture is written before any fallible step, and nothing is ever dropped.**

## Files
- Create: `src/bellweather/ingest.py`
- Test: `tests/test_ingest.py` (requires `make up` + GCS emulator)

## Interface (referenced by exact name in T07)
```python
# ingest.py
KNOWN_CONTENT_TYPES: set[str]      # for now {"gdelt-gkg-v2"}; extended as extractors are added
def ingest_record(sub: Submission) -> IngestResult: ...
```

## Ordering (MUST be exactly this — the bronze-first guarantee)
1. Build the **envelope** = `sub.model_dump(mode="json")` and write it to bronze (GCS) **first**, when `payload` is inline. If the producer supplied `payload_uri`, trust it as the bronze pointer (don't re-upload).
2. Open a DB transaction. Insert into `raw_records`. On unique-violation `(source, idempotency_key)` → it's a **duplicate**: roll back, look up the existing row, return `status="duplicate"`.
3. Decide routability: if `content_type not in KNOWN_CONTENT_TYPES` → set `raw_records.status='unroutable'`, **do not enqueue**, return `status="unroutable"`. (Data is safe in bronze; replayable later.)
4. Otherwise `enqueue(conn, raw_record_id)` and commit. Return `status="created"`.

## Steps

- [ ] **Step 1: Failing tests** `tests/test_ingest.py`
```python
from datetime import datetime, timezone
import pytest
from bellweather.migrate import apply_migrations
from bellweather.contracts import Submission
from bellweather.ingest import ingest_record
from bellweather.db import get_conn
from tests.conftest import requires_gcs

@pytest.fixture(autouse=True)
def _m(): apply_migrations()

def _sub(key, content_type="gdelt-gkg-v2", payload={"a": 1}):
    return Submission(source="gdelt.gkg", kind="unstructured", content_type=content_type,
        fetched_at=datetime(2026,5,31,14,15,tzinfo=timezone.utc), idempotency_key=key, payload=payload)

@requires_gcs
def test_created_writes_bronze_and_enqueues():
    r = ingest_record(_sub("k-created-1"))
    assert r.status == "created" and r.payload_uri.startswith("gs://")
    with get_conn() as c:
        q = c.execute("select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)).fetchone()[0]
        rr = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[0]
    assert q == 1 and rr == "received"

@requires_gcs
def test_duplicate_is_noop():
    r1 = ingest_record(_sub("k-dup"))
    r2 = ingest_record(_sub("k-dup"))
    assert r2.status == "duplicate" and r2.raw_record_id == r1.raw_record_id
    with get_conn() as c:
        n = c.execute("select count(*) from work_queue where raw_record_id=%s", (r1.raw_record_id,)).fetchone()[0]
    assert n == 1  # not enqueued twice

@requires_gcs
def test_unknown_content_type_is_unroutable_not_enqueued():
    r = ingest_record(_sub("k-unr", content_type="mystery-v9"))
    assert r.status == "unroutable"
    with get_conn() as c:
        n = c.execute("select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)).fetchone()[0]
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[0]
    assert n == 0 and st == "unroutable"
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `ingest.py`**
```python
from psycopg.errors import UniqueViolation
from bellweather.contracts import Submission, IngestResult
from bellweather.storage import get_bronze_store
from bellweather.db import get_conn
from bellweather.queue import enqueue

KNOWN_CONTENT_TYPES: set[str] = {"gdelt-gkg-v2"}

def ingest_record(sub: Submission) -> IngestResult:
    # 1. bronze-first
    if sub.payload_uri is not None:
        payload_uri = sub.payload_uri
    else:
        payload_uri = get_bronze_store().put(
            sub.source, sub.fetched_at, sub.idempotency_key, sub.model_dump(mode="json")
        )
    routable = sub.content_type in KNOWN_CONTENT_TYPES
    status = "received" if routable else "unroutable"
    with get_conn() as conn:
        try:
            rid = conn.execute(
                """insert into raw_records
                     (source,kind,content_type,idempotency_key,payload_uri,fetched_at,provenance,status)
                   values (%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (sub.source, sub.kind, sub.content_type, sub.idempotency_key,
                 payload_uri, sub.fetched_at, Jsonb(sub.provenance), status),
            ).fetchone()[0]
        except UniqueViolation:
            conn.rollback()
            existing = conn.execute(
                "select id, payload_uri from raw_records where source=%s and idempotency_key=%s",
                (sub.source, sub.idempotency_key),
            ).fetchone()
            return IngestResult(raw_record_id=existing[0], status="duplicate", payload_uri=existing[1])
        if routable:
            enqueue(conn, rid)
        conn.commit()
    return IngestResult(
        raw_record_id=rid, status=("created" if routable else "unroutable"), payload_uri=payload_uri
    )
```
> Add the import `from psycopg.types.json import Jsonb` at the top.
- [ ] **Step 4: Run** → PASS. Commit (`feat: add bronze-first ingest_record with dedup and routing`).

## Acceptance criteria
- Bronze object is written before the DB insert; a `created` result is enqueued exactly once.
- Re-submitting the same `(source, idempotency_key)` returns `duplicate` and does NOT enqueue again.
- Unknown `content_type` → `raw_records.status='unroutable'`, no queue row, data preserved in bronze.
