# T23 — Run-harness + `DryRunClient` + `bellweather run-template`

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§3.1 `run-harness`/`dry-run client`, §7 `cli.py`, K4/K9).
**Depends on:** T08 (`BellwetherClient`), T22 (`templates.py` discovery/validate/load). **Branch:** `ticket/T23-run-harness`. **PR, do not merge without approval.**

## Goal
Build the in-subprocess **run-harness** that executes a template: discover its manifest, validate the passed params against the schema, import the entrypoint, build a client, call `entrypoint(params, client)`, and print a single JSON summary line. Add `DryRunClient` — a no-I/O twin of `BellwetherClient` that captures submissions instead of POSTing — so previews (and tests) run trusted template code committing nothing (K9). This is the unit the orchestrator (T24) and the API preview (T25) spawn as a minimal-env subprocess (K4).

## Files
- Modify: `src/bellweather/client.py` — add `DryRunClient` (same surface as `BellwetherClient`, no HTTP).
- Modify: `src/bellweather/cli.py` — add the `run-template` command (the harness).
- Test: `tests/test_run_template.py` — `DryRunClient` capture behavior + the `run-template --dry-run` CLI via `typer.testing.CliRunner`, pointing `BELLWEATHER_TEMPLATES_DIR` at a fixture template dir.
- Test (fixture): `tests/fixtures/templates/echo/template.toml` + `tests/fixtures/templates/echo/producer.py` — a template whose `run(params, client)` builds a `numeric-series-v1` `Submission` and calls `client.ingest_batch`.

## Interface
From the build plan's **Locked interfaces** (copy verbatim — do not rename).

`client.py` — `DryRunClient` (same surface as `BellwetherClient`, no I/O):
```python
class DryRunClient:
    def __init__(self): self.captured: list[Submission] = []
    def ingest(self, sub: Submission) -> IngestResult:
        self.captured.append(sub); return IngestResult(status="created")
    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]:
        self.captured.extend(subs); return [IngestResult(status="created") for _ in subs]
    def close(self): ...
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
```

`cli.py`:
```python
@app.command("run-template")
def run_template(template: str, params: str = "{}", dry_run: bool = False) -> None: ...
    # discover->validate(json.loads(params))->load_entrypoint->client=(DryRunClient if dry_run else BellwetherClient)
    # ->summary=entrypoint(p,client)->print(json.dumps({"submitted":..., "sample":[...] if dry_run}))
```

From T22 (`templates.py`), used here at run time:
```python
def get_template(name: str, templates_dir: str | None = None) -> Template | None: ...
def validate_params(template: Template, params: dict) -> dict: ...   # defaults + required + choices + coercion; ValueError on bad
def load_entrypoint(entrypoint: str): ...   # "module.path:function" -> callable (run-time only)
```

