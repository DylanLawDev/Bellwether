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


def fetch_price_history(
    token_id: str, *, interval: str = "max", fidelity: int = 60
) -> list[PricePoint]:
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
