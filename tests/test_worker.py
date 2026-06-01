from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record
from bellweather.migrate import apply_migrations
from bellweather.worker import run_worker
from tests.conftest import requires_gcs

_KEYS = ("wk-1", "wk-fail-1")


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    # Tests use fixed idempotency keys, so clear the rows they left behind on a
    # prior run; otherwise the second run would see them as duplicates. tags and
    # work_queue both FK-reference raw_records, so delete children before parents.
    with get_conn() as c:
        c.execute(
            "delete from tags where raw_record_id in"
            " (select id from raw_records where source='gdelt.gkg' and idempotency_key = any(%s))",
            (list(_KEYS),),
        )
        c.execute(
            "delete from work_queue where raw_record_id in"
            " (select id from raw_records where source='gdelt.gkg' and idempotency_key = any(%s))",
            (list(_KEYS),),
        )
        c.execute(
            "delete from raw_records where source='gdelt.gkg' and idempotency_key = any(%s)",
            (list(_KEYS),),
        )
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