Entrypoint contract (manifest's `entrypoint = "module:func"`): `def run(params: dict, client) -> dict | None` (returns an optional `{"submitted": int, ...}` summary).

## Steps

> No DB, no GCS, no network in this ticket — `DryRunClient` performs zero I/O and the harness test only exercises `--dry-run`. `make up`/`make migrate` are NOT required.

- [ ] **Step 1: Fixture template** `tests/fixtures/templates/echo/template.toml`
```toml
name        = "echo"
entrypoint  = "echo.producer:run"
description = "Fixture template: emits a numeric-series-v1 submission from params."

[params]
symbol_key = { type = "str", required = true, help = "tracked symbol key" }
value      = { type = "float", default = 0.5, help = "the single point's value" }

[schedule]
default_interval = "30m"
```

- [ ] **Step 2: Fixture entrypoint** `tests/fixtures/templates/echo/producer.py` — builds a `numeric-series-v1` `Submission` and calls `client.ingest_batch` (the entrypoint is imported by `load_entrypoint("echo.producer:run")`, so the fixture dir must be importable as a top-level package path; the test puts it on `sys.path` — see Step 4).
```python
from datetime import datetime, timezone

from bellweather.contracts import Submission


def run(params: dict, client) -> dict:
    sub = Submission(
        source="fixture.echo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        idempotency_key=f"{params['symbol_key']}:1",
        payload={
            "symbol_key": params["symbol_key"],
            "symbol_kind": "fixture-metric",
            "unit": "probability",
            "description": "echo fixture point",
            "points": [{"ts": "2026-06-01T12:00:00Z", "value": params["value"]}],
        },
    )
    results = client.ingest_batch([sub])
    return {"submitted": len(results)}
```

- [ ] **Step 3: Failing test (DryRunClient)** `tests/test_run_template.py`
```python
import json

from typer.testing import CliRunner

from bellweather.cli import app
from bellweather.client import DryRunClient
from bellweather.contracts import Submission
from tests.conftest import _sub  # not exported; build inline instead — see below
```
Replace the bad import line above with an inline submission builder, then add the `DryRunClient` test:
```python
import json
from datetime import datetime, timezone

from typer.testing import CliRunner

from bellweather.cli import app
from bellweather.client import DryRunClient
from bellweather.contracts import Submission


def _sub(key: str) -> Submission:
    return Submission(
        source="fixture.echo",
        kind="structured",
        content_type="numeric-series-v1",
        fetched_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        idempotency_key=key,
        payload={"symbol_key": "s", "symbol_kind": "k",
                 "points": [{"ts": "2026-06-01T12:00:00Z", "value": 0.5}]},
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
```

- [ ] **Step 4: Failing test (run-template CLI)** — append to `tests/test_run_template.py`. The harness imports the entrypoint by its manifest path (`echo.producer:run`), so the fixture template dir must be importable: the test prepends `tests/fixtures/templates` to `sys.path` and points `BELLWEATHER_TEMPLATES_DIR` at the same dir.
```python
import sys
from pathlib import Path

FIXTURE_TEMPLATES = Path(__file__).parent / "fixtures" / "templates"


def test_run_template_dry_run_emits_summary_with_sample(monkeypatch):
    monkeypatch.setenv("BELLWEATHER_TEMPLATES_DIR", str(FIXTURE_TEMPLATES))
    monkeypatch.syspath_prepend(str(FIXTURE_TEMPLATES))
    sys.modules.pop("echo.producer", None)  # avoid stale import across tests

    result = CliRunner().invoke(
        app,
        ["run-template", "--template", "echo", "--dry-run",
         "--params", json.dumps({"symbol_key": "fixture:x", "value": 0.42})],
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
```

- [ ] **Step 5: Run → FAIL** (`uv run pytest tests/test_run_template.py -v`) — `ImportError: cannot import name 'DryRunClient'` and the `run-template` command does not exist.

- [ ] **Step 6: Implement `DryRunClient`** in `src/bellweather/client.py` (append below `BellwetherClient`; `Submission`/`IngestResult` are already imported).
```python
class DryRunClient:
    """Same surface as ``BellwetherClient`` but performs no I/O.

    Captures every submission in ``.captured`` and returns ``created`` results.
    Used by the dry-run preview (K9) and by the run-harness under ``--dry-run``;
    commits nothing, makes no HTTP.
    """

    def __init__(self) -> None:
        self.captured: list[Submission] = []

    def ingest(self, sub: Submission) -> IngestResult:
        self.captured.append(sub)
        return IngestResult(status="created")

    def ingest_batch(self, subs: list[Submission]) -> list[IngestResult]:
        self.captured.extend(subs)
        return [IngestResult(status="created") for _ in subs]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()
```

- [ ] **Step 7: Implement `run-template`** in `src/bellweather/cli.py`. Add the imports at the top alongside the existing `import sys`/`import typer`:
```python
import json

from bellweather.client import BellwetherClient, DryRunClient
from bellweather.templates import get_template, load_entrypoint, validate_params
```
Then add the command (after the existing `ui` command):
```python
SAMPLE_LIMIT = 20  # dry-run shows at most this many would-be submissions


@app.command("run-template")
def run_template(template: str, params: str = "{}", dry_run: bool = False) -> None:
    """Run one template's entrypoint with validated params.

    Discovers the manifest, validates ``params`` against its schema, imports the
    entrypoint (run time only), builds a client (a capturing ``DryRunClient`` when
    ``--dry-run``, else a real ``BellwetherClient``), calls
    ``entrypoint(params, client)``, and prints a single JSON summary line. With
    ``--dry-run`` the summary also carries a ``sample`` of captured submissions and
    commits nothing / makes no HTTP (K9).
    """
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
        result = entrypoint(validated, client) or {}
    finally:
        client.close()

    summary: dict = {"template": template, "submitted": result.get("submitted", 0)}
    summary.update({k: v for k, v in result.items() if k != "submitted"})
    if dry_run:
        summary["dry_run"] = True
        summary["sample"] = [
            s.model_dump(mode="json") for s in client.captured[:SAMPLE_LIMIT]
        ]
        summary["submitted"] = len(client.captured)
    typer.echo(json.dumps(summary))
```

- [ ] **Step 8: Run → PASS** (`uv run pytest tests/test_run_template.py -v`). The dry-run test asserts `submitted == 1`, `dry_run is True`, and the captured `sample` carries the `numeric-series-v1` payload; the unknown-template test asserts a non-zero exit code.

- [ ] **Step 9: `make check`** — `ruff check . && ruff format --check . && pytest` green. (No DB/GCS tests added here, so the suite passes without `make up`.)

- [ ] **Step 10: Commit** (`feat: run-harness CLI + DryRunClient`).

## Acceptance criteria
- `DryRunClient` mirrors `BellwetherClient`'s surface (`ingest`/`ingest_batch`/`close`/context manager), captures every submission in `.captured`, returns `IngestResult(status="created")`, and performs no HTTP or DB I/O.
- `bellweather run-template --template NAME --params JSON [--dry-run]` discovers the manifest via `templates.get_template`, validates params via `validate_params` (`json.loads(params)` first), imports the entrypoint via `load_entrypoint` only at run time, builds a `DryRunClient` under `--dry-run` (else a `BellwetherClient`), calls `entrypoint(params, client)`, and prints exactly one JSON summary line.
- Under `--dry-run` the summary includes `"dry_run": true`, a `"submitted"` count equal to the captured submissions, and a `"sample"` list (capped at `SAMPLE_LIMIT`) of `Submission.model_dump(mode="json")` dicts; nothing is committed and no HTTP is made.
- An unknown template (or invalid params) exits non-zero with an actionable message.
- The fixture template's entrypoint builds a `numeric-series-v1` `Submission` and calls `client.ingest_batch`; the test drives it via `typer.testing.CliRunner` with `BELLWEATHER_TEMPLATES_DIR` pointed at the fixture dir — no DB, no GCS, no network.
- `make check` is green.
