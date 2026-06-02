import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from bellweather.cli import app
from bellweather.client import DryRunClient
from bellweather.contracts import Submission

FIXTURE_TEMPLATES = Path(__file__).parent / "fixtures" / "templates"


def _sub(key: str) -> Submission:
    return Submission(
        source="fixture.echo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        idempotency_key=key,
        payload={
            "symbol_key": "s",
            "symbol_kind": "k",
            "points": [{"ts": "2026-06-01T12:00:00Z", "value": 0.5}],
        },
    )


def test_dry_run_client_captures_without_io():
    c = DryRunClient()
    r1 = c.ingest(_sub("a"))
    rs = c.ingest_batch([_sub("b"), _sub("c")])
    assert r1.status == "created"
    assert [r.status for r in rs] == ["created", "created"]
    assert [s.idempotency_key for s in c.captured] == ["a", "b", "c"]


def test_dry_run_client_context_manager():
    with DryRunClient() as c:
        c.ingest(_sub("a"))
    assert len(c.captured) == 1


def test_run_template_dry_run_emits_summary_with_sample(monkeypatch):
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", str(FIXTURE_TEMPLATES))
    monkeypatch.syspath_prepend(str(FIXTURE_TEMPLATES))
    sys.modules.pop("echo.producer", None)  # avoid stale import across tests

    result = CliRunner().invoke(
        app,
        [
            "run-template",
            "--template",
            "echo",
            "--dry-run",
            "--params",
            json.dumps({"symbol_key": "fixture:x", "value": 0.42}),
        ],
    )
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["submitted"] == 1
    assert summary["dry_run"] is True
    assert summary["sample"][0]["payload"]["symbol_key"] == "fixture:x"
    assert summary["sample"][0]["content_type"] == "numeric-series-v1"


def test_run_template_unknown_template_errors(monkeypatch):
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", str(FIXTURE_TEMPLATES))
    result = CliRunner().invoke(
        app, ["run-template", "--template", "nope", "--dry-run", "--params", "{}"]
    )
    assert result.exit_code != 0
