from datetime import datetime, timezone

from bellweather.contracts import Submission


def run(params: dict, client) -> dict:
    sub = Submission(
        source="fixture.echo_series",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        idempotency_key=f"{params['symbol_key']}:1",
        payload={
            "symbol_key": params["symbol_key"],
            "symbol_kind": "fixture-metric",
            "unit": "dimensionless",
            "description": "echo_series fixture point",
            "points": [{"ts": "2026-06-01T12:00:00Z", "value": float(params.get("value", 0.5))}],
        },
    )
    results = client.ingest_batch([sub])
    return {"submitted": len(results)}
