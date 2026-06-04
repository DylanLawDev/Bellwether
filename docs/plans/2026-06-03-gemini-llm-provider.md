# Gemini LLM Provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google's **Gemini** (AI Studio free tier) as a second, first-class LLM provider behind the existing `LlmExtractor` seam, make it the **deployment default** (removing the per-call Anthropic spend from the default path, D-b), and keep Anthropic as the per-spec opt-in upgrade. No caller changes: `extractors/scrape_llm.py`, the `api.py` preview, the scrape-spec UI's free-text model field, and every test that injects a fake `llm` keep working unchanged.

**Architecture:** `LlmExtractor` becomes a thin router over two private provider classes in `llm.py` — `_AnthropicLlm` (the existing tool-use `emit` call, K7) and `_GeminiLlm` (`response_json_schema` + JSON mime type via the `google-genai` SDK). Routing: explicit model name wins by prefix (`claude-*` → Anthropic, `gemini-*` → Gemini); otherwise the new `llm_provider` setting (default `"gemini"`) picks the provider and its per-provider default model (`gemini_model` / `scrape_llm_model`). Both clients are lazy (importing `bellweather.llm` needs no key). Infra mirrors the Anthropic secret trio for `GEMINI_API_KEY` (worker Job only, K1/K4/K10) and makes **both** keys' secret versions + env mounts conditional on a non-empty tfvar — fixing the empty-payload failure that broke the 2026-06-03 apply.

**Tech Stack:** Python 3.12 + `uv`, `google-genai` (**new runtime dep — free tier**), existing `anthropic`, Terraform (secret wiring only — no new Cloud Run resources).

**Spec:** `docs/specs/2026-06-03-gemini-llm-provider-design.md`.

**Builds on (on `main` / in flight):** the LLM scrape engine epic (T33–T42) — `llm.py` (T36), `LlmScrapeExtractor` (T38), the preview route (T39), and the Anthropic secret wiring (T42).

---

## How to run a ticket (lifecycle)

Tickets live in `docs/plans/tickets/{Open, In Progress, Closed}/`. To work one: move it `Open → In Progress`, branch `ticket/T<NN>-<slug>`, follow TDD, get `make check` green, open one PR. **Merge gate:** a ticket's contents may merge to `main` only when it is in `In Progress/` (work underway) or `Closed/` (done) — never from `Open/`. Move it to `Closed/` when merged. (Mirrors `CLAUDE.md` Conventions.)

---

## Module layout (locked — new + modified for this epic)

```
src/bellweather/
├── config.py        # MODIFY: + llm_provider, gemini_api_key, gemini_model        [T43]
└── llm.py           # MODIFY: LlmExtractor → router; + _AnthropicLlm, _GeminiLlm  [T43]
pyproject.toml       # MODIFY: dependencies += "google-genai>=1.0"                 [T43]
tests/conftest.py    # MODIFY: + requires_gemini marker (mirrors requires_llm)     [T43]
tests/test_llm.py    # MODIFY: routing matrix + Gemini fakes + live smoke          [T43]
infra/
├── variables.tf     # MODIFY: + gemini_api_key (default "", sensitive)            [T44]
├── main.tf          # MODIFY: gemini secret trio; conditional versions/env mounts [T44]
└── README.md        # MODIFY: gemini key docs; fix "drop the key in later" advice [T44]
```

## Locked interfaces (use these exact names/signatures across tickets)

**config.py** — add to `Settings` (only `config.py` reads env):

```python
llm_provider: str = "gemini"            # "gemini" | "anthropic" — default provider
gemini_api_key: str | None = None
gemini_model: str = "gemini-2.5-flash"  # free-tier default; per-spec override wins
```

`anthropic_api_key` / `scrape_llm_model` are unchanged; `scrape_llm_model` is now the **Anthropic-side** default model.

**llm.py** — the public contract is **frozen**; internals reshuffle:

```python
class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None: ...
        # api_key override applies to whichever provider the call routes to
    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict: ...
        # model: per-call → per-instance → routed provider's default
        # provider: "claude-*" → anthropic, "gemini-*" → gemini,
        #           else get_settings().llm_provider (other values → RuntimeError)
        # content capped at _MAX_CONTENT_CHARS (logged) BEFORE dispatch

class _AnthropicLlm:   # existing behavior, verbatim: lazy client, tool-use "emit",
    ...                # temperature=0, max_tokens=4096, returns tool_use .input

class _GeminiLlm:      # lazy genai.Client(api_key=...); RuntimeError names GEMINI_API_KEY
    def extract(self, content: str, output_schema: dict, model: str) -> dict: ...
        # client.models.generate_content(model=model, contents=content,
        #   config=GenerateContentConfig(temperature=0, max_output_tokens=4096,
        #     response_mime_type="application/json", response_json_schema=output_schema))
        # → json.loads(resp.text); empty/invalid text → RuntimeError
```

**tests/conftest.py** — add after `requires_llm`:

```python
requires_gemini = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set"
)
```

**infra/main.tf** — the conditional pattern (both providers):

```hcl
resource "google_secret_manager_secret_version" "gemini_key" {
  count       = var.gemini_api_key == "" ? 0 : 1
  secret      = google_secret_manager_secret.gemini_key.id
  secret_data = var.gemini_api_key
}
# worker Job container:
dynamic "env" {
  for_each = var.gemini_api_key == "" ? [] : [1]
  content {
    name = "GEMINI_API_KEY"
    value_source {
      secret_key_ref {
        secret  = google_secret_manager_secret.gemini_key.secret_id
        version = "latest"
      }
    }
  }
}
```

## Build order & dependency graph

```
T43 (provider seam: config + llm.py + dep + tests)   ── code, no DB/GCS
 └─▶ T44 (infra: gemini secret trio + conditional baseline fix + README)
```

T44 depends on T43 only in the sense that mounting `GEMINI_API_KEY` is useless until the code reads it; the Terraform itself applies independently. Run them as one stacked PR chain (`ticket/T43-…` ← `ticket/T44-…`).

## Ticket index

| Ticket | Title | Files |
| --- | --- | --- |
| T43 | Gemini provider behind `LlmExtractor` (routing + `google-genai` dep + `requires_gemini`) | `config.py`, `llm.py`, `pyproject.toml`, `tests/conftest.py`, `tests/test_llm.py` |
| T44 | Gemini key infra + conditional secret baseline (fixes empty-payload apply failure) | `infra/variables.tf`, `infra/main.tf`, `infra/README.md` |

## Self-review notes

- The default-provider flip (D3) changes behavior for existing specs with **no** `llm_model`: they move from Claude Haiku to Gemini Flash on the first deploy after T43+T44. Accepted in design review (the free tier is the point); per-spec `llm_model: "claude-…"` opts any spec back.
- `response_json_schema` needs the Gemini 2.5 family — guarded by the default and documented; a per-spec override to an older Gemini model fails at the API with a clear error, dead-letters, and is replayable from bronze.
- The existing `test_llm.py` Anthropic cases assert the settings default model lands in `messages.create`; under D3 those tests must pin `LLM_PROVIDER=anthropic` (or pass explicit `claude-*` models) — called out in T43's steps so the rework is mechanical, not incidental.
