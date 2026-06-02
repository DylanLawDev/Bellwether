import json
import sys

import typer
import uvicorn

from bellweather.migrate import apply_migrations

app = typer.Typer(help="Bellweather")


@app.command()
def migrate():
    applied = apply_migrations()
    typer.echo(f"applied: {applied}")


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000):
    uvicorn.run("bellweather.api:app", host=host, port=port)


@app.command()
def worker(once: bool = False):
    from bellweather.worker import run_worker  # imported lazily; implemented in T11

    run_worker(once=once)


@app.command()
def orchestrate(once: bool = False):
    from bellweather.orchestrator import run_orchestrator

    run_orchestrator(once=once)


@app.command()
def ui(port: int = 8501):
    """Launch the Streamlit web UI (needs the `ui` dependency group)."""
    from pathlib import Path

    try:
        from streamlit.web import cli as st_cli
    except ModuleNotFoundError:
        raise SystemExit("Streamlit not installed. Run: uv sync --group ui")
    app_path = str(Path(__file__).with_name("web") / "app.py")
    sys.argv = ["streamlit", "run", app_path, "--server.port", str(port)]
    st_cli.main()


GDELT_DEMO_NAME = "gdelt-demo"
GDELT_DEMO_TEMPLATE = "gdelt"  # the name in producers/gdelt/template.toml (T28)

# A bundled, plain-text GKG sample so the local go-live walkthrough actually
# completes end-to-end out of the box (the producer's _fetch_lines reads plain
# `.gkg.csv` and does NOT unzip). The live GDELT feed publishes only
# `*.gkg.csv.zip` (master file list: http://data.gdeltproject.org/gdeltv2/masterfilelist.txt),
# so a live batch must be downloaded + unzipped first and the schedule pointed at
# the local file (or a reachable plain-csv URL) — override it via the Schedules UI.
# seed-gdelt-demo only writes the row; it never fetches.
GDELT_DEMO_GKG_URL = "tests/fixtures/gkg_sample.csv"
GDELT_DEMO_INTERVAL_SECONDS = 15 * 60  # 15m default (GDELT publishes every 15 minutes)


@app.command("seed-gdelt-demo")
def seed_demo() -> None:
    """Idempotently seed the gdelt-demo producer schedule (Phase-2 go-live)."""
    from bellweather import schedules
    from bellweather.db import get_conn

    with get_conn() as conn:
        existing = [s for s in schedules.list_schedules(conn) if s["name"] == GDELT_DEMO_NAME]
        if existing:
            typer.echo(
                f"skip: schedule {GDELT_DEMO_NAME!r} already exists (id={existing[0]['id']})"
            )
            return
        sid = schedules.create_schedule(
            conn,
            name=GDELT_DEMO_NAME,
            template=GDELT_DEMO_TEMPLATE,
            params={"url": GDELT_DEMO_GKG_URL},
            interval_seconds=GDELT_DEMO_INTERVAL_SECONDS,
            enabled=True,
        )
        conn.commit()
        typer.echo(f"created: schedule {GDELT_DEMO_NAME!r} (id={sid})")


SAMPLE_LIMIT = 20  # dry-run shows at most this many would-be submissions


@app.command("run-template")
def run_template(
    template: str = typer.Option(..., help="Template name to run"),
    params: str = typer.Option("{}", help="JSON params dict"),
    dry_run: bool = typer.Option(False, help="Capture submissions without sending"),
) -> None:
    """Run one template's entrypoint with validated params."""
    import contextlib

    from bellweather.client import BellwetherClient, DryRunClient
    from bellweather.templates import get_template, load_entrypoint, validate_params

    tmpl = get_template(template)
    if tmpl is None:
        raise SystemExit(f"unknown template: {template}")
    try:
        validated = validate_params(tmpl, json.loads(params))
    except ValueError as e:
        raise SystemExit(f"invalid params: {e}")

    entrypoint = load_entrypoint(tmpl.entrypoint)
    client = DryRunClient() if dry_run else BellwetherClient()
    try:
        # Redirect entrypoint stdout to stderr so template debug output does
        # not contaminate the JSON summary line read by the orchestrator.
        with contextlib.redirect_stdout(sys.stderr):
            result = entrypoint(validated, client) or {}
    finally:
        client.close()

    summary: dict = {"template": template, "submitted": result.get("submitted", 0)}
    summary.update({k: v for k, v in result.items() if k != "submitted"})
    if dry_run:
        summary["dry_run"] = True
        summary["sample"] = [s.model_dump(mode="json") for s in client.captured[:SAMPLE_LIMIT]]
        summary["submitted"] = len(client.captured)
    typer.echo(json.dumps(summary))


POLYMARKET_DEMO_NAME = "polymarket-demo"
POLYMARKET_DEMO_TEMPLATE = "polymarket"
# VERIFY the event URL is still a live Polymarket market before demoing (see producers/polymarket/README.md).
POLYMARKET_DEMO_URL = "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by"
POLYMARKET_DEMO_PARAMS = {"url": POLYMARKET_DEMO_URL, "backfill": "all"}
POLYMARKET_DEMO_INTERVAL = "30m"


@app.command("seed-polymarket-demo")
def seed_polymarket_demo() -> None:
    """Idempotently seed the polymarket-demo producer schedule (Phase-2 go-live)."""
    from bellweather import schedules
    from bellweather.db import get_conn
    from bellweather.templates import parse_interval

    with get_conn() as conn:
        existing = [s for s in schedules.list_schedules(conn) if s["name"] == POLYMARKET_DEMO_NAME]
        if existing:
            typer.echo(
                f"skip: schedule {POLYMARKET_DEMO_NAME!r} already exists (id={existing[0]['id']})"
            )
            return
        sid = schedules.create_schedule(
            conn,
            name=POLYMARKET_DEMO_NAME,
            template=POLYMARKET_DEMO_TEMPLATE,
            params=POLYMARKET_DEMO_PARAMS,
            interval_seconds=parse_interval(POLYMARKET_DEMO_INTERVAL),
            enabled=True,
        )
        conn.commit()
        typer.echo(f"created: schedule {POLYMARKET_DEMO_NAME!r} (id={sid})")


if __name__ == "__main__":  # `python -m bellweather.cli` parity with the console script
    app()
