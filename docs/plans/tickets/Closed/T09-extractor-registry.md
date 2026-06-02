# T09 — Extractor registry + `Extractor` protocol

**Spec:** §3 extractor-registry, §4 routing, D8 (one extractor behind a registry).
**Depends on:** T04. **Branch:** `ticket/T09-extractor-registry`. **PR, do not merge without approval.**

## Goal
Define the pluggable extraction seam: a `content_type → Extractor` registry and the `Extractor` protocol. v0 will register exactly one extractor (T10), but the seam must make adding more trivial.

## Files
- Create: `src/bellweather/extractors/__init__.py`
- Test: `tests/test_extractor_registry.py`

## Interfaces (referenced by exact name in T10, T11)
```python
# extractors/__init__.py
@dataclass
class ExtractedTag:
    tag_type: str        # theme | person | org | location | tone
    raw_value: str
    score: dict          # arbitrary numeric payload, JSON-serializable

class Extractor(Protocol):
    content_type: str
    def extract(self, envelope: dict) -> list[ExtractedTag]: ...

def register(extractor: Extractor) -> None: ...
def get_extractor(content_type: str) -> Extractor | None: ...
def known_content_types() -> set[str]: ...
```
> `envelope` is the bronze JSON (the `Submission.model_dump(mode="json")`), so `extract` reads `envelope["payload"]`.

## Steps

- [ ] **Step 1: Failing tests** `tests/test_extractor_registry.py`
```python
from bellweather.extractors import (
    ExtractedTag, register, get_extractor, known_content_types)

class _Fake:
    content_type = "fake-v1"
    def extract(self, envelope):
        return [ExtractedTag(tag_type="theme", raw_value=envelope["payload"]["t"], score={})]

def test_register_and_lookup():
    register(_Fake())
    ex = get_extractor("fake-v1")
    assert ex is not None
    tags = ex.extract({"payload": {"t": "ECON"}})
    assert tags[0].raw_value == "ECON" and "fake-v1" in known_content_types()

def test_unknown_returns_none():
    assert get_extractor("does-not-exist") is None
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement `extractors/__init__.py`**
```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

@dataclass
class ExtractedTag:
    tag_type: str
    raw_value: str
    score: dict

@runtime_checkable
class Extractor(Protocol):
    content_type: str
    def extract(self, envelope: dict) -> list["ExtractedTag"]: ...

_REGISTRY: dict[str, Extractor] = {}

def register(extractor: Extractor) -> None:
    _REGISTRY[extractor.content_type] = extractor

def get_extractor(content_type: str) -> Extractor | None:
    return _REGISTRY.get(content_type)

def known_content_types() -> set[str]:
    return set(_REGISTRY)
```
- [ ] **Step 4: Run** → PASS. Commit (`feat: add extractor registry and protocol`).

## Acceptance criteria
- Register/lookup/known-types all work; unknown type returns `None`.
- No dependency on DB/GCS — pure in-memory seam.

## Follow-up wiring (done in T10/T11, noted here)
- T06's `KNOWN_CONTENT_TYPES` and the worker's routing should be backed by `known_content_types()` once real extractors register at import time. T11 imports `bellweather.extractors.gdelt_gkg` so its `register()` runs.
