from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from bellweather.normalizers import NormalizedPoint  # reuse the gold-value point shape


@dataclass
class ExtractedTag:
    tag_type: str
    raw_value: str
    score: dict


@dataclass
class ExtractionResult:
    tags: list["ExtractedTag"] = field(default_factory=list)
    observations: list[NormalizedPoint] = field(default_factory=list)


@runtime_checkable
class Extractor(Protocol):
    content_type: str

    def extract(self, envelope: dict) -> "list[ExtractedTag] | ExtractionResult": ...


_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> None:
    _REGISTRY[extractor.content_type] = extractor


def get_extractor(content_type: str) -> Extractor | None:
    return _REGISTRY.get(content_type)


def known_content_types() -> set[str]:
    return set(_REGISTRY)
