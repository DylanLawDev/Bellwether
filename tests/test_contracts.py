import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from bellweather.contracts import Submission

BASE = dict(
    source="gdelt.gkg",
    kind="unstructured",
    content_type="gdelt-gkg-v2",
    fetched_at=datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc),
    idempotency_key="k1",
)


def test_accepts_inline_payload():
    s = Submission(**BASE, payload={"a": 1})
    assert s.source == "gdelt.gkg" and s.kind == "unstructured"


def test_accepts_payload_uri():
    Submission(**BASE, payload_uri="gs://b/x.json")


def test_rejects_both_payload_and_uri():
    with pytest.raises(ValidationError):
        Submission(**BASE, payload={"a": 1}, payload_uri="gs://b/x")


def test_rejects_neither_payload_nor_uri():
    with pytest.raises(ValidationError):
        Submission(**BASE)


def test_rejects_naive_datetime():
    bad = {**BASE, "fetched_at": datetime(2026, 5, 31, 14, 15)}  # naive
    with pytest.raises(ValidationError):
        Submission(**bad, payload={"a": 1})


def test_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        Submission(**{**BASE, "kind": "weird"}, payload={"a": 1})
