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
    sub = _sub("c1")
    expected_body = sub.model_dump(mode="json")
    httpserver.expect_request("/ingest", method="POST", json=expected_body).respond_with_json(
        {"raw_record_id": 7, "status": "created", "payload_uri": "gs://b/x"}
    )
    c = BellwetherClient(base_url=httpserver.url_for(""))
    r = c.ingest(sub)
    assert r.raw_record_id == 7 and r.status == "created"


def test_ingest_batch(httpserver):
    subs = [_sub("c1"), _sub("c2")]
    expected_body = {"records": [s.model_dump(mode="json") for s in subs]}
    httpserver.expect_request("/ingest/batch", method="POST", json=expected_body).respond_with_json(
        {
            "results": [
                {"raw_record_id": 1, "status": "created", "payload_uri": "gs://b/1"},
                {"raw_record_id": 2, "status": "duplicate", "payload_uri": "gs://b/2"},
            ]
        }
    )
    c = BellwetherClient(base_url=httpserver.url_for(""))
    rs = c.ingest_batch(subs)
    assert [r.status for r in rs] == ["created", "duplicate"]
    assert [r.raw_record_id for r in rs] == [1, 2]


def test_client_context_manager(httpserver):
    sub = _sub("c1")
    expected_body = sub.model_dump(mode="json")
    httpserver.expect_request("/ingest", method="POST", json=expected_body).respond_with_json(
        {"raw_record_id": 99, "status": "created", "payload_uri": "gs://b/z"}
    )
    with BellwetherClient(base_url=httpserver.url_for("")) as c:
        r = c.ingest(sub)
    assert r.raw_record_id == 99 and r.status == "created"
