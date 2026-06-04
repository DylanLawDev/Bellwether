"""LlmExtractor — two-provider router (Anthropic + Gemini).

The public contract is frozen:
  LlmExtractor(model=..., api_key=...) + .extract(content, output_schema, *, model=None) -> dict

Routing (D1): per-call model → per-instance model → provider default.
  - ``claude-*`` prefix → Anthropic tool-use (regardless of llm_provider setting).
  - ``gemini-*`` prefix → Gemini response_json_schema (regardless of llm_provider setting).
  - No prefix match → route by ``llm_provider`` setting (invalid value → RuntimeError).
  - No model at all → ``llm_provider``'s default (``gemini_model`` / ``scrape_llm_model``).

Both provider classes build their SDK clients LAZILY — importing this module
(which the worker does unconditionally) needs no key. A missing key raises
RuntimeError naming the env var only when extraction is attempted (K7 / D-b).

Content is capped once in the facade before dispatch (cost discipline).
"""

import json
import logging

import anthropic
from google import genai
from google.genai import types as genai_types

from bellweather.config import get_settings

logger = logging.getLogger(__name__)

# Cost/size cap on the raw page sent to the LLM. Truncation is LOGGED (never a
# silent drop). Phase D HTML pre-cleaning will shrink real pages further.
_MAX_CONTENT_CHARS = 200_000


# ---------------------------------------------------------------------------
# Private provider classes
# ---------------------------------------------------------------------------


class _AnthropicLlm:
    """Lazy anthropic.Anthropic wrapper — tool-use / 'emit' schema constraint."""

    def __init__(self, api_key: str | None) -> None:
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

    def extract(self, content: str, output_schema: dict, model: str) -> dict:
        client = self._ensure_client()
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


class _GeminiLlm:
    """Lazy google-genai wrapper — response_json_schema structured output."""

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key
        self._client: genai.Client | None = None

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            key = self._api_key or get_settings().gemini_api_key
            if not key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not configured; cannot call Gemini. "
                    "Set it in the environment (only the trusted worker holds this key)."
                )
            self._client = genai.Client(api_key=key)
        return self._client

    def extract(self, content: str, output_schema: dict, model: str) -> dict:
        client = self._ensure_client()
        resp = client.models.generate_content(
            model=model,
            contents=content,
            config=genai_types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=4096,
                response_mime_type="application/json",
                response_json_schema=output_schema,
            ),
        )
        text = resp.text
        if not text:
            raise RuntimeError("Gemini returned empty response text; cannot parse JSON.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned invalid JSON: {text!r}") from exc


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class LlmExtractor:
    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        # Store overrides only — DO NOT construct any SDK client here, so
        # importing/instantiating needs no key (the worker imports extractors
        # unconditionally). Both provider clients are built lazily on first use.
        self._model = model
        self._api_key = api_key
        self._anthropic: _AnthropicLlm | None = None
        self._gemini: _GeminiLlm | None = None

    def _route(self, model: str | None) -> tuple[str, str]:
        """Resolve (provider, model) from per-call/per-instance model + settings."""
        s = get_settings()
        if model is not None:
            if model.startswith("claude-"):
                return "anthropic", model
            if model.startswith("gemini-"):
                return "gemini", model
        provider = s.llm_provider
        if provider not in ("gemini", "anthropic"):
            raise RuntimeError(
                f"Unknown llm_provider {provider!r} (expected 'gemini' or 'anthropic')"
            )
        if model is None:
            model = s.gemini_model if provider == "gemini" else s.scrape_llm_model
        return provider, model

    def extract(self, content: str, output_schema: dict, *, model: str | None = None) -> dict:
        provider, resolved_model = self._route(model or self._model)

        if len(content) > _MAX_CONTENT_CHARS:
            logger.warning(
                "llm content truncated from %d to %d chars", len(content), _MAX_CONTENT_CHARS
            )
            content = content[:_MAX_CONTENT_CHARS]

        if provider == "anthropic":
            if self._anthropic is None:
                self._anthropic = _AnthropicLlm(self._api_key)
            return self._anthropic.extract(content, output_schema, resolved_model)
        else:  # "gemini"
            if self._gemini is None:
                self._gemini = _GeminiLlm(self._api_key)
            return self._gemini.extract(content, output_schema, resolved_model)
