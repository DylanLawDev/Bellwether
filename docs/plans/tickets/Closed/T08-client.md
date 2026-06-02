# T08 — Thin `BellwetherClient` (httpx)

**Spec:** §3 ingestion-client, D3 ("+ a thin Python client").
**Depends on:** T04, T07. **Branch:** `ticket/T08-client`. **PR, do not merge without approval.**

## Goal
A small, dependency-light client so any producer (including the reference GDELT producer in T12) can submit records without hand-rolling HTTP. Safe to retry (server-side idempotency does the dedup).

## Files
- Create: `src/bellweather/client.py`
- Test: `tests/test_client.py` (uses `pytest-httpserver` — no live API needed)

## Interface (referenced by exact name in T12)
```python
# client.py
class BellwetherClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0): ...
    def ingest(self, sub: Submission) -> IngestResult: ...
    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]: ...
```
`base_url` defaults to `Settings.bellweather_api_url`.

## Steps

- [ ] **Step 1: Failing tests** `tests/test_client.py`
```python
from datetime import datetime, timezone
from bellweather.client import BellwetherClient
from bellweather.contracts import Submission

def _sub(key):
    return Submission(source="gdelt.gkg", kind="unstructured", content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026,5,31,14,15,tzinfo=timezone.utc), idempotency_key=key, payload={"a":1})

def test_ingest_posts_and_parses(httpserver):
    httpserver.expect_request("/ingest", method="POST").respond_with_json(
        {"raw_record_id": 7, "status": "created", "payload_uri": "gs://b/x"})
    c = BellwetherClient(base_url=httpserver.url_for(""))
    r = c.ingest(_sub("c1"))
    assert r.raw_record_id == 7 and r.status == "created"

def test_ingest_batch(httpserver):
    httpserver.expect_request("/ingest/batch", method="POST").respond_with_json(
        {"results": [{"raw_record_id": 1, "status": "created", "payload_uri": "gs://b/1"},
                     {"raw_record_id": 1, "status": "duplicate", "payload_uri": "gs://b/1"}]})
    c = BellwetherClient(base_url=httpserver.url_for(""))
    rs = c.ingest_batch([_sub("c1"), _sub("c1")])
    assert [r.status for r in rs] == ["created", "duplicate"]
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `client.py`**
```python
import httpx
from bellweather.config import get_settings
from bellweather.contracts import Submission, IngestResult

class BellwetherClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self._base = (base_url or get_settings().bellweather_api_url).rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def ingest(self, sub: Submission) -> IngestResult:
        resp = self._client.post(f"{self._base}/ingest", json=sub.model_dump(mode="json"))
        resp.raise_for_status()
        return IngestResult.model_validate(resp.json())

    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]:
        body = {"records": [s.model_dump(mode="json") for s in subs]}
        resp = self._client.post(f"{self._base}/ingest/batch", json=body)
        resp.raise_for_status()
        return [IngestResult.model_validate(r) for r in resp.json()["results"]]
```
- [ ] **Step 4: Run** → PASS. Commit (`feat: add BellwetherClient`).

## Acceptance criteria
- `ingest` / `ingest_batch` serialize `Submission` correctly and parse `IngestResult`.
- Tests pass with a mock HTTP server (no DB/GCS/live API required).
