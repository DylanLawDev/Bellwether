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
        if not points:
            # Nothing to record. An empty numeric-series yields no observations, and
            # build_submission would stamp a _now()-based fetched_at, so each re-fetch
            # of a resolved/sparse market would write a fresh orphan bronze object under
            # a new YYYY/MM/DD prefix instead of being the documented no-op.
            continue
        subs.append(build_submission(slug, variant, points))

    results = client.ingest_batch(subs)
    return {"submitted": len(results), "symbols": len(subs)}


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
