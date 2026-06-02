from datetime import datetime, timezone

from bellweather.contracts import Submission


def run(params: dict, client) -> dict:
    sub = Submission(
        source="fixture.echo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        idempotency_key=f"echo:{params['url']}",
        payload={
            "symbol_key": "echo:url",
            "symbol_kind": "fixture-metric",
            "unit": "probability",
            "description": "echo fixture point",
            "points": [{"ts": "2026-06-01T12:00:00Z", "value": 0.5}],
        },
    )
    results = client.ingest_batch([sub])
    return {"submitted": len(results)}
