from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ExtractedTag:
    tag_type: str
    raw_value: str
    score: dict


@runtime_checkable
class Extractor(Protocol):
    content_type: str

    def extract(self, envelope: dict) -> list["ExtractedTag"]: ...


_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> None:
    _REGISTRY[extractor.content_type] = extractor


def get_extractor(content_type: str) -> Extractor | None:
    return _REGISTRY.get(content_type)


def known_content_types() -> set[str]:
    return set(_REGISTRY)
