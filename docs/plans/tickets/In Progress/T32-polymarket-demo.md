# T32 — Polymarket demo schedule + end-to-end verify (Stack C)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§6 structured path / `numeric-series-v1`, §6.1 structured idempotency, §11 Phase 2 "Demo config", §13 D1 last-value-wins).
**Depends on:** T31 (Polymarket template — `producers/polymarket/template.toml` + `run(params, client)`), T21 (`schedules.py` + migration `0002`), T24 (orchestrator tick + `bellweather orchestrate`), T20 (worker `kind` routing → normalizer → gold), T25 (control-plane API, so the UI Schedules page reads the seeded row).
**Branch:** `ticket/T32-polymarket-demo`. **PR, do not merge without approval.**

## Goal
Make the Polymarket producer *demonstrable end to end* without any manual SQL. Add `bellweather seed-polymarket-demo`, a CLI command that **idempotently** inserts one `producer_schedules` row for the Polymarket template (the `us-x-iran-permanent-peace-deal-by` market) on a 30-minute interval, then document the full live path in `producers/polymarket/README.md`.

```
make up && make migrate              # tables incl. producer_schedules (0002)
bellweather seed-polymarket-demo     # inserts the polymarket-demo schedule (idempotent)
bellweather orchestrate --once       # claims the due schedule -> spawns the Polymarket producer
                                     #   -> producer POSTs numeric-series-v1 to /ingest
bellweather worker --once            # generic normalizer -> gold upsert_value -> observations
bellweather ui                       # Schedules page lists the schedule; Symbols shows the
                                     #   market-probability price series
```

This is **Stack C's go-live ticket** — the structured-path mirror of T29 (GDELT). It adds *only* demo seed state (a schedule row) + docs; the producer logic ships in T31, the structured worker path in T18–T20. No worker/normalizer code here (K6: `numeric-series-v1` is already handled generically on `main`).

**Independent of Stack B.** Stack B (GDELT) owns `bellweather seed-gdelt-demo`; this stack owns `bellweather seed-polymarket-demo`. They are **separate commands** that touch different `producer_schedules` rows, so the two stacks never collide on `cli.py` and can be implemented/merged in either order. (Each mirrors the same self-contained, idempotent shape; see T29.)

## ⚠ Verify before building (external producer caveat)
The producer *script* is T31's responsibility; this ticket only seeds a schedule that **names** the T31 template and passes it params. Before finalizing the demo:

1. **Confirm the template name and param schema in `producers/polymarket/template.toml`** (shipped by T31). This ticket assumes:
   - template `name = "polymarket"`,
   - param `url` (`type="str"`, `required=true`),
   - param `backfill` (`type="str"`, `default="all"`, `choices=["all","recent"]`),
   - `[schedule] default_interval = "30m"`.
   If T31 used different names, **update the seed params + interval to match** — the schedule's `template`/`params` must validate against the manifest, or the orchestrator subprocess exits non-zero and the run records `error`. (`bellweather run-template --template polymarket --params '{...}' --dry-run` validates params without any network or DB.)
2. **Confirm the demo event URL still resolves.** `https://polymarket.com/event/us-x-iran-permanent-peace-deal-by` is a live market that may close/rename. Carry a visible "VERIFY the event URL is still live" comment next to the seeded URL; if it 404s at demo time, swap in any current event URL — the seed is the only place the example URL lives.

**No live Polymarket calls in any test here.** The seed test only inserts a DB row and asserts on it via `schedules.list_schedules`; it never spawns the producer or hits the network.

## Files
- **Modify:** `src/bellweather/cli.py` — add a self-contained `seed-polymarket-demo` command (constants + a name-exists guard + one `schedules.create_schedule` inside `get_conn()` + commit). Mirrors T29's `seed-gdelt-demo`; does **not** touch or depend on the GDELT command.
- **Test:** `tests/test_seed_polymarket_demo.py` — DB test (`make up` + `make migrate`).
- **Doc (create):** `producers/polymarket/README.md` — the end-to-end runbook above plus the `us-x-iran` worked example and the verify caveats.

## Interface
The locked demo constants + command (mirrors the T29 `seed-gdelt-demo` shape; uses the T21 `schedules.create_schedule`/`list_schedules` and T22 `parse_interval` verbatim — owns its own transaction, commits once, the helpers never commit):

