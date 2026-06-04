# Gemini LLM Provider — Design

**Date:** 2026-06-03
**Status:** Approved
**Builds on:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (the LLM scrape engine — K7 schema-constrained extraction, K9 cheap-default model, D-b cost flag, K1/K4/K10 key posture).
**Plan:** `docs/plans/2026-06-03-gemini-llm-provider.md` (tickets T43–T44).

## 1. Problem

The LLM scrape engine's extraction primitive (`bellweather.llm.LlmExtractor`, T36) is
Anthropic-only. That makes the Anthropic API key the pipeline's **only paid runtime
dependency** (design D-b) and a **single point of failure**: the 2026-06-03 infra
rebuild left the `bellweather-anthropic-api-key` secret without a version, and every
scrape extraction raises until the key is restored. Google's AI Studio Gemini API has a
**free tier** (e.g. `gemini-2.5-flash`) that supports schema-constrained JSON output —
good enough for the engine's "emit JSON valid against the spec's `output_schema`"
contract.

Goal: add Gemini as a **second, free, first-class provider** behind the existing
extraction seam, make it the deployment default, and keep Anthropic as the per-spec
opt-in upgrade — without changing any caller.

## 2. Decisions

- **D1 — Routing: global provider default + model-name override.** A new
  `llm_provider` setting (`"gemini"` | `"anthropic"`) picks the provider when nothing
  else decides. An explicit model name (per-call — which is how a spec's `llm_model`
  arrives — else per-instance) overrides by prefix: `claude-*` → Anthropic, `gemini-*` → Gemini. A model name with an
  unrecognized prefix goes to the global provider verbatim (the provider rejects unknown
  models naturally). Any other `llm_provider` value raises `RuntimeError`. Existing
  specs whose `llm_model` is a `claude-*` name keep routing to Anthropic unchanged.
- **D2 — Client: the official `google-genai` SDK,** structured output via
  `response_json_schema` + `response_mime_type="application/json"` at `temperature=0`.
  The spec's `output_schema` passes through as **raw JSON Schema** — no translation
  layer — exactly mirroring how Anthropic tool-use consumes it (K7). Constraint:
  `response_json_schema` requires the **Gemini 2.5 family** (`gemini-2.5-flash`,
  `-flash-lite`, `-pro`); the default respects this and per-spec overrides must too.
- **D3 — Gemini is the deployment default.** `llm_provider` defaults to `"gemini"`,
  default model `gemini-2.5-flash` (new `gemini_model` setting). The existing
  `scrape_llm_model` (`claude-haiku-4-5-20251001`) becomes the **Anthropic-side**
  default, used when routing resolves to Anthropic without an explicit model. This
  removes the per-call Anthropic spend from the default path (D-b) — Anthropic becomes
  the opt-in upgrade a spec selects via `llm_model: "claude-…"`.
- **D4 — Key posture: worker only, same as Anthropic (K1/K4/K10).** The
  `GEMINI_API_KEY` is a Secret Manager secret (`bellweather-gemini-api-key`) granted to
  the runtime SA and mounted as an env var on the **worker Job only** — not the public
  API service (the dry-run preview stays disabled in prod; with a free key the drain
  vector is quota exhaustion rather than money, but an abuser could still starve real
  extractions), and not the orchestrator SA (spawned templates must not reach it via
  ambient ADC). `config.py` remains the only env reader; `llm.py` reads the key only
  from settings, never via ADC/Secret Manager at runtime.
- **D5 — Conditional secret baseline (fixes the empty-payload apply failure).** GCP
  rejects a secret version with an empty payload, so the current "leave
  `anthropic_api_key` empty to apply the baseline" promise is broken — the 2026-06-03
  apply failed exactly there and skipped the worker Job + its scheduler. Both keys'
  `google_secret_manager_secret_version` resources AND their worker-Job env mounts
  become conditional on the var being non-empty (`count` / `dynamic "env"`). Corollary:
  **the tfvar is the source of truth** — enabling a key later means setting the var and
  re-applying (which creates the version *and* mounts the env), not hand-adding a
  secret version. An unset key degrades exactly as today: `extract()` raises a clear
  `RuntimeError` for that provider only; fetch/ingest/GDELT and the other provider are
  unaffected.

## 3. Architecture

`LlmExtractor` stays the **single seam** callers use — `extractors/scrape_llm.py:35`
and the `api.py` preview don't change at all, and neither do tests that inject a fake
`llm`. Internally it becomes a thin router over two private provider classes:

```
LlmExtractor.extract(content, output_schema, model=None)
    │  resolve model: per-call → per-instance → provider default
    │  resolve provider: model prefix → else settings.llm_provider
    │  cap content at _MAX_CONTENT_CHARS (logged, never silent)
    ├──"claude-*" / provider=anthropic──▶ _AnthropicLlm  (tool-use "emit", unchanged)
    └──"gemini-*" / provider=gemini────▶ _GeminiLlm     (response_json_schema)
```

