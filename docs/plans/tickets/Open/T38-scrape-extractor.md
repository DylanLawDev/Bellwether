# T38 — `LlmScrapeExtractor` + `scrape-llm-v1` routing

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§6.3 generic extractor; K3 one generic extractor parameterized by a DB-stored spec). **Depends on:** T34 (`scrape_specs` migration + `scrape.specs.get_spec`), T35 (`scrape.binding.apply_binding`), T36 (`llm.LlmExtractor`), T37 (`ExtractionResult` + worker writes observations). **Branch:** `ticket/T38-scrape-extractor`. **PR, do not merge without approval.**

## Goal
Add the one **generic** `LlmScrapeExtractor` (registered for `content_type="scrape-llm-v1"`) that closes the worker-side path of the scrape engine. It loads the scrape spec by the name carried in the record's `provenance.scrape_spec`, feeds the raw page + the spec's `output_schema` to the injected LLM (schema-constrained tool-use), then runs the spec's `binding` to produce `ExtractionResult(tags, observations)` — which T37's worker shim already writes as silver tags + gold values via `upsert_value`. The extractor is parameterized entirely by **data** (the spec row), so a new source needs config, not code (K3). Spec lookup and LLM client are both **injectable** (defaults read the DB / build the Anthropic client) so unit tests run with no DB and no key; bronze keeps the raw page, so extraction stays replayable (D-d). This ticket also makes `scrape-llm-v1` routable: it adds the type to `ingest.KNOWN_CONTENT_TYPES` and imports the module in `worker.py` so it self-registers.

## Files
- Create: `src/bellweather/extractors/scrape_llm.py` — `LlmScrapeExtractor` (`content_type="scrape-llm-v1"`), the `_db_spec_loader` default, and `register(LlmScrapeExtractor())` at import.
- Modify: `src/bellweather/worker.py` — add `import bellweather.extractors.scrape_llm  # noqa: F401` alongside the existing gdelt import so the worker process registers the extractor.
- Modify: `src/bellweather/ingest.py` — `KNOWN_CONTENT_TYPES += "scrape-llm-v1"` so an ingested raw page is routable (enqueued, status `created`) instead of `unroutable`.
- Test: `tests/test_scrape_extractor.py` — (1) pure unit tests injecting a fake spec-loader + fake LLM (no DB, no key); (2) a DB+GCS end-to-end test (`make up` + `make migrate`): seed a `scrape_specs` row, ingest a raw page, override the registry with a fake-LLM extractor, run the worker once → assert an `observations` row.

## Interface
Copied verbatim from the build plan's "Locked interfaces" (`docs/plans/2026-06-02-llm-scrape-engine.md`).

`extractors/scrape_llm.py`:
```python
class LlmScrapeExtractor:
    content_type = "scrape-llm-v1"
    def __init__(self, *, spec_loader=None, llm=None) -> None:
        self._load = spec_loader or _db_spec_loader   # (name) -> spec dict | None
        self._llm = llm or LlmExtractor()
    def extract(self, envelope: dict) -> ExtractionResult:
        spec = self._load(envelope["provenance"]["scrape_spec"])
        if spec is None:
            return ExtractionResult()                  # nothing written; worker still ack/processed
        content = envelope["payload"] if isinstance(envelope["payload"], str) \
                  else json.dumps(envelope["payload"])
        instance = self._llm.extract(content, spec["output_schema"], model=spec.get("llm_model"))
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        obs, tags = apply_binding(instance, spec["binding"], fetched_at=fetched_at)
        return ExtractionResult(tags=tags, observations=obs)

def _db_spec_loader(name: str) -> dict | None:
    with get_conn() as c:                              # read-only spec lookup (trusted worker)
        return get_spec(c, name)

register(LlmScrapeExtractor())
```
`worker.py` adds `import bellweather.extractors.scrape_llm  # noqa: F401` (registers). `ingest.py` `KNOWN_CONTENT_TYPES = {"gdelt-gkg-v2", "numeric-series-v1", "scrape-llm-v1"}`.

