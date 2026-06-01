import httpx

from bellweather.config import get_settings
from bellweather.contracts import IngestResult, Submission


class BellwetherClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self._base = (base_url or get_settings().bellweather_api_url).rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def ingest(self, sub: Submission) -> IngestResult:
        resp = self._client.post(f"{self._base}/ingest", json=sub.model_dump(mode="json"))
        resp.raise_for_status()
        return IngestResult.model_validate(resp.json())

    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]:
        body = {"records": [s.model_dump(mode="json") for s in subs]}
        resp = self._client.post(f"{self._base}/ingest/batch", json=body)
        resp.raise_for_status()
        return [IngestResult.model_validate(r) for r in resp.json()["results"]]

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()
