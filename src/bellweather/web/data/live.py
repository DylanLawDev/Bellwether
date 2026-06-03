"""Live backend — the "endpoints after" half of the seam.

Each function issues an httpx call to a Bellwether read-endpoint
(``GET /api/...``, see ``bellweather.reads`` / the ``/api`` router) and assembles
the **same** DataFrame/dict shapes the mock backend returns (column/key contract
in ``bellweather.web.data.source``). Signatures are kept byte-identical to
``mock.py`` so the screens never know which backend is active — only
``BELLWEATHER_UI_SOURCE`` flips from ``mock`` to ``live``.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pandas as pd

from bellweather.config import get_ui_settings
from bellweather.web.data import source as contract

_TIMEOUT = 30.0
# Orchestrator run-now and preview spawn producer subprocesses synchronously
# (up to orchestrator's 600s subprocess cap), so they need a longer client
# timeout than the read endpoints.
_LONG_TIMEOUT = 600.0


def _get(path: str, **params) -> object:
    """GET ``{bellweather_api_url}{path}`` with ``params`` and return parsed JSON.

    ``None`` params are dropped; ``datetime`` params are sent as ISO-8601. The
    base URL is read at call time (not import) so tests can repoint it. Uses
    UISettings (not the full pipeline Settings) so a client-only UI environment
    needs no DB/GCS secrets to reach a remote API.
    """
    clean: dict = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[key] = value.isoformat() if isinstance(value, datetime) else value
    base = get_ui_settings().bellweather_api_url
    with httpx.Client(base_url=base, timeout=_TIMEOUT) as client:
        resp = client.get(path, params=clean)
        resp.raise_for_status()
        return resp.json()


def _frame(rows, columns, ts_cols=()):
    df = pd.DataFrame(rows, columns=columns)
    for col in ts_cols:
        if not df.empty:
            df[col] = pd.to_datetime(df[col])
    return df


def get_tracked_symbols() -> pd.DataFrame:
    return _frame(_get("/api/symbols"), contract.TRACKED_SYMBOL_COLUMNS)


def get_observations(keys, start=None, end=None) -> pd.DataFrame:
    rows = _get("/api/observations", keys=list(keys), start=start, end=end)
    return _frame(rows, contract.OBSERVATION_COLUMNS, ts_cols=("ts_bucket",))


def query_raw_records(
    source=None,
    content_type=None,
    status=None,
    search=None,
    start=None,
    end=None,
    limit=100,
    offset=0,
) -> pd.DataFrame:
    rows = _get(
        "/api/records",
        source=source,
        content_type=content_type,
        status=status,
        search=search,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
    return _frame(rows, contract.RAW_RECORD_COLUMNS, ts_cols=("fetched_at",))


def query_tags(
    tag_type=None, search=None, start=None, end=None, limit=100, offset=0
) -> pd.DataFrame:
    rows = _get(
        "/api/tags",
        tag_type=tag_type,
        search=search,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )
    return _frame(rows, contract.TAG_COLUMNS, ts_cols=("observed_at",))


def get_queue_stats() -> dict:
    return _get("/api/queue")


def get_worker_runs() -> pd.DataFrame:
    # No backing worker_runs table in the schema yet (deferred — would need a
    # small migration; see T15). Return an empty, correctly-shaped frame so the
    # Pipeline screen renders queue + ingestion-rate without a worker-run history.
    return pd.DataFrame(columns=contract.WORKER_RUN_COLUMNS)


def get_ingestion_rate() -> pd.DataFrame:
    return _frame(_get("/api/ingestion-rate"), contract.INGESTION_RATE_COLUMNS, ts_cols=("hour",))


def get_settings_view() -> list[dict]:
    return _get("/api/config")


def _request(
    method: str, path: str, json: dict | None = None, *, timeout: float = _TIMEOUT, **params
) -> object:
    """Issue ``method {bellweather_api_url}{path}`` and return parsed JSON.

    Mirrors ``_get`` but covers the write verbs (POST/PATCH/DELETE) the Schedules
    control plane needs. ``None`` query params are dropped; the base URL is read
    at call time via ``UISettings`` (no DB/GCS secrets needed). ``timeout``
    defaults to the read timeout; long-running writes (run-now, preview) pass
    ``_LONG_TIMEOUT``.
    """
    clean = {k: v for k, v in params.items() if v is not None}
    base = get_ui_settings().bellweather_api_url
    with httpx.Client(base_url=base, timeout=timeout) as client:
        resp = client.request(method, path, json=json, params=clean)
        resp.raise_for_status()
        return resp.json()


def get_schedules() -> pd.DataFrame:
    return _frame(_get("/api/schedules"), contract.SCHEDULE_COLUMNS, ts_cols=("last_run_at",))


def get_templates() -> list[dict]:
    return _get("/api/templates")


def get_runs(schedule_id=None) -> pd.DataFrame:
    rows = _get("/api/runs", schedule_id=schedule_id)
    return _frame(rows, contract.RUN_COLUMNS, ts_cols=("started_at", "finished_at"))


def create_schedule(name, template, params, interval_seconds, enabled=True) -> int:
    body = {
        "name": name,
        "template": template,
        "params": params,
        "interval_seconds": int(interval_seconds),
        "enabled": bool(enabled),
    }
    return _request("POST", "/api/schedules", json=body)["id"]


def update_schedule(id, **fields) -> None:
    _request("PATCH", f"/api/schedules/{id}", json=fields)


def delete_schedule(id) -> None:
    _request("DELETE", f"/api/schedules/{id}")


def force_schedule(id) -> None:
    _request("POST", f"/api/schedules/{id}/force")


def run_orchestrator_now() -> dict:
    return _request("POST", "/api/orchestrator/run", timeout=_LONG_TIMEOUT)


def preview_template(name, params) -> dict:
    # The preview endpoint binds the whole JSON body to its ``params`` arg, so
    # post ``params`` unwrapped (wrapping it as {"params": ...} makes the server
    # reject "params" as an unknown template param). Returns {submitted, symbols,
    # sample}.
    return _request("POST", f"/api/templates/{name}/preview", json=params, timeout=_LONG_TIMEOUT)


def get_fetch_adapter_choices() -> list[str]:
    return _get("/api/fetch-adapters")["adapters"]


def get_scrape_specs() -> pd.DataFrame:
    return _frame(_get("/api/scrape-specs"), contract.SCRAPE_SPEC_COLUMNS)


def get_scrape_spec(name) -> dict:
    return _get(f"/api/scrape-specs/{name}")


def create_scrape_spec(
    name, sites, output_schema, binding, *, description=None, fetch_adapter="httpx", llm_model=None
) -> int:
    body = {
        "name": name,
        "sites": sites,
        "output_schema": output_schema,
        "binding": binding,
        "description": description,
        "fetch_adapter": fetch_adapter,
        "llm_model": llm_model,
    }
    return _request("POST", "/api/scrape-specs", json=body)["id"]


def update_scrape_spec(name, **fields) -> None:
    _request("PATCH", f"/api/scrape-specs/{name}", json=fields)


def delete_scrape_spec(name) -> None:
    _request("DELETE", f"/api/scrape-specs/{name}")


def preview_scrape_spec(name, url=None) -> dict:
    # Trusted in-process dry-run (K10): the API fetches one URL + LLM-extracts +
    # binds, committing nothing. Holds the LLM key, so it can take orchestrator's
    # long timeout. Returns {extracted, symbols, sample, tags}.
    return _request(
        "POST", f"/api/scrape-specs/{name}/preview", json={"url": url}, timeout=_LONG_TIMEOUT
    )
