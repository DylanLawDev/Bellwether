# T19 — Normalizer registry + generic `numeric-series-v1`

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§6.2 Normalizer registry, §6.1 canonical `numeric-series-v1` payload).
**Depends on:** T04. **Branch:** `ticket/T19-normalizer-registry`. **PR, do not merge without approval.**

## Goal
Stand up the structured-path counterpart to the extractor registry. Create a `normalizers/` package that mirrors `extractors/` **exactly** — a `NormalizedPoint` dataclass, a `Normalizer` Protocol keyed on `content_type`, and `register`/`get_normalizer`/`known_content_types` over a module-level dict. Ship the one generic normalizer (`NumericSeriesNormalizer`, `content_type="numeric-series-v1"`) that parses the canonical numeric payload into one `NormalizedPoint` per point and self-registers at import. Pure, DB-free, fixture-tested; the worker (T20) and gold write (T18) consume it later.

## Files
- Create: `src/bellweather/normalizers/__init__.py` — `NormalizedPoint`, `Normalizer`, `register`, `get_normalizer`, `known_content_types`, `_REGISTRY`.
- Create: `src/bellweather/normalizers/numeric_series.py` — `NumericSeriesNormalizer`; calls `register(...)` at import.
- Test: `tests/test_normalizers.py` — registry roundtrip + `known_content_types` + `numeric-series-v1` normalization (incl. empty `points`).

## Interface
Copied verbatim from the build plan's Locked interfaces.

```python
# normalizers/__init__.py
@dataclass
class NormalizedPoint:
    symbol_key: str
    symbol_kind: str
    ts: datetime
    value: float
    unit: str | None = None
    description: str | None = None

@runtime_checkable
class Normalizer(Protocol):
    content_type: str
    def normalize(self, envelope: dict) -> list["NormalizedPoint"]: ...

def register(n: Normalizer) -> None: ...
def get_normalizer(content_type: str) -> Normalizer | None: ...
def known_content_types() -> set[str]: ...
```

`normalizers/numeric_series.py` — `NumericSeriesNormalizer.content_type = "numeric-series-v1"`. Reads `envelope["payload"]` (the bronze envelope is `Submission.model_dump(mode="json")`, so the payload dict is under `"payload"`) with keys `symbol_key, symbol_kind, unit?, description?, points:[{ts, value}]`; yields one `NormalizedPoint` per point (`datetime.fromisoformat(ts)`, `float(value)`). Calls `register(NumericSeriesNormalizer())` at import.

The canonical `numeric-series-v1` payload (spec §6.1):
```jsonc
{
  "symbol_key":  "polymarket:us-x-iran-permanent-peace-deal-by:<variant>",
  "symbol_kind": "market-probability",
  "unit":        "probability",
  "description": "Will X happen by D? (YES)",
  "points": [ { "ts": "2026-05-31T14:00:00Z", "value": 0.37 }, ... ]
}
```

## Steps

This ticket is pure Python — **no `make up`/`make migrate`** (no Postgres or GCS). Run `make dev` once if deps are not synced.

- [ ] **Step 1: Failing test** `tests/test_normalizers.py`. Mirror `tests/test_extractor_registry.py`'s autouse registry-snapshot fixture so the test never leaks fakes into the process-wide `_REGISTRY`.
```python
from datetime import datetime, timezone

import pytest

from bellweather.normalizers import (
    NormalizedPoint,
    register,
    get_normalizer,
    known_content_types,
    _REGISTRY,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class _Fake:
    content_type = "fake-series-v1"

    def normalize(self, envelope):
        p = envelope["payload"]
        return [
            NormalizedPoint(
                symbol_key=p["symbol_key"],
                symbol_kind=p["symbol_kind"],
                ts=datetime.fromisoformat(p["points"][0]["ts"]),
                value=float(p["points"][0]["value"]),
            )
        ]


def test_register_and_lookup():
    register(_Fake())
    n = get_normalizer("fake-series-v1")
    assert n is not None
    pts = n.normalize(
        {"payload": {"symbol_key": "k", "symbol_kind": "m", "points": [{"ts": "2026-05-31T14:00:00+00:00", "value": "0.5"}]}}
    )
    assert pts[0].symbol_key == "k" and pts[0].value == 0.5
    assert "fake-series-v1" in known_content_types()


def test_unknown_returns_none():
    assert get_normalizer("does-not-exist") is None


def test_numeric_series_normalizer_is_registered_on_import():
    n = get_normalizer("numeric-series-v1")
    assert n is not None
    assert n.content_type == "numeric-series-v1"


def test_numeric_series_yields_one_point_per_entry():
    from bellweather.normalizers.numeric_series import NumericSeriesNormalizer

    envelope = {
        "payload": {
            "symbol_key": "polymarket:demo:yes",
            "symbol_kind": "market-probability",
            "unit": "probability",
            "description": "Will X happen by D? (YES)",
            "points": [
                {"ts": "2026-05-31T14:00:00+00:00", "value": 0.37},
                {"ts": "2026-05-31T15:00:00+00:00", "value": "0.42"},
            ],
        }
    }
    pts = NumericSeriesNormalizer().normalize(envelope)
    assert len(pts) == 2
    assert pts[0] == NormalizedPoint(
        symbol_key="polymarket:demo:yes",
        symbol_kind="market-probability",
        ts=datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc),
        value=0.37,
        unit="probability",
        description="Will X happen by D? (YES)",
    )
    assert pts[1].value == 0.42 and isinstance(pts[1].value, float)
    assert pts[1].ts == datetime(2026, 5, 31, 15, 0, tzinfo=timezone.utc)
    assert pts[1].unit == "probability"


def test_numeric_series_optional_fields_default_to_none():
    envelope = {
        "payload": {
            "symbol_key": "src:bare",
            "symbol_kind": "counter",
            "points": [{"ts": "2026-05-31T14:00:00+00:00", "value": 1}],
        }
    }
    pts = get_normalizer("numeric-series-v1").normalize(envelope)
    assert pts[0].unit is None and pts[0].description is None
    assert pts[0].value == 1.0


def test_numeric_series_empty_points_yields_empty_list():
    envelope = {
        "payload": {"symbol_key": "src:bare", "symbol_kind": "counter", "points": []}
    }
    assert get_normalizer("numeric-series-v1").normalize(envelope) == []
```

