"""Live backend builds the same frames/dicts as mock from served API JSON.

Uses pytest-httpserver (a dev dep) to stand up a fake read API, points
BELLWEATHER_API_URL at it, and asserts each live.* function returns a
DataFrame/dict matching the bellweather.web.data.source contract constants.
No network.
"""

import pandas as pd
import pytest

from bellweather.config import get_ui_settings
from bellweather.web.data import live, source as contract

_TS = "2026-06-01T11:00:00+00:00"

_SYMBOLS = [
    {
        "id": 1,
        "key": "theme:ECON_STOCKMARKET",
        "tag_type": "theme",
        "raw_value": "ECON_STOCKMARKET",
        "kind": "coverage",
        "latest_value": 5.0,
        "total_samples": 8.0,
    }
]
_OBS = [{"ts_bucket": _TS, "key": "theme:ECON_STOCKMARKET", "value": 5.0, "sample_count": 5}]
_RECORDS = [
    {
        "id": 1,
        "source": "gdelt.gkg",
        "kind": "unstructured",
        "content_type": "gdelt-gkg-v2",
        "idempotency_key": "k1",
        "status": "processed",
        "fetched_at": _TS,
        "payload_uri": "gs://b/x.json",
    }
]
_TAGS = [
    {
        "id": 1,
        "raw_record_id": 1,
        "source": "gdelt.gkg",
        "tag_type": "tone",
        "raw_value": "tone",
        "observed_at": _TS,
        "score": {"tone": -1.5},
    }
]
_QUEUE = {"pending": 2, "leased": 1, "done": 3, "failed": 1}
_RATE = [{"hour": _TS, "records": 3}]
_CONFIG = [{"key": "database_url", "value": "postgresql://***@h/db", "note": "spine"}]


@pytest.fixture(autouse=True)
def _api(httpserver, monkeypatch):
    httpserver.expect_request("/api/symbols").respond_with_json(_SYMBOLS)
    httpserver.expect_request("/api/observations").respond_with_json(_OBS)
    httpserver.expect_request("/api/records").respond_with_json(_RECORDS)
    httpserver.expect_request("/api/tags").respond_with_json(_TAGS)
    httpserver.expect_request("/api/queue").respond_with_json(_QUEUE)
    httpserver.expect_request("/api/ingestion-rate").respond_with_json(_RATE)
    httpserver.expect_request("/api/config").respond_with_json(_CONFIG)
    monkeypatch.setenv("BELLWEATHER_API_URL", httpserver.url_for("").rstrip("/"))
    get_ui_settings.cache_clear()
    yield
    get_ui_settings.cache_clear()


def test_get_tracked_symbols():
    df = live.get_tracked_symbols()
    assert list(df.columns) == contract.TRACKED_SYMBOL_COLUMNS
    assert df.iloc[0]["key"] == "theme:ECON_STOCKMARKET"
    assert df.iloc[0]["latest_value"] == 5.0


def test_get_observations_parses_timestamp():
    df = live.get_observations(["theme:ECON_STOCKMARKET"])
    assert list(df.columns) == contract.OBSERVATION_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["ts_bucket"])
    assert df.iloc[0]["value"] == 5.0


def test_query_raw_records_parses_timestamp():
    df = live.query_raw_records(source="gdelt.gkg")
    assert list(df.columns) == contract.RAW_RECORD_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["fetched_at"])
    assert df.iloc[0]["idempotency_key"] == "k1"


def test_query_tags_score_is_dict():
    df = live.query_tags(tag_type="tone")
    assert list(df.columns) == contract.TAG_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["observed_at"])
    assert df.iloc[0]["score"] == {"tone": -1.5}


def test_get_queue_stats():
    assert live.get_queue_stats() == _QUEUE
    assert set(live.get_queue_stats()) == set(contract.QUEUE_STATES)


def test_get_ingestion_rate():
    df = live.get_ingestion_rate()
    assert list(df.columns) == contract.INGESTION_RATE_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(df["hour"])
    assert df.iloc[0]["records"] == 3


def test_get_settings_view():
    rows = live.get_settings_view()
    assert isinstance(rows, list)
    assert all(set(r) == {"key", "value", "note"} for r in rows)


def test_get_worker_runs_is_empty_but_shaped():
    # No backing worker_runs table yet (deferred); live returns an empty,
    # correctly-shaped frame so the Pipeline screen still renders.
    df = live.get_worker_runs()
    assert list(df.columns) == contract.WORKER_RUN_COLUMNS
    assert df.empty


def test_live_needs_no_db_or_gcs_env(monkeypatch, tmp_path, httpserver):
    # The UI may run as a thin client against a remote API with none of the
    # pipeline's DB/GCS secrets present. Isolate from the repo .env and clear the
    # env so only bellweather_api_url (defaulted) remains: the live backend must
    # still work — proving it doesn't build the full Settings (which requires
    # database_url + bellweather_bucket and would fail validation here).
    import pytest as _pytest

    from bellweather.config import Settings, UISettings, get_settings

    absent = str(tmp_path / "absent.env")
    monkeypatch.setattr(UISettings, "model_config", UISettings.model_config | {"env_file": absent})
    monkeypatch.setattr(Settings, "model_config", Settings.model_config | {"env_file": absent})
    for var in ("DATABASE_URL", "BELLWEATHER_BUCKET", "STORAGE_EMULATOR_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BELLWEATHER_API_URL", httpserver.url_for("").rstrip("/"))
    get_settings.cache_clear()
    get_ui_settings.cache_clear()

    # Guard the guard: the full pipeline Settings genuinely can't build here.
    with _pytest.raises(Exception):
        get_settings()
    # The live backend works anyway — it uses UISettings, not Settings.
    assert live.get_queue_stats() == _QUEUE
    get_settings.cache_clear()
    get_ui_settings.cache_clear()
