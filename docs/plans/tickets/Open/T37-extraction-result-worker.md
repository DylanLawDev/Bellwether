# T37 — `ExtractionResult` + worker writes observations

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§3.1 Units — Worker integration; K4 "Extraction produces gold *values* (and tags) off the unstructured path"; D-c "unstructured branch can now write gold values"). **Depends on:** T09 (extractor registry / `ExtractedTag`), T11 (`worker.process_job` + the unstructured branch), T18 (`gold.upsert_value`). **Branch:** `ticket/T37-extraction-result-worker`. **PR, do not merge without approval.**

## Goal
Widen the unstructured extraction path so an extractor can emit gold **values** in addition to tags, without breaking the GDELT/legacy path. Add a small `ExtractionResult(tags, observations)` dataclass to `extractors/__init__.py` (reusing `NormalizedPoint` from `bellweather.normalizers` — that package does not import `extractors`, so the import stays acyclic), then teach `worker.process_job`'s **unstructured branch only** to normalize the extractor return: an `ExtractionResult` yields `.tags` + `.observations`, while a bare `list[ExtractedTag]` (what `GdeltGkgExtractor` still returns) yields `(result, [])`. Tags are written exactly as today (insert + `upsert_coverage`); each observation is written via the existing `gold.upsert_value` (set-semantics, K8/D-c). This is the legitimate bridge from unstructured input to numeric output (K4): the structured branch and the GDELT extractor stay byte-identical.

## Files
- Modify: `src/bellweather/extractors/__init__.py` — add the `ExtractionResult` dataclass (import `NormalizedPoint` from `bellweather.normalizers`); widen the `Extractor.extract` return annotation to `list[ExtractedTag] | ExtractionResult`. `GdeltGkgExtractor` is untouched.
- Modify: `src/bellweather/worker.py` — in the **unstructured branch only**, normalize `extractor.extract(envelope)` to `(ex_tags, ex_obs)`, keep the existing tags insert + `upsert_coverage` loop, and write each observation with `upsert_value(conn, o.symbol_key, o.symbol_kind, o.ts, o.value, unit=o.unit, description=o.description)`. Import `ExtractionResult` from `bellweather.extractors` (`upsert_value` is already imported). The structured branch is untouched.
- Test: `tests/test_worker_observations.py` — DB+GCS-backed (`make up` + `make migrate`). Registers a **fake** extractor under a throwaway `content_type`, writes a bronze envelope + a `raw_records` row + `enqueue` (directly, not via `ingest_record`, since the fake type is not in `KNOWN_CONTENT_TYPES`), runs the worker, and asserts both an `observations` row and a `tags` row land. A second case proves a legacy `list[ExtractedTag]` extractor still writes tags only (GDELT/back-compat unchanged).

## Interface
Copied verbatim from the build plan's "Locked interfaces".

`extractors/__init__.py` — add (worker accepts BOTH the legacy `list[ExtractedTag]` and this):
```python
from dataclasses import dataclass, field
from bellweather.normalizers import NormalizedPoint   # reuse the gold-value point shape

@dataclass
class ExtractionResult:
    tags: list[ExtractedTag] = field(default_factory=list)
    observations: list[NormalizedPoint] = field(default_factory=list)
```
(`normalizers/__init__.py` does not import `extractors`, so this import is acyclic.) The `Extractor` Protocol's `extract` return is widened to `list[ExtractedTag] | ExtractionResult`; existing `GdeltGkgExtractor` is **unchanged** (still returns a list).

`worker.py` — the **unstructured** branch normalizes the extractor return, then writes tags (as today) AND observations:
```python
result = extractor.extract(envelope)
if isinstance(result, ExtractionResult):
    ex_tags, ex_obs = result.tags, result.observations
else:
    ex_tags, ex_obs = result, []        # legacy list[ExtractedTag] — GDELT path unchanged
for t in ex_tags:
    ...                                 # existing tags insert + upsert_coverage (unchanged)
for o in ex_obs:
    upsert_value(conn, o.symbol_key, o.symbol_kind, o.ts, o.value,
                 unit=o.unit, description=o.description)
```
Import `ExtractionResult` from `bellweather.extractors`. (`upsert_value` is already imported.) The structured branch is untouched.

