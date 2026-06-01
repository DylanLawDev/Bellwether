from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from bellweather.api import app
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from tests.conftest import requires_gcs

client = TestClient(app)

_KEYS = ("api-1", "api-b1", "api-b2")


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    # Tests use fixed idempotency keys, so clear any rows they left behind on a
    # prior run; otherwise the second run would see them as duplicates.
    with get_conn() as c:
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


def _rec(key):
    return dict(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc).isoformat(),
        idempotency_key=key,
        payload={"a": 1},
    )


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


@requires_gcs
def test_ingest_single_created_then_duplicate():
    r1 = client.post("/ingest", json=_rec("api-1"))
    assert r1.status_code == 200
    assert r1.json()["status"] == "created"
    r2 = client.post("/ingest", json=_rec("api-1"))
    assert r2.json()["status"] == "duplicate"


@requires_gcs
def test_ingest_batch_isolates_records():
    body = {"records": [_rec("api-b1"), _rec("api-b1"), _rec("api-b2")]}  # 2nd is dup of 1st
    res = client.post("/ingest/batch", json=body).json()["results"]
    statuses = [r["status"] for r in res]
    assert statuses == ["created", "duplicate", "created"]


@requires_gcs
def test_ingest_batch_isolates_invalid_record():
    # A malformed record in the middle must not fail the whole batch: it is
    # reported as "error" while the valid records on either side still ingest.
    body = {"records": [_rec("api-b1"), {"kind": "weird"}, _rec("api-b2")]}
    res = client.post("/ingest/batch", json=body)
    assert res.status_code == 200
    out = res.json()["results"]
    assert [r["status"] for r in out] == ["created", "error", "created"]
    assert out[1]["error"] and out[1]["raw_record_id"] is None


def test_ingest_validation_error_is_422():
    bad = _rec("api-x")
    bad["kind"] = "weird"
    assert client.post("/ingest", json=bad).status_code == 422
