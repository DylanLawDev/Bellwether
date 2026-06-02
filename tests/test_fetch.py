"""Fetch adapter seam: registry round-trip + httpx default.

Mirrors tests/test_extractor_registry.py (the sibling registry) for (a)/(b),
and tests/test_web_live.py's pytest-httpserver pattern for (c). No DB, no GCS,
no real network — the only server is a loopback pytest-httpserver.
"""

import pytest

from bellweather.fetch import (
    FetchProvider,
    FetchResult,
    _REGISTRY,
    get_fetcher,
    known_fetchers,
    register,
)


@pytest.fixture(autouse=True)
def _registry_snapshot():
    # Snapshot + restore so a dummy registered in one test never leaks into the
    # next (and so the import-time "httpx" registration survives the suite).
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class _DummyFetcher:
    name = "dummy"

    def fetch(self, url, **opts):
        return FetchResult(
            content=f"got {url}", status=200, content_type="text/plain", final_url=url
        )


# --- (a) registry round-trip -------------------------------------------------
def test_register_and_lookup():
    provider = _DummyFetcher()
    register(provider)
    assert get_fetcher("dummy") is provider
    assert "dummy" in known_fetchers()
    # A registered provider satisfies the runtime-checkable Protocol.
    assert isinstance(provider, FetchProvider)


def test_unknown_returns_none():
    assert get_fetcher("does-not-exist") is None


# --- (b) httpx_fetch registers itself at import ------------------------------
def test_importing_httpx_fetch_registers_httpx():
    import bellweather.fetch.httpx_fetch as httpx_fetch  # noqa: F401  (import has the side effect)

    assert "httpx" in known_fetchers()
    fetcher = get_fetcher("httpx")
    assert fetcher is not None
    assert fetcher.name == "httpx"
    assert isinstance(fetcher, FetchProvider)


# --- (c) HttpxFetcher against a local pytest-httpserver ----------------------
def test_httpx_fetcher_maps_response(httpserver):
    from bellweather.fetch.httpx_fetch import HttpxFetcher

    body = "<html><body><h1>hello</h1></body></html>"
    httpserver.expect_request("/page").respond_with_data(
        body, status=200, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/page")

    result = HttpxFetcher().fetch(url)

    assert isinstance(result, FetchResult)
    assert result.content == body
    assert result.status == 200
    assert result.content_type == "text/html; charset=utf-8"
    assert result.final_url == url
