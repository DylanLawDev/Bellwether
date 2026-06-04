# T45 — Firecrawl fetch adapter (`firecrawl-py` dep + lazy key + per-template env allowlist)

**Spec:** `docs/specs/2026-06-03-firecrawl-fetch-adapter-design.md` (D1–D6).
**Plan:** `docs/plans/2026-06-03-firecrawl-fetch-adapter.md`.
**Depends on:** T33 (the `fetch/` seam + registry), T40 (the scrape collector this serves), T39 (`/api/fetch-adapters` the UI dropdown reads). **Branch:** `ticket/T45-firecrawl-fetch-adapter`. **PR, do not merge without approval.**

## Goal

Land the first **paid** fetch adapter behind the K5 seam — Firecrawl, markdown output — plus the conventions every later vendor copies. A scrape spec opts in with `fetch_adapter: "firecrawl"`; the adapter scrapes via the official `firecrawl-py` SDK with `formats=["markdown"]` and returns `FetchResult(content=markdown, content_type="text/markdown", …)`, so bronze stores LLM-ready markdown (≈10× smaller extraction input — the Phase D "HTML pre-cleaning" goal, delivered by the vendor). The key is read **lazily** from `get_settings().firecrawl_api_key` (config.py stays the only env reader; registration is unconditional so the adapter shows in `known_fetchers()`/the UI dropdown everywhere, and a keyless fetch raises `RuntimeError` naming `FIRECRAWL_API_KEY`). Failures **raise** — a failed Firecrawl scrape has no content, so nothing is ingested and no fabricated bronze exists (contrast httpx, where a 404's body is still real). Finally, the key reaches the collector via the **D-e scoped exception, narrowed**: `orchestrator._child_env()` grows a per-template extra-env allowlist that forwards `FIRECRAWL_API_KEY` only to the first-party `scrape` template — every other template keeps the K4 minimal env.

## Files

- Modify: `src/bellweather/config.py` — `firecrawl_api_key: str | None = None`.
- Create: `src/bellweather/fetch/firecrawl_fetch.py` — `FirecrawlFetcher`, self-registers `"firecrawl"`.
- Modify: `src/bellweather/orchestrator.py` — `_TEMPLATE_EXTRA_ENV` + `_child_env(template)`.
- Modify: `src/bellweather/api.py` — registering import next to the httpx one (line 10).
- Modify: `producers/scrape/collector.py` — registering import next to the httpx one.
- Modify: `src/bellweather/web/data/mock.py` — add `"firecrawl"` to the offline adapter-choices list (~line 631; the live backend picks it up via `/api/fetch-adapters` automatically).
- Modify: `pyproject.toml` — `firecrawl-py` runtime dep.
- Modify: `.env.example` — `FIRECRAWL_API_KEY=` (comment: optional, paid fetch adapter).
- Modify: `tests/conftest.py` — `requires_firecrawl` marker (mirrors `requires_gemini`).
- Create: `tests/test_fetch_firecrawl.py`.
- Modify: `tests/test_orchestrator.py` — env-forwarding cases (and adapt any direct `_child_env()` calls to the new signature).

## Interface

Copied verbatim from the plan's "Locked interfaces" (`docs/plans/2026-06-03-firecrawl-fetch-adapter.md`):

```python
class FirecrawlFetcher:
    name = "firecrawl"
    def fetch(self, url: str, **opts) -> FetchResult: ...
        # key = get_settings().firecrawl_api_key
        #   → None/empty: RuntimeError("firecrawl adapter selected but FIRECRAWL_API_KEY is not set")
        # doc = <lazy firecrawl-py client>.scrape(url, formats=["markdown"])
        # return FetchResult(content=doc.markdown,
        #                    status=<doc metadata status code or 200>,
        #                    content_type="text/markdown",
        #                    final_url=<doc metadata url or url>)

register(FirecrawlFetcher())
```

```python
_TEMPLATE_EXTRA_ENV: dict[str, tuple[str, ...]] = {
    "scrape": ("FIRECRAWL_API_KEY",),  # D-e scoped exception, design D6
}
def _child_env(template: str) -> dict[str, str]: ...
    # existing minimal dict, then forward each allowlisted var set (non-empty)
    # in os.environ; _run_subprocess passes env=_child_env(template)
```

> Pin the exact SDK call/attribute names (`scrape` vs `scrape_url`, metadata casing)
> against the installed `firecrawl-py` at implementation time; the tests lock the
> contract through a fake client, never the live SDK.

## Steps (TDD: failing test first at each step)

- [ ] **Step 1: dep + config.** `uv add firecrawl-py`; add `firecrawl_api_key` to `Settings`; `.env.example` line. Test: a settings round-trip with `FIRECRAWL_API_KEY` set (autouse cache-reset fixture already handles hygiene).

- [ ] **Step 2: adapter unit tests (fake SDK).** In `tests/test_fetch_firecrawl.py`, with a fake client object injected (monkeypatch the lazy client factory): markdown → `content`; `content_type == "text/markdown"`; metadata status/url mapped, defaults (200 / request url) when metadata is missing; **missing key** → `RuntimeError` naming `FIRECRAWL_API_KEY`; import side effect registers `"firecrawl"` (mirror `test_fetch.py::test_importing_httpx_fetch_registers_httpx`); importing the module with no key raises nothing (lazy client, D3).

- [ ] **Step 3: implement `firecrawl_fetch.py`** to green. Lazy key (D4), lazy cached SDK client, error propagation (D5 — no try/except wrapping), self-register at import bottom + the Protocol-satisfaction line, all mirroring `httpx_fetch.py`.

- [ ] **Step 4: registering imports.** Add `import bellweather.fetch.firecrawl_fetch  # noqa: F401` to `api.py` (next to line 10) and `producers/scrape/collector.py`. Test: `GET /api/fetch-adapters` now lists both adapters (extend the existing T39 route test).

- [ ] **Step 5: orchestrator allowlist.** Tests in `test_orchestrator.py`: `_child_env("scrape")` contains `FIRECRAWL_API_KEY` when set in the parent env; omits it when unset/empty; `_child_env("gdelt")` never contains it. Implement `_TEMPLATE_EXTRA_ENV` + the signature change; `_run_subprocess` passes its template through.

- [ ] **Step 6: mock UI choices.** Add `"firecrawl"` to `web/data/mock.py`'s adapter list; adjust any AppTest smoke that asserts the dropdown contents.

- [ ] **Step 7: live smoke (opt-in).** One `requires_firecrawl`-marked test: scrape a stable public URL, assert non-empty markdown + 200. Auto-skips without the key (CI never has it).

- [ ] **Step 8: `make check` green.** Commit per step along the way (`feat:`/`test:` conventional commits).

## Acceptance criteria

- `make check` green; no test requires a network, key, or `make up` beyond what existing suites already require.
- `known_fetchers()` includes `"firecrawl"` in both API and collector processes; the UI Edit form's adapter dropdown offers it (live via `/api/fetch-adapters`, offline via the mock list).
- With `FIRECRAWL_API_KEY` unset, importing every touched module works and `FirecrawlFetcher().fetch(...)` raises `RuntimeError` naming the env var; nothing is ingested on failure paths.
- With the fake client: `FetchResult.content` is the markdown, `content_type == "text/markdown"`, status/final_url honor metadata with 200/request-url defaults.
- `_child_env("scrape")` forwards the key (set → present, unset → absent); all other templates keep today's exact minimal env.
- `config.py` remains the only env reader (the orchestrator's `os.environ` use for forwarding is the existing K4 exemption pattern, extended — not a new settings read).
