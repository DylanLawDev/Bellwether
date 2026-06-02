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


SAMPLE_LIMIT = 20  # dry-run shows at most this many would-be submissions


@app.command("run-template")
def run_template(
    template: str = typer.Option(..., help="Template name to run"),
    params: str = typer.Option("{}", help="JSON params dict"),
    dry_run: bool = typer.Option(False, help="Capture submissions without sending"),
) -> None:
    """Run one template's entrypoint with validated params."""
    import contextlib
    from pathlib import Path

    from bellweather.client import BellwetherClient, DryRunClient
    from bellweather.config import get_settings
    from bellweather.templates import get_template, load_entrypoint, validate_params

    tmpl = get_template(template)
    if tmpl is None:
        raise SystemExit(f"unknown template: {template}")
    try:
        validated = validate_params(tmpl, json.loads(params))
    except ValueError as e:
        raise SystemExit(f"invalid params: {e}")

    # Add templates dir to sys.path so template-local modules are importable.
    templates_dir = str(Path(get_settings().bellweather_templates_dir).resolve())
    if templates_dir not in sys.path:
        sys.path.insert(0, templates_dir)

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