- [ ] **Step 2: Run** `uv run pytest tests/test_normalizers.py -v` → **FAIL** (module `bellweather.normalizers` does not exist yet).

- [ ] **Step 3: Implement** `src/bellweather/normalizers/__init__.py` — a byte-for-byte mirror of `src/bellweather/extractors/__init__.py`, with `NormalizedPoint`/`Normalizer`/`normalize` swapped in for `ExtractedTag`/`Extractor`/`extract`.
```python
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class NormalizedPoint:
    symbol_key: str
    symbol_kind: str
    ts: datetime
    value: float
    unit: str | None = None
    description: str | None = None


@runtime_checkable
class Normalizer(Protocol):
    content_type: str

    def normalize(self, envelope: dict) -> list["NormalizedPoint"]: ...


_REGISTRY: dict[str, Normalizer] = {}


def register(normalizer: Normalizer) -> None:
    _REGISTRY[normalizer.content_type] = normalizer


def get_normalizer(content_type: str) -> Normalizer | None:
    return _REGISTRY.get(content_type)


def known_content_types() -> set[str]:
    return set(_REGISTRY)
```

- [ ] **Step 4: Implement** `src/bellweather/normalizers/numeric_series.py` — mirror `extractors/gdelt_gkg.py`: read `envelope["payload"]`, iterate `points`, register at import.
```python
from datetime import datetime

from bellweather.normalizers import NormalizedPoint, register


class NumericSeriesNormalizer:
    content_type = "numeric-series-v1"

    def normalize(self, envelope: dict) -> list[NormalizedPoint]:
        p = envelope["payload"]
        symbol_key = p["symbol_key"]
        symbol_kind = p["symbol_kind"]
        unit = p.get("unit")
        description = p.get("description")
        points: list[NormalizedPoint] = []
        for point in p.get("points") or []:
            points.append(
                NormalizedPoint(
                    symbol_key=symbol_key,
                    symbol_kind=symbol_kind,
                    ts=datetime.fromisoformat(point["ts"]),
                    value=float(point["value"]),
                    unit=unit,
                    description=description,
                )
            )
        return points


register(NumericSeriesNormalizer())
```

- [ ] **Step 5: Run** `uv run pytest tests/test_normalizers.py -v` → **PASS** (all six tests green).

- [ ] **Step 6: Gate** `make check` → green (ruff check + ruff format --check + pytest).

- [ ] **Step 7: Commit** (`feat: add normalizer registry + generic numeric-series-v1 normalizer`).

## Acceptance criteria
- `bellweather.normalizers` mirrors `bellweather.extractors` exactly: `NormalizedPoint` dataclass, `@runtime_checkable Normalizer` Protocol with `content_type` + `normalize(envelope) -> list[NormalizedPoint]`, and `register`/`get_normalizer`/`known_content_types` over a module-level `_REGISTRY`.
- Importing `bellweather.normalizers.numeric_series` self-registers `NumericSeriesNormalizer`; `get_normalizer("numeric-series-v1")` returns it and `"numeric-series-v1" in known_content_types()`.
- `normalize()` reads `envelope["payload"]` and yields one `NormalizedPoint` per entry in `points`, with `ts` parsed via `datetime.fromisoformat` (tz preserved), `value` coerced via `float`, and `unit`/`description` carried (defaulting to `None` when absent).
- Empty `points` (or a missing `points` key) yields `[]`.
- `get_normalizer` of an unknown `content_type` returns `None`.
- Pure module — no DB, GCS, or network; tests pass without `make up`. `make check` is green.
