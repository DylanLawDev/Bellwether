# T30 — Polymarket fetch helpers — Gamma + CLOB (Stack C)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§11 Phase 2 — Polymarket template; §6.1 `numeric-series-v1`).
**Depends on:** the Phase-1 infra already merged on `main` (T18–T27). No other ticket blocks this one — it is pure, network-isolated fetch/parse helpers and ships nothing that touches Postgres, GCS, or the worker.
**Branch:** `ticket/T30-polymarket-fetch`. **PR, do not merge without approval.**

## Goal

Build the **pure fetch + parse helpers** for an external Polymarket collector — the first half of Stack C. This ticket delivers *only* the module `producers/polymarket/fetch.py`: the `Variant` / `PricePoint` dataclasses and the three helpers (`event_slug_from_url`, `fetch_variants`, `fetch_price_history`) that T31's template imports and calls. The template (manifest + `run(params, client)` that shapes these into `numeric-series-v1` submissions and POSTs via the injected `client`) is **T31** — do not write it here.

> **This module surface is the locked T30↔T31 seam** (build plan → Locked interfaces → "Polymarket fetch.py contract"). T31 does `from producers.polymarket.fetch import Variant, PricePoint, event_slug_from_url, fetch_variants, fetch_price_history` and operates on `Variant`/`PricePoint` **objects** — so this ticket must ship exactly those names and dataclasses, no more, no less.

Like `producers/gdelt/producer.py`, this producer is **external**: it lives under `producers/polymarket/`, uses nothing privileged, never imports `bellweather.db` / `bellweather.gold` / the bronze store, and never calls `get_settings()`. **All network lives behind one tiny `_get(url, params)` function**; every test feeds canned JSON to the parse helpers or monkeypatches `_get` — **no live calls, ever.**

## ⚠ Verify before building (external endpoints drift)

Polymarket's API shapes change. **Before writing fetch code, re-confirm the two endpoints and their field names against the current official docs** (`https://docs.polymarket.com`). Carry the verification result as inline `# VERIFY against current Polymarket docs (<date>)` comments on every URL and field choice, exactly like `producers/gdelt/producer.py` does for the GKG columns.

Verified at authoring time (2026-06-01) — wire these in, but re-confirm:

- **Gamma API — event + nested markets by slug.** Base `https://gamma-api.polymarket.com`. Get an event (with its markets) by slug via **`GET /events?slug=<slug>`** (returns a JSON **array** of matching events; take the first). **This array form is the one this module uses** — do not use the alternate `/events/slug/<slug>` single-object path. An event carries `slug`, `title`, and a nested **`markets`** array. Each market carries:
  - `question` — human label for that contract (e.g. "Will X happen by D?").
  - `groupItemTitle` — short label for the variant within the event (e.g. "Permanent peace deal"); may be absent → default `""`.
  - `outcomes` — a **JSON-encoded string**, e.g. `"[\"Yes\", \"No\"]"` (NOT a native array — must `json.loads`).
  - `clobTokenIds` — a **JSON-encoded string** of the CLOB token ids parallel to `outcomes`, e.g. `"[\"7184...\", \"5872...\"]"` (also `json.loads`).
  - `conditionId` — the on-chain condition id (kept for provenance).
- **CLOB API — price history by token id.** Base `https://clob.polymarket.com`. **`GET /prices-history?market=<token_id>&interval=<max|1w|1d|6h|1h>&fidelity=<minutes>`** → `{"history": [{"t": <unix epoch seconds int>, "p": <float 0..1 probability/price>}, ...]}`. Note the query param is named **`market`** but its value is the **CLOB token id**, not the conditionId.

If a re-check shows a field renamed or moved, change it in **one place** (the parse helper) and update the fixture + comment — the helper boundaries below are designed so a drift touches one function.

## Files

- **Create:** `producers/polymarket/__init__.py` (empty package marker, like `producers/gdelt/__init__.py`).
- **Create:** `producers/polymarket/fetch.py` (the dataclasses + helpers below).
- **Test:** `tests/test_polymarket_fetch.py`.
- **Test fixtures:** `tests/fixtures/polymarket/event.json` (one Gamma event with ≥1 market), `tests/fixtures/polymarket/prices_history.json` (one CLOB `prices-history` response).