Upstream contracts this ticket consumes (locked in their own tickets, not redefined here):
```python
# extractors/__init__.py (T37)
@dataclass
class ExtractionResult:
    tags: list[ExtractedTag] = field(default_factory=list)
    observations: list[NormalizedPoint] = field(default_factory=list)

# scrape/binding.py (T35) — pure, stdlib-only
def apply_binding(instance: dict, binding: dict, *, fetched_at: datetime
                  ) -> tuple[list[NormalizedPoint], list[ExtractedTag]]: ...

# scrape/specs.py (T34) — never commits (caller owns txn)
def get_spec(conn, name: str) -> dict | None: ...
def create_spec(conn, *, name: str, sites: list, output_schema: dict, binding: dict,
                description: str | None = None, fetch_adapter: str = "httpx",
                llm_model: str | None = None, enabled: bool = True) -> int: ...
def delete_spec(conn, name: str) -> None: ...          # used by the e2e fixture's teardown/reset

# llm.py (T36) — lazy client; importing needs no key
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
```
Note the **binding return order**: `apply_binding` returns `(observations, tags)`, so `extract` unpacks `obs, tags = apply_binding(...)` then builds `ExtractionResult(tags=tags, observations=obs)`.

## Steps

- [ ] **Step 0: Bring up infra.** `make up` (Postgres 16 + fake-gcs) then `make migrate`. This ticket adds **no** new migration — the `scrape_specs` table is T34's `0003_scrape_specs.sql`; `make migrate` applies the full chain (`0001`–`0003`). The end-to-end test is DB- and GCS-backed (it ingests a raw page → bronze and reads `observations`); the unit tests need neither.

