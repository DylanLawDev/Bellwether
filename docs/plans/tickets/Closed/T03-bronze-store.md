# T03 — GCS bronze store (emulator-aware)

**Spec:** §6.1 (GCS bronze), §4 (bronze-first immutability), §6.4 provenance.
**Depends on:** T01. **Branch:** `ticket/T03-bronze-store`. **PR, do not merge without approval.**

## Goal
A tiny, immutable object store over GCS. Writes the full submission envelope as one JSON object keyed by source/date/idempotency-key. Works against fake-gcs locally and real GCS in prod with the same code.

## Files
- Create: `src/bellweather/storage.py`
- Test: `tests/test_storage.py`, `tests/conftest.py` (shared GCS skip-guard)

## Interface (referenced by exact name in T06)
```python
# storage.py
class BronzeStore:
    def __init__(self, bucket: str | None = None): ...
    def object_key(self, source: str, fetched_at: datetime, idempotency_key: str) -> str: ...
    def put(self, source: str, fetched_at: datetime, idempotency_key: str, envelope: dict) -> str:
        """Write envelope JSON immutably; return the gs:// URI. Never overwrites."""
    def get(self, uri: str) -> dict: ...

def get_bronze_store() -> BronzeStore: ...   # cached, uses Settings.bellweather_bucket
```

## Key behaviors
- **Key scheme:** `{source}/{yyyy}/{mm}/{dd}/{idempotency_key}.json`; URI `gs://{bucket}/{key}`.
- **Emulator-aware:** if `Settings.storage_emulator_host` is set, build the client against it with anonymous credentials and ensure the bucket exists; otherwise use default ADC credentials.
- **Immutable:** `put` must not silently overwrite. Use `if_generation_match=0` (create-only); if the object already exists, treat it as success (idempotent re-capture) and return the URI.

## Steps

- [ ] **Step 1: `tests/conftest.py`** — skip GCS tests when the emulator is unreachable
```python
import os, socket, pytest

def _gcs_reachable() -> bool:
    host = os.environ.get("STORAGE_EMULATOR_HOST")
    if not host:
        return False
    netloc = host.split("//", 1)[-1]
    h, _, p = netloc.partition(":")
    try:
        socket.create_connection((h, int(p or 80)), timeout=1).close()
        return True
    except OSError:
        return False

requires_gcs = pytest.mark.skipif(not _gcs_reachable(), reason="GCS emulator not reachable")
```
- [ ] **Step 2: Failing test** `tests/test_storage.py`
```python
from datetime import datetime, timezone
from bellweather.storage import get_bronze_store
from tests.conftest import requires_gcs

@requires_gcs
def test_put_then_get_roundtrip():
    store = get_bronze_store()
    ts = datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc)
    uri = store.put("gdelt.gkg", ts, "key-1", {"hello": "world"})
    assert uri.startswith("gs://")
    assert store.get(uri) == {"hello": "world"}

@requires_gcs
def test_put_is_idempotent_and_key_scheme():
    store = get_bronze_store()
    ts = datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc)
    assert store.object_key("gdelt.gkg", ts, "k") == "gdelt.gkg/2026/05/31/k.json"
    u1 = store.put("gdelt.gkg", ts, "dup", {"a": 1})
    u2 = store.put("gdelt.gkg", ts, "dup", {"a": 1})  # re-capture → no error
    assert u1 == u2
```
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement `storage.py`**
```python
import json
from datetime import datetime
from functools import lru_cache
from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage
from bellweather.config import get_settings

class BronzeStore:
    def __init__(self, bucket: str | None = None):
        s = get_settings()
        self._bucket_name = bucket or s.bellweather_bucket
        if s.storage_emulator_host:
            self._client = storage.Client(
                project="local", credentials=AnonymousCredentials(),
                client_options={"api_endpoint": s.storage_emulator_host},
            )
            b = self._client.bucket(self._bucket_name)
            if not b.exists():
                self._client.create_bucket(self._bucket_name)
        else:
            self._client = storage.Client()
        self._bucket = self._client.bucket(self._bucket_name)

    def object_key(self, source, fetched_at: datetime, idempotency_key: str) -> str:
        return f"{source}/{fetched_at:%Y/%m/%d}/{idempotency_key}.json"

    def put(self, source, fetched_at, idempotency_key, envelope: dict) -> str:
        key = self.object_key(source, fetched_at, idempotency_key)
        blob = self._bucket.blob(key)
        try:
            blob.upload_from_string(
                json.dumps(envelope), content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed:
            pass  # already captured → immutable, treat as success
        return f"gs://{self._bucket_name}/{key}"

    def get(self, uri: str) -> dict:
        key = uri.removeprefix(f"gs://{self._bucket_name}/")
        return json.loads(self._bucket.blob(key).download_as_text())

@lru_cache
def get_bronze_store() -> BronzeStore:
    return BronzeStore()
```
- [ ] **Step 5: Run** → PASS. Commit (`feat: add immutable GCS bronze store`).

## Acceptance criteria
- Round-trip put/get works against fake-gcs; key scheme matches `{source}/{Y}/{m}/{d}/{key}.json`.
- Re-`put` of the same key does not error and returns the same URI (immutability honored).
- Tests skip cleanly (not fail) when no emulator is present, so CI stays green even if the GCS service container is flaky.
