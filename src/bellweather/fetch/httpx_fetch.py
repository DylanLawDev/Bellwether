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