- [ ] **Step 1: Failing test** `tests/test_scrape_extractor.py`. Three layers: (a) two pure unit tests injecting a fixture spec-loader + a `_FakeLlm` returning canned JSON (no DB, no GCS, no key) — one asserts the bound observations + tags, one asserts a spec-loader returning `None` yields an empty `ExtractionResult`; (b) a DB+GCS end-to-end test that seeds a real `scrape_specs` row via `create_spec`, ingests a raw-page `Submission` (`content_type="scrape-llm-v1"`, `provenance.scrape_spec=<name>`, `payload=<raw string>`) through `ingest_record`, **overrides the registry** with a `LlmScrapeExtractor(llm=_FakeLlm(...))` (its default `_db_spec_loader` reads the seeded row, so no key is needed), runs `run_worker(once=True)`, and asserts one `observations` row keyed to the bound symbol. The `_reset_registry` fixture snapshots/restores `_REGISTRY` so the override never leaks.
```python
"""LlmScrapeExtractor — generic scrape-llm-v1 extractor (T38).

Unit layer: inject a fixture spec-loader + a fake LLM (no DB, no GCS, no key) and
assert the bound observations/tags; a spec-loader returning None yields an empty
ExtractionResult. End-to-end layer (needs `make up` + `make migrate`): seed a
real scrape_specs row, ingest a raw page (content_type=scrape-llm-v1), override
the registry with a fake-LLM extractor whose default DB loader reads the seeded
row, run the worker once, and assert an observations row keyed to the symbol.
"""

from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.extractors import ExtractionResult, _REGISTRY, register
from bellweather.extractors.scrape_llm import LlmScrapeExtractor
from bellweather.ingest import KNOWN_CONTENT_TYPES, ingest_record
from bellweather.migrate import apply_migrations
from bellweather.scrape.specs import create_spec, delete_spec
from bellweather.worker import run_worker
from tests.conftest import clear_observations, clear_records, requires_gcs

# A scrape spec exercised by every test here: one record per page, value from
# $.price, ts = the literal "fetched_at" param, symbol_key templated from the
# record's `name`, plus a tag carrying `category`.
_SPEC = {
    "output_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "number"},
                       "category": {"type": "string"}},
        "required": ["name", "price"],
    },
    "binding": {
        "records_path": None,                       # whole instance is ONE record
        "symbol_key": "scrape:prices:{category}:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",                         # literal -> the fetched_at arg
        "unit": "usd",                              # literal
        "description": "$.name",                    # field ref
        "tags": ["category"],                       # field name -> ExtractedTag
    },
}

# What the (fake) LLM "extracts" from the raw page.
_INSTANCE = {"name": "widget", "price": 19.99, "category": "tools"}
# Resulting symbol key, given the binding template + _INSTANCE.
_SYMBOL_KEY = "scrape:prices:tools:widget"

_SPEC_NAME = "t38-prices"
_SOURCE = f"scrape:{_SPEC_NAME}"
_KEY = "t38-page-1"


class _FakeLlm:
    """Stand-in for llm.LlmExtractor: returns a canned instance, never calls the API."""

    def __init__(self, instance: dict) -> None:
        self._instance = instance
        self.calls: list[tuple[str, dict, str | None]] = []

    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict:
        self.calls.append((content, output_schema, model))
        return self._instance


@pytest.fixture(autouse=True)
def _reset_registry():
    # Snapshot/restore the extractor registry so an e2e override (a fake-LLM
    # extractor for scrape-llm-v1) can never leak into another test.
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# --- unit: no DB, no GCS, no key --------------------------------------------
def test_extract_binds_instance_to_observations_and_tags():
    fake = _FakeLlm(_INSTANCE)
    ex = LlmScrapeExtractor(spec_loader=lambda name: dict(_SPEC, _name=name), llm=fake)
    fetched_at = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    envelope = {
        "payload": "<html>widget 19.99</html>",      # raw page string
        "fetched_at": fetched_at.isoformat(),
        "provenance": {"scrape_spec": _SPEC_NAME},
    }

    result = ex.extract(envelope)

    assert isinstance(result, ExtractionResult)
    # One observation, fully resolved from the binding.
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.symbol_key == _SYMBOL_KEY
    assert obs.symbol_kind == "scraped-metric"
    assert obs.value == pytest.approx(19.99)
    assert obs.ts == fetched_at                       # "fetched_at" literal -> the arg
    assert obs.unit == "usd"
    assert obs.description == "widget"                 # "$.name" field ref
    # One tag, from the `category` field.
    assert len(result.tags) == 1
    tag = result.tags[0]
    assert tag.tag_type == "category"
    assert tag.raw_value == "tools"
    # The LLM saw the raw page string + the spec's output_schema (str passed through).
    assert fake.calls == [("<html>widget 19.99</html>", _SPEC["output_schema"], None)]


def test_extract_returns_empty_result_when_spec_missing():
    # A record whose provenance.scrape_spec resolves to no spec writes nothing,
    # but does NOT raise (the worker still acks/processes — same rule as an
    # unknown extractor: unroutable, no data lost).
    ex = LlmScrapeExtractor(spec_loader=lambda name: None, llm=_FakeLlm(_INSTANCE))
    envelope = {
        "payload": "irrelevant",
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "provenance": {"scrape_spec": "does-not-exist"},
    }
    result = ex.extract(envelope)
    assert isinstance(result, ExtractionResult)
    assert result.observations == []
    assert result.tags == []


def test_extract_json_encodes_non_string_payload():
    # If the bronze payload is a dict (not a raw string), the extractor json-dumps
    # it before handing it to the LLM (the contract's content is always str).
    fake = _FakeLlm(_INSTANCE)
    ex = LlmScrapeExtractor(spec_loader=lambda name: _SPEC, llm=fake)
    envelope = {
        "payload": {"raw": {"a": 1}},                 # dict payload
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "provenance": {"scrape_spec": _SPEC_NAME},
    }
    ex.extract(envelope)
    sent_content = fake.calls[0][0]
    assert sent_content == '{"raw": {"a": 1}}'        # json.dumps of the dict


# --- routing: ingest type table -------------------------------------------
def test_scrape_llm_is_routable():
    assert "scrape-llm-v1" in KNOWN_CONTENT_TYPES


# --- end-to-end: DB + GCS ----------------------------------------------------
@pytest.fixture()
def _seeded_spec():
    apply_migrations()
    # Reset any rows a prior run left behind, then seed the spec used end-to-end.
    with get_conn() as c:
        clear_records(c, _SOURCE, (_KEY,))
        clear_observations(c, (_SYMBOL_KEY,))
        delete_spec(c, _SPEC_NAME)                    # idempotent: no-op if absent
        create_spec(
            c,
            name=_SPEC_NAME,
            sites=["http://example.test/prices"],
            output_schema=_SPEC["output_schema"],
            binding=_SPEC["binding"],
            description="T38 e2e fixture spec",
        )
        c.commit()
    yield
    with get_conn() as c:
        clear_records(c, _SOURCE, (_KEY,))
        clear_observations(c, (_SYMBOL_KEY,))
        delete_spec(c, _SPEC_NAME)
        c.commit()


@requires_gcs
def test_ingest_then_worker_writes_scraped_observation(_seeded_spec):
    sub = Submission(
        source=_SOURCE,
        kind="unstructured",
        content_type="scrape-llm-v1",
        fetched_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        idempotency_key=_KEY,
        payload="<html>widget 19.99</html>",          # the raw page string
        provenance={"scrape_spec": _SPEC_NAME, "url": "http://example.test/prices"},
    )
    r = ingest_record(sub)
    assert r.status == "created"                       # scrape-llm-v1 is now routable

    # Override the registered scrape extractor with one whose LLM is faked; its
    # default _db_spec_loader still reads the seeded scrape_specs row, so no key.
    register(LlmScrapeExtractor(llm=_FakeLlm(_INSTANCE)))

    run_worker(once=True)

    with get_conn() as c:
        value = c.execute(
            "select o.value from observations o"
            " join tracked_symbols s on s.id = o.tracked_symbol_id"
            " where s.key = %s",
            (_SYMBOL_KEY,),
        ).fetchone()
        kind = c.execute(
            "select kind from tracked_symbols where key = %s", (_SYMBOL_KEY,)
        ).fetchone()
        status = c.execute(
            "select status from raw_records where id = %s", (r.raw_record_id,)
        ).fetchone()[0]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id = %s", (r.raw_record_id,)
        ).fetchone()[0]

    assert value is not None and value[0] == pytest.approx(19.99)
    assert kind[0] == "scraped-metric"
    assert status == "processed"
    assert ntags == 1                                  # the `category` tag
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_scrape_extractor.py -v` (with `make up`/`make migrate` done, and T34–T37 already merged on this stacked branch). With its dependencies present, the only thing the module is missing is the file under test, so collection fails first on its top-level import:
  `ModuleNotFoundError: No module named 'bellweather.extractors.scrape_llm'`. Once that import resolves (Step 3), `test_scrape_llm_is_routable` is the next to fail, because `"scrape-llm-v1"` is not yet in `KNOWN_CONTENT_TYPES`.