```python
# src/bellweather/cli.py
POLYMARKET_DEMO_NAME = "polymarket-demo"            # producer_schedules.name (idempotency key)
POLYMARKET_DEMO_TEMPLATE = "polymarket"            # must match producers/polymarket/template.toml (T31)
# VERIFY the event URL is still a live Polymarket market before demoing (see ⚠ above).
POLYMARKET_DEMO_URL = "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by"
POLYMARKET_DEMO_PARAMS = {"url": POLYMARKET_DEMO_URL, "backfill": "all"}
POLYMARKET_DEMO_INTERVAL = "30m"                   # templates.parse_interval -> 1800s


@app.command("seed-polymarket-demo")
def seed_polymarket_demo() -> None:
    """Idempotently seed the polymarket-demo producer schedule (Phase-2 go-live)."""
    from bellweather import schedules
    from bellweather.db import get_conn
    from bellweather.templates import parse_interval

    with get_conn() as conn:
        existing = [s for s in schedules.list_schedules(conn) if s["name"] == POLYMARKET_DEMO_NAME]
        if existing:
            typer.echo(f"skip: schedule {POLYMARKET_DEMO_NAME!r} already exists (id={existing[0]['id']})")
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
```

Notes on the locked contracts this consumes (do not redefine — they ship on `main` via T21/T22 and on the branch via T31):
- `schedules.create_schedule(conn, *, name, template, params: dict, interval_seconds: int, enabled=True) -> int` — **never commits**; this command owns the txn. `params` is a plain dict (`schedules.py` wraps it as JSONB internally).
- `templates.parse_interval("30m") -> 1800` (T22).
- `template="polymarket"` MUST equal the `name` in `producers/polymarket/template.toml` (T31); `BELLWEATHER_TEMPLATES_DIR` defaults to `producers`, so the manifest is discovered in prod.

## Steps

- [ ] **Step 0: Prereqs.** `make up` (Postgres + fake-gcs), then `make migrate` so `0002_orchestrator.sql` created `producer_schedules`/`producer_runs`. This command is **self-contained** — it does not need or touch the GDELT `seed-gdelt-demo` command (T29); the two stacks are independent.

- [ ] **Step 1: Failing test** `tests/test_seed_polymarket_demo.py`. Drives the CLI through Typer's `CliRunner` (no subprocess, no network), then asserts the seeded row via `schedules.list_schedules`. The autouse settings-cache fixture in `tests/conftest.py` handles cache resets; this test cleans up only the row it touches.
```python
import pytest
from typer.testing import CliRunner

from bellweather import schedules
from bellweather.cli import (
    POLYMARKET_DEMO_INTERVAL,
    POLYMARKET_DEMO_NAME,
    POLYMARKET_DEMO_PARAMS,
    POLYMARKET_DEMO_TEMPLATE,
    app,
)
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from bellweather.templates import parse_interval

runner = CliRunner()


@pytest.fixture(autouse=True)
def _migrated_and_clean():
    apply_migrations()  # ensures 0002 (producer_schedules) is present
    _delete()
    yield
    _delete()


def _delete():
    with get_conn() as conn:
        conn.execute(
            "delete from producer_runs where schedule_id in"
            " (select id from producer_schedules where name=%s)",
            (POLYMARKET_DEMO_NAME,),
        )
        conn.execute("delete from producer_schedules where name=%s", (POLYMARKET_DEMO_NAME,))
        conn.commit()


def _rows():
    with get_conn() as conn:
        return [s for s in schedules.list_schedules(conn) if s["name"] == POLYMARKET_DEMO_NAME]


def test_seed_polymarket_demo_inserts_one_row():
    result = runner.invoke(app, ["seed-polymarket-demo"])
    assert result.exit_code == 0, result.output
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["template"] == POLYMARKET_DEMO_TEMPLATE
    assert row["params"] == POLYMARKET_DEMO_PARAMS
    assert row["params"]["url"].startswith("https://polymarket.com/event/")
    assert row["interval_seconds"] == parse_interval(POLYMARKET_DEMO_INTERVAL) == 1800
    assert row["enabled"] is True


def test_seed_polymarket_demo_is_idempotent_on_rerun():
    first = runner.invoke(app, ["seed-polymarket-demo"])
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    second = runner.invoke(app, ["seed-polymarket-demo"])
    assert second.exit_code == 0, second.output
    assert "skip" in second.output

    assert len(_rows()) == 1  # second run is a no-op, not a duplicate
```

- [ ] **Step 2: Run → FAIL** (`make up` running): `uv run pytest tests/test_seed_polymarket_demo.py -v`. Expect `ImportError` on the `POLYMARKET_DEMO_*` symbols (and the `seed-polymarket-demo` command not existing yet).

