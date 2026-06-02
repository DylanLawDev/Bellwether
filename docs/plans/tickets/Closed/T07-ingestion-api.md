# T07 — FastAPI `POST /ingest` (+ batch), `/healthz`, CLI

**Spec:** §3 ingestion-api, §4 contract, D3 (HTTP front door).
**Depends on:** T06. **Branch:** `ticket/T07-ingestion-api`. **PR, do not merge without approval.**

## Goal
Expose the ingestion core over HTTP and wire the `bellweather` CLI (api/worker/migrate commands). This is the external front door producers hit.

## Files
- Create: `src/bellweather/api.py`
- Modify/Create: `src/bellweather/cli.py` (replace the T00 stub)
- Test: `tests/test_api.py`

## Endpoints
- `POST /ingest` — body: a single `Submission`. Returns `200` + `IngestResult`.
- `POST /ingest/batch` — body: `{"records": [Submission, ...]}`. Returns `{"results": [IngestResult, ...]}`. Per-record isolation: one bad/duplicate record does not fail the batch.
- `GET /healthz` — returns `{"status": "ok"}` after a `select 1` against the DB.

## Steps

- [ ] **Step 1: Failing tests** `tests/test_api.py` (uses FastAPI `TestClient`; requires `make up` + GCS)
```python
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from bellweather.migrate import apply_migrations
from bellweather.api import app
from tests.conftest import requires_gcs

client = TestClient(app)

@pytest.fixture(autouse=True)
def _m(): apply_migrations()

def _rec(key):
    return dict(source="gdelt.gkg", kind="unstructured", content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026,5,31,14,15,tzinfo=timezone.utc).isoformat(),
        idempotency_key=key, payload={"a": 1})

def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}

@requires_gcs
def test_ingest_single_created_then_duplicate():
    r1 = client.post("/ingest", json=_rec("api-1")); assert r1.status_code == 200
    assert r1.json()["status"] == "created"
    r2 = client.post("/ingest", json=_rec("api-1"))
    assert r2.json()["status"] == "duplicate"

@requires_gcs
def test_ingest_batch_isolates_records():
    body = {"records": [_rec("api-b1"), _rec("api-b1"), _rec("api-b2")]}  # 2nd is dup of 1st
    res = client.post("/ingest/batch", json=body).json()["results"]
    statuses = [r["status"] for r in res]
    assert statuses == ["created", "duplicate", "created"]

def test_ingest_validation_error_is_422():
    bad = _rec("api-x"); bad["kind"] = "weird"
    assert client.post("/ingest", json=bad).status_code == 422
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `api.py`**
```python
from fastapi import FastAPI
from pydantic import BaseModel
from bellweather.contracts import Submission, IngestResult
from bellweather.ingest import ingest_record
from bellweather.db import get_conn

app = FastAPI(title="Bellweather Ingestion")

class BatchRequest(BaseModel):
    records: list[Submission]

class BatchResponse(BaseModel):
    results: list[IngestResult]

@app.get("/healthz")
def healthz():
    with get_conn() as conn:
        conn.execute("select 1")
    return {"status": "ok"}

@app.post("/ingest", response_model=IngestResult)
def ingest(sub: Submission):
    return ingest_record(sub)

@app.post("/ingest/batch", response_model=BatchResponse)
def ingest_batch(req: BatchRequest):
    return BatchResponse(results=[ingest_record(s) for s in req.records])
```
- [ ] **Step 4: Implement `cli.py`** (replaces the T00 stub)
```python
import typer, uvicorn
from bellweather.migrate import apply_migrations

app = typer.Typer(help="Bellweather")

@app.command()
def migrate():
    applied = apply_migrations()
    typer.echo(f"applied: {applied}")

@app.command()
def api(host: str = "0.0.0.0", port: int = 8000):
    uvicorn.run("bellweather.api:app", host=host, port=port)

@app.command()
def worker(once: bool = False):
    from bellweather.worker import run_worker   # imported lazily; implemented in T11
    run_worker(once=once)
```
- [ ] **Step 5: Run** → PASS. Commit (`feat: add ingestion HTTP API and CLI`).

## Acceptance criteria
- `POST /ingest` returns `IngestResult`; duplicates reported as `duplicate`; invalid bodies → `422`.
- Batch endpoint isolates per-record outcomes.
- `bellweather migrate` and `bellweather api` work; `bellweather worker` imports lazily (no failure if T11 not yet merged, as long as you don't invoke it).
