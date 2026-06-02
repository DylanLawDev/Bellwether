# T40 — Scrape collector template (`producers/scrape/`)

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§3 Architecture; K6 rides the orchestrator as the generic template). **Depends on:** T08 (BellwetherClient + Submission contract), T22 (template manifest discovery `discover_templates`/`parse_interval`), T33 (fetch adapter seam: `get_fetcher` + `HttpxFetcher`), T39 (scrape-spec control-plane API: `GET /api/scrape-specs/{name}`). **Branch:** `ticket/T40-scrape-collector`. **PR, do not merge without approval.**

## Goal
Add the generic **scrape** orchestrator template — the thin, unprivileged collector that turns a DB-authored scrape spec into bronze raw pages. It rides the producer orchestrator (K6): the orchestrator fires `run(params, client)`, the collector reads `params["spec"]`, resolves the spec via `GET {BELLWEATHER_API_URL}/api/scrape-specs/{spec}` (the **API**, never the DB — it is unprivileged, the same external-producer exemption `producers/gdelt` uses), picks the spec's fetch adapter (`get_fetcher(spec["fetch_adapter"])` falling back to `HttpxFetcher()`), fetches each site, and `client.ingest(...)`s **one raw page per site** as `kind="unstructured"`, `content_type="scrape-llm-v1"`. The raw page lands in bronze immutably; the worker's `LlmScrapeExtractor` (T38) re-reads the spec from the DB and does the LLM extraction later — bronze-first, replayable. The page-content `sha1` in the idempotency key makes an unchanged page a `duplicate` no-op and a changed page a fresh bronze snapshot.

## Files
- Create: `producers/scrape/__init__.py` — package marker (so `producers.scrape.collector` is importable).
- Create: `producers/scrape/collector.py` — `run(params, client) -> {"submitted": int}`; resolves the spec via the API, fetches each site via the `fetch` seam, builds the locked Submission per page, ingests through the injected client. Reads only `BELLWEATHER_API_URL` from env (producer exemption); never touches the DB.
- Create: `producers/scrape/template.toml` — the manifest: `name="scrape"`, `entrypoint="producers.scrape.collector:run"`, a required `spec` param, `default_interval="6h"`.
- Test: `tests/test_scrape_collector.py` — unit tests with **no DB and no network**: inject a capturing client + monkeypatch the spec-GET (return a fixture spec dict) + monkeypatch `get_fetcher` to a fake returning a canned `FetchResult`; assert the built Submissions (source, content_type, kind, payload, idempotency_key shape, provenance). Plus a discovery test that `discover_templates()` over the default `producers/` dir includes `scrape` with the `spec` param **without importing the entrypoint**.

## Interface
Copied verbatim from the build plan's "Locked interfaces" (`docs/plans/2026-06-02-llm-scrape-engine.md`).

**Scrape collector contract (locked — `producers/scrape/`):**
- `template.toml`: `name = "scrape"`, `entrypoint = "producers.scrape.collector:run"`, `description`, `[params] spec = { type = "str", required = true, help = "scrape_specs.name" }`, `[schedule] default_interval = "6h"`.
- `def run(params: dict, client) -> dict:` reads `params["spec"]`, GETs `{BELLWEATHER_API_URL}/api/scrape-specs/{spec}` (producer reads the API URL from env — the same external-producer exemption `producers/gdelt` uses; it does NOT touch the DB), picks `get_fetcher(spec["fetch_adapter"]) or HttpxFetcher()`, fetches each `spec["sites"]`, and `client.ingest(...)` one raw page per site, returning `{"submitted": n}`.
- **Submission per page (locked):** `source=f"scrape:{spec_name}"`, `kind="unstructured"`, `content_type="scrape-llm-v1"`, `fetched_at=datetime.now(timezone.utc)`, `payload=res.content` (the raw page string), `idempotency_key=f"{spec_name}:{url}:{sha1(res.content)}"` (unchanged page → `duplicate` no-op; changed page → new bronze snapshot → re-extract), `provenance={"scrape_spec": spec_name, "url": url, "final_url": res.final_url, "fetch_status": res.status}`.

