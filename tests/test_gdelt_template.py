import json
import pathlib

from typer.testing import CliRunner

from bellweather.cli import app
from bellweather.templates import discover_templates

# Repo "producers/" dir (where the gdelt manifest lives) and the GKG fixture (3 rows),
# resolved relative to this test file so the test is CWD-independent.
PRODUCERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "producers"
GKG_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "gkg_sample.csv"
GKG_ROWS = len([ln for ln in GKG_FIXTURE.read_text().splitlines() if ln.strip()])


def test_discover_finds_gdelt_with_url_param_and_15m_interval():
    found = discover_templates(str(PRODUCERS_DIR))
    assert "gdelt" in found  # sibling producer templates may coexist; assert membership
    gdelt = found["gdelt"]
    assert gdelt.entrypoint == "producers.gdelt.producer:run"
    assert gdelt.default_interval_seconds == 900  # "15m"
    by_name = {p.name: p for p in gdelt.params}
    assert "url" in by_name
    assert by_name["url"].required is True
    assert by_name["url"].type == "str"


def test_run_template_dry_run_submits_one_per_gkg_row(monkeypatch):
    # Point the harness at the repo's real producers/ dir, then drive the gdelt
    # template through the dry-run path with the local fixture file as the URL —
    # no network, no DB, no GCS (DryRunClient captures submissions in memory).
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", str(PRODUCERS_DIR))

    result = CliRunner().invoke(
        app,
        [
            "run-template",
            "--template",
            "gdelt",
            "--dry-run",
            "--params",
            json.dumps({"url": str(GKG_FIXTURE)}),
        ],
    )
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["submitted"] == GKG_ROWS  # one submission per GKG row (3)
    assert summary["sample"][0]["content_type"] == "gdelt-gkg-v2"
    assert summary["sample"][0]["kind"] == "unstructured"
    assert summary["sample"][0]["source"] == "gdelt.gkg"
