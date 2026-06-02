"""LlmExtractor — schema-constrained Anthropic tool-use wrapper (lazy client).

The unit tests inject a fake anthropic.Anthropic (no network, no real key): its
messages.create(**kw) records the kwargs and returns a response whose .content is
a single tool_use block carrying canned .input. They assert the locked call shape
(temperature=0, tool_choice -> the "emit" tool, output_schema as input_schema, the
resolved model) and that extract() returns the tool's .input. A separate case
asserts RuntimeError when no key is configured. One @requires_llm test is the
opt-in live smoke (auto-skipped when ANTHROPIC_API_KEY is unset). No DB, no GCS.
"""

import types

import pytest

import bellweather.llm as llm_mod
from bellweather.config import get_settings
from bellweather.llm import LlmExtractor
from tests.conftest import requires_llm


class _FakeMessages:
    """Records the kwargs of the most recent create() and returns canned tool_use."""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        block = types.SimpleNamespace(type="tool_use", name="emit", input=self.result)
        return types.SimpleNamespace(content=[block])


class _FakeAnthropic:
    """Stand-in for anthropic.Anthropic; remembers the api_key it was built with.

    Class-level ``result`` / ``last_api_key`` are set per-test (in the _fake
    fixture) BEFORE the extractor constructs the client inside extract().
    """

    last_api_key: str | None = None
    result: dict = {}

    def __init__(self, *, api_key=None):
        type(self).last_api_key = api_key
        self.messages = _FakeMessages(_FakeAnthropic.result)


@pytest.fixture
def _key(monkeypatch):
    # get_settings() is a process-wide @lru_cache; clear before so the patched env
    # is read, and after so the throwaway key never leaks into a later test.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake(monkeypatch):
    _FakeAnthropic.result = {"items": [{"name": "Widget", "price": 9.5}]}
    _FakeAnthropic.last_api_key = None
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", _FakeAnthropic)
    return _FakeAnthropic


_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array"}},
    "required": ["items"],
}


def test_import_needs_no_key():
    # Importing the module and constructing the extractor must NOT build a client
    # (the worker imports extractors unconditionally, possibly with no key set).
    monkey = LlmExtractor()
    assert monkey is not None  # no RuntimeError, no anthropic client constructed


def test_extract_returns_tool_input(_key, _fake):
    out = LlmExtractor().extract("<html>raw page</html>", _SCHEMA)
    assert out == {"items": [{"name": "Widget", "price": 9.5}]}


def test_extract_uses_locked_call_shape(_key, _fake):
    ext = LlmExtractor()
    ext.extract("raw", _SCHEMA)
    # the FakeMessages instance lives on the client built lazily inside extract()
    kw = ext._client.messages.calls[-1]
    assert kw["temperature"] == 0
    assert kw["max_tokens"] == 4096
    assert kw["tool_choice"] == {"type": "tool", "name": "emit"}
    assert kw["tools"][0]["name"] == "emit"
    assert kw["tools"][0]["input_schema"] is _SCHEMA  # output_schema IS the tool input_schema
    # cheap default model from settings (no per-call / per-instance override)
    assert kw["model"] == "claude-haiku-4-5-20251001"


def test_per_call_model_overrides_default(_key, _fake):
    ext = LlmExtractor()
    ext.extract("raw", _SCHEMA, model="claude-sonnet-4-5-20250514")
    assert ext._client.messages.calls[-1]["model"] == "claude-sonnet-4-5-20250514"


def test_instance_model_used_when_no_per_call(_key, _fake):
    ext = LlmExtractor(model="claude-opus-4-1-20250805")
    ext.extract("raw", _SCHEMA)
    assert ext._client.messages.calls[-1]["model"] == "claude-opus-4-1-20250805"


def test_explicit_api_key_passed_to_client(_fake, monkeypatch):
    # No env key; an explicit api_key override is what reaches anthropic.Anthropic.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    LlmExtractor(api_key="sk-explicit").extract("raw", _SCHEMA)
    assert _fake.last_api_key == "sk-explicit"
    get_settings.cache_clear()


def test_content_truncated_to_cap(_key, _fake):
    ext = LlmExtractor()
    big = "x" * (llm_mod._MAX_CONTENT_CHARS + 5000)
    ext.extract(big, _SCHEMA)
    sent = ext._client.messages.calls[-1]["messages"][0]["content"]
    assert len(sent) == llm_mod._MAX_CONTENT_CHARS  # capped, not silently dropped


def test_missing_key_raises_runtime_error(monkeypatch, _fake):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        LlmExtractor().extract("raw", _SCHEMA)
    get_settings.cache_clear()


@requires_llm
def test_live_smoke():
    # Opt-in live call (skipped unless ANTHROPIC_API_KEY is set). Proves the real
    # SDK returns schema-valid JSON for a trivial schema.
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = LlmExtractor().extract("The answer to 2+2 is four.", schema)
    assert isinstance(out, dict) and "answer" in out