**Why the split paths to the spec (locked seam):** the **collector** runs unprivileged (orchestrator minimal-env, K4) so it reads the spec via the **API** (`GET /api/scrape-specs/{name}`); the **worker** is trusted (has DB) so its extractor reads the spec via **`scrape.specs.get_spec`** directly. One authored spec row, two read paths — never duplicated into schedule params.

Consumed seams (defined by dependencies, used verbatim here):

```python
# bellweather.contracts (T08)
class Submission(BaseModel):
    source: str; kind: Kind; content_type: str; fetched_at: datetime
    idempotency_key: str; payload: dict | str | None = None
    payload_uri: str | None = None; provenance: dict = {}

# bellweather.fetch (T33)
@dataclass
class FetchResult:
    content: str; status: int
    content_type: str | None = None; final_url: str | None = None

def get_fetcher(name: str) -> FetchProvider | None: ...
# bellweather.fetch.httpx_fetch.HttpxFetcher  (name == "httpx")
```

## Steps

- [ ] **Step 1: Failing test** `tests/test_scrape_collector.py`. Two halves — collector unit (inject a capturing client, stub the spec-GET + `get_fetcher`; no DB, no network) and manifest discovery over the **default** `producers/` dir (must not import the entrypoint). The capturing client mirrors `DryRunClient`'s surface (`ingest` returns a `created` `IngestResult`, captures the `Submission`).
```python
"""Scrape collector builds the locked raw-page Submissions (no DB, no network).

The collector is an UNPRIVILEGED external producer: it resolves its spec via the
control-plane API (GET /api/scrape-specs/{name}) and fetches each site through the
pluggable `fetch` seam. These tests stub both seams (the spec GET + get_fetcher)
and inject a capturing client, so nothing touches Postgres, GCS, or the network.
"""

import hashlib

from bellweather.contracts import IngestResult, Submission
from bellweather.fetch import FetchResult
from bellweather.templates import discover_templates

import producers.scrape.collector as collector


class _CapturingClient:
    """Same surface as BellwetherClient/DryRunClient; records every Submission."""

    def __init__(self) -> None:
        self.captured: list[Submission] = []

    def ingest(self, sub: Submission) -> IngestResult:
        self.captured.append(sub)
        return IngestResult(status="created")


class _FakeFetcher:
    """Canned fetcher: returns a fixed FetchResult per URL (no network)."""

    name = "httpx"

    def __init__(self, by_url: dict[str, FetchResult]) -> None:
        self._by_url = by_url

    def fetch(self, url: str, **opts) -> FetchResult:
        return self._by_url[url]


_SPEC = {
    "name": "prices",
    "sites": ["https://shop.test/a", "https://shop.test/b"],
    "fetch_adapter": "httpx",
    "output_schema": {"type": "object"},
    "binding": {"symbol_key": "scrape:prices:{name}", "value": "$.price"},
}

_PAGES = {
    "https://shop.test/a": FetchResult(
        content="<html>A 1.00</html>", status=200,
        content_type="text/html", final_url="https://shop.test/a?ok=1",
    ),
    "https://shop.test/b": FetchResult(
        content="<html>B 2.00</html>", status=200,
        content_type="text/html", final_url="https://shop.test/b",
    ),
}


def _patch_seams(monkeypatch, spec=_SPEC, pages=_PAGES):
    # Stub the spec GET (no HTTP) and the fetcher lookup (no network).
    monkeypatch.setattr(collector, "_get_spec", lambda spec_name: spec)
    monkeypatch.setattr(collector, "get_fetcher", lambda name: _FakeFetcher(pages))


def test_run_submits_one_raw_page_per_site(monkeypatch):
    _patch_seams(monkeypatch)
    client = _CapturingClient()

    result = collector.run({"spec": "prices"}, client)

    assert result == {"submitted": 2}
    assert len(client.captured) == 2
    urls = [s.provenance["url"] for s in client.captured]
    assert urls == ["https://shop.test/a", "https://shop.test/b"]


def test_submission_has_locked_shape(monkeypatch):
    _patch_seams(monkeypatch)
    client = _CapturingClient()

    collector.run({"spec": "prices"}, client)
    sub = client.captured[0]

    assert sub.source == "scrape:prices"
    assert sub.kind == "unstructured"
    assert sub.content_type == "scrape-llm-v1"
    # The raw page string is bronze — carried verbatim as the inline payload.
    assert sub.payload == "<html>A 1.00</html>"
    assert sub.fetched_at.tzinfo is not None  # tz-aware (Submission enforces UTC)


def test_idempotency_key_is_spec_url_and_content_sha1(monkeypatch):
    _patch_seams(monkeypatch)
    client = _CapturingClient()

    collector.run({"spec": "prices"}, client)
    sub = client.captured[0]

    page = _PAGES["https://shop.test/a"].content
    digest = hashlib.sha1(page.encode("utf-8")).hexdigest()
    assert sub.idempotency_key == f"prices:https://shop.test/a:{digest}"


def test_changed_page_changes_idempotency_key(monkeypatch):
    # A different page body → a different sha1 → a new bronze snapshot (re-extract).
    changed = dict(_PAGES)
    changed["https://shop.test/a"] = FetchResult(
        content="<html>A 9.99</html>", status=200, final_url="https://shop.test/a",
    )
    _patch_seams(monkeypatch, pages=changed)
    client = _CapturingClient()

    collector.run({"spec": "prices"}, client)
    keys = {s.provenance["url"]: s.idempotency_key for s in client.captured}
    assert keys["https://shop.test/a"].endswith(
        hashlib.sha1(b"<html>A 9.99</html>").hexdigest()
    )


def test_provenance_carries_spec_url_finalurl_status(monkeypatch):
    _patch_seams(monkeypatch)
    client = _CapturingClient()

    collector.run({"spec": "prices"}, client)
    prov = client.captured[0].provenance

    assert prov == {
        "scrape_spec": "prices",
        "url": "https://shop.test/a",
        "final_url": "https://shop.test/a?ok=1",
        "fetch_status": 200,
    }


def test_unknown_fetch_adapter_falls_back_to_httpx(monkeypatch):
    # get_fetcher(name) -> None must fall back to HttpxFetcher() (never crash).
    spec = dict(_SPEC, fetch_adapter="oxylabs")  # not registered
    monkeypatch.setattr(collector, "_get_spec", lambda spec_name: spec)
    monkeypatch.setattr(collector, "get_fetcher", lambda name: None)

    captured_fetchers: list[str] = []

    class _Httpx:
        name = "httpx"

        def fetch(self, url, **opts):
            captured_fetchers.append(url)
            return FetchResult(content="x", status=200, final_url=url)

    monkeypatch.setattr(collector, "HttpxFetcher", _Httpx)
    client = _CapturingClient()

    result = collector.run({"spec": "prices"}, client)
    assert result == {"submitted": 2}
    assert captured_fetchers == _SPEC["sites"]


def test_run_reads_spec_via_api_not_db(monkeypatch):
    # The collector resolves its spec through the module-level _get_spec seam (which
    # hits the API URL), NOT a server-side DB helper. Stub _get_spec and assert it is
    # the path the collector takes and that it receives the requested spec name.
    seen = {}

    def _fake_get_spec(spec_name):
        seen["name"] = spec_name
        return _SPEC

    monkeypatch.setattr(collector, "_get_spec", _fake_get_spec)
    monkeypatch.setattr(collector, "get_fetcher", lambda name: _FakeFetcher(_PAGES))
    collector.run({"spec": "prices"}, _CapturingClient())
    assert seen["name"] == "prices"


# --- manifest discovery (default producers/ dir; no entrypoint import) --------
def test_discover_default_dir_includes_scrape_with_spec_param():
    # Default templates dir is "producers" (config default) — discovery scans the
    # real producers/scrape/template.toml shipped by this ticket.
    found = discover_templates()  # default dir
    assert "scrape" in found
    scrape = found["scrape"]
    assert scrape.entrypoint == "producers.scrape.collector:run"
    assert scrape.default_interval_seconds == 6 * 3600
    by_name = {p.name: p for p in scrape.params}
    assert by_name["spec"].required is True
    assert by_name["spec"].type == "str"


def test_discovery_does_not_import_collector():
    import sys

    sys.modules.pop("producers.scrape.collector", None)
    discover_templates()  # default dir
    assert "producers.scrape.collector" not in sys.modules
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_scrape_collector.py -v` →
  `ModuleNotFoundError: No module named 'producers.scrape'` (the package + manifest don't exist yet).

- [ ] **Step 3: Implement.** Create the package marker, the manifest, and the collector.

  `producers/scrape/__init__.py` (empty marker):
```python
```

  `producers/scrape/template.toml`:
```toml
name        = "scrape"
entrypoint  = "producers.scrape.collector:run"
description = "Generic schema-driven scrape collector: fetch each site in a scrape spec and POST the raw page to /ingest (LLM extraction happens worker-side)."

[params]
spec = { type = "str", required = true, help = "scrape_specs.name" }

[schedule]
default_interval = "6h"
```

  `producers/scrape/collector.py` — module-level `get_fetcher`/`HttpxFetcher`/`_get_spec` so the tests can monkeypatch each seam. Reads `BELLWEATHER_API_URL` from env directly (the documented external-producer exemption — same as `producers/gdelt`; it must NOT reach `get_settings()` or the DB):
```python
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
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_scrape_collector.py -v` → all tests pass (no DB, no network).

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green with `make up` running (the new tests need neither, but the suite as a whole does).

- [ ] **Step 6: Commit** (`feat: add scrape collector template (producers/scrape)`).

## Acceptance criteria
- `producers/scrape/template.toml` declares `name="scrape"`, `entrypoint="producers.scrape.collector:run"`, a required `str` `spec` param (`help="scrape_specs.name"`), and `[schedule] default_interval="6h"`; `discover_templates()` over the **default** `producers/` dir includes `scrape` with that param and `default_interval_seconds == 21600`, **without importing** `producers.scrape.collector`.
- `producers.scrape.collector.run(params, client) -> {"submitted": n}` reads `params["spec"]`, resolves the spec via `GET {BELLWEATHER_API_URL}/api/scrape-specs/{spec}` (API only — no DB, no `get_settings()`), picks `get_fetcher(spec["fetch_adapter"]) or HttpxFetcher()`, fetches each `spec["sites"]`, and `client.ingest(...)`s one Submission per site.
- Each Submission matches the locked shape: `source=f"scrape:{spec_name}"`, `kind="unstructured"`, `content_type="scrape-llm-v1"`, tz-aware `fetched_at`, `payload=res.content` (raw page string), `idempotency_key=f"{spec_name}:{url}:{sha1(res.content).hexdigest()}"`, `provenance={scrape_spec, url, final_url, fetch_status}`.
- An unknown/unregistered `fetch_adapter` (`get_fetcher` returns `None`) falls back to `HttpxFetcher()` rather than crashing; a changed page body yields a different `idempotency_key` (new bronze snapshot → re-extract), an unchanged page yields the same key (`duplicate` no-op).
- Tests inject a capturing client + stub the spec-GET and `get_fetcher`; **no Postgres, no GCS, no network**. `make check` green.
