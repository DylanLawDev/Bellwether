# Firecrawl Fetch Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the first **paid fetch adapter** behind the K5 `FetchProvider` seam — Firecrawl, markdown output — and the conventions every later vendor adapter copies: one self-registering module in `fetch/`, one optional `Settings` key read lazily at fetch time, failures raise (no fabricated bronze), and the key reaches the scrape collector through a per-template env allowlist in the orchestrator (the D-e scoped exception, narrowed to the `scrape` template only). No consumer changes: the collector, the API preview, and the UI dropdown all pick the adapter up through the existing registry/`/api/fetch-adapters` plumbing.

**Architecture:** `FirecrawlFetcher` (`name = "firecrawl"`) mirrors `HttpxFetcher`'s self-registering shape; `fetch()` lazily reads `get_settings().firecrawl_api_key` (RuntimeError naming `FIRECRAWL_API_KEY` when unset), calls Firecrawl's scrape endpoint via the official `firecrawl-py` SDK with `formats=["markdown"]`, and returns `FetchResult(content=markdown, content_type="text/markdown", status=<metadata status|200>, final_url=<metadata url|url>)`. Bronze stores the markdown (≈10× smaller LLM input). Infra mirrors the T44 conditional-secret pattern, except the grant + env mount go to the **orchestrator** SA/Job (not runtime), and `orchestrator._child_env()` forwards the key only to the `scrape` template.

**Tech Stack:** Python 3.12 + `uv`, `firecrawl-py` (**new runtime dep — free tier, 1,000 credits/mo**), Terraform (secret wiring only).

**Spec:** `docs/specs/2026-06-03-firecrawl-fetch-adapter-design.md`.

**Builds on (on `main`):** the LLM scrape engine epic (T33–T42) — the `fetch/` seam (T33), the scrape collector (T40), the control-plane API + `/api/fetch-adapters` (T39), and the T44 conditional-secret baseline this copies.

---

## How to run a ticket (lifecycle)

Tickets live in `docs/plans/tickets/{Open, In Progress, Closed}/`. To work one: move it `Open → In Progress`, branch `ticket/T<NN>-<slug>`, follow TDD, get `make check` green, open one PR. **Merge gate:** a ticket's contents may merge to `main` only when it is in `In Progress/` (work underway) or `Closed/` (done) — never from `Open/`. Move it to `Closed/` when merged. (Mirrors `CLAUDE.md` Conventions.)

---

## Module layout (locked — new + modified for this epic)

```
src/bellweather/
├── config.py                  # MODIFY: + firecrawl_api_key                        [T45]
├── fetch/
│   └── firecrawl_fetch.py     # NEW: FirecrawlFetcher, self-registers "firecrawl"  [T45]
├── orchestrator.py            # MODIFY: _child_env(template) + per-template
│                              #         extra-env allowlist (D6)                   [T45]
├── api.py                     # MODIFY: + 1 registering import (mirrors line 10)   [T45]
└── web/data/mock.py           # MODIFY: + "firecrawl" in the adapter choices list  [T45]
producers/scrape/collector.py  # MODIFY: + 1 registering import                     [T45]
pyproject.toml                 # MODIFY: dependencies += "firecrawl-py"             [T45]
.env.example                   # MODIFY: + FIRECRAWL_API_KEY=                       [T45]
tests/
├── conftest.py                # MODIFY: + requires_firecrawl marker                [T45]
├── test_fetch_firecrawl.py    # NEW: fake-SDK unit tests + live smoke              [T45]
└── test_orchestrator.py       # MODIFY: per-template env forwarding cases          [T45]
infra/
├── variables.tf               # MODIFY: + firecrawl_api_key (default "", sensitive)[T46]
├── main.tf                    # MODIFY: firecrawl secret trio (orchestrator SA!);
│                              #         orchestrator Job env mount; comment surgery[T46]
└── README.md                  # MODIFY: document the secret + who holds it         [T46]
```

## Locked interfaces (use these exact names/signatures across tickets)

**config.py** — add to `Settings` (only `config.py` reads env):

```python
firecrawl_api_key: str | None = None
```

