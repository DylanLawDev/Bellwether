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
