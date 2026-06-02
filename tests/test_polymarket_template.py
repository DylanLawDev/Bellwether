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


def test_run_skips_variants_with_empty_history(monkeypatch):
    # NO returns no price points (resolved/sparse market). It must not produce a
    # Submission: an empty numeric-series carries no observations, and a non-empty
    # snapshot would get a _now()-based fetched_at, writing a fresh orphan bronze
    # object under a new YYYY/MM/DD prefix on every re-fetch.
    history = {"111": _points(0.30, 0.37), "222": []}
    _patch(monkeypatch, variants=[YES, NO], history=history)

    client = DryRunClient()
    summary = pmkt.run({"url": "u", "backfill": "all"}, client)

    assert summary == {"submitted": 1, "symbols": 1}
    assert [s.payload["symbol_key"] for s in client.captured] == ["polymarket:us-x-by-d:111"]


def test_backfill_recent_uses_daily_interval(monkeypatch):
    history = {"111": _points(0.30)}
    seen = {}
    _patch(monkeypatch, variants=[YES], history=history, interval_seen=seen)
    pmkt.run(
        {"url": "https://polymarket.com/event/us-x-by-d", "backfill": "recent"},
        DryRunClient(),
    )
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
        [
            "run-template",
            "--template",
            "polymarket",
            "--dry-run",
            "--params",
            json.dumps({"url": "https://polymarket.com/event/us-x-by-d"}),
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["submitted"] == 1
    assert summary["sample"][0]["content_type"] == "numeric-series-v1"
    assert summary["sample"][0]["payload"]["symbol_key"] == "polymarket:us-x-by-d:111"
