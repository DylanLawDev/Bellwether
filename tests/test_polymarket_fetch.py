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
