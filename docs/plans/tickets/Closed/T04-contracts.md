# T04 — Ingestion contract models

**Spec:** §4 (the ingestion contract — the central seam).
**Depends on:** T00. **Branch:** `ticket/T04-contracts`. **PR, do not merge without approval.**

## Goal
Define the validated submission envelope every producer sends to `POST /ingest`, plus the result type the API returns. This is the platform's most important interface — get the fields and validation exactly right.

## Files
- Create: `src/bellweather/contracts.py`
- Test: `tests/test_contracts.py`

## Interface (referenced by exact name in T06, T07, T08)
```python
# contracts.py
Kind = Literal["unstructured", "structured"]

class Submission(BaseModel):
    source: str                       # namespaced, e.g. "gdelt.gkg"
    kind: Kind
    content_type: str                 # selects extractor/normalizer, e.g. "gdelt-gkg-v2"
    fetched_at: datetime              # tz-aware UTC
    idempotency_key: str              # unique per logical record within a source
    payload: dict | str | None = None # inline small payloads
    payload_uri: str | None = None    # OR pointer to a pre-uploaded blob
    provenance: dict = {}             # free-form, stamped into bronze

    # validation: exactly one of payload / payload_uri must be set; fetched_at must be tz-aware

class IngestResult(BaseModel):
    raw_record_id: int
    status: Literal["created", "duplicate", "unroutable"]
    payload_uri: str
```

## Steps

- [ ] **Step 1: Failing tests** `tests/test_contracts.py`
```python
import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from bellweather.contracts import Submission

BASE = dict(source="gdelt.gkg", kind="unstructured", content_type="gdelt-gkg-v2",
            fetched_at=datetime(2026,5,31,14,15,tzinfo=timezone.utc), idempotency_key="k1")

def test_accepts_inline_payload():
    s = Submission(**BASE, payload={"a": 1})
    assert s.source == "gdelt.gkg" and s.kind == "unstructured"

def test_accepts_payload_uri():
    Submission(**BASE, payload_uri="gs://b/x.json")

def test_rejects_both_payload_and_uri():
    with pytest.raises(ValidationError):
        Submission(**BASE, payload={"a": 1}, payload_uri="gs://b/x")

def test_rejects_neither_payload_nor_uri():
    with pytest.raises(ValidationError):
        Submission(**BASE)

def test_rejects_naive_datetime():
    bad = {**BASE, "fetched_at": datetime(2026,5,31,14,15)}  # naive
    with pytest.raises(ValidationError):
        Submission(**bad, payload={"a": 1})

def test_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Submission(**{**BASE, "kind": "weird"}, payload={"a": 1})
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `contracts.py`**
```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, model_validator, field_validator

Kind = Literal["unstructured", "structured"]

class Submission(BaseModel):
    source: str
    kind: Kind
    content_type: str
    fetched_at: datetime
    idempotency_key: str
    payload: dict | str | None = None
    payload_uri: str | None = None
    provenance: dict = {}

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _exactly_one_payload(self):
        has_inline = self.payload is not None
        has_uri = self.payload_uri is not None
        if has_inline == has_uri:
            raise ValueError("provide exactly one of payload or payload_uri")
        return self

class IngestResult(BaseModel):
    raw_record_id: int
    status: Literal["created", "duplicate", "unroutable"]
    payload_uri: str
```
- [ ] **Step 4: Run** → PASS. Commit (`feat: add ingestion contract models`).

## Acceptance criteria
- Exactly-one-of `payload`/`payload_uri` enforced; naive datetimes and unknown `kind` rejected.
- `make check` green (no DB/GCS needed — pure models).