Reused shapes (already on `main`, do **not** redefine):
```python
# bellweather/extractors/__init__.py
@dataclass
class ExtractedTag:
    tag_type: str
    raw_value: str
    score: dict

# bellweather/normalizers/__init__.py
@dataclass
class NormalizedPoint:
    symbol_key: str
    symbol_kind: str
    ts: datetime
    value: float
    unit: str | None = None
    description: str | None = None

# bellweather/gold.py
def upsert_value(conn, symbol_key, symbol_kind, ts, value, *,
                 unit=None, description=None, sample_count=1) -> int: ...
```

## Steps

- [ ] **Step 0: Bring up infra.** `make up` (Postgres 16 + fake-gcs) then `make migrate` (applies the existing 6-table schema — `tags`, `tracked_symbols`, `observations` already exist; **no new migration** in this ticket). The test writes to bronze (fake-gcs), so it carries `@requires_gcs` and auto-skips if the emulator is down.

- [ ] **Step 1: Failing test** `tests/test_worker_observations.py`. Two DB+GCS cases. The first registers a fake extractor under a throwaway `content_type` that returns `ExtractionResult(tags=[...], observations=[NormalizedPoint(...)])`; it writes the bronze envelope + a `raw_records` row + `enqueue` **directly** (not `ingest_record`, since the fake type is not in `KNOWN_CONTENT_TYPES`), runs the worker, and asserts an `observations` row (via `upsert_value`) **and** a `tags` row landed and the record is `processed`. The second registers a legacy extractor returning a bare `list[ExtractedTag]` and asserts it still writes tags only (no observations) — GDELT/back-compat unchanged. Unique source + symbol keys + reset fixtures keep it order-independent.

  > **Order-independence note (load-bearing):** `upsert_value` creates a *persistent* `tracked_symbols` row (`scrape:demo:widget`, kind `scraped-metric`) that survives across tests — the shared `clear_observations()` helper only deletes `observations` rows, never `tracked_symbols`. The fixture therefore also deletes that persistent symbol row (after `clear_observations` clears its child `observations`, satisfying the FK), and the legacy test scopes its "no gold value" assertion to that exact key. Without this, test 1 leaves a `scraped-metric` symbol behind and a global `count(*) where kind='scraped-metric'` assertion in test 2 fails (pytest runs in definition order).
