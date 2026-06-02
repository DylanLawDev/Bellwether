"""Generic scrape collector — the orchestrator template for the LLM scrape engine.

UNPRIVILEGED external producer (orchestrator minimal-env, K4/K6): it resolves its
spec via the control-plane API (GET /api/scrape-specs/{name}) and fetches each
site through the pluggable `fetch` seam, then POSTs each raw page to /ingest as
kind="unstructured", content_type="scrape-llm-v1". Bronze keeps the raw page; the
worker's LlmScrapeExtractor (trusted, DB-backed) re-reads the spec and does the
LLM extraction later — bronze-first and replayable. This module touches neither
the DB nor the server settings; like producers/gdelt it reads only the public API
URL from the environment.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

import httpx

from bellweather.contracts import Submission
from bellweather.fetch import get_fetcher
from bellweather.fetch.httpx_fetch import HttpxFetcher

_API_TIMEOUT = 30.0


def _api_base() -> str:
    # External-producer exemption: the collector has only BELLWEATHER_API_URL, never
    # the server's DB/storage settings. Read it straight from the env (no get_settings).
    return os.environ.get("BELLWEATHER_API_URL", "http://localhost:8000").rstrip("/")


def _get_spec(spec_name: str) -> dict:
    """Resolve the scrape spec via the control-plane API (never the DB)."""
    resp = httpx.get(f"{_api_base()}/api/scrape-specs/{spec_name}", timeout=_API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def run(params: dict, client) -> dict:
    """Fetch every site in the named spec and ingest one raw page per site."""
    spec_name = params["spec"]
    spec = _get_spec(spec_name)
    fetcher = get_fetcher(spec.get("fetch_adapter") or "httpx") or HttpxFetcher()

    submitted = 0
    for url in spec.get("sites", []):
        res = fetcher.fetch(url)
        digest = hashlib.sha1(res.content.encode("utf-8")).hexdigest()
        sub = Submission(
            source=f"scrape:{spec_name}",
            kind="unstructured",
            content_type="scrape-llm-v1",
            fetched_at=datetime.now(timezone.utc),
            idempotency_key=f"{spec_name}:{url}:{digest}",
            payload=res.content,
            provenance={
                "scrape_spec": spec_name,
                "url": url,
                "final_url": res.final_url,
                "fetch_status": res.status,
            },
        )
        client.ingest(sub)
        submitted += 1
    return {"submitted": submitted}
