# T36 — LLM client `llm.py` + config + `anthropic` dep + `requires_llm`

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§3.1 Units — LLM client; K7 schema-constrained tool-use / K9 Anthropic cheap-default; D-b first paid runtime dep / cost flag).
**Depends on:** T01 (config + `Settings`/`get_settings`). **Branch:** `ticket/T36-llm-client`. **PR, do not merge without approval.**

## Goal
Add the thin Anthropic wrapper `LlmExtractor` plus its config + dependency wiring — the schema-constrained extraction primitive the generic scrape extractor (T38) and the API preview (T39) call. The user's `output_schema` *is* the LLM tool's `input_schema`, so the model is forced to emit JSON valid against it at `temperature=0` (K7); the default model is the cheap Haiku tier, per-spec overridable (K9). The client is built **lazily** behind a `_ensure_client()` seam: importing `bellweather.llm` must need **no key** (the worker imports extractors unconditionally), and the real `anthropic.Anthropic` is constructed only on first `extract(...)` call — raising `RuntimeError` if no key is configured. This is the first paid runtime dependency (D-b), so it is added with cost discipline: cheap default model, `max_tokens` capped, and the raw page truncated to a sane cap with a logged warning (never silently dropped). Tests inject a fake `anthropic.Anthropic` so CI needs no real key; a single opt-in `@requires_llm` live smoke is skipped without `ANTHROPIC_API_KEY`.

## Files
- Modify: `src/bellweather/config.py` — add `anthropic_api_key: str | None = None` and `scrape_llm_model: str = "claude-haiku-4-5-20251001"` to `Settings` (only `config.py` reads env).
- Modify: `pyproject.toml` — add `"anthropic>=0.40"` to `[project].dependencies` (then `make dev` / `uv sync` to lock it).
- Modify: `tests/conftest.py` — add a `requires_llm` marker mirroring `requires_gcs` (skips when `ANTHROPIC_API_KEY` is unset).
- Create: `src/bellweather/llm.py` — `LlmExtractor` (lazy client via `_ensure_client()`; schema-constrained tool-use `extract`).
- Test: `tests/test_llm.py` — unit tests injecting a fake `anthropic.Anthropic` (no network/key), plus one `@requires_llm` live smoke. **No DB, no GCS.**

## Interface
Copied verbatim from the build plan's "Locked interfaces".

**config.py** — add to `Settings` (only `config.py` reads env):
```python
anthropic_api_key: str | None = None
scrape_llm_model: str = "claude-haiku-4-5-20251001"   # cheap default; per-spec override wins
```

**llm.py** — thin Anthropic wrapper; **lazy client** so importing the module needs no key (the worker imports extractors unconditionally):
```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
        # store overrides; DO NOT construct the anthropic client here
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
        # lazy: build anthropic.Anthropic(api_key=api_key or get_settings().anthropic_api_key)
        #       on first use; raise RuntimeError if no key.
        # tools=[{"name":"emit","description":"Emit the extracted record(s).","input_schema":output_schema}]
        # tool_choice={"type":"tool","name":"emit"}, temperature=0, max_tokens=4096,
        # model = model or self._model or get_settings().scrape_llm_model
        # content is truncated to a sane cap (e.g. 200_000 chars) — log truncation, never silently drop.
        # return the tool_use block's .input dict.
```

**tests/conftest.py** — add after `requires_gcs`:
```python
requires_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)
```
Unit tests inject a fake LLM (`class _FakeLlm: def extract(self, content, output_schema, *, model=None): return {...}`); only one opt-in live test carries `@requires_llm`.

## Steps

> **No DB/GCS:** this ticket needs no `make up` / `make migrate`. Tests inject a fake `anthropic.Anthropic` and never touch Postgres or the bucket.

- [ ] **Step 1: Add the dependency.** Add `"anthropic>=0.40"` to `[project].dependencies` in `pyproject.toml`, then `make dev` (`uv sync`) so the lockfile resolves it and `import anthropic` works:
```toml
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "psycopg[binary,pool]>=3.2",
  "google-cloud-storage>=2.18",
  "pydantic>=2.9",
  "pydantic-settings>=2.6",
  "httpx>=0.27",
  "typer>=0.12",
  "anthropic>=0.40",
]
```

- [ ] **Step 2: Failing test** `tests/test_llm.py`. The unit tests monkeypatch `anthropic.Anthropic` with a fake whose `messages.create(**kw)` records the kwargs and returns a response with `.content = [obj(type="tool_use", input={...})]`. They set `ANTHROPIC_API_KEY` via env + clear the `get_settings` cache, then assert the locked call shape (`temperature == 0`, `tool_choice`, `input_schema == output_schema`, the chosen model) and that `extract` returns the tool's `.input` dict; a missing-key case asserts `RuntimeError`; one `@requires_llm` test is the live smoke (skipped in CI):
```python
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
```

