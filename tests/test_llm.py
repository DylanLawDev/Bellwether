"""LlmExtractor — two-provider router (Anthropic + Gemini) unit + live smokes.

Unit tests inject fake SDK clients (no network, no real keys).
- _FakeAnthropic / _FakeMessages: record kwargs, return canned tool_use block.
- _FakeGenaiClient / _FakeGenaiModels: record kwargs, return canned text.
Routing matrix, call-shape asserts, error paths, and the 200k cap are all unit
cases that run without any key. The two live smokes (@requires_llm, @requires_gemini)
are opt-in and auto-skipped when the respective env var is absent. No DB, no GCS.
"""

import json
import types

import pytest

import bellweather.llm as llm_mod
from bellweather.config import get_settings
from bellweather.llm import LlmExtractor
from tests.conftest import requires_gemini, requires_llm


# ---------------------------------------------------------------------------
# Fake Anthropic SDK
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fake Gemini SDK
# ---------------------------------------------------------------------------


class _FakeGenaiModels:
    def __init__(self, result_text: str):
        self.result_text = result_text
        self.calls: list[dict] = []

    def generate_content(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(text=self.result_text)


class _FakeGenaiClient:
    last_api_key: str | None = None
    result_text: str = "{}"

    def __init__(self, *, api_key=None):
        type(self).last_api_key = api_key
        self.models = _FakeGenaiModels(_FakeGenaiClient.result_text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _key(monkeypatch):
    # Pin existing Anthropic cases to the anthropic provider so D3's default-
    # provider flip to gemini doesn't re-route them.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake(monkeypatch):
    _FakeAnthropic.result = {"items": [{"name": "Widget", "price": 9.5}]}
    _FakeAnthropic.last_api_key = None
    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", _FakeAnthropic)
    return _FakeAnthropic


@pytest.fixture
def _gemini_key(monkeypatch):
    """Set GEMINI_API_KEY and clear settings cache before/after."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _fake_gemini(monkeypatch):
    _FakeGenaiClient.result_text = json.dumps({"items": [{"name": "Widget", "price": 9.5}]})
    _FakeGenaiClient.last_api_key = None
    monkeypatch.setattr(llm_mod.genai, "Client", _FakeGenaiClient)
    return _FakeGenaiClient


_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array"}},
    "required": ["items"],
}


# ---------------------------------------------------------------------------
# Existing Anthropic-path tests (pinned to LLM_PROVIDER=anthropic via _key)
# ---------------------------------------------------------------------------


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
    kw = ext._anthropic._client.messages.calls[-1]
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
    assert ext._anthropic._client.messages.calls[-1]["model"] == "claude-sonnet-4-5-20250514"


def test_instance_model_used_when_no_per_call(_key, _fake):
    ext = LlmExtractor(model="claude-opus-4-1-20250805")
    ext.extract("raw", _SCHEMA)
    assert ext._anthropic._client.messages.calls[-1]["model"] == "claude-opus-4-1-20250805"


def test_explicit_api_key_passed_to_client(_fake, monkeypatch):
    # No env key; an explicit api_key override is what reaches anthropic.Anthropic.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    LlmExtractor(api_key="sk-explicit").extract("raw", _SCHEMA)
    assert _fake.last_api_key == "sk-explicit"
    get_settings.cache_clear()


def test_content_truncated_to_cap(_key, _fake):
    ext = LlmExtractor()
    big = "x" * (llm_mod._MAX_CONTENT_CHARS + 5000)
    ext.extract(big, _SCHEMA)
    sent = ext._anthropic._client.messages.calls[-1]["messages"][0]["content"]
    assert len(sent) == llm_mod._MAX_CONTENT_CHARS  # capped, not silently dropped


def test_missing_key_raises_runtime_error(monkeypatch, _fake):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        LlmExtractor().extract("raw", _SCHEMA)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Routing matrix
# ---------------------------------------------------------------------------


def test_routing_default_is_gemini(_gemini_key, _fake_gemini, monkeypatch):
    """No model override + default settings → Gemini with gemini-2.5-flash."""
    # Default llm_provider is gemini
    ext = LlmExtractor()
    ext.extract("content", _SCHEMA)
    assert _fake_gemini.last_api_key == "test-gemini-key"
    assert len(ext._gemini._client.models.calls) == 1
    assert ext._gemini._client.models.calls[0]["model"] == "gemini-2.5-flash"


def test_routing_claude_prefix_forces_anthropic(_fake, _fake_gemini, monkeypatch):
    """model='claude-*' → Anthropic even when LLM_PROVIDER=gemini."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    ext = LlmExtractor()
    ext.extract("raw", _SCHEMA, model="claude-haiku-4-5-20251001")
    # Anthropic was used
    assert ext._anthropic._client.messages.calls[-1]["model"] == "claude-haiku-4-5-20251001"
    # Gemini was not called
    assert ext._gemini is None or len(ext._gemini._client.models.calls) == 0
    get_settings.cache_clear()


def test_routing_gemini_prefix_forces_gemini(_key, _fake_gemini, monkeypatch):
    """model='gemini-*' → Gemini even when LLM_PROVIDER=anthropic."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    ext = LlmExtractor()
    ext.extract("raw", _SCHEMA, model="gemini-2.5-flash-lite")
    assert ext._gemini._client.models.calls[-1]["model"] == "gemini-2.5-flash-lite"
    get_settings.cache_clear()


def test_routing_unrecognized_prefix_uses_provider(_fake_gemini, monkeypatch):
    """model='gpt-4' under LLM_PROVIDER=gemini → sent to Gemini verbatim."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    get_settings.cache_clear()
    ext = LlmExtractor()
    ext.extract("raw", _SCHEMA, model="gpt-4")
    assert ext._gemini._client.models.calls[-1]["model"] == "gpt-4"
    get_settings.cache_clear()


def test_routing_invalid_provider_raises(monkeypatch):
    """LLM_PROVIDER=nonsense → RuntimeError."""
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="nonsense"):
        LlmExtractor().extract("raw", _SCHEMA)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Gemini call shape
# ---------------------------------------------------------------------------


def test_gemini_call_shape(_gemini_key, _fake_gemini):
    """Gemini calls use correct params inside config= GenerateContentConfig."""
    from google.genai import types as genai_types

    ext = LlmExtractor()
    ext.extract("hello world", _SCHEMA)
    kw = ext._gemini._client.models.calls[-1]
    cfg = kw["config"]
    assert isinstance(cfg, genai_types.GenerateContentConfig)
    assert cfg.temperature == 0
    assert cfg.max_output_tokens == 4096
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema is _SCHEMA
    assert kw["contents"] == "hello world"
    assert kw["model"] == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Gemini return + error cases
# ---------------------------------------------------------------------------


def test_gemini_returns_parsed_json(_gemini_key, _fake_gemini):
    """extract() returns json.loads(resp.text)."""
    expected = {"items": [{"name": "Widget", "price": 9.5}]}
    out = LlmExtractor().extract("content", _SCHEMA)
    assert out == expected


def test_gemini_empty_text_raises(_gemini_key, monkeypatch):
    """result_text='' → RuntimeError."""
    _FakeGenaiClient.result_text = ""
    monkeypatch.setattr(llm_mod.genai, "Client", _FakeGenaiClient)
    with pytest.raises(RuntimeError):
        LlmExtractor().extract("content", _SCHEMA)


def test_gemini_invalid_json_raises(_gemini_key, monkeypatch):
    """result_text='not json' → RuntimeError."""
    _FakeGenaiClient.result_text = "not json"
    monkeypatch.setattr(llm_mod.genai, "Client", _FakeGenaiClient)
    with pytest.raises(RuntimeError):
        LlmExtractor().extract("content", _SCHEMA)


def test_gemini_missing_key_raises_runtime_error(monkeypatch, _fake_gemini):
    """No GEMINI_API_KEY → RuntimeError naming GEMINI_API_KEY."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        LlmExtractor().extract("content", _SCHEMA)
    get_settings.cache_clear()


def test_gemini_explicit_api_key_passed(_fake_gemini, monkeypatch):
    """explicit api_key= reaches genai.Client."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    get_settings.cache_clear()
    LlmExtractor(api_key="explicit-gemini-key").extract("content", _SCHEMA)
    assert _fake_gemini.last_api_key == "explicit-gemini-key"
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Content cap — Gemini path
# ---------------------------------------------------------------------------


def test_gemini_content_truncated_to_cap(_gemini_key, _fake_gemini):
    """Oversized content is truncated before the Gemini call."""
    ext = LlmExtractor()
    big = "x" * (llm_mod._MAX_CONTENT_CHARS + 5000)
    ext.extract(big, _SCHEMA)
    sent = ext._gemini._client.models.calls[-1]["contents"]
    assert len(sent) == llm_mod._MAX_CONTENT_CHARS


# ---------------------------------------------------------------------------
# Live smokes (opt-in)
# ---------------------------------------------------------------------------


@requires_llm
def test_live_smoke():
    # Opt-in live call (skipped unless ANTHROPIC_API_KEY is set). Proves the real
    # SDK returns schema-valid JSON for a trivial schema.
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = LlmExtractor(model="claude-haiku-4-5-20251001").extract(
        "The answer to 2+2 is four.", schema
    )
    assert isinstance(out, dict) and "answer" in out


@requires_gemini
def test_live_smoke_gemini():
    # Opt-in live call (skipped unless GEMINI_API_KEY is set). Proves the real
    # Gemini SDK returns schema-valid JSON for a trivial schema.
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    out = LlmExtractor().extract("The answer to 2+2 is four.", schema)
    assert isinstance(out, dict) and "answer" in out
