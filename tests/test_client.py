from datetime import datetime, timezone

from bellweather.client import BellwetherClient
from bellweather.contracts import Submission


def _sub(key):
    return Submission(
        source="gdelt.gkg",
        kind="unstructured",
        content_type="gdelt-gkg-v2",
        fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
        idempotency_key=key,
        payload={"a": 1},
    )


def test_ingest_posts_and_parses(httpserver):
    httpserver.expect_request("/ingest", method="POST").respond_with_json(
        {"raw_record_id": 7, "status": "created", "payload_uri": "gs://b/x"}
    )
    c = BellwetherClient(base_url=httpserver.url_for(""))
    r = c.ingest(_sub("c1"))
    assert r.raw_record_id == 7 and r.status == "created"


def test_ingest_batch(httpserver):
    httpserver.expect_request("/ingest/batch", method="POST").respond_with_json(
        {
            "results": [
                {"raw_record_id": 1, "status": "created", "payload_uri": "gs://b/1"},
                {"raw_record_id": 1, "status": "duplicate", "payload_uri": "gs://b/1"},
            ]
        }
    )
    c = BellwetherClient(base_url=httpserver.url_for(""))
    rs = c.ingest_batch([_sub("c1"), _sub("c1")])
    assert [r.status for r in rs] == ["created", "duplicate"]