No migrations, no `make up`/`make migrate` — this ticket touches no database or bucket. `make check` runs the new tests with zero network and zero datastore.

## Interface (LOCKED — exactly what T31 imports)

```python
# producers/polymarket/fetch.py
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Variant:
    token_id: str          # the CLOB token id (one tradable outcome)
    outcome: str           # outcome label, e.g. "Yes"
    question: str          # the market question text
    group_item_title: str  # short variant label (market.groupItemTitle; "" if absent)
    condition_id: str      # market conditionId (provenance)


@dataclass
class PricePoint:
    ts: datetime           # tz-aware UTC (converted from the CLOB unix-seconds `t`)
    value: float           # the CLOB price `p` in [0, 1] (implied probability)


def event_slug_from_url(url: str) -> str: ...
    # "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by" -> "us-x-iran-permanent-peace-deal-by"
    # tolerant of a trailing slash, query string, and fragment.

def _get(url: str, params: dict | None = None) -> dict | list: ...
    # the ONLY network call. httpx.get(url, params=params, timeout=30).raise_for_status().json().
    # Tests monkeypatch this; nothing else does real I/O.

def fetch_variants(slug: str) -> list[Variant]: ...
    # GET /events?slug=<slug> via _get -> first event -> one Variant per (market, outcome).
    # decodes the JSON-encoded `outcomes`/`clobTokenIds` strings and zips them.
    # raises ValueError if no event matches the slug.

def fetch_price_history(token_id: str, *, interval: str = "max", fidelity: int = 60) -> list[PricePoint]: ...
    # GET /prices-history?market=<token_id>&interval=&fidelity= via _get; parses {"history":[{"t","p"}]}
    # into [PricePoint(ts=datetime.fromtimestamp(t, tz=utc), value=float(p))].
```

T31 consumes these objects directly: it reads `Variant.token_id`/`.outcome`/`.question`/`.group_item_title`/`.condition_id` to build the `symbol_key` + description, and `PricePoint.ts`/`.value` to build the `numeric-series-v1` points. **Keep the names/fields exact.**

## Steps

> TDD: write the failing test, run it, see it fail for the stated reason, write the minimal impl, see it pass, commit. Run with `uv run pytest tests/test_polymarket_fetch.py -v`.

- [ ] **Step 1: Package marker.** Create `producers/polymarket/__init__.py` empty (mirrors `producers/gdelt/__init__.py`).

- [ ] **Step 2: Fixtures.** Create `tests/fixtures/polymarket/event.json` — a trimmed but realistic Gamma `/events?slug=` response: a JSON array with one event. The market's `outcomes`/`clobTokenIds` are **JSON-encoded strings**, and it carries `groupItemTitle` (the load-bearing details to capture):

```json
[
  {
    "slug": "us-x-iran-permanent-peace-deal-by",
    "title": "US x Iran permanent peace deal by ...?",
    "markets": [
      {
        "question": "Will the US and Iran sign a permanent peace deal by year end?",
        "groupItemTitle": "Permanent peace deal",
        "conditionId": "0xabc123",
        "outcomes": "[\"Yes\", \"No\"]",
        "clobTokenIds": "[\"71846647...YES\", \"58726391...NO\"]"
      }
    ]
  }
]
```

  Create `tests/fixtures/polymarket/prices_history.json` — a trimmed CLOB `prices-history` response. `t` is unix epoch **seconds**:

```json
{
  "history": [
    { "t": 1717200000, "p": 0.37 },
    { "t": 1717203600, "p": 0.41 }
  ]
}
```

  (`1717200000` = `2024-06-01T00:00:00+00:00`; `1717203600` = `2024-06-01T01:00:00+00:00` — used so the test can assert exact timestamps.)

- [ ] **Step 3: Failing test** `tests/test_polymarket_fetch.py`:

