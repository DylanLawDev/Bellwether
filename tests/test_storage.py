from datetime import datetime, timezone

from bellweather.storage import get_bronze_store
from tests.conftest import requires_gcs


@requires_gcs
def test_put_then_get_roundtrip():
    store = get_bronze_store()
    ts = datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc)
    uri = store.put("gdelt.gkg", ts, "key-1", {"hello": "world"})
    assert uri.startswith("gs://")
    assert store.get(uri) == {"hello": "world"}


@requires_gcs
def test_put_is_idempotent_and_key_scheme():
    store = get_bronze_store()
    ts = datetime(2026, 5, 31, 14, 15, tzinfo=timezone.utc)
    assert store.object_key("gdelt.gkg", ts, "k") == "gdelt.gkg/2026/05/31/k.json"
    u1 = store.put("gdelt.gkg", ts, "dup", {"a": 1})
    u2 = store.put("gdelt.gkg", ts, "dup", {"a": 1})  # re-capture → no error
    assert u1 == u2
