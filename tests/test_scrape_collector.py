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
        content="<html>A 1.00</html>",
        status=200,
        content_type="text/html",
        final_url="https://shop.test/a?ok=1",
    ),
    "https://shop.test/b": FetchResult(
        content="<html>B 2.00</html>",
        status=200,
        content_type="text/html",
        final_url="https://shop.test/b",
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
        content="<html>A 9.99</html>",
        status=200,
        final_url="https://shop.test/a",
    )
    _patch_seams(monkeypatch, pages=changed)
    client = _CapturingClient()

    collector.run({"spec": "prices"}, client)
    keys = {s.provenance["url"]: s.idempotency_key for s in client.captured}
    assert keys["https://shop.test/a"].endswith(hashlib.sha1(b"<html>A 9.99</html>").hexdigest())


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
