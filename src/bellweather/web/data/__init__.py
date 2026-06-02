"""Data-access seam for the web UI.

Pages import the data API from here and nowhere else. The active backend is chosen
by the ``BELLWEATHER_UI_SOURCE`` environment variable (``mock`` default, or ``live``).
Swapping mock → live is the only change needed once real read-endpoints exist; no
screen code changes. See ``bellweather.web.data.source`` for the function/shape contract.
"""

from __future__ import annotations

import os

_BACKEND = os.environ.get("BELLWEATHER_UI_SOURCE", "mock").lower()

if _BACKEND == "live":
    from bellweather.web.data import live as _b
elif _BACKEND == "mock":
    from bellweather.web.data import mock as _b
else:
    raise ValueError(f"BELLWEATHER_UI_SOURCE must be 'mock' or 'live', got {_BACKEND!r}")

BACKEND = _BACKEND

get_tracked_symbols = _b.get_tracked_symbols
get_observations = _b.get_observations
query_raw_records = _b.query_raw_records
query_tags = _b.query_tags
get_queue_stats = _b.get_queue_stats
get_worker_runs = _b.get_worker_runs
get_ingestion_rate = _b.get_ingestion_rate
get_settings_view = _b.get_settings_view
get_schedules = _b.get_schedules
get_templates = _b.get_templates
get_runs = _b.get_runs
create_schedule = _b.create_schedule
update_schedule = _b.update_schedule
delete_schedule = _b.delete_schedule
force_schedule = _b.force_schedule
run_orchestrator_now = _b.run_orchestrator_now
preview_template = _b.preview_template
get_scrape_specs = _b.get_scrape_specs
get_scrape_spec = _b.get_scrape_spec
create_scrape_spec = _b.create_scrape_spec
update_scrape_spec = _b.update_scrape_spec
delete_scrape_spec = _b.delete_scrape_spec
preview_scrape_spec = _b.preview_scrape_spec

__all__ = [
    "BACKEND",
    "get_tracked_symbols",
    "get_observations",
    "query_raw_records",
    "query_tags",
    "get_queue_stats",
    "get_worker_runs",
    "get_ingestion_rate",
    "get_settings_view",
    "get_schedules",
    "get_templates",
    "get_runs",
    "create_schedule",
    "update_schedule",
    "delete_schedule",
    "force_schedule",
    "run_orchestrator_now",
    "preview_template",
    "get_scrape_specs",
    "get_scrape_spec",
    "create_scrape_spec",
    "update_scrape_spec",
    "delete_scrape_spec",
    "preview_scrape_spec",
]