```python
from datetime import datetime, timezone

import pytest

from bellweather.db import get_conn
from bellweather.extractors import ExtractedTag, ExtractionResult, register
from bellweather.migrate import apply_migrations
from bellweather.normalizers import NormalizedPoint
from bellweather.queue import enqueue
from bellweather.storage import get_bronze_store
from bellweather.worker import run_worker
from tests.conftest import clear_observations, clear_records, requires_gcs

# Throwaway content_types registered only for this test (NOT in KNOWN_CONTENT_TYPES,
# so these records are inserted + enqueued directly rather than via ingest_record).
_CT_RESULT = "test-extraction-result-v1"
_CT_LEGACY = "test-legacy-tags-v1"
_SOURCE = "test.worker_obs"
_KEYS = ("wkobs-result-1", "wkobs-legacy-1")
# The gold symbol the ExtractionResult observation lands on (value-bearing,
# kind='scraped-metric'), plus the coverage symbol its tag writes
# (key = "<tag_type>:<raw_value>", kind='coverage'). Both accumulate across runs.
_GOLD_SYMBOL = "scrape:demo:widget"
_SYMBOLS = (_GOLD_SYMBOL, "category:widgets")


class _ResultExtractor:
    """Returns the NEW ExtractionResult shape: one tag + one observation."""

    content_type = _CT_RESULT

    def extract(self, envelope):
        return ExtractionResult(
            tags=[ExtractedTag(tag_type="category", raw_value="widgets", score={})],
            observations=[
                NormalizedPoint(
                    symbol_key=_GOLD_SYMBOL,
                    symbol_kind="scraped-metric",
                    ts=datetime(2026, 6, 1, 14, 15, tzinfo=timezone.utc),
                    value=19.99,
                    unit="usd",
                    description="Demo widget price",
                )
            ],
        )


class _LegacyExtractor:
    """Returns the LEGACY bare list[ExtractedTag] (GDELT-style): tags only."""

    content_type = _CT_LEGACY

    def extract(self, envelope):
        return [ExtractedTag(tag_type="category", raw_value="widgets", score={})]


def _seed_record(conn, *, source, content_type, key, fetched_at):
    """Bronze-write an envelope + insert a routable raw_records row + enqueue it.

    Mirrors what ingest_record does, but inline because the throwaway content_type
    is not in KNOWN_CONTENT_TYPES (so ingest_record would park it as unroutable).
    Does NOT commit — the caller owns the transaction.
    """
    envelope = {
        "source": source,
        "kind": "unstructured",
        "content_type": content_type,
        "fetched_at": fetched_at.isoformat(),
        "idempotency_key": key,
        "payload": "<html>raw page bytes</html>",
        "provenance": {},
    }
    payload_uri = get_bronze_store().put(source, fetched_at, key, envelope)
    rid = conn.execute(
        """insert into raw_records
             (source, kind, content_type, idempotency_key, payload_uri, fetched_at, status)
           values (%s, 'unstructured', %s, %s, %s, %s, 'received') returning id""",
        (source, content_type, key, payload_uri, fetched_at),
    ).fetchone()[0]
    enqueue(conn, rid)
    return rid


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    # Register the fakes so the worker's get_extractor(content_type) finds them.
    register(_ResultExtractor())
    register(_LegacyExtractor())
    # Clear rows from prior runs (fixed idempotency keys) + reset the shared
    # gold/coverage symbols so value/count assertions start clean.
    with get_conn() as c:
        clear_records(c, _SOURCE, _KEYS)
        # clear_observations only deletes the observations rows (the FK children),
        # NOT the tracked_symbols rows. The value-bearing scraped-metric symbol
        # that upsert_value created persists otherwise, so a prior test would leave
        # it behind and break the legacy test's "no gold value" check. Delete the
        # observations first (FK: observations -> tracked_symbols), then the symbol.
        clear_observations(c, _SYMBOLS)
        c.execute("delete from tracked_symbols where key = %s", (_GOLD_SYMBOL,))
        c.commit()


@requires_gcs
def test_extraction_result_writes_observation_and_tag():
    fetched_at = datetime(2026, 6, 1, 14, 15, tzinfo=timezone.utc)
    with get_conn() as c:
        rid = _seed_record(
            c, source=_SOURCE, content_type=_CT_RESULT, key="wkobs-result-1",
            fetched_at=fetched_at,
        )
        c.commit()

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (rid,)).fetchone()[0]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (rid,)
        ).fetchone()[0]
        # The ExtractionResult.observation lands a gold value via upsert_value.
        value, unit, descr = c.execute(
            "select o.value, s.unit, s.description from observations o"
            " join tracked_symbols s on s.id = o.tracked_symbol_id"
            " where s.key = %s",
            (_GOLD_SYMBOL,),
        ).fetchone()
        kind = c.execute(
            "select kind from tracked_symbols where key = %s", (_GOLD_SYMBOL,)
        ).fetchone()[0]
    assert st == "processed"
    assert ntags == 1  # the one ExtractedTag was still written
    assert value == pytest.approx(19.99)  # observation written via upsert_value
    assert kind == "scraped-metric"
    assert unit == "usd"
    assert descr == "Demo widget price"


@requires_gcs
def test_legacy_list_extractor_writes_tags_only():
    # Back-compat: a bare list[ExtractedTag] (GDELT path) still writes tags and
    # NEVER observations — the new branch must not invent gold values for legacy
    # extractors.
    fetched_at = datetime(2026, 6, 1, 14, 15, tzinfo=timezone.utc)
    with get_conn() as c:
        rid = _seed_record(
            c, source=_SOURCE, content_type=_CT_LEGACY, key="wkobs-legacy-1",
            fetched_at=fetched_at,
        )
        c.commit()

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (rid,)).fetchone()[0]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (rid,)
        ).fetchone()[0]
        # No value-bearing gold symbol was created for this legacy path. The
        # fixture deletes the scraped-metric symbol up front, so it must be absent.
        # (The tag's "category:widgets" coverage symbol may exist — that is fine.)
        nscraped = c.execute(
            "select count(*) from tracked_symbols where key = %s", (_GOLD_SYMBOL,)
        ).fetchone()[0]
    assert st == "processed"
    assert ntags == 1
    assert nscraped == 0  # legacy list[ExtractedTag] never lands a gold value
```