- [ ] **Step 3: Implement.**

  Create `src/bellweather/extractors/scrape_llm.py` — verbatim from the locked interface:
```python
import json
from datetime import datetime

from bellweather.db import get_conn
from bellweather.extractors import ExtractionResult, register
from bellweather.llm import LlmExtractor
from bellweather.scrape.binding import apply_binding
from bellweather.scrape.specs import get_spec


class LlmScrapeExtractor:
    content_type = "scrape-llm-v1"

    def __init__(self, *, spec_loader=None, llm=None) -> None:
        # spec_loader: (name) -> spec dict | None. Default reads the DB; the LLM
        # client is lazy (importing this module needs no Anthropic key).
        self._load = spec_loader or _db_spec_loader
        self._llm = llm or LlmExtractor()

    def extract(self, envelope: dict) -> ExtractionResult:
        spec = self._load(envelope["provenance"]["scrape_spec"])
        if spec is None:
            # Unknown spec name: write nothing, but don't raise — the worker
            # still acks/marks processed (same rule as an unknown extractor).
            return ExtractionResult()
        content = (
            envelope["payload"]
            if isinstance(envelope["payload"], str)
            else json.dumps(envelope["payload"])
        )
        instance = self._llm.extract(content, spec["output_schema"], model=spec.get("llm_model"))
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        obs, tags = apply_binding(instance, spec["binding"], fetched_at=fetched_at)
        return ExtractionResult(tags=tags, observations=obs)


def _db_spec_loader(name: str) -> dict | None:
    with get_conn() as c:  # read-only spec lookup (trusted worker has DB access)
        return get_spec(c, name)


register(LlmScrapeExtractor())
```

  Modify `src/bellweather/worker.py` — add the self-registering import alongside the existing gdelt one (after line 5):