- [ ] **Step 3: Implement the command** in `src/bellweather/cli.py` — add the five constants and the `seed-polymarket-demo` command exactly as in the Interface section, placing it alongside `worker`/`ui`/`orchestrate` (and `seed-gdelt-demo` if T29 is on the branch — they are independent commands, no shared code). `typer` is already imported at the top; import `schedules`/`get_conn`/`parse_interval` lazily inside the function (matching `worker`/`orchestrate`/T29's `seed-gdelt-demo`), so importing `cli.py` never forces a DB import. **Carry the "VERIFY the event URL is still live" comment** directly above `POLYMARKET_DEMO_URL`.

- [ ] **Step 4: Run → PASS** (`make up` running): `uv run pytest tests/test_seed_polymarket_demo.py -v`. Both cases green. Smoke the wiring: `uv run bellweather seed-polymarket-demo --help` shows the command, and `uv run bellweather --help` lists it.

- [ ] **Step 5: Create `producers/polymarket/README.md`** — the operator runbook + worked example. It must contain:
  - The end-to-end command sequence (from this ticket's Goal), each step annotated (`seed-polymarket-demo` → schedule row; `orchestrate --once` → producer POSTs `numeric-series-v1`; `worker --once` → generic normalizer → `upsert_value` → `observations`; `ui` → Symbols page shows the `market-probability` series).
  - The `us-x-iran` worked example: the seeded URL, the expected `symbol_key` shape `polymarket:us-x-iran-permanent-peace-deal-by:<token_id>` with `symbol_kind="market-probability"`, `unit="probability"` (values are YES-probabilities in `[0, 1]`).
  - The structured-idempotency note (§6.1): one record per (symbol, fetch) with `idempotency_key = f"{symbol_key}:{sha1(canonical-json(points))}"`, so an identical re-fetch dedups (no-op) while any new/gap-filled point makes a **new** immutable bronze snapshot that re-normalizes — gold stays correct because `upsert_value` is set-semantics (last-value-wins per bucket, §13 D1).
  - The two ⚠ verify caveats (template/param schema must match T31's `template.toml`; the event URL must still resolve, else swap any live event URL into the seed).
  - The isolation note (K4): the orchestrator spawns the producer in a subprocess with only `BELLWEATHER_API_URL` + `BELLWEATHER_TEMPLATES_DIR` (+ inert placeholder DB/bucket); the producer can only `POST /ingest`.
  - A dry-run smoke check needing **no network/DB** (validates the seed params against the manifest):
    `BELLWEATHER_TEMPLATES_DIR=producers uv run bellweather run-template --template polymarket --params '{"url": "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by", "backfill": "recent"}' --dry-run`.

- [ ] **Step 6: `make check`** → green (`ruff check`, `ruff format --check`, full `pytest`). Keep `make up` running so the DB seed test executes rather than erroring.

- [ ] **Step 7: Manual end-to-end smoke (optional, network).** With the API running (`bellweather api`) and `BELLWEATHER_TEMPLATES_DIR=producers`: `bellweather seed-polymarket-demo` → `bellweather orchestrate --once` → `bellweather worker --once`, then confirm a `tracked_symbols` row with `kind="market-probability"` and `observations` rows for the `polymarket:us-x-iran-…` symbol(s). (Skip if Polymarket is unreachable; the demo schedule + dry-run already prove the wiring without the live pull.)

- [ ] **Step 8: Commit** (`feat: seed Polymarket demo schedule + end-to-end runbook`).

## Acceptance criteria
- `bellweather seed-polymarket-demo` inserts a `producer_schedules` row `name="polymarket-demo"`, `template="polymarket"`, `params={"url": "https://polymarket.com/event/us-x-iran-permanent-peace-deal-by", "backfill": "all"}`, `interval_seconds=1800` (`templates.parse_interval("30m")`), `enabled=true` — verified via `schedules.list_schedules`.
- The seed is **idempotent**: a second invocation prints a `skip` line and leaves exactly one `polymarket-demo` row (name-exists guard, no DB unique constraint, no migration change).
- The command is **self-contained and independent of Stack B**: it defines its own `seed-polymarket-demo` command and never references or modifies the GDELT `seed-gdelt-demo` command (T29) — the two stacks merge in any order without conflict.
- `_seed`/`create_schedule` never commit — `seed-polymarket-demo` owns the single transaction + commit (repo convention).
- `producers/polymarket/README.md` documents the full `migrate → seed-polymarket-demo → orchestrate --once → worker --once → UI` path, the `us-x-iran` worked example (`symbol_key` shape, `market-probability`/`probability` unit), the §6.1 snapshot-idempotency key, the K4 isolation note, and both ⚠ verify caveats.
- No live Polymarket calls anywhere in the test suite; the DB test only inserts/asserts on the schedule row.
- `make check` is green (DB seed test requires `make up` + the `0002` migration).
