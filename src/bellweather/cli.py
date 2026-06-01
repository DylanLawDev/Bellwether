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