```python
# Polymarket fetch helpers — pure parse + ONE isolated _get(). No live network: the
# fetch_* tests monkeypatch fetch._get to return canned fixture JSON. The Gamma event
# carries `outcomes`/`clobTokenIds` as JSON-encoded STRINGS (verified against the
# Polymarket Gamma docs 2026-06-01); decoding them is the load-bearing parse step.
import json
import pathlib
from datetime import datetime, timezone

import pytest

from producers.polymarket import fetch
from producers.polymarket.fetch import PricePoint, Variant

FIX = pathlib.Path(__file__).parent / "fixtures" / "polymarket"
EVENT = json.loads((FIX / "event.json").read_text())
PRICES = json.loads((FIX / "prices_history.json").read_text())


def test_event_slug_from_url_parses_example():
    url = "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by"
    assert fetch.event_slug_from_url(url) == "us-x-iran-permanent-peace-deal-by"


def test_event_slug_from_url_tolerates_trailing_slash_query_and_fragment():
    base = "us-x-iran-permanent-peace-deal-by"
    assert fetch.event_slug_from_url(f"https://polymarket.com/event/{base}/") == base
    assert fetch.event_slug_from_url(f"https://polymarket.com/event/{base}?tid=99") == base
    assert fetch.event_slug_from_url(f"https://polymarket.com/event/{base}#yes") == base


def test_fetch_variants_decodes_json_encoded_strings(monkeypatch):
    seen = {}

    def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return EVENT  # the Gamma list-form response

    monkeypatch.setattr(fetch, "_get", fake_get)
    variants = fetch.fetch_variants("us-x-iran-permanent-peace-deal-by")

    assert seen["url"].startswith("https://gamma-api.polymarket.com")
    assert seen["params"] == {"slug": "us-x-iran-permanent-peace-deal-by"}
    assert len(variants) == 2
    yes = variants[0]
    assert isinstance(yes, Variant)
    assert yes.token_id == "71846647...YES"
    assert yes.outcome == "Yes"
    assert yes.question == "Will the US and Iran sign a permanent peace deal by year end?"
    assert yes.group_item_title == "Permanent peace deal"
    assert yes.condition_id == "0xabc123"
    assert variants[1].token_id == "58726391...NO"
    assert variants[1].outcome == "No"


def test_fetch_variants_raises_when_no_event(monkeypatch):
    monkeypatch.setattr(fetch, "_get", lambda url, params=None: [])
    with pytest.raises(ValueError):
        fetch.fetch_variants("does-not-exist")


def test_fetch_price_history_parses_to_pricepoints(monkeypatch):
    seen = {}

    def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return PRICES

    monkeypatch.setattr(fetch, "_get", fake_get)
    points = fetch.fetch_price_history("71846647...YES", interval="max")

    assert seen["url"].startswith("https://clob.polymarket.com")
    # the CLOB query param is literally `market` but carries the TOKEN id.
    assert seen["params"]["market"] == "71846647...YES"
    assert seen["params"]["interval"] == "max"
    assert points == [
        PricePoint(ts=datetime(2024, 6, 1, 0, 0, tzinfo=timezone.utc), value=0.37),
        PricePoint(ts=datetime(2024, 6, 1, 1, 0, tzinfo=timezone.utc), value=0.41),
    ]
    assert points[0].ts.tzinfo is not None
```

- [ ] **Step 4: Run → FAIL** (`uv run pytest tests/test_polymarket_fetch.py -v`): the module/functions don't exist yet.

- [ ] **Step 5: Implement** `producers/polymarket/fetch.py`:

```python
"""External Polymarket fetch helpers: Gamma (event -> variants) + CLOB (token -> price history).

Pure parse functions + dataclasses, plus a single isolated ``_get`` network call so tests run
on canned fixtures. The consuming template (T31) imports Variant/PricePoint and the three
helpers, shapes them into a ``numeric-series-v1`` submission, and POSTs via the injected client;
this module touches nothing privileged.

VERIFY against current Polymarket docs (https://docs.polymarket.com) before relying on the URLs
and field names below -- the API drifts (mirrors producers/gdelt's GKG-column caveat).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

# VERIFY against current Polymarket docs (2026-06-01):
#   Gamma event+markets by slug: GET /events?slug=<slug> -> JSON array; take the first.
#   CLOB price history by token:  GET /prices-history?market=<token_id>&interval=&fidelity=
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class Variant:
    token_id: str
    outcome: str
    question: str
    group_item_title: str
    condition_id: str


@dataclass
class PricePoint:
    ts: datetime
    value: float


def event_slug_from_url(url: str) -> str:
    """https://polymarket.com/event/<slug>[/][?query][#frag] -> "<slug>"."""
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def _get(url: str, params: dict | None = None) -> dict | list:
    """The ONLY network call. Isolated so tests can monkeypatch it with fixture JSON."""
    resp = httpx.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _decode_list(value: object) -> list:
    """Gamma sends list fields as JSON-encoded strings; tolerate native lists too."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return json.loads(value)


def fetch_variants(slug: str) -> list[Variant]:
    """Gamma: event by slug -> one Variant per (market, outcome).

    VERIFY: /events?slug= returns an array (take first); each market's `outcomes` and
    `clobTokenIds` are JSON-ENCODED STRINGS, parallel by index -- decode then zip.
    """
    events = _get(f"{GAMMA_BASE}/events", params={"slug": slug})
    if not events:
        raise ValueError(f"no Polymarket event for slug {slug!r}")
    event = events[0]
    variants: list[Variant] = []
    for market in event.get("markets", []):
        outcomes = _decode_list(market.get("outcomes"))
        token_ids = _decode_list(market.get("clobTokenIds"))
        question = market.get("question", "")
        group_item_title = market.get("groupItemTitle", "")
        condition_id = market.get("conditionId", "")
        for outcome, token_id in zip(outcomes, token_ids):
            variants.append(
                Variant(
                    token_id=token_id,
                    outcome=outcome,
                    question=question,
                    group_item_title=group_item_title,
                    condition_id=condition_id,
                )
            )
    return variants


def fetch_price_history(token_id: str, *, interval: str = "max", fidelity: int = 60) -> list[PricePoint]:
    """CLOB price history for a token id -> PricePoints.

    VERIFY: query param is `market` but its value is the CLOB token id; response is
    {"history": [{"t": <unix seconds>, "p": <float>}]}.
    """
    data = _get(
        f"{CLOB_BASE}/prices-history",
        params={"market": token_id, "interval": interval, "fidelity": fidelity},
    )
    points: list[PricePoint] = []
    for row in data.get("history", []):
        ts = datetime.fromtimestamp(int(row["t"]), tz=timezone.utc)
        points.append(PricePoint(ts=ts, value=float(row["p"])))
    return points
```

- [ ] **Step 6: Run → PASS** (`uv run pytest tests/test_polymarket_fetch.py -v`). All tests green.

- [ ] **Step 7: `make check`** green (`ruff check . && ruff format --check . && pytest`). Fix lint/format (e.g. import order) if `ruff` complains.

- [ ] **Step 8: Commit** (`feat: add Polymarket fetch helpers (Gamma + CLOB)`), then open the PR. Do not merge without approval.

## Acceptance criteria

- `producers/polymarket/fetch.py` ships **exactly** the locked surface T31 imports: dataclasses `Variant(token_id, outcome, question, group_item_title, condition_id)` and `PricePoint(ts: datetime, value: float)`, plus `event_slug_from_url`, `_get`, `fetch_variants(slug) -> list[Variant]`, `fetch_price_history(token_id, *, interval="max", fidelity=60) -> list[PricePoint]`.
- `event_slug_from_url("https://polymarket.com/event/us-x-iran-permanent-peace-deal-by")` returns `"us-x-iran-permanent-peace-deal-by"`, robust to a trailing slash, query string, and fragment.
- `fetch_variants` calls `GET /events?slug=<slug>` (array form, first event), decodes the **JSON-encoded** `outcomes`/`clobTokenIds` strings, captures `groupItemTitle`, and yields one `Variant` per outcome; raises `ValueError` when no event matches.
- `fetch_price_history` passes the token id as the `market` query param plus `interval`/`fidelity`, and parses `{"history":[{"t","p"}]}` into `[PricePoint(ts: tz-aware UTC datetime, value: float)]`.
- **All network is behind `_get`**; every test monkeypatches `_get` or calls a pure parser. No live calls, no Postgres, no GCS — `make up`/`make migrate` not needed.
- Every URL/field choice carries a visible `# VERIFY against current Polymarket docs` caveat; the module imports nothing from `bellweather.db`/`bellweather.gold`/the bronze store and never calls `get_settings()`.
- `make check` is green.