Both provider classes build their SDK client **lazily** on first use (the worker
imports extractors unconditionally; importing `bellweather.llm` must need no key) and
raise `RuntimeError` naming their env var when the key is missing. The shared content
cap and model/provider resolution live in the facade; providers receive the final
`(content, output_schema, model)`.

## 4. Interfaces (locked)

**`config.py`** — add to `Settings` (only `config.py` reads env; pydantic maps
`llm_provider` → `LLM_PROVIDER`, `gemini_api_key` → `GEMINI_API_KEY`, etc.):

```python
llm_provider: str = "gemini"            # "gemini" | "anthropic" — default provider
gemini_api_key: str | None = None
gemini_model: str = "gemini-2.5-flash"  # free-tier default; per-spec override wins
# anthropic_api_key / scrape_llm_model unchanged — scrape_llm_model is now the
# Anthropic-side default model (used when routing resolves to Anthropic).
```

**`llm.py`** — public contract is **frozen** (constructor + `extract` signature):

```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None): ...
        # api_key override applies to whichever provider the call routes to
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
```

**Gemini call shape** (`_GeminiLlm.extract`):

```python
client.models.generate_content(
    model=model,
    contents=content,
    config=genai_types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=4096,
        response_mime_type="application/json",
        response_json_schema=output_schema,
    ),
)
# → json.loads(resp.text); empty/invalid JSON → RuntimeError
```

**Dependency:** `google-genai>=1.0` joins `[project].dependencies`.

## 5. Infrastructure (T44)

Mirrors the Anthropic trio (`infra/main.tf:90-126`), with D5's conditionals:

- `var.gemini_api_key` (string, default `""`, sensitive).
- `google_secret_manager_secret.gemini_key` (`bellweather-gemini-api-key`) +
  conditional `_version` (`count = var.gemini_api_key == "" ? 0 : 1`) + runtime-SA
  `secretAccessor` grant. Orchestrator SA deliberately excluded (K1/K4).
- Worker Job: `dynamic "env"` mounting `GEMINI_API_KEY` only when the var is set.
- **Retrofit:** the Anthropic `_version` + worker env mount get the same conditionals.
- No GitHub secrets change; `deploy.yml` untouched (it only sets `DATABASE_URL` +
  bucket vars on the migrate job).
- `infra/README.md`: document the new secret, the tfvar-is-source-of-truth rule, and
  correct the broken "drop the key in later" guidance.

## 6. Error handling

| Failure | Behavior |
| --- | --- |
| Missing key for the routed provider | `RuntimeError` naming `GEMINI_API_KEY` / `ANTHROPIC_API_KEY`; job retries → dead-letters via `max_attempts`; preview returns a graceful error (unchanged) |
| `llm_provider` not in `{gemini, anthropic}` | `RuntimeError` at extract time |
| Gemini returns empty/non-JSON text | `RuntimeError` (replayable from bronze, same as the Anthropic no-`tool_use` case) |
| Oversized content | truncated to 200k chars with a logged warning (shared cap, unchanged) |

Free-tier rate limiting (HTTP 429) is deliberately **not** specially handled in v0: the
SDK error propagates, the job `fail()`s, and the queue's retry/dead-letter machinery
absorbs it. Backoff tuning is deferred until observed.

## 7. Testing

- Rework `tests/test_llm.py`: existing Anthropic unit tests pin their provider
  (explicit `claude-*` model or `LLM_PROVIDER=anthropic` + settings-cache clear); new
  Gemini tests inject a fake `genai.Client` recording `generate_content` kwargs and
  returning canned `.text` JSON. No network, no keys, no DB/GCS.
- Routing matrix: no model → gemini default model; `claude-*` → Anthropic under
  gemini default; `gemini-*` → Gemini under anthropic default; unknown prefix → global
  provider verbatim; bad `llm_provider` → `RuntimeError`.
- Gemini call-shape lock: `temperature=0`, `max_output_tokens=4096`, JSON mime type,
  `response_json_schema is output_schema`; invalid-JSON and missing-key `RuntimeError`s.
- `requires_gemini` conftest marker (mirrors `requires_llm`/`requires_gcs`): one opt-in
  live smoke, auto-skipped without `GEMINI_API_KEY`.

## 8. Out of scope

- Vertex AI auth (ADC-based) — AI Studio API key only.
- Automatic provider fallback chains (silent provider switches make extraction quality
  non-deterministic; rejected during design).
- A per-spec `provider` column — the `llm_model` prefix already encodes it.
- Re-enabling the prod preview route — still gated on a future auth/rate-limit boundary.
- 429-aware backoff/quota budgeting — deferred until the free tier is observed in anger.
