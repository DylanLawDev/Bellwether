"""The data-access interface for the web UI.

This module documents the one seam between the screens and their data. Every
page imports from ``bellweather.web.data`` (which re-exports the *active* backend
selected by ``BELLWEATHER_UI_SOURCE``), never from ``mock`` or ``live`` directly.

A backend is any module exposing the functions below. Tabular results are pandas
``DataFrame``s with the fixed columns noted; aggregates are plain dicts/lists. The
column/key contract is what lets ``live.py`` build identical shapes from API JSON
later without any screen changing.

    get_tracked_symbols()              -> DataFrame[id, key, tag_type, raw_value,
                                                    kind, latest_value, total_samples]
    get_observations(keys, start, end) -> DataFrame[ts_bucket, key, value, sample_count]
    query_raw_records(...)             -> DataFrame[id, source, kind, content_type,
                                                    idempotency_key, status, fetched_at,
                                                    payload_uri]
    query_tags(...)                    -> DataFrame[id, raw_record_id, source, tag_type,
                                                    raw_value, observed_at, score]
    get_queue_stats()                  -> dict[pending, leased, done, failed]
    get_worker_runs()                  -> DataFrame[run_at, leased, processed, failed,
                                                    duration_s]
    get_ingestion_rate()               -> DataFrame[hour, records]
    get_settings_view()                -> list[dict[key, value, note]]
"""

# Column contracts, importable by both backends and tests so the shapes stay in sync.
TRACKED_SYMBOL_COLUMNS = [
    "id",
    "key",
    "tag_type",
    "raw_value",
    "kind",
    "latest_value",
    "total_samples",
]
OBSERVATION_COLUMNS = ["ts_bucket", "key", "value", "sample_count"]
RAW_RECORD_COLUMNS = [
    "id",
    "source",
    "kind",
    "content_type",
    "idempotency_key",
    "status",
    "fetched_at",
    "payload_uri",
]
TAG_COLUMNS = [
    "id",
    "raw_record_id",
    "source",
    "tag_type",
    "raw_value",
    "observed_at",
    "score",
]
WORKER_RUN_COLUMNS = ["run_at", "leased", "processed", "failed", "duration_s"]
INGESTION_RATE_COLUMNS = ["hour", "records"]
QUEUE_STATES = ["pending", "leased", "done", "failed"]
