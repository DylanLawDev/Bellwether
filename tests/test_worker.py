from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record
from bellweather.migrate import apply_migrations
from bellweather.worker import run_worker
from tests.conftest import clear_observations, clear_records, requires_gcs

_KEYS = ("wk-1", "wk-fail-1", "wk-agg-1", "wk-agg-2")
# Coverage symbols these tests write observations into. Their observation rows
# accumulate (value/sample_count) across runs and are SHARED between tests
# (e.g. theme:ECON_STOCKMARKET is touched by both the e2e test and the batch
# aggregation test), so we reset them per-test to keep value-sensitive
# assertions deterministic and order-independent.
_SYMBOLS = ("theme:ECON_STOCKMARKET",)


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    # Tests use fixed idempotency keys, so clear the rows they left behind on a
    # prior run; otherwise the second run would see them as duplicates.
    with get_conn() as c:
        clear_records(c, "gdelt.gkg", _KEYS)
        # Reset the shared coverage observations so value/sample_count assertions
        # start from a clean slate regardless of test order or prior runs.
        clear_observations(c, _SYMBOLS)
        c.commit()


@requires_gcs
def test_ingest_then_worker_creates_tags_and_observations():
    sub = Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="wk-1",
        payload={
            "v2_themes": "ECON_STOCKMARKET;TAX_FNCACT",
            "v2_persons": "Jerome Powell",
            "v2_organizations": "",
            "v2_locations": "",
            "v15_tone": "-2.13,0",
            "date": "2026-05-31T14:15:00Z",
        },
    )
    r = ingest_record(sub)
    assert r.status == "created"
    run_worker(once=True)
    with get_conn() as c:
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        nobs = c.execute(
            "select count(*) from observations o join tracked_symbols s on s.id=o.tracked_symbol_id "
            "where s.key='theme:ECON_STOCKMARKET'"
        ).fetchone()[0]
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
    assert ntags >= 3 and nobs == 1 and st == "processed"


@requires_gcs
def test_throwing_extractor_routes_through_fail(monkeypatch):
    # A routable record whose extractor raises must NOT crash run_worker; the job
    # is routed through queue.fail() -> state back to 'pending' (retry) with
    # attempts incremented and last_error set, and is never acked/processed.
    sub = Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="wk-fail-1",
        payload={"v2_themes": "ECON_STOCKMARKET", "v15_tone": "1.0,0"},
    )
    r = ingest_record(sub)
    assert r.status == "created"

    class _Boom:
        content_type = "gdelt-gkg-v2"

        def extract(self, envelope):
            raise RuntimeError("boom")

    # Patch the lookup the worker uses so the registered extractor raises.
    monkeypatch.setattr("bellweather.worker.get_extractor", lambda ct: _Boom())

    # Must return without raising.
    run_worker(once=True)

    with get_conn() as c:
        state, attempts, last_error = c.execute(
            "select state, attempts, last_error from work_queue where raw_record_id=%s",
            (r.raw_record_id,),
        ).fetchone()
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
        ntags = c.execute(
            "select count(*) from tags where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
    assert state == "pending"  # retry, not done/failed
    assert attempts >= 1
    assert last_error is not None and "boom" in last_error
    assert st == "received"  # never marked processed
    assert ntags == 0  # nothing written


@requires_gcs
def test_batch_leasing_and_coverage_aggregation():
    # Two routable records with the SAME fetched_at (-> same observation bucket)
    # and the SAME theme, but DISTINCT idempotency keys -> two queued jobs.
    fetched_at = datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc)
    rec_ids = []
    for key in ("wk-agg-1", "wk-agg-2"):
        sub = Submission(
            source="gdelt.gkg",
            kind="unstructured",
            content_type="gdelt-gkg-v2",
            fetched_at=fetched_at,
            idempotency_key=key,
            payload={"v2_themes": "ECON_STOCKMARKET", "v15_tone": "0,0"},
        )
        r = ingest_record(sub)
        assert r.status == "created"
        rec_ids.append(r.raw_record_id)

    # A single run_worker(once=True) leases a BATCH (limit=20) and must drain
    # BOTH jobs in one pass.
    run_worker(once=True)

    with get_conn() as c:
        statuses = [
            c.execute("select status from raw_records where id=%s", (rid,)).fetchone()[0]
            for rid in rec_ids
        ]
        # Exactly one observation row for the shared symbol+bucket, and its
        # value/sample_count reflect BOTH jobs (the DO UPDATE increment arm).
        nobs, value, sample_count = c.execute(
            "select count(*), max(o.value), max(o.sample_count) from observations o"
            " join tracked_symbols s on s.id=o.tracked_symbol_id"
            " where s.key='theme:ECON_STOCKMARKET'"
        ).fetchone()

    # Both processed -> the batch loop handled job #2 after committing job #1.
    assert statuses == ["processed", "processed"]
    assert nobs == 1
    assert value == 2.0
    assert sample_count == 2
