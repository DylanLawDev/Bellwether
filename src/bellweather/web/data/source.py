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
    get_schedules()                    -> DataFrame[id, name, template, interval_seconds,
                                                    enabled, force_run, last_run_at]
    get_templates()                    -> list[dict[name, description,
                                                    default_interval_seconds, params]]
    get_runs(schedule_id=None)         -> DataFrame[id, schedule_id, template, started_at,
                                                    finished_at, status, submitted, error]
    create_schedule(name, template, params, interval_seconds, enabled=True) -> int
    update_schedule(id, **fields)      -> None   # name|params|interval_seconds|enabled
    delete_schedule(id)                -> None
    force_schedule(id)                 -> None   # one-shot force_run flag
    run_orchestrator_now()             -> dict[started_run_ids]
    preview_template(name, params)     -> dict[symbols, sample]   # dry-run, commits nothing
    get_scrape_specs()                 -> DataFrame[id, name, description,
                                                    fetch_adapter, llm_model, enabled]
    get_scrape_spec(name)              -> dict   # full spec incl sites/output_schema/binding
    create_scrape_spec(name, sites, output_schema, binding, *, description=None,
                       fetch_adapter="httpx", llm_model=None) -> int
    update_scrape_spec(name, **fields) -> None   # name|description|sites|output_schema|
                                                 # binding|fetch_adapter|llm_model|enabled
    delete_scrape_spec(name)           -> None
    preview_scrape_spec(name, url=None) -> dict  # {extracted, symbols, sample, tags}; commits nothing
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
# `score` is a JSON object (the `tags.score` jsonb column), not a scalar — e.g.
# {"tone": -1.2} or {"count": 3}. A live backend must surface it as a dict, not flatten it.
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

# Producer orchestrator control plane (T26). `last_run_at`/`started_at`/`finished_at`
# are timestamps (None until set); `params` is carried per-template, not a column here.
SCHEDULE_COLUMNS = [
    "id",
    "name",
    "template",
    "interval_seconds",
    "enabled",
    "force_run",
    "last_run_at",
]
RUN_COLUMNS = [
    "id",
    "schedule_id",
    "template",
    "started_at",
    "finished_at",
    "status",
    "submitted",
    "error",
]

# Scrape-spec control plane (T41). `sites`/`output_schema`/`binding` are nested JSON
# carried per-spec (like `params` on a schedule), not flat columns here.
SCRAPE_SPEC_COLUMNS = [
    "id",
    "name",
    "description",
    "fetch_adapter",
    "llm_model",
    "enabled",
]
