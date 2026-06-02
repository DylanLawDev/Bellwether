# T31 — Polymarket template → `numeric-series-v1` (Stack C)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§4 template contract, §6 / §6.1 structured path + idempotency, §11 Phase 2 Polymarket, K1/K4/K6/K8).
**Depends on:** T30 (Polymarket fetch helpers — Gamma + CLOB), T22 (`templates.py` discovery/validate/load), T23 (run-harness + `DryRunClient` + `bellweather run-template`), T19 (`numeric-series-v1` normalizer — the worker-side consumer of what this emits).
**Branch:** `ticket/T31-polymarket-template`. **PR, do not merge without approval.**

## Goal

Add the **Polymarket producer template** — a manifest (`producers/polymarket/template.toml`) plus a `run(params, client)` entrypoint (`producers/polymarket/producer.py`) — that turns a Polymarket event URL into canonical `numeric-series-v1` submissions, one immutable snapshot **per market variant per fetch**.

This is an **external producer** in exactly the sense T12's GDELT producer is (decision K1): collection logic lives in `producers/<name>/`, the script uses **only the injected `client`** to `POST`, and it never constructs DB/bucket access or calls `get_settings()` for the datastore. It is the structured-path twin of GDELT: GDELT is unstructured (themes/persons/orgs → tags via the existing `gdelt-gkg-v2` extractor); Polymarket is **structured** (market probabilities over time → gold observations via the generic `numeric-series-v1` normalizer from T19). **No worker/normalizer code is added here** (K6) — this ticket only emits the canonical payload the merged Phase-1 path already lands in gold.

The network fetch/parse is **not** in this ticket: T30 owns `producers/polymarket/fetch.py` (resolve URL → variants, fetch price history). T31 **consumes** T30's helpers and shapes their output into submissions. Tests monkeypatch the T30 helpers and drive `run(...)` with a `DryRunClient` — **no network, no DB, no GCS** (mirroring how T12's parsing tests never hit the live feed).

## ⚠ Verify before building (external endpoints — same caveat as T12)

Polymarket's API drifts. **T30 already verified and isolated the live endpoints** behind `producers/polymarket/fetch.py`; T31 must NOT re-fetch or re-verify them — it imports T30's helpers and works on their already-parsed return values. Still, carry a visible **"VERIFY against current Polymarket docs"** caveat in `producer.py` referencing T30, and keep T31 free of any direct HTTP. For reference, the contract T30 exposes (confirmed against current docs on 2026-06-01) is:

- **Gamma — event → variants:** T30 owns the live call (`GET https://gamma-api.polymarket.com/events?slug=<slug>`, **array form — take the first** event); each market has `question`, `groupItemTitle`, `conditionId`, and `outcomes`/`clobTokenIds` (returned as **JSON-encoded strings** — T30 decodes them). T31 receives fully-parsed `Variant` objects (`token_id`, `outcome`, `question`, `group_item_title`, `condition_id`), one per tradable outcome token, and never calls Gamma itself. (See T30 for the verified endpoint shape — single source of truth.)
- **CLOB — price history:** `GET https://clob.polymarket.com/prices-history?market={clob_token_id}&interval={all|1h|6h|1d|1w|max}&fidelity={minutes}` → `{"history": [{"t": <unix seconds>, "p": <price float in [0,1]>}]}`. The price `p` is the market-implied probability for that outcome. (Resolved markets can return an empty `history` at fine fidelity — a known quirk; T30 handles fetch, T31 just maps whatever points it gets.)

These are documented here only so the ticket is self-contained; the live calls live in T30.

## Files

- Create: `producers/polymarket/__init__.py` (empty; makes the dir a package, like `producers/gdelt/__init__.py`).
- Create: `producers/polymarket/template.toml` — the manifest.
- Create: `producers/polymarket/producer.py` — `run(params, client)` + the pure shaping helpers below.
- Create: `producers/polymarket/README.md` — how to run it, the `backfill` param, and the T30 verify caveat.
- Test: `tests/test_polymarket_template.py` — no network / no DB / no GCS; monkeypatches T30 helpers, drives `run(params, DryRunClient())`, plus one `bellweather run-template --dry-run` smoke test.