- [ ] **Step 2: Run → FAIL.** With `make up`/`make migrate` done:
```
uv run pytest tests/test_worker_observations.py -v
```
Expect `ImportError: cannot import name 'ExtractionResult' from 'bellweather.extractors'` (the dataclass and the worker wiring don't exist yet).

- [ ] **Step 3a: Implement `ExtractionResult`** in `src/bellweather/extractors/__init__.py`. Add the imports + dataclass, and widen the `Extractor.extract` return annotation. `NormalizedPoint` comes from `bellweather.normalizers`, which does **not** import `extractors`, so this is acyclic. Full file:
```python
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from bellweather.normalizers import NormalizedPoint  # reuse the gold-value point shape


@dataclass
class ExtractedTag:
    tag_type: str
    raw_value: str
    score: dict


@dataclass
class ExtractionResult:
    tags: list["ExtractedTag"] = field(default_factory=list)
    observations: list[NormalizedPoint] = field(default_factory=list)


@runtime_checkable
class Extractor(Protocol):
    content_type: str

    def extract(self, envelope: dict) -> "list[ExtractedTag] | ExtractionResult": ...


_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> None:
    _REGISTRY[extractor.content_type] = extractor


def get_extractor(content_type: str) -> Extractor | None:
    return _REGISTRY.get(content_type)


def known_content_types() -> set[str]:
    return set(_REGISTRY)
```

- [ ] **Step 3b: Implement the worker shim** in `src/bellweather/worker.py`. Import `ExtractionResult` alongside `get_extractor`, and replace the unstructured branch's `for t in extractor.extract(envelope):` loop with: normalize the return, keep the existing tags insert + `upsert_coverage`, then write observations. The **structured branch and `ack`/status lines are untouched**. Change the import line:
```python
from bellweather.extractors import ExtractionResult, get_extractor
```
And replace the body of the unstructured branch (after `envelope = get_bronze_store().get(payload_uri)`) up to the status update:
```python
    envelope = get_bronze_store().get(payload_uri)
    result = extractor.extract(envelope)
    if isinstance(result, ExtractionResult):
        ex_tags, ex_obs = result.tags, result.observations
    else:
        ex_tags, ex_obs = result, []  # legacy list[ExtractedTag] — GDELT path unchanged
    for t in ex_tags:
        conn.execute(
            "insert into tags(raw_record_id, source, observed_at, tag_type, raw_value, score)"
            " values (%s,%s,%s,%s,%s,%s)",
            (job.raw_record_id, source, fetched_at, t.tag_type, t.raw_value, Jsonb(t.score)),
        )
        if t.tag_type != "tone":
            upsert_coverage(conn, source, t.tag_type, t.raw_value, fetched_at)
    for o in ex_obs:
        upsert_value(
            conn,
            o.symbol_key,
            o.symbol_kind,
            o.ts,
            o.value,
            unit=o.unit,
            description=o.description,
        )
    conn.execute("update raw_records set status='processed' where id=%s", (job.raw_record_id,))
    ack(conn, job.id)
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_worker_observations.py -v` → 2 passed. Re-run the existing worker tests to confirm the GDELT/structured paths are unchanged: `uv run pytest tests/test_worker.py tests/test_worker_structured.py -v` → all pass.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green with `make up` running.

- [ ] **Step 6: Commit** (`feat: ExtractionResult + worker writes gold observations off the unstructured path`).

## Acceptance criteria
- `bellweather.extractors.ExtractionResult` exists with `tags: list[ExtractedTag]` and `observations: list[NormalizedPoint]`, both defaulting to empty lists; it imports `NormalizedPoint` from `bellweather.normalizers` with **no import cycle** (importing `bellweather.extractors` and `bellweather.worker` both succeed).
- The `Extractor` Protocol's `extract` return is widened to `list[ExtractedTag] | ExtractionResult`; `GdeltGkgExtractor` is byte-identical (still returns a list).
- `worker.process_job`'s **unstructured branch** normalizes the extractor return: an `ExtractionResult` writes its `.tags` (existing insert + `upsert_coverage`) **and** each `.observations` point via `gold.upsert_value(conn, symbol_key, symbol_kind, ts, value, unit=..., description=...)`; a bare `list[ExtractedTag]` writes tags only.
- An extractor returning `ExtractionResult(tags=[...], observations=[NormalizedPoint(...)])` yields both a `tags` row and an `observations` row (gold value, with `kind`/`unit`/`description` on the `tracked_symbol`), and the record ends `processed`.
- A legacy `list[ExtractedTag]` extractor still writes tags only — no gold value lands (back-compat / GDELT unchanged).
- The **structured branch** (`get_normalizer` → `upsert_value`) and `ack`/status handling are untouched; DB helpers still never commit (the worker's `run_worker` owns the txn). No new migration.
- The two test cases are **order-independent**: the fixture deletes the persistent `scrape:demo:widget` `tracked_symbols` row (after `clear_observations` clears its FK children), so the legacy case's "no gold value" assertion holds regardless of test order.
- `make check` green.
