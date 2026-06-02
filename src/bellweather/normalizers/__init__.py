from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass
class NormalizedPoint:
    symbol_key: str
    symbol_kind: str
    ts: datetime
    value: float
    unit: str | None = None
    description: str | None = None


@runtime_checkable
class Normalizer(Protocol):
    content_type: str

    def normalize(self, envelope: dict) -> list["NormalizedPoint"]: ...


_REGISTRY: dict[str, Normalizer] = {}


def register(normalizer: Normalizer) -> None:
    _REGISTRY[normalizer.content_type] = normalizer


def get_normalizer(content_type: str) -> Normalizer | None:
    return _REGISTRY.get(content_type)


def known_content_types() -> set[str]:
    return set(_REGISTRY)


# Self-registering normalizers — import triggers register() call at module level.
from bellweather.normalizers import numeric_series as _numeric_series  # noqa: E402, F401