## T30 helper contract (consumed here — do NOT redefine or re-implement)

T30 ships `producers/polymarket/fetch.py` with these network-isolated helpers (small functions, fixture-tested in T30). T31 imports and calls them; tests monkeypatch them:

```python
# producers/polymarket/fetch.py   (authored in T30 — referenced, not created here)
from dataclasses import dataclass

@dataclass
class Variant:
    token_id: str          # the CLOB token id (one tradable outcome)
    outcome: str           # outcome label, e.g. "Yes"
    question: str          # the market question text
    group_item_title: str  # short label, e.g. "Permanent peace deal"
    condition_id: str      # market conditionId (provenance)

@dataclass
class PricePoint:
    ts: datetime           # tz-aware UTC (T30 converts the CLOB unix-seconds `t`)
    value: float           # the CLOB price `p` in [0, 1] (implied probability)

def event_slug_from_url(url: str) -> str: ...                       # parse the event slug out of a Polymarket URL
def fetch_variants(slug: str) -> list[Variant]: ...                 # Gamma: event -> tradable outcome variants
def fetch_price_history(token_id: str, *, interval: str) -> list[PricePoint]: ...  # CLOB prices-history
```

`run(params, client)` is the **only** new surface this ticket adds beyond pure shaping helpers; it composes the three T30 helpers and the shaping below. T31 contains **zero** `httpx`/network code.

## Interface

```python
# producers/polymarket/producer.py
from producers.polymarket.fetch import Variant, PricePoint  # T30

_INTERVAL = {"all": "max", "recent": "1d"}  # backfill param -> CLOB `interval`

def _symbol_key(slug: str, token_id: str) -> str:
    return f"polymarket:{slug}:{token_id}"

def _canonical_points(points: list[PricePoint]) -> list[dict]:
    # sorted-by-ts list of {"ts": ISO8601, "value": float}; the canonical body the
    # idempotency hash is computed over (same ordering => same hash => dedup).
    ...

def _idempotency_key(symbol_key: str, points: list[dict]) -> str:
    # f"{symbol_key}:{sha1(canonical-json(points))}" — one snapshot per (symbol, fetch).
    ...

def build_submission(slug: str, variant: Variant, points: list[PricePoint]) -> Submission:
    # one numeric-series-v1 Submission for a single variant (kind="structured").
    ...

def run(params: dict, client) -> dict:
    # params: {"url": str (required), "backfill": "all"|"recent" (default "all")}
    # resolve url -> slug -> variants; for each variant fetch price history,
    # build ONE submission; client.ingest_batch(all); return {"submitted": <int>, "symbols": <int>}.
    ...
```

**Locked payload (do not vary):** `kind="structured"`, `content_type="numeric-series-v1"`, `source="polymarket"`, and
`payload = {symbol_key, symbol_kind="market-probability", unit="probability", description=<variant question/outcome>, points:[{ts (ISO8601), value (float)}, ...]}`.
`symbol_key = f"polymarket:{slug}:{token_id}"`.
`idempotency_key = f"{symbol_key}:{sha1(canonical-json(points))}"` (the **structured idempotency rule** from the locked contract / spec §6.1): identical re-fetches dedup (no-op, no re-store); any new or gap-filled point changes the hash → a new immutable bronze snapshot → re-normalized (gold upsert is set-semantics, so safe). One record **per (symbol, fetch)** carrying all points.

## Steps

> No DB, no GCS, no network in this ticket. `DryRunClient` does zero I/O and the T30 fetch helpers are monkeypatched in every test, so **`make up` / `make migrate` are NOT required.** (The `run-template --dry-run` smoke test only needs `BELLWEATHER_TEMPLATES_DIR` pointed at `producers/`.)

