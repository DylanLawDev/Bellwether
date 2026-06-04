# T33 — Fetch adapter seam + httpx default

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§3.1 fetch seam; K5).
**Depends on:** T00. **Branch:** `ticket/T33-fetch-adapter-seam`. **PR, do not merge without approval.**

## Goal
Add a pluggable **fetch adapter seam** so the scrape engine fetches a page through a swappable HTTP
provider rather than calling `httpx` inline — the K5 invariant ("fetching is an adapter, not a
hard-coded call"). It mirrors the existing extractor registry exactly: a `FetchResult` dataclass, a
`runtime_checkable` `FetchProvider` Protocol keyed by `name`, and `register` / `get_fetcher` /
`known_fetchers`. The default `HttpxFetcher` (`name="httpx"`) does a redirect-following `httpx.get`
and maps the response into a `FetchResult`, registering itself at import so `get_fetcher("httpx")`
works the moment the module is loaded. No secret, no DB, no GCS — this is pure, fixture-testable
infrastructure later consumed by the collector (T40) and the in-process preview (T39).

## Files
- Create: `src/bellweather/fetch/__init__.py` — `FetchResult` dataclass, `FetchProvider` Protocol,
  the `_REGISTRY` + `register` / `get_fetcher` / `known_fetchers` (mirrors
  `extractors/__init__.py`).
- Create: `src/bellweather/fetch/httpx_fetch.py` — `HttpxFetcher` (`name="httpx"`) wrapping
  `httpx.get(url, follow_redirects=True, timeout=30)`; calls `register(HttpxFetcher())` at import.
- Test: `tests/test_fetch.py` — registry round-trip + import-time registration + an `HttpxFetcher`
  fetch against a local `pytest-httpserver` (no DB, no GCS, no real network).

## Interface
Copied verbatim from the build plan's "Locked interfaces" (`fetch/__init__.py`):
```python
@dataclass
class FetchResult:
    content: str                 # raw page text (HTML / JSON / text)
    status: int
    content_type: str | None = None
    final_url: str | None = None

@runtime_checkable
class FetchProvider(Protocol):
    name: str
    def fetch(self, url: str, **opts) -> FetchResult: ...

def register(provider: FetchProvider) -> None: ...
def get_fetcher(name: str) -> FetchProvider | None: ...
def known_fetchers() -> set[str]: ...
```
`fetch/httpx_fetch.py` (locked) — `HttpxFetcher.name = "httpx"`; `fetch` does
`httpx.get(url, follow_redirects=True, timeout=30)`, returns
`FetchResult(resp.text, resp.status_code, resp.headers.get("content-type"), str(resp.url))`. Calls
`register(HttpxFetcher())` at import. **No secret.**

## Steps

> Not a DB/GCS ticket — no `make up` / `make migrate`. The whole suite runs without Postgres, GCS,
> or live network (the only "network" is a local `pytest-httpserver` bound to loopback).

- [ ] **Step 1: Failing test** `tests/test_fetch.py`. Three cases: (a) the registry round-trips a
  dummy provider (`get_fetcher` returns it, `known_fetchers` contains its name); (b) importing
  `httpx_fetch` registers `"httpx"`; (c) `HttpxFetcher().fetch(url)` against a local
  `pytest-httpserver` serving an HTML body + `200` maps every `FetchResult` field, including
  `content_type` and `final_url`. An autouse fixture snapshots/restores `_REGISTRY` so registering
  the dummy never leaks into other tests (mirrors `tests/test_extractor_registry.py`).
```python
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
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_fetch.py -v` →
  `ModuleNotFoundError: No module named 'bellweather.fetch'`.

- [ ] **Step 3: Implement.** Create the package and the default fetcher.

  `src/bellweather/fetch/__init__.py` — verbatim from the locked interface, mirroring
  `extractors/__init__.py`:
```python
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class FetchResult:
    content: str  # raw page text (HTML / JSON / text)
    status: int
    content_type: str | None = None
    final_url: str | None = None


@runtime_checkable
class FetchProvider(Protocol):
    name: str

    def fetch(self, url: str, **opts) -> FetchResult: ...


_REGISTRY: dict[str, FetchProvider] = {}


def register(provider: FetchProvider) -> None:
    _REGISTRY[provider.name] = provider


def get_fetcher(name: str) -> FetchProvider | None:
    return _REGISTRY.get(name)


def known_fetchers() -> set[str]:
    return set(_REGISTRY)
```

  `src/bellweather/fetch/httpx_fetch.py` — the `httpx` default; `register(...)` at import, no secret:
```python
import httpx

from bellweather.fetch import FetchProvider, FetchResult, register


class HttpxFetcher:
    """Default fetch adapter: a redirect-following httpx GET, no secret."""

    name = "httpx"

    def fetch(self, url: str, **opts) -> FetchResult:
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        return FetchResult(
            content=resp.text,
            status=resp.status_code,
            content_type=resp.headers.get("content-type"),
            final_url=str(resp.url),
        )


register(HttpxFetcher())


# Satisfy the runtime-checkable Protocol (kept for type-checkers / readers).
_: FetchProvider = HttpxFetcher()
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_fetch.py -v` → 4 passed.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green.
  (No `make up` needed for this file's tests, but the rest of the suite may want it — run the full
  gate as usual.)

- [ ] **Step 6: Commit** (`feat: fetch adapter seam + httpx default fetcher`).

## Acceptance criteria
- `bellweather.fetch` exposes `FetchResult` (dataclass: `content`, `status`, `content_type=None`,
  `final_url=None`), a `runtime_checkable` `FetchProvider` Protocol (`name: str`,
  `fetch(self, url, **opts) -> FetchResult`), and `register` / `get_fetcher` / `known_fetchers`
  with exactly the locked signatures — mirroring `extractors/__init__.py`.
- `register` keys the provider by its `name`; `get_fetcher` returns the registered provider (or
  `None` for an unknown name); `known_fetchers()` returns the set of registered names.
- Importing `bellweather.fetch.httpx_fetch` registers `"httpx"` (`get_fetcher("httpx").name ==
  "httpx"`) as a side effect.
- `HttpxFetcher.name == "httpx"`; `fetch(url)` issues `httpx.get(url, follow_redirects=True,
  timeout=30)` and returns `FetchResult(resp.text, resp.status_code,
  resp.headers.get("content-type"), str(resp.url))` — verified against a local `pytest-httpserver`,
  including `content_type` and `final_url`. No secret read; only `httpx` imported.
- Tests need **no Postgres, no GCS, no real network** (only a loopback `pytest-httpserver`); the
  registry-mutating test snapshots/restores `_REGISTRY` so it is order-independent.
- `make check` green.
