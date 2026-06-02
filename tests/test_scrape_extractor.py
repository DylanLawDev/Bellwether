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
        "properties": {
            "name": {"type": "string"},
            "price": {"type": "number"},
            "category": {"type": "string"},
        },
        "required": ["name", "price"],
    },
    "binding": {
        "records_path": None,  # whole instance is ONE record
        "symbol_key": "scrape:prices:{category}:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",  # literal -> the fetched_at arg
        "unit": "usd",  # literal
        "description": "$.name",  # field ref
        "tags": ["category"],  # field name -> ExtractedTag
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
        "payload": "<html>widget 19.99</html>",  # raw page string
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
    assert obs.ts == fetched_at  # "fetched_at" literal -> the arg
    assert obs.unit == "usd"
    assert obs.description == "widget"  # "$.name" field ref
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


def test_extract_returns_empty_result_when_provenance_has_no_scrape_spec():
    # A scrape-llm-v1 record whose provenance lacks scrape_spec entirely (provenance
    # defaults to {}) must be treated like a missing spec — write nothing, no raise —
    # rather than KeyError'ing into a poison-message retry in the worker.
    loaded: list = []
    ex = LlmScrapeExtractor(
        spec_loader=lambda name: loaded.append(name) or None, llm=_FakeLlm(_INSTANCE)
    )
    envelope = {
        "payload": "irrelevant",
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "provenance": {},  # no scrape_spec key
    }
    result = ex.extract(envelope)
    assert isinstance(result, ExtractionResult)
    assert result.observations == []
    assert result.tags == []
    assert loaded == []  # loader is never called for a missing spec name


def test_extract_json_encodes_non_string_payload():
    # If the bronze payload is a dict (not a raw string), the extractor json-dumps
    # it before handing it to the LLM (the contract's content is always str).
    fake = _FakeLlm(_INSTANCE)
    ex = LlmScrapeExtractor(spec_loader=lambda name: _SPEC, llm=fake)
    envelope = {
        "payload": {"raw": {"a": 1}},  # dict payload
        "fetched_at": datetime(2026, 6, 1, tzinfo=timezone.utc).isoformat(),
        "provenance": {"scrape_spec": _SPEC_NAME},
    }
    ex.extract(envelope)
    sent_content = fake.calls[0][0]
    assert sent_content == '{"raw": {"a": 1}}'  # json.dumps of the dict


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
        delete_spec(c, _SPEC_NAME)  # idempotent: no-op if absent
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
        payload="<html>widget 19.99</html>",  # the raw page string
        provenance={"scrape_spec": _SPEC_NAME, "url": "http://example.test/prices"},
    )
    r = ingest_record(sub)
    assert r.status == "created"  # scrape-llm-v1 is now routable

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
    assert ntags == 1  # the `category` tag