- [ ] **Step 1: Package marker** — create `producers/polymarket/__init__.py` (empty file), mirroring `producers/gdelt/__init__.py`, so `producers.polymarket.producer:run` is importable.

- [ ] **Step 2: Manifest** `producers/polymarket/template.toml`
```toml
name        = "polymarket"
entrypoint  = "producers.polymarket.producer:run"   # "module.path:function"
description = "Polymarket event price-history collector -> numeric-series-v1"

[params]
url      = { type = "str", required = true, help = "Polymarket event URL" }
backfill = { type = "str", default = "all", choices = ["all", "recent"] }

[schedule]
default_interval = "30m"
```

- [ ] **Step 3: Failing test** `tests/test_polymarket_template.py` — one copy-pasteable file. It monkeypatches the T30 helpers (no network) and drives `run(...)` with a `DryRunClient` (no DB/GCS). The fakes live in the test so the test is self-contained even before T30's real `fetch.py` is verified end-to-end.
```python
import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from bellweather.cli import app
from bellweather.client import DryRunClient
from producers.polymarket import producer as pmkt
from producers.polymarket.fetch import PricePoint, Variant

PRODUCERS_DIR = Path(__file__).resolve().parents[1] / "producers"

YES = Variant(
    token_id="111",
    outcome="Yes",
    question="Will X happen by D?",
    group_item_title="X happens",
    condition_id="0xcond",
)
NO = Variant(
    token_id="222",
    outcome="No",
    question="Will X happen by D?",
    group_item_title="X happens",
    condition_id="0xcond",
)


def _points(*vals):
    return [
        PricePoint(ts=datetime(2026, 5, 31, h, 0, tzinfo=timezone.utc), value=v)
        for h, v in enumerate(vals, start=10)
    ]


def _patch(monkeypatch, *, variants, history, interval_seen=None):
    """Stub the three T30 helpers; record the interval each token was fetched with."""
    monkeypatch.setattr(pmkt, "event_slug_from_url", lambda url: "us-x-by-d")
    monkeypatch.setattr(pmkt, "fetch_variants", lambda slug: variants)

    def fake_history(token_id, *, interval):
        if interval_seen is not None:
            interval_seen[token_id] = interval
        return history[token_id]

    monkeypatch.setattr(pmkt, "fetch_price_history", fake_history)


def test_run_emits_one_numeric_series_submission_per_variant(monkeypatch):
    history = {"111": _points(0.30, 0.37), "222": _points(0.70, 0.63)}
    seen = {}
    _patch(monkeypatch, variants=[YES, NO], history=history, interval_seen=seen)

    client = DryRunClient()
    summary = pmkt.run({"url": "https://polymarket.com/event/us-x-by-d", "backfill": "all"}, client)

    assert summary == {"submitted": 2, "symbols": 2}
    assert seen == {"111": "max", "222": "max"}  # backfill="all" -> CLOB interval "max"

    subs = {s.payload["symbol_key"]: s for s in client.captured}
    assert set(subs) == {
        "polymarket:us-x-by-d:111",
        "polymarket:us-x-by-d:222",
    }
    yes = subs["polymarket:us-x-by-d:111"]
    assert yes.kind == "structured"
    assert yes.content_type == "numeric-series-v1"
    assert yes.source == "polymarket"
    assert yes.payload["symbol_kind"] == "market-probability"
    assert yes.payload["unit"] == "probability"
    assert "Will X happen by D?" in yes.payload["description"]
    assert yes.payload["points"] == [
        {"ts": "2026-05-31T10:00:00+00:00", "value": 0.30},
        {"ts": "2026-05-31T11:00:00+00:00", "value": 0.37},
    ]
    assert yes.fetched_at.tzinfo is not None
    # idempotency_key is "<symbol_key>:<sha1>"
    assert yes.idempotency_key.startswith("polymarket:us-x-by-d:111:")
    assert len(yes.idempotency_key.rsplit(":", 1)[1]) == 40  # sha1 hex digest


def test_backfill_recent_uses_daily_interval(monkeypatch):
    history = {"111": _points(0.30)}
    seen = {}
    _patch(monkeypatch, variants=[YES], history=history, interval_seen=seen)
    pmkt.run({"url": "https://polymarket.com/event/us-x-by-d", "backfill": "recent"}, DryRunClient())
    assert seen == {"111": "1d"}


def test_idempotency_key_stable_across_identical_runs(monkeypatch):
    history = {"111": _points(0.30, 0.37)}
    _patch(monkeypatch, variants=[YES], history=history)

    c1, c2 = DryRunClient(), DryRunClient()
    pmkt.run({"url": "u", "backfill": "all"}, c1)
    pmkt.run({"url": "u", "backfill": "all"}, c2)

    assert c1.captured[0].idempotency_key == c2.captured[0].idempotency_key


def test_idempotency_key_changes_when_a_point_is_added(monkeypatch):
    base = {"111": _points(0.30, 0.37)}
    _patch(monkeypatch, variants=[YES], history=base)
    c1 = DryRunClient()
    pmkt.run({"url": "u", "backfill": "all"}, c1)

    extended = {"111": _points(0.30, 0.37, 0.41)}  # one new gap-filled point
    _patch(monkeypatch, variants=[YES], history=extended)
    c2 = DryRunClient()
    pmkt.run({"url": "u", "backfill": "all"}, c2)

    assert c1.captured[0].idempotency_key != c2.captured[0].idempotency_key


def test_idempotency_key_changes_when_a_point_value_changes(monkeypatch):
    _patch(monkeypatch, variants=[YES], history={"111": _points(0.30, 0.37)})
    c1 = DryRunClient()
    pmkt.run({"url": "u", "backfill": "all"}, c1)

    _patch(monkeypatch, variants=[YES], history={"111": _points(0.30, 0.38)})  # value changed
    c2 = DryRunClient()
    pmkt.run({"url": "u", "backfill": "all"}, c2)

    assert c1.captured[0].idempotency_key != c2.captured[0].idempotency_key


def test_run_template_dry_run_smoke(monkeypatch):
    """The manifest is discoverable and runnable via the harness; T30 helpers stubbed."""
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", str(PRODUCERS_DIR))
    monkeypatch.setattr(pmkt, "event_slug_from_url", lambda url: "us-x-by-d")
    monkeypatch.setattr(pmkt, "fetch_variants", lambda slug: [YES])
    monkeypatch.setattr(
        pmkt, "fetch_price_history", lambda token_id, *, interval: _points(0.30, 0.37)
    )

    result = CliRunner().invoke(
        app,
        ["run-template", "--template", "polymarket", "--dry-run",
         "--params", json.dumps({"url": "https://polymarket.com/event/us-x-by-d"})],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["submitted"] == 1
    assert summary["sample"][0]["content_type"] == "numeric-series-v1"
    assert summary["sample"][0]["payload"]["symbol_key"] == "polymarket:us-x-by-d:111"
```

