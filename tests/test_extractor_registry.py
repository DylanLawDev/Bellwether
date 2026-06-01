import pytest
from bellweather.extractors import (
    ExtractedTag,
    register,
    get_extractor,
    known_content_types,
    _REGISTRY,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class _Fake:
    content_type = "fake-v1"

    def extract(self, envelope):
        return [ExtractedTag(tag_type="theme", raw_value=envelope["payload"]["t"], score={})]


def test_register_and_lookup():
    register(_Fake())
    ex = get_extractor("fake-v1")
    assert ex is not None
    tags = ex.extract({"payload": {"t": "ECON"}})
    assert tags[0].raw_value == "ECON"
    assert "fake-v1" in known_content_types()


def test_unknown_returns_none():
    assert get_extractor("does-not-exist") is None
