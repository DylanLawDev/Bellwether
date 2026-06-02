from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.ingest import KNOWN_CONTENT_TYPES, ingest_record
from bellweather.migrate import apply_migrations
from bellweather.worker import run_worker
from tests.conftest import clear_observations, clear_records, requires_gcs

_KEYS = ("st-num-1", "st-unknown-1", "st-gdelt-1")
_SYMBOLS = ("polymarket:demo:yes", "theme:ECON_STOCKMARKET")


def test_numeric_series_is_routable():
    # Unit-level: structured payloads must be enqueued, not parked as unroutable.
    assert "numeric-series-v1" in KNOWN_CONTENT_TYPES


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    with get_conn() as c:
        clear_records(c, "polymarket.demo", _KEYS)
        clear_records(c, "gdelt.gkg", _KEYS)
        clear_observations(c, _SYMBOLS)
        c.commit()


@requires_gcs
def test_structured_record_lands_an_observation():
    sub = Submission(
        source="polymarket.demo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-num-1",
        payload={
            "symbol_key": "polymarket:demo:yes",
            "symbol_kind": "market-probability",
            "unit": "probability",
            "description": "Will X happen by D? (YES)",
            "points": [
                {"ts": "2026-05-31T14:00:00Z", "value": 0.37},
                {"ts": "2026-05-31T15:00:00Z", "value": 0.41},
            ],
        },
    )
    r = ingest_record(sub)
    assert r.status == "created"

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
        rows = c.execute(
            "select o.value from observations o"
            " join tracked_symbols s on s.id=o.tracked_symbol_id"
            " where s.key='polymarket:demo:yes' order by o.ts_bucket"
        ).fetchall()
        kind, unit, descr = c.execute(
            "select kind, unit, description from tracked_symbols where key='polymarket:demo:yes'"
        ).fetchone()
    assert st == "processed"
    assert [v for (v,) in rows] == [0.37, 0.41]  # two hourly buckets, set-semantics
    assert kind == "market-probability"
    assert unit == "probability"
    assert descr == "Will X happen by D? (YES)"


@requires_gcs
def test_unknown_structured_content_type_is_unroutable():
    # Routable=False at ingest -> parked, never enqueued. Belt-and-braces: even if a
    # structured record reached the worker with no normalizer, it must mark
    # unroutable (no data lost), mirroring the unknown-extractor rule.
    sub = Submission(
        source="polymarket.demo",
        kind="structured",
        content_type="mystery-feed-v9",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-unknown-1",
        payload={"anything": 1},
    )
    r = ingest_record(sub)
    assert r.status == "unroutable"  # ingest parked it (not in KNOWN_CONTENT_TYPES)
    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
        nq = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
    assert st == "unroutable"
    assert nq == 0  # never enqueued -> the worker is never asked to route it


@requires_gcs
def test_unstructured_path_still_produces_tags():
    # Regression: the gdelt/unstructured branch is unchanged by kind-routing.
    sub = Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="st-gdelt-1",
        payload={
            "v2_themes": "ECON_STOCKMARKET",
            "v15_tone": "-2.13,0",
            "date": "2026-05-31T14:15:00Z",
        },
    )
    r = ingest_record(sub)
    assert r.status == "created"

    run_worker(once=True)

    with get_conn() as c:
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
    assert st == "processed"
    assert ntags >= 1