- [ ] **Step 4: Run → FAIL** (`uv run pytest tests/test_polymarket_template.py -v`) — `ModuleNotFoundError: producers.polymarket.producer` (this ticket not implemented yet). T31 **stacks on T30**, so `producers/polymarket/fetch.py` (the real `Variant`/`PricePoint` dataclasses + `event_slug_from_url`/`fetch_variants`/`fetch_price_history`) is already present on the branch — import it directly. Do **not** create a stub `fetch.py` (that would overwrite T30's verified module). The tests monkeypatch the T30 helpers on the `producer` module, so they stay fully offline. (T31 contains no live HTTP regardless.)

- [ ] **Step 5: Implement** `producers/polymarket/producer.py`
```python
"""EXTERNAL producer: a Polymarket event URL -> numeric-series-v1 submissions.

Resolves an event URL to its tradable outcome *variants* (Gamma), fetches each
variant's price history (CLOB), and submits ONE immutable numeric-series-v1
snapshot per variant via the injected client. Uses nothing privileged: only the
``client`` passed by the run-harness (a real ``BellwetherClient`` for a scheduled
run, a ``DryRunClient`` for a preview). It never constructs DB/bucket access and
never calls ``get_settings()`` for the datastore (decision K1/K4).

The worker lands these in gold via the generic ``numeric-series-v1`` normalizer
(T19) -> ``gold.upsert_value`` (T18); NO worker-side code is needed here (K6).

VERIFY against current Polymarket docs: all live HTTP (Gamma + CLOB endpoints and
their response shapes) lives in ``producers.polymarket.fetch`` (authored/verified
in T30). This module contains no network code and re-uses those helpers as-is.
"""

from __future__ import annotations

import hashlib
import json

from bellweather.contracts import Submission

# Imported at module scope so tests can monkeypatch these names on this module.
from producers.polymarket.fetch import (  # noqa: F401  (re-exported for run/patch)
    PricePoint,
    Variant,
    event_slug_from_url,
    fetch_price_history,
    fetch_variants,
)

SOURCE = "polymarket"
SYMBOL_KIND = "market-probability"
UNIT = "probability"

# backfill param -> CLOB `interval` (decision K8: backfill is a param the SCRIPT
# interprets, not orchestrator logic; the full window is fetched each run and
# idempotent dedup fills gaps + adds new points).
_INTERVAL = {"all": "max", "recent": "1d"}


def _symbol_key(slug: str, token_id: str) -> str:
    return f"{SOURCE}:{slug}:{token_id}"


def _canonical_points(points: list[PricePoint]) -> list[dict]:
    """Stable {ts, value} list, sorted by ts — the body the idempotency hash covers.

    Sorting makes the hash order-independent, so re-fetches that return the same
    points in a different order still dedup.
    """
    return [
        {"ts": p.ts.isoformat(), "value": float(p.value)}
        for p in sorted(points, key=lambda p: p.ts)
    ]


def _idempotency_key(symbol_key: str, canonical_points: list[dict]) -> str:
    """``<symbol_key>:<sha1(canonical-json(points))>`` (structured idempotency, spec §6.1).

    Identical re-fetches -> identical hash -> dedup (no-op). Any new/changed point
    -> new hash -> a new immutable bronze snapshot that re-normalizes (gold upsert
    is set-semantics, so safe).
    """
    blob = json.dumps(canonical_points, sort_keys=True, separators=(",", ":")).encode()
    return f"{symbol_key}:{hashlib.sha1(blob).hexdigest()}"


def build_submission(slug: str, variant: Variant, points: list[PricePoint]) -> Submission:
    """One numeric-series-v1 Submission for a single market variant."""
    symbol_key = _symbol_key(slug, variant.token_id)
    canonical = _canonical_points(points)
    latest = max((p.ts for p in points), default=None)
    return Submission(
        source=SOURCE,
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=latest if latest is not None else _now(),
        idempotency_key=_idempotency_key(symbol_key, canonical),
        payload={
            "symbol_key": symbol_key,
            "symbol_kind": SYMBOL_KIND,
            "unit": UNIT,
            "description": f"{variant.question} ({variant.outcome})",
            "points": canonical,
        },
        provenance={
            "producer": "polymarket",
            "event_slug": slug,
            "token_id": variant.token_id,
            "condition_id": variant.condition_id,
            "outcome": variant.outcome,
        },
    )


def run(params: dict, client) -> dict:
    """Resolve a Polymarket event URL -> per-variant numeric-series-v1 snapshots.

    ``params``: {"url": <event url> (required), "backfill": "all"|"recent" (default "all")}.
    Returns ``{"submitted": <records>, "symbols": <variants>}``.
    """
    interval = _INTERVAL[params.get("backfill", "all")]
    slug = event_slug_from_url(params["url"])

    subs: list[Submission] = []
    for variant in fetch_variants(slug):
        points = fetch_price_history(variant.token_id, interval=interval)
        subs.append(build_submission(slug, variant, points))

    results = client.ingest_batch(subs)
    return {"submitted": len(results), "symbols": len(subs)}


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
```

- [ ] **Step 6: Run → PASS** (`uv run pytest tests/test_polymarket_template.py -v`). The per-variant test asserts one `numeric-series-v1` submission per variant with the locked payload shape and a `sha1`-suffixed `idempotency_key`; the interval tests assert `backfill` maps `all→max`/`recent→1d`; the two idempotency tests assert the key is **stable** across identical runs and **changes** when a point is added or a value changes; the CLI smoke test runs it through `bellweather run-template --dry-run`.

- [ ] **Step 7: `producers/polymarket/README.md`** — document:
  - Purpose: turns a Polymarket event URL into one `numeric-series-v1` snapshot per market variant.
  - Params: `url` (required, the event URL), `backfill` (`all` → CLOB `interval=max`, `recent` → `interval=1d`).
  - Run it: `uv run bellweather run-template --template polymarket --dry-run --params '{"url": "https://polymarket.com/event/<slug>"}'` (point `BELLWEATHER_TEMPLATES_DIR` at `producers/` if not already the default).
  - The **VERIFY against current Polymarket docs** caveat: live Gamma/CLOB calls are in `producers/polymarket/fetch.py` (T30); this script contains no network code.
  - Idempotency: one immutable snapshot per (symbol, fetch); identical re-fetches dedup, new/gap-filled points make a new snapshot (spec §6.1, K8).

- [ ] **Step 8: `make check`** — `ruff check . && ruff format --check . && pytest` green. (No DB/GCS tests added here, so the suite passes without `make up`.)

- [ ] **Step 9: Commit** (`feat: polymarket producer template -> numeric-series-v1`).

## Acceptance criteria

- `producers/polymarket/template.toml` declares `name="polymarket"`, `entrypoint="producers.polymarket.producer:run"`, `[params]` `url` (str, required) + `backfill` (str, default `"all"`, choices `["all","recent"]`), and `[schedule] default_interval="30m"`; it is discoverable via `templates.discover_templates` with `BELLWEATHER_TEMPLATES_DIR=producers` (the prod default).
- `run(params, client)` resolves the event URL → variants → per-variant price history using **only** the T30 helpers (`event_slug_from_url`, `fetch_variants`, `fetch_price_history`) and the injected `client`; it contains **no** `httpx`/network code and never touches DB/bucket or `get_settings()` (K1/K4).
- For **each** variant it builds exactly one submission with `kind="structured"`, `content_type="numeric-series-v1"`, `source="polymarket"`, and `payload={symbol_key=f"polymarket:{slug}:{token_id}", symbol_kind="market-probability", unit="probability", description=<question (outcome)>, points=[{ts: ISO8601, value: float}, ...]}`; all submissions go through `client.ingest_batch` in one call; `run` returns `{"submitted": <int>, "symbols": <int>}`.
- `idempotency_key == f"{symbol_key}:{sha1(canonical-json(points))}"` over the **sorted-by-ts** canonical points: it is **stable** across two identical fetches (dedup → no-op) and **changes** when any point is added or a value changes (new immutable snapshot → re-normalized; gold upsert is set-semantics, so safe) — asserted by comparing two `DryRunClient` captures (spec §6.1 / K8).
- `backfill` maps `all → CLOB interval "max"` and `recent → "1d"` (K8: a param the script interprets).
- GDELT stays unstructured and untouched — this ticket emits `numeric-series-v1` only for Polymarket and adds no worker/normalizer code (K6).
- Tests are fully offline: the T30 fetch helpers are monkeypatched and `DryRunClient` does zero I/O — no network, no DB, no GCS; `make up`/`make migrate` not required.
- `producer.py` carries a visible "VERIFY against current Polymarket docs" caveat pointing at `producers/polymarket/fetch.py` (T30).
- `make check` is green.
