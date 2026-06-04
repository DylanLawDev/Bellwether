# Firecrawl Fetch Adapter — Design

**Date:** 2026-06-03
**Status:** Approved
**Builds on:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (K5 pluggable fetch seam, D-e collector-key exception, §6 Phase D paid adapters) and the T33 `FetchProvider` registry.
**Plan:** `docs/plans/2026-06-03-firecrawl-fetch-adapter.md` (tickets T45–T46).

## 1. Problem

The scrape engine fetches every site with the no-secret `httpx` adapter (K5): a plain
GET with no JS rendering, no anti-bot handling, and raw-HTML output that the
`LlmScrapeExtractor` then feeds — full size — into a paid LLM call. K5 deliberately
deferred paid/rendered adapters ("Oxylabs, Bright Data, … drop in later, selected per
spec") and §6 D-e pre-authorized the scrape collector to hold a paid adapter's key when
one lands. This design lands the **first paid adapter** and fixes the conventions every
later one (Decodo, Scrapfly, …) will copy: where the key lives, how registration works,
what failures do, and how the key reaches the collector.

**Vendor choice: Firecrawl** (firecrawl.dev), selected over ScrapingAnt (the incumbent
candidate), Decodo, Jina Reader, and a broad market sweep, on the project's criteria:

- **Recurring free tier** (1,000 credits/mo, no card; cheapest paid tier $16/mo) — the
  only shortlisted vendor whose free tier plausibly covers Bellwether's entire scheduled
  scrape volume, keeping Phase D at **$0** inside the `<$40/mo` envelope.
- **Markdown-native output** — 1 credit per page, and it directly delivers the Phase D
  "HTML pre-cleaning to shrink LLM input" goal before the page ever reaches the LLM.
- **Schema-based JSON extraction exists** (+4 credits/page) as a future option that maps
  onto scrape-spec `output_schema`s — deliberately out of scope here (§8).
- Official Python SDK (`firecrawl-py`), JS rendering by default, AGPL self-host escape
  hatch.
- Known trade-offs, accepted: mediocre anti-bot (~61% in independent benchmarks; ~0% on
  social platforms), blocked requests still consume credits, free tier is capped at 10
  requests/min and 2 concurrent. ScrapingAnt was rejected on independent benchmark data
  (~34–36% success, ranked last, reportedly bills blocked requests); Decodo (~86%
  success) is the documented fallback vendor if targets prove hostile, but has
  effectively no free tier and template-only parsing.

## 2. Decisions

- **D1 — Per-spec opt-in via the existing seam; conventions are the deliverable.**
  `FirecrawlFetcher` registers as `"firecrawl"` in the T33 registry; a spec selects it
  with `fetch_adapter: "firecrawl"`. No consumer *logic* changes (each consumer gains
  only a registering import, §4): the collector (`producers/scrape/collector.py:45`)
  and the API preview (`api.py:308`) already resolve `spec.fetch_adapter` through
  `get_fetcher()`, and the UI dropdown reads `GET /api/fetch-adapters` →
  `known_fetchers()`. The module's shape — one file in
  `fetch/`, one optional Settings key, lazy key check, unconditional registration —
  is the template for every future paid adapter.
- **D2 — Markdown is the payload; bronze stores Firecrawl's markdown.** The adapter
  requests `formats=["markdown"]` only (1 credit) and returns the markdown as
  `FetchResult.content` with `content_type="text/markdown"`. The collector freezes that
  markdown as the bronze envelope and `content_type` lands in provenance, so a replay
  knows it is reading Firecrawl's rendering, not raw HTML. Accepted trade-off:
  re-extraction cannot recover anything markdown dropped; in exchange GCS objects and
  LLM extraction input shrink by roughly an order of magnitude. (A future dual-format
  variant is §8.)
- **D3 — Official `firecrawl-py` SDK,** lazy client construction (importing
  `bellweather.fetch.firecrawl_fetch` must need no key — registration happens at import
  in keyless processes). One new runtime dependency.
- **D4 — Key via `get_settings().firecrawl_api_key`, read lazily at fetch time.**
  `config.py` stays the only env reader; every `Settings` field is optional, so the
  minimal-env collector can still construct settings. Registration is unconditional, so
  `"firecrawl"` appears in `known_fetchers()` and the UI dropdown even where the key is
  absent; fetching without a key raises `RuntimeError` naming `FIRECRAWL_API_KEY`.
- **D5 — Failures raise; no fabricated bronze.** With httpx a 404 still has a real body
  worth freezing; a failed Firecrawl scrape has no content, so SDK/API errors propagate.
  The collector run fails, the orchestrator records `status="error"`, and nothing is
  ingested. A successful scrape maps the upstream page status from response metadata
  (defaulting to 200) into `FetchResult.status`.
- **D6 — Key posture: orchestrator Job only, forwarded only to the `scrape` template
  (the D-e scoped exception, narrowed).** The secret mounts as an env var on the
  orchestrator Cloud Run Job; `orchestrator._child_env()` forwards `FIRECRAWL_API_KEY`
  **only when the spawned template is `scrape`** — other templates keep the K4 minimal
  env, so the exception stays exactly as wide as D-e authorized (the scrape collector is
  trusted first-class code; arbitrary templates are not). The **API service and worker
  never mount the key** — same posture as the LLM keys (K1/K4/K10): a prod preview of a
  firecrawl spec degrades into the same graceful keyless error the LLM path already has,
  and local dev gets the key from `.env`. The secret reuses T44's conditional baseline
  (`nonsensitive()` presence local; `count`/`dynamic "env"` gated on a non-empty tfvar;
  the tfvar is the source of truth).

## 3. Architecture

```
spec.fetch_adapter == "firecrawl"
        │
collector (scrape template, spawned by orchestrator with FIRECRAWL_API_KEY, D6)
        │  get_fetcher("firecrawl")
        ▼
FirecrawlFetcher.fetch(url)                      [fetch/firecrawl_fetch.py, T45]
        │  key = get_settings().firecrawl_api_key   (lazy, D4 — RuntimeError if unset)
        │  SDK: client.scrape(url, formats=["markdown"])   (D3)
        ▼
FetchResult(content=markdown, status=meta status|200,
            content_type="text/markdown", final_url=meta url|url)
        │
collector POSTs /ingest → bronze keeps the markdown (D2) → LlmScrapeExtractor
                                                            (≈10× fewer input tokens)
```

## 4. Interfaces (locked)

**`config.py`** — add to `Settings`:

```python
firecrawl_api_key: str | None = None
```

**`fetch/firecrawl_fetch.py`** — mirrors `httpx_fetch.py`'s self-registering shape:

```python
class FirecrawlFetcher:
    """Paid fetch adapter: Firecrawl /v2/scrape, markdown output (design D2/D3)."""

    name = "firecrawl"

    def fetch(self, url: str, **opts) -> FetchResult:
        # lazy key (D4): RuntimeError naming FIRECRAWL_API_KEY when unset
        # lazy SDK client, cached on the instance
        # doc = client.scrape(url, formats=["markdown"])
        # return FetchResult(content=doc.markdown,
        #                    status=<metadata status code or 200>,
        #                    content_type="text/markdown",
        #                    final_url=<metadata url or url>)
        ...

register(FirecrawlFetcher())
```

Consumers add one import each, matching the existing idiom
(`import bellweather.fetch.firecrawl_fetch  # noqa: F401`): `api.py:10` and
`producers/scrape/collector.py`.

**`orchestrator.py`** — `_child_env()` grows a per-template extra-env allowlist (D6):

```python
_TEMPLATE_EXTRA_ENV: dict[str, tuple[str, ...]] = {
    "scrape": ("FIRECRAWL_API_KEY",),  # D-e scoped exception, design D6
}

def _child_env(template: str) -> dict[str, str]:
    ...  # existing minimal env, then:
    # for var in _TEMPLATE_EXTRA_ENV.get(template, ()):
    #     if os.environ.get(var): env[var] = os.environ[var]
```

(`_run_subprocess` passes its `template` through; no other behavior changes.)

**Dependency:** `firecrawl-py` joins `[project].dependencies`. Exact SDK call/response
attribute names are pinned at implementation time against the installed version; the
contract above (markdown string + metadata status/url) is what tests lock.

## 5. Infrastructure (T46)

Mirrors the T44 LLM-key pattern with one deliberate difference — the grant goes to the
**orchestrator SA**, not the runtime SA:

- `var.firecrawl_api_key` (string, default `""`, sensitive).
- `google_secret_manager_secret.firecrawl_key` (`bellweather-firecrawl-api-key`) +
  conditional `_version` (`count = local.firecrawl_key_set ? 1 : 0`) +
  `secretAccessor` grant to **`google_service_account.orchestrator`**.
- `locals`: `firecrawl_key_set = nonsensitive(var.firecrawl_api_key != "")`.
- Orchestrator Job: `dynamic "env"` mounting `FIRECRAWL_API_KEY` when set, +
  `depends_on` additions.
- **Comment surgery is part of the work:** `main.tf`'s orchestrator-SA comment currently
  promises that SA reads *only* the DB-URL secret because anything it can read is
  reachable by an external template. That promise changes: the comment must cite D-e/D6
  (key forwarded only to the first-party scrape template by `_child_env`'s allowlist).
- API service and worker Job: **no changes** (D6).
- `infra/README.md`: document the secret, who holds it, and the tfvar-is-source-of-truth
  rule (same as the LLM keys).

Cost: $0 — the free tier (1,000 credits/mo) covers scheduled specs at v0 volume; the
10 req/min free-tier cap is comfortably above the collector's sequential per-site loop.

## 6. Error handling

| Failure | Behavior |
| --- | --- |
| `fetch_adapter: "firecrawl"` but key unset | `RuntimeError` naming `FIRECRAWL_API_KEY` at fetch time (D4); collector run fails, orchestrator records `status="error"`; nothing ingested |
| Firecrawl API/SDK error (blocked page, 5xx, quota, timeout) | exception propagates (D5); same failure path; no empty bronze |
| Firecrawl succeeds but upstream page errored | metadata status code lands in `FetchResult.status` → provenance `fetch_status`, mirroring httpx behavior |
| Free-tier 429 / rate limit | not specially handled in v0 (same stance as the Gemini design): the error propagates, the run fails, the schedule retries next tick; backoff tuning deferred until observed |
| Unknown adapter name in a spec | unchanged: preview 400s; collector falls back to `HttpxFetcher` (existing `collector.py:45` behavior) |

## 7. Testing

- `tests/test_fetch_firecrawl.py` — unit tests with a **fake SDK client** injected
  (canned document object; no network, no key, plain `pytest`): result mapping
  (markdown → `content`, `content_type="text/markdown"`, metadata status/url + their
  defaults), missing-key `RuntimeError` naming the env var, import-registers-`firecrawl`
  (mirrors `test_fetch.py`'s httpx cases), SDK client built lazily and with the settings
  key.
- Orchestrator: `_child_env("scrape")` includes `FIRECRAWL_API_KEY` when set in the
  parent env and omits it when unset; `_child_env("gdelt")` never includes it (D6).
- Settings-cache hygiene: tests that set `FIRECRAWL_API_KEY` rely on the existing
  autouse settings-cache reset fixture.
- Terraform: `terraform validate` + eyeballed `terraform plan` with the var empty
  (no version, no env mount) and set (exactly the version + orchestrator-Job env).
- A `requires_firecrawl` opt-in live smoke (mirrors `requires_llm`/`requires_gemini`),
  auto-skipped without `FIRECRAWL_API_KEY`.

## 8. Out of scope

- **Firecrawl JSON extraction** (+4 credits/page) as an alternative to our own LLM
  extractor — revisit once the markdown path is proven; it would bypass, not feed, the
  spec's `output_schema`/binding machinery.
- Dual-format fetch (rawHtml as bronze + markdown alongside) — needs a `FetchResult`
  shape change; only worth it if markdown-only replay proves lossy in practice.
- Per-spec fetch options (geo, stealth/proxy modes, `**opts` passthrough) — `opts` stays
  unused, as in `HttpxFetcher`.
- 429-aware backoff / credit-budget telemetry — deferred until the free tier is observed
  in anger.
- Additional vendors (Decodo as the anti-bot fallback, Scrapfly, …) — each is now "copy
  `firecrawl_fetch.py`, add a Settings key, extend `_TEMPLATE_EXTRA_ENV` + the secret
  trio."
- Re-enabling the prod preview route — unchanged; still gated on a future auth boundary.
