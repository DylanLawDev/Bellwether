import json
from datetime import datetime

from bellweather.db import get_conn
from bellweather.extractors import ExtractionResult, register
from bellweather.llm import LlmExtractor
from bellweather.scrape.binding import apply_binding
from bellweather.scrape.specs import get_spec


class LlmScrapeExtractor:
    content_type = "scrape-llm-v1"

    def __init__(self, *, spec_loader=None, llm=None) -> None:
        # spec_loader: (name) -> spec dict | None. Default reads the DB; the LLM
        # client is lazy (importing this module needs no Anthropic key).
        self._load = spec_loader or _db_spec_loader
        self._llm = llm or LlmExtractor()

    def extract(self, envelope: dict) -> ExtractionResult:
        # A missing provenance.scrape_spec is treated exactly like an unknown
        # spec name: Submission defaults provenance to {} and scrape-llm-v1 is
        # routable, so guard the lookup rather than KeyError into a poison retry.
        spec_name = envelope.get("provenance", {}).get("scrape_spec")
        spec = self._load(spec_name) if spec_name else None
        if spec is None:
            # Unknown/missing spec name: write nothing, but don't raise — the
            # worker still acks/marks processed (same rule as an unknown extractor).
            return ExtractionResult()
        content = (
            envelope["payload"]
            if isinstance(envelope["payload"], str)
            else json.dumps(envelope["payload"])
        )
        instance = self._llm.extract(content, spec["output_schema"], model=spec.get("llm_model"))
        fetched_at = datetime.fromisoformat(envelope["fetched_at"])
        obs, tags = apply_binding(instance, spec["binding"], fetched_at=fetched_at)
        return ExtractionResult(tags=tags, observations=obs)


def _db_spec_loader(name: str) -> dict | None:
    with get_conn() as c:  # read-only spec lookup (trusted worker has DB access)
        return get_spec(c, name)


register(LlmScrapeExtractor())
