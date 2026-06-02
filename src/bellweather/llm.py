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
