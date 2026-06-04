# T43 — Gemini provider behind `LlmExtractor` (routing + `google-genai` dep + `requires_gemini`)

**Spec:** `docs/specs/2026-06-03-gemini-llm-provider-design.md` (D1 routing, D2 `google-genai`/`response_json_schema`, D3 Gemini default).
**Depends on:** T36 (`llm.py` + config). **Branch:** `ticket/T43-gemini-llm-provider`. **PR, do not merge without approval.**

## Goal
Add Gemini (AI Studio free tier) as a second provider behind the existing `LlmExtractor` seam and make it the deployment default. The public contract is **frozen** — `LlmExtractor(model=…, api_key=…)` and `extract(content, output_schema, *, model=None) -> dict` keep their exact signatures, so `extractors/scrape_llm.py`, the `api.py` preview, and every test that injects a fake `llm` are untouched. Internally `extract` resolves the model (per-call → per-instance → provider default), routes by model-name prefix (`claude-*` → Anthropic, `gemini-*` → Gemini, unrecognized → the global `llm_provider` setting verbatim, invalid setting → `RuntimeError`), caps content once, and dispatches to a private provider class. Both providers build their SDK clients **lazily** (importing `bellweather.llm` needs no key — the worker imports extractors unconditionally) and raise `RuntimeError` naming their env var (`GEMINI_API_KEY` / `ANTHROPIC_API_KEY`) only when an extraction is attempted. Gemini structured output: `response_json_schema=output_schema` + `response_mime_type="application/json"` at `temperature=0`, `max_output_tokens=4096` — the spec's `output_schema` passes through as raw JSON Schema, mirroring Anthropic tool-use (K7). Requires the Gemini 2.5 family; the `gemini-2.5-flash` default respects this.

## Files
- Modify: `src/bellweather/config.py` — add `llm_provider: str = "gemini"`, `gemini_api_key: str | None = None`, `gemini_model: str = "gemini-2.5-flash"` to `Settings` (only `config.py` reads env).
- Modify: `pyproject.toml` — add `"google-genai>=1.0"` to `[project].dependencies` (then `make dev` / `uv sync`).
- Modify: `tests/conftest.py` — add a `requires_gemini` marker mirroring `requires_llm` (skips when `GEMINI_API_KEY` is unset).
- Modify: `src/bellweather/llm.py` — `LlmExtractor` → router; extract the existing Anthropic logic into `_AnthropicLlm`; add `_GeminiLlm`.
- Modify: `tests/test_llm.py` — pin existing Anthropic cases to their provider; add Gemini fakes, the routing matrix, and one `@requires_gemini` live smoke. **No DB, no GCS.**

## Interface
Copied verbatim from the plan's "Locked interfaces" (`docs/plans/2026-06-03-gemini-llm-provider.md`).

**config.py** — add to `Settings` after `scrape_llm_model`:
```python
llm_provider: str = "gemini"            # "gemini" | "anthropic" — default provider
gemini_api_key: str | None = None
gemini_model: str = "gemini-2.5-flash"  # free-tier default; per-spec override wins
```
`scrape_llm_model` keeps its value and becomes the **Anthropic-side** default model — update its trailing comment to say so.

**llm.py** — frozen public contract; new internals:
```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...

class _AnthropicLlm: ...   # existing lazy-client + tool-use "emit" behavior, verbatim
class _GeminiLlm: ...      # lazy genai.Client; response_json_schema call shape
```

## Steps

> **No DB/GCS:** this ticket needs no `make up` / `make migrate`. Tests inject fake SDK clients and never touch Postgres or the bucket.

- [ ] **Step 1: Add the dependency.** Add `"google-genai>=1.0"` to `[project].dependencies` in `pyproject.toml`, then `make dev` so `from google import genai` resolves.

- [ ] **Step 2: Add the `requires_gemini` marker** to `tests/conftest.py`, right after `requires_llm`:
```python
requires_gemini = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"
)
```