```python
import bellweather.extractors.gdelt_gkg  # noqa: F401  (registers the extractor)
import bellweather.extractors.scrape_llm  # noqa: F401  (registers the extractor)
import bellweather.normalizers.numeric_series  # noqa: F401  (registers the normalizer)
```

  Modify `src/bellweather/ingest.py` — add `"scrape-llm-v1"` to the routable set:
```python
KNOWN_CONTENT_TYPES: set[str] = {"gdelt-gkg-v2", "numeric-series-v1", "scrape-llm-v1"}
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_scrape_extractor.py -v` → unit + routing tests pass; the `@requires_gcs` end-to-end test passes with `make up` running (auto-skips if the emulator is unreachable).

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green with `make up` running.

- [ ] **Step 6: Commit** (`feat: LlmScrapeExtractor + scrape-llm-v1 routing`).

## Acceptance criteria
- `src/bellweather/extractors/scrape_llm.py` defines `LlmScrapeExtractor` with `content_type = "scrape-llm-v1"`, an injectable `spec_loader` (default `_db_spec_loader`, which reads via `scrape.specs.get_spec` inside `get_conn()`) and an injectable `llm` (default `LlmExtractor()`), and calls `register(LlmScrapeExtractor())` at import.
- `extract(envelope)` loads the spec by `envelope["provenance"]["scrape_spec"]`, returns an empty `ExtractionResult()` (no raise) when the spec is `None`, otherwise feeds `envelope["payload"]` (a `str`, else `json.dumps`'d) + `spec["output_schema"]` to the LLM (passing `model=spec.get("llm_model")`), then `apply_binding(instance, spec["binding"], fetched_at=datetime.fromisoformat(envelope["fetched_at"]))` and returns `ExtractionResult(tags=tags, observations=obs)` (note the `(observations, tags)` unpack order).
- `worker.py` imports `bellweather.extractors.scrape_llm` (so the worker process registers the extractor); `ingest.KNOWN_CONTENT_TYPES` includes `"scrape-llm-v1"` (a `scrape-llm-v1` record ingests as `created`/routable, not `unroutable`).
- Unit tests inject a fixture spec-loader + a fake LLM and run with **no DB, no GCS, no key**; the spec-not-found case writes nothing and does not raise.
- The DB+GCS end-to-end test seeds a `scrape_specs` row, ingests a raw page, overrides the registry with a fake-LLM extractor (whose default DB loader reads the seeded row), runs the worker once, and finds one `observations` row keyed to the bound `tracked_symbol` (+ its tag); it carries `@requires_gcs` and the registry-snapshot fixture so it auto-skips without the emulator and never leaks the override.
- The GDELT path is unchanged (its extractor still returns `list[ExtractedTag]`); no new migration; `make check` green.