- [ ] **Step 3: Run → FAIL.** `uv run pytest tests/test_llm.py -q` →
  `ModuleNotFoundError: No module named 'bellweather.llm'` (the module does not exist yet; the
  `@requires_llm` live test imports `requires_llm` from `tests.conftest`, which also doesn't exist).

- [ ] **Step 4: Add config fields** to `src/bellweather/config.py` `Settings` (only `config.py` reads
  env). Add the two fields after `bellweather_templates_dir`:
```python
    bellweather_templates_dir: str = "producers"  # dir scanned for */template.toml
    anthropic_api_key: str | None = None
    scrape_llm_model: str = "claude-haiku-4-5-20251001"  # cheap default; per-spec override wins
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
```

- [ ] **Step 5: Add the `requires_llm` marker** to `tests/conftest.py`, right after `requires_gcs`
  (mirrors it; `os` is already imported at the top of the file):
```python
requires_gcs = pytest.mark.skipif(not _gcs_reachable(), reason="GCS emulator not reachable")

requires_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)
```

- [ ] **Step 6: Implement** `src/bellweather/llm.py` — verbatim from the locked interface. The client
  is built lazily in `_ensure_client()` (importing the module needs no key); the call uses the
  output schema as the tool's `input_schema`, `tool_choice` forces the `emit` tool, `temperature=0`,
  `max_tokens=4096`, and the content is capped to `_MAX_CONTENT_CHARS` with a logged warning (never
  silently dropped, per D-b cost discipline). `extract` returns the first `tool_use` block's `.input`:
```python
"""Thin Anthropic wrapper: schema-constrained extraction via tool-use.

The user's ``output_schema`` IS the LLM tool's ``input_schema``, so Claude is
forced to emit JSON valid against it (``temperature=0``) — no bespoke parsing or
repair (K7). The default model is the cheap Haiku tier (K9), overridable per call
or per instance. The ``anthropic.Anthropic`` client is built LAZILY on the first
``extract(...)`` call (``_ensure_client``), so importing this module — which the
worker does unconditionally — needs no API key; a missing key raises only when an
extraction is actually attempted. Content is capped (cost discipline, D-b); the
cap is logged, never silently applied.
"""

import logging

import anthropic

from bellweather.config import get_settings

logger = logging.getLogger(__name__)

# Cost/size cap on the raw page sent to the LLM. Truncation is LOGGED (never a
# silent drop). Phase D HTML pre-cleaning will shrink real pages further.
_MAX_CONTENT_CHARS = 200_000


class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        # Store overrides only — DO NOT construct the anthropic client here, so
        # importing/instantiating needs no key (the worker imports extractors
        # unconditionally). The client is built lazily on first extract().
        self._model = model
        self._api_key = api_key
        self._client: anthropic.Anthropic | None = None

    def _ensure_client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = self._api_key or get_settings().anthropic_api_key
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not configured; cannot call the LLM. "
                    "Set it in the environment (only the trusted worker holds this key)."
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict:
        client = self._ensure_client()
        model = model or self._model or get_settings().scrape_llm_model
        if len(content) > _MAX_CONTENT_CHARS:
            logger.warning(
                "llm content truncated from %d to %d chars", len(content), _MAX_CONTENT_CHARS
            )
            content = content[:_MAX_CONTENT_CHARS]
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            temperature=0,
            tools=[
                {
                    "name": "emit",
                    "description": "Emit the extracted record(s).",
                    "input_schema": output_schema,
                }
            ],
            tool_choice={"type": "tool", "name": "emit"},
            messages=[{"role": "user", "content": content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return block.input
        raise RuntimeError("LLM returned no tool_use block (expected the 'emit' tool call)")
```

- [ ] **Step 7: Run → PASS.** `uv run pytest tests/test_llm.py -q` → the 8 unit cases pass and the live
  smoke is **skipped** (no `ANTHROPIC_API_KEY`): `8 passed, 1 skipped`.

- [ ] **Step 8: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green
  with `make up` running. (`make up` is only needed for the rest of the suite's DB/GCS tests; this
  ticket's own tests need neither.)

- [ ] **Step 9: Commit** (`feat: add LlmExtractor (lazy Anthropic tool-use client) + config + anthropic dep + requires_llm marker`).

## Acceptance criteria
- `Settings` carries `anthropic_api_key: str | None = None` and `scrape_llm_model: str = "claude-haiku-4-5-20251001"`; only `config.py` reads the environment.
- `pyproject.toml` `[project].dependencies` includes `"anthropic>=0.40"`, and `import anthropic` resolves after `make dev`.
- `tests/conftest.py` exposes a `requires_llm` marker that skips when `ANTHROPIC_API_KEY` is unset (mirrors `requires_gcs`).
- `import bellweather.llm` and `LlmExtractor(...)` construct **no** anthropic client (no key required to import); the client is built lazily in `_ensure_client()` on first `extract(...)`, and a missing key raises `RuntimeError` only then.
- `extract(content, output_schema, *, model=None)` calls `client.messages.create` with the locked shape — `temperature=0`, `max_tokens=4096`, `tool_choice={"type":"tool","name":"emit"}`, and `tools[0].input_schema is output_schema` — and returns the first `tool_use` block's `.input` dict.
- Model resolution is `model (per-call) → self._model (per-instance) → get_settings().scrape_llm_model` (cheap Haiku default, K9); an explicit `api_key` override reaches `anthropic.Anthropic`.
- Content longer than `_MAX_CONTENT_CHARS` (200_000) is truncated to the cap with a logged warning — never silently dropped (D-b cost discipline).
- Unit tests inject a fake `anthropic.Anthropic` (no network/key); exactly one `@requires_llm` live smoke exists and is skipped in CI. No DB, no GCS.
- `make check` green.