- [ ] **Step 3: Failing tests** in `tests/test_llm.py`. Keep the existing `_FakeAnthropic`/`_FakeMessages` machinery; **pin the Anthropic-shape cases** (they assert the settings default model reaches `messages.create`, which D3's provider flip would otherwise reroute) by adding `monkeypatch.setenv("LLM_PROVIDER", "anthropic")` + `get_settings.cache_clear()` to their `_key` fixture (clear again on teardown — the cache-leak gotcha). Add a Gemini fake:
```python
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
```
monkeypatched in via `monkeypatch.setattr(llm_mod.genai, "Client", _FakeGenaiClient)`, plus a `_gemini_key` fixture (sets `GEMINI_API_KEY=test-key`, clears the settings cache before/after). New cases:
  - **Routing matrix:** no model + default settings → Gemini with `gemini-2.5-flash`; `model="claude-haiku-4-5-20251001"` → Anthropic even under `LLM_PROVIDER=gemini`; `model="gemini-2.5-flash-lite"` → Gemini even under `LLM_PROVIDER=anthropic`; `model="gpt-4"` under `LLM_PROVIDER=gemini` → sent to Gemini verbatim; `LLM_PROVIDER=nonsense` → `RuntimeError`.
  - **Gemini call shape:** `temperature == 0`, `max_output_tokens == 4096` (note: NOT `max_tokens`), `response_mime_type == "application/json"`, `response_json_schema is output_schema` — all inside the `config=` `GenerateContentConfig` kwarg; `contents` is the (possibly capped) content string.
  - **Return + errors:** `extract` returns `json.loads(resp.text)`; `result_text=""` → `RuntimeError`; `result_text="not json"` → `RuntimeError`; no `GEMINI_API_KEY` → `RuntimeError` matching `GEMINI_API_KEY`; explicit `api_key=` reaches `genai.Client`.
  - **Cap:** oversized content is truncated before the Gemini call (mirror `test_content_truncated_to_cap`).
  - **Live smoke:** one `@requires_gemini` test mirroring `test_live_smoke` (trivial schema, asserts a dict with the key).

- [ ] **Step 4: Run → FAIL.** `uv run pytest tests/test_llm.py -q` — the routing/Gemini cases fail (`LlmExtractor` knows no Gemini; default routing still hits Anthropic).

- [ ] **Step 5: Add the config fields** (Step's snippet above) to `Settings` in `src/bellweather/config.py`.

- [ ] **Step 6: Implement `llm.py`.** Move the existing client logic into `_AnthropicLlm` **verbatim** (lazy `_ensure_client`, the `emit` tool call, the no-`tool_use` `RuntimeError`); add `_GeminiLlm` with the locked call shape; rebuild `LlmExtractor` as the router — module docstring updated to describe the two providers + routing. Keep `_MAX_CONTENT_CHARS = 200_000` at module level (tests import it); cap once in the facade before dispatch (`extract` opens with `provider, model = self._route(model or self._model)`). Routing helper:
```python
def _route(self, model: str | None) -> tuple[str, str]:
    s = get_settings()
    if model is not None:
        if model.startswith("claude-"):
            return "anthropic", model
        if model.startswith("gemini-"):
            return "gemini", model
    provider = s.llm_provider
    if provider not in ("gemini", "anthropic"):
        raise RuntimeError(f"Unknown llm_provider {provider!r} (expected 'gemini' or 'anthropic')")
    if model is None:
        model = s.gemini_model if provider == "gemini" else s.scrape_llm_model
    return provider, model
```

- [ ] **Step 7: Run → PASS.** `uv run pytest tests/test_llm.py -q` — all unit cases pass; the two live smokes (`@requires_llm`, `@requires_gemini`) skip without keys.

- [ ] **Step 8: Full gate.** `make check` green with `make up` running (the rest of the suite needs DB/GCS; this ticket's tests don't).

- [ ] **Step 9: Commit** (`feat: add Gemini provider behind LlmExtractor (prefix routing, free-tier default) + google-genai dep + requires_gemini marker`).

## Acceptance criteria
- `Settings` carries `llm_provider` (default `"gemini"`), `gemini_api_key` (`None`), `gemini_model` (`"gemini-2.5-flash"`); only `config.py` reads the environment.
- `pyproject.toml` includes `"google-genai>=1.0"`; `from google import genai` resolves after `make dev`.
- `tests/conftest.py` exposes `requires_gemini` (skips when `GEMINI_API_KEY` unset).
- `LlmExtractor.__init__` / `.extract` signatures are byte-for-byte unchanged; `extractors/scrape_llm.py` and `api.py` need **no** edits.
- Importing `bellweather.llm` and constructing `LlmExtractor` builds **no** SDK client; a missing key raises `RuntimeError` naming the right env var only at `extract()`.
- Routing: `claude-*` → Anthropic, `gemini-*` → Gemini regardless of `llm_provider`; no model → `llm_provider`'s default model (`gemini_model` / `scrape_llm_model`); unrecognized prefix → global provider verbatim; invalid `llm_provider` → `RuntimeError`.
- Gemini calls use `generate_content` with `temperature=0`, `max_output_tokens=4096`, `response_mime_type="application/json"`, `response_json_schema is output_schema`, and return `json.loads(resp.text)`; empty/invalid text → `RuntimeError`.
- The shared 200k content cap applies to both providers with a logged warning.
- Anthropic behavior under explicit `claude-*` models / `LLM_PROVIDER=anthropic` is bit-identical to T36 (same call shape asserts pass).
- `make check` green.
