from datetime import datetime, timezone

import pytest

from bellweather.contracts import Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record
from bellweather.migrate import apply_migrations
from bellweather.storage import get_bronze_store
from tests.conftest import requires_gcs


_KEYS = ("k-created-1", "k-dup", "k-unr", "k-uri-passthrough")


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


def _sub(key, content_type="gdelt-gkg-v2", payload={"a": 1}):
    return Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type=content_type,
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key=key,
        payload=payload,
    )


@requires_gcs
def test_created_writes_bronze_and_enqueues():
    r = ingest_record(_sub("k-created-1"))
    assert r.status == "created" and r.payload_uri.startswith("gs://")
    with get_conn() as c:
        q = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        rr = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
    assert q == 1 and rr == "received"


@requires_gcs
def test_payload_uri_passthrough_does_not_write_bronze():
    store = get_bronze_store()
    # A key that does not exist in the emulator bucket. If ingest wrongly called
    # bronze.put() for the passthrough branch it would create this object.
    passthrough_key = "preexisting/key.json"
    passthrough_uri = f"gs://{store._bucket_name}/{passthrough_key}"
    # Clean any leftover from a prior run so the "did not write" check is honest.
    blob = store._bucket.blob(passthrough_key)
    if blob.exists():
        blob.delete()
    assert not store._bucket.blob(passthrough_key).exists()

    sub = Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key="k-uri-passthrough",
        payload=None,
        payload_uri=passthrough_uri,
    )
    r = ingest_record(sub)
    assert r.status == "created"
    assert r.payload_uri == passthrough_uri  # verbatim, untouched
    with get_conn() as c:
        q = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        rr = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
    assert q == 1 and rr == "received"
    # Proof bronze.put() was NOT called: the passthrough key still does not exist.
    assert not store._bucket.blob(passthrough_key).exists()


@requires_gcs
def test_duplicate_is_noop():
    r1 = ingest_record(_sub("k-dup"))
    r2 = ingest_record(_sub("k-dup"))
    assert r2.status == "duplicate" and r2.raw_record_id == r1.raw_record_id
    with get_conn() as c:
        n = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r1.raw_record_id,)
        ).fetchone()[0]
    assert n == 1  # not enqueued twice


@requires_gcs
def test_unknown_content_type_is_unroutable_not_enqueued():
    r = ingest_record(_sub("k-unr", content_type="mystery-v9"))
    assert r.status == "unroutable"
    with get_conn() as c:
        n = c.execute(
            "select count(*) from work_queue where raw_record_id=%s", (r.raw_record_id,)
        ).fetchone()[0]
        st = c.execute("select status from raw_records where id=%s", (r.raw_record_id,)).fetchone()[
            0
        ]
    assert n == 0 and st == "unroutable"