**fetch/firecrawl_fetch.py** — mirrors `httpx_fetch.py` (self-registers at import; lazy
key + lazy SDK client; `**opts` accepted and unused, like `HttpxFetcher`):

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

> Pin the exact `firecrawl-py` call/attribute names (`scrape` vs `scrape_url`,
> `doc.metadata.status_code` casing) against the installed SDK version at
> implementation time; tests lock the *contract* via a fake client, not the SDK.

**orchestrator.py** — `_child_env` gains the template name; everything else unchanged:

```python
_TEMPLATE_EXTRA_ENV: dict[str, tuple[str, ...]] = {
    "scrape": ("FIRECRAWL_API_KEY",),  # D-e scoped exception, design D6
}

def _child_env(template: str) -> dict[str, str]:
    ...  # existing dict, then forward each _TEMPLATE_EXTRA_ENV[template] var
         # that is set (non-empty) in os.environ
# _run_subprocess: env=_child_env(template)
```

**tests/conftest.py** — add after `requires_gemini`:

```python
requires_firecrawl = pytest.mark.skipif(
    not os.environ.get("FIRECRAWL_API_KEY"), reason="FIRECRAWL_API_KEY not set"
)
```

**infra** — T44's conditional pattern with the orchestrator SA as the principal:

```hcl
locals {
  firecrawl_key_set = nonsensitive(var.firecrawl_api_key != "")
}
resource "google_secret_manager_secret_version" "firecrawl_key" {
  count       = local.firecrawl_key_set ? 1 : 0
  secret      = google_secret_manager_secret.firecrawl_key.id
  secret_data = var.firecrawl_api_key
}
resource "google_secret_manager_secret_iam_member" "firecrawl_key_access" {
  secret_id = google_secret_manager_secret.firecrawl_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator.email}"
}
# orchestrator Job container:
dynamic "env" {
  for_each = local.firecrawl_key_set ? [1] : []
  content {
    name = "FIRECRAWL_API_KEY"
    value_source {
      secret_key_ref {
        secret  = google_secret_manager_secret.firecrawl_key.secret_id
        version = "latest"
      }
    }
  }
}
```

## Build order & dependency graph

```
T45 (adapter + config + orchestrator allowlist + dep + tests)   ── code, no DB/GCS
 └─▶ T46 (infra: firecrawl secret trio on the ORCHESTRATOR SA/Job + README)
```

Run them as one stacked PR chain (`ticket/T45-…` ← `ticket/T46-…`).

## Ticket index

| Ticket | Title | Files |
| --- | --- | --- |
| T45 | Firecrawl fetch adapter (`firecrawl-py` dep + lazy key + per-template env allowlist) | `config.py`, `fetch/firecrawl_fetch.py`, `orchestrator.py`, `api.py`, `web/data/mock.py`, `producers/scrape/collector.py`, `pyproject.toml`, `.env.example`, `tests/*` |
| T46 | Firecrawl key infra (orchestrator-SA secret trio + Job env mount + comment surgery) | `infra/variables.tf`, `infra/main.tf`, `infra/README.md` |

## Self-review notes

- **The orchestrator-SA comment in `main.tf` is load-bearing** — it currently argues the
  SA must read *only* the DB-URL secret because anything it can read is reachable by an
  external template. T46 must rewrite it, not append to it: the firecrawl grant is safe
  *because* `_child_env`'s allowlist (T45) forwards the key only to the first-party
  `scrape` template. The infra comment should point at `orchestrator._TEMPLATE_EXTRA_ENV`.
- The collector's existing fallback (`get_fetcher(...) or HttpxFetcher()`) means a spec
  naming a *registered-but-unkeyed* firecrawl adapter fails loudly (D4 RuntimeError),
  while an *unknown* adapter name silently falls back to httpx — pre-existing behavior,
  unchanged here; noted so nobody "fixes" one into the other in passing.
- Bronze for firecrawl specs is **markdown** (`content_type="text/markdown"` in
  provenance). Replays of those records re-extract from markdown; that is the accepted
  D2 trade-off, not a bug.
- `_child_env` currently takes no arguments; its one caller is `_run_subprocess`. The
  signature change is internal to `orchestrator.py` — but check `tests/test_orchestrator.py`
  for direct `_child_env()` calls that need the new argument.
