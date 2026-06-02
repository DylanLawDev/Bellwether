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
            c,
            source=_SOURCE,
            content_type=_CT_RESULT,
            key="wkobs-result-1",
            fetched_at=fetched_at,
        )
        c.commit()

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (rid,)).fetchone()[0]
        ntags = c.execute("select count(*) from tags where raw_record_id=%s", (rid,)).fetchone()[0]
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
            c,
            source=_SOURCE,
            content_type=_CT_LEGACY,
            key="wkobs-legacy-1",
            fetched_at=fetched_at,
        )
        c.commit()

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (rid,)).fetchone()[0]
        ntags = c.execute("select count(*) from tags where raw_record_id=%s", (rid,)).fetchone()[0]
        # No value-bearing gold symbol was created for this legacy path. The
        # fixture deletes the scraped-metric symbol up front, so it must be absent.
        # (The tag's "category:widgets" coverage symbol may exist — that is fine.)
        nscraped = c.execute(
            "select count(*) from tracked_symbols where key = %s", (_GOLD_SYMBOL,)
        ).fetchone()[0]
    assert st == "processed"
    assert ntags == 1
    assert nscraped == 0  # legacy list[ExtractedTag] never lands a gold value
