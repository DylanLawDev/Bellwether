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


class DryRunClient:
    """Same surface as ``BellwetherClient`` but performs no I/O.

    Captures every submission in ``.captured`` and returns ``created`` results.
    Used by the dry-run preview (K9) and by the run-harness under ``--dry-run``;
    commits nothing, makes no HTTP.
    """

    def __init__(self) -> None:
        self.captured: list[Submission] = []

    def ingest(self, sub: Submission) -> IngestResult:
        self.captured.append(sub)
        return IngestResult(status="created")

    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]:
        self.captured.extend(subs)
        return [IngestResult(status="created") for _ in subs]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()
