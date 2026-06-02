# T29 — GDELT demo schedule + go-live (Stack B)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§11 Phase 2 — "Demo config: seed schedules"; §5 the schedule registry; K3 schedules-as-app-state). **Depends on:** T28 (the `producers/gdelt/template.toml` manifest + `run(params, client)`), T21 (`schedules.py` + migration `0002`), T24 (`bellweather orchestrate`), T25 (control-plane API, so the UI Schedules page can read the seeded row). **Branch:** `ticket/T29-gdelt-demo`. **PR, do not merge without approval.**

## Goal
Make the orchestrator + GDELT template **go live** with a single command. Add `bellweather seed-gdelt-demo`, a CLI command that **idempotently** inserts one `producer_schedules` row binding the `gdelt` template (from T28) to a concrete GKG file URL on a 15-minute interval, enabled. Re-running it is a no-op (skip if a schedule named `gdelt-demo` already exists), so it is safe to run on every deploy / locally as many times as you like.

Then document the full end-to-end go-live runbook in `producers/gdelt/README.md`:

```
bellweather migrate        # apply 0001 + 0002 (creates producer_schedules)
bellweather seed-gdelt-demo      # insert the gdelt-demo schedule (idempotent)
bellweather orchestrate --once   # due schedule -> subprocess gdelt template -> POST /ingest (bronze + queue)
bellweather worker --once  # drains the queue -> gdelt-gkg-v2 extractor -> tags + gold
# -> the UI Schedules page lists gdelt-demo; the Dashboard shows the new tags/observations
```

This is the GDELT half of Phase 2's demo (the Polymarket half is T32). It proves the orchestrator generalizes the **existing** v0 GDELT producer with **zero new pipeline code** — GDELT is **unstructured** and reuses the `gdelt-gkg-v2` extractor already on `main` (themes/persons/orgs/locations/tone → tags). It does **not** emit `numeric-series-v1`.

## ⚠ Verify before building (current GDELT docs)
The seeded schedule's `params["url"]` is a **concrete GKG file URL**, and GDELT URLs/layout drift (same caveat as T12 / `producers/gdelt/producer.py`). **Before committing the seed value, confirm a current, fetchable GKG 2.1 file URL** from the master file list:

```
http://data.gdeltproject.org/gdeltv2/masterfilelist.txt
```

Each line is `size MD5 URL`; pick a recent `*.gkg.csv` URL (GDELT publishes a new batch every 15 minutes; `*.gkg.csv.zip` is also listed — the producer reads plain `.gkg.csv`). Use that as the seed default. **Carry the "VERIFY against current GDELT docs" caveat as a visible comment next to the URL constant in the seed code** — the URL is a demo default an operator overrides via the UI; the *acceptance test never fetches it* (it asserts only the schedule row).

Keep all network behavior out of this ticket entirely: `seed-gdelt-demo` only writes a DB row; the actual fetch happens later, inside the `gdelt` template subprocess spawned by `orchestrate`. The test must never make a live call.

## Files
- Modify: `src/bellweather/cli.py` — add the `seed-gdelt-demo` command (the only new code; it calls `schedules.create_schedule` inside a `get_conn()` + commit, guarded by a name-exists check).
- Modify: `producers/gdelt/README.md` — add a "## Go-live: run it under the orchestrator" section with the runbook above.
- Test: `tests/test_seed_demo.py` — DB test (`make up` + `make migrate`): `seed-gdelt-demo` inserts exactly one `gdelt-demo` schedule; running it twice does **not** duplicate.

## Interface
The seed command (locked shape — uses the T21 `schedules.create_schedule` / `schedules.list_schedules` signatures verbatim, owns its own transaction via `get_conn()` + `conn.commit()`, never reads `os.environ` for the DB):

```python
# cli.py
GDELT_DEMO_NAME = "gdelt-demo"
GDELT_DEMO_TEMPLATE = "gdelt"   # the name in producers/gdelt/template.toml (T28)

# VERIFY against current GDELT docs (master file list, see producers/gdelt/README.md):
#   http://data.gdeltproject.org/gdeltv2/masterfilelist.txt
# A concrete GKG 2.1 *.gkg.csv batch URL. This is only a demo default — an operator
# overrides it via the Schedules UI. seed-gdelt-demo writes the row; it never fetches.
GDELT_DEMO_GKG_URL = "http://data.gdeltproject.org/gdeltv2/20260601000000.gkg.csv"
GDELT_DEMO_INTERVAL_SECONDS = 15 * 60   # 15m default (GDELT publishes every 15 minutes)


@app.command("seed-gdelt-demo")
def seed_demo() -> None:
    """Idempotently seed the gdelt-demo producer schedule (Phase-2 go-live)."""
    from bellweather import schedules
    from bellweather.db import get_conn

    with get_conn() as conn:
        existing = [s for s in schedules.list_schedules(conn) if s["name"] == GDELT_DEMO_NAME]
        if existing:
            typer.echo(f"skip: schedule {GDELT_DEMO_NAME!r} already exists (id={existing[0]['id']})")
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
```

Notes on the locked contracts this consumes (do not redefine them — they ship on `main` via T21/T28):
- `schedules.create_schedule(conn, *, name, template, params: dict, interval_seconds: int, enabled=True) -> int` — **never commits** (this command owns the txn). `params` is a plain dict; `schedules.py` wraps it with `psycopg.types.json.Json` internally.
- The `params` key is **`url`** — the key the **T28** `gdelt` manifest declares (`[params] url`). T28's template entrypoint `run(params, client)` reads `params["url"]`; the manual/CLI helper `run_path(path_or_url, client)` is a separate function (there is no `producer.run(path_or_url, client)` after T28). The seeded `url` MUST match T28's manifest param name, or `orchestrate` validates the schedule against the manifest (a required `url` param), drops the unknown key, and the run records `error`.
- `template="gdelt"` MUST equal the `name` field in `producers/gdelt/template.toml` (T28). `BELLWEATHER_TEMPLATES_DIR` defaults to `producers`, so the manifest is discovered in prod without extra config.

## Steps

- [ ] **Step 0: Prereqs.** Start Postgres and apply migrations so `producer_schedules` exists:
  `make up` then `make migrate` (applies `0001_initial.sql` + `0002_orchestrator.sql`). Confirm
  T28 is present: `ls producers/gdelt/template.toml` exists and its `name = "gdelt"`. If T28 is
  not yet merged into the branch base, stack this branch on T28 first (per the stacked-branch
  convention in `CLAUDE.md`).

- [ ] **Step 1: Failing test** `tests/test_seed_demo.py`. This is a DB test (needs `make up` +
  `make migrate`). It invokes the command through Typer's `CliRunner` so it exercises the real
  `seed-gdelt-demo` wiring (the `get_conn()` + commit path), and asserts via `schedules.list_schedules`
  that exactly one `gdelt-demo` row exists after one run AND after a second run (idempotent). It
  cleans up its own rows on teardown so re-runs are deterministic, and it never touches the network.
```python
import pytest
from typer.testing import CliRunner

from bellweather import schedules
from bellweather.cli import GDELT_DEMO_GKG_URL, GDELT_DEMO_NAME, app
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations

runner = CliRunner()


@pytest.fixture(autouse=True)
def _migrated_and_clean():
    # Ensure 0002 is applied, and remove any pre-existing gdelt-demo row so the
    # idempotency assertions start from a known-empty state. Clean up after too.
    apply_migrations()
    _delete_demo()
    yield
    _delete_demo()


def _delete_demo():
    with get_conn() as conn:
        conn.execute(
            "delete from producer_runs where schedule_id in"
            " (select id from producer_schedules where name=%s)",
            (GDELT_DEMO_NAME,),
        )
        conn.execute("delete from producer_schedules where name=%s", (GDELT_DEMO_NAME,))
        conn.commit()


def _demo_rows():
    with get_conn() as conn:
        return [s for s in schedules.list_schedules(conn) if s["name"] == GDELT_DEMO_NAME]


def test_seed_demo_inserts_one_gdelt_demo_schedule():
    result = runner.invoke(app, ["seed-gdelt-demo"])
    assert result.exit_code == 0, result.output

    rows = _demo_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["template"] == "gdelt"
    assert row["enabled"] is True
    assert row["interval_seconds"] == 15 * 60
    # params round-trips as a real dict (JSONB) under the key T28's manifest
    # declares (`url`), so orchestrate validates + fetches.
    assert row["params"] == {"url": GDELT_DEMO_GKG_URL}


def test_seed_demo_is_idempotent_no_duplicate_on_second_run():
    first = runner.invoke(app, ["seed-gdelt-demo"])
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    second = runner.invoke(app, ["seed-gdelt-demo"])
    assert second.exit_code == 0, second.output
    assert "skip" in second.output

    # exactly one row despite two invocations
    assert len(_demo_rows()) == 1
```

- [ ] **Step 2: Run → FAIL** (`make up` running): `uv run pytest tests/test_seed_demo.py -v`.
  Expect `ImportError: cannot import name 'GDELT_DEMO_NAME' from 'bellweather.cli'` (and the
  `seed-gdelt-demo` command not existing yet).

- [ ] **Step 3: Implement** the `seed-gdelt-demo` command in `src/bellweather/cli.py`. Add the four
  module-level constants and the command exactly as in the Interface section above, placing the
  command alongside `worker`/`ui`/`orchestrate`. `typer` is already imported at the top of
  `cli.py`; import `schedules` and `get_conn` lazily inside the function (matching how `worker`
  and `orchestrate` lazily import their deps) so importing `cli.py` never forces a DB import.
  **Carry the "VERIFY against current GDELT docs" comment** directly above `GDELT_DEMO_GKG_URL`,
  and **VERIFY** that the URL is a current, fetchable `*.gkg.csv` batch from the master file list
  (Step's ⚠ caveat) before committing.

- [ ] **Step 4: Run → PASS** (`make up` running): `uv run pytest tests/test_seed_demo.py -v`.
  Both cases green. Smoke the CLI wiring: `uv run bellweather seed-gdelt-demo --help` shows the command,
  and `uv run bellweather --help` lists `seed-gdelt-demo`.

- [ ] **Step 5: Document the go-live** in `producers/gdelt/README.md` — append a new section
  (after the existing "Run" / "Live feed" sections) titled **"## Go-live: run it under the
  orchestrator"** containing the exact runbook below. This is the operator-facing Phase-2 demo.
```markdown
## Go-live: run it under the orchestrator

The orchestrator (T24) can run this producer on a schedule instead of you invoking
it by hand. The `gdelt` template manifest (`producers/gdelt/template.toml`, T28)
declares the entrypoint and params; a **schedule** binds it to a concrete GKG URL
and a 15-minute interval. `BELLWEATHER_TEMPLATES_DIR` defaults to `producers`, so
the manifest is discovered automatically.

Seed the demo schedule and drive one full pass locally:

```bash
make up                          # Postgres 16 + fake-gcs-server
bellweather migrate              # applies 0001 + 0002 (creates producer_schedules)
bellweather seed-gdelt-demo            # inserts the 'gdelt-demo' schedule (idempotent — safe to re-run)

# In a second terminal, start the ingest API the producer POSTs to:
bellweather api                  # http://localhost:8000  (BELLWEATHER_API_URL)

# Front of the pipe: the orchestrator finds the due 'gdelt-demo' schedule,
# spawns the gdelt template in a subprocess (BELLWEATHER_API_URL only — no DB/bucket
# creds), which fetches the GKG batch and POSTs each row to /ingest (bronze + queue):
bellweather orchestrate --once

# Back of the pipe: the worker drains the queue, routes the unstructured records to
# the gdelt-gkg-v2 extractor, and writes tags (silver) + observations (gold):
bellweather worker --once
```

Then open the UI (`bellweather ui`):
- **Schedules** page lists `gdelt-demo` (template `gdelt`, 15m interval, enabled) and its run
  history; use **Run now** / **Force Run** to trigger another pass without waiting for the interval.
- **Dashboard** shows the new tags and the observations the extractor wrote.

`seed-gdelt-demo` is idempotent (it skips if a schedule named `gdelt-demo` already exists), so it is
safe to run on every deploy. In GCP the every-minute Cloud Scheduler ping drives `orchestrate`
and `worker` automatically; you only seed once.

> ⚠ The seeded GKG URL is a demo default that may go stale — GDELT URLs/layout drift. Verify a
> current `*.gkg.csv` against the master file list (above) and override it from the Schedules UI
> if a run fails to fetch.
```

- [ ] **Step 6: `make check`** → green (`ruff check . && ruff format --check . && pytest`). Keep
  `make up` running so `tests/test_seed_demo.py` executes against Postgres rather than erroring.

- [ ] **Step 7: Commit** (`feat: seed-gdelt-demo command + GDELT orchestrator go-live runbook`).

## Acceptance criteria
- `bellweather seed-gdelt-demo` inserts exactly **one** `producer_schedules` row named `gdelt-demo`,
  bound to `template="gdelt"`, with `enabled=true`, `interval_seconds=900` (15m), and
  `params={"url": <GKG file URL>}` (the key the T28 `gdelt` manifest declares),
  via `schedules.create_schedule` inside a `get_conn()` + `conn.commit()`.
- The command is **idempotent**: a second invocation prints a `skip` line and creates **no**
  duplicate row (asserted via `schedules.list_schedules`). The guard is a name-exists check, not
  a DB unique constraint, so no migration change is required.
- The seed value carries a visible "VERIFY against current GDELT docs" comment, and the URL is a
  current, fetchable `*.gkg.csv` batch from the master file list (verified before commit). The
  test asserts only the schedule row — **never** a live GDELT fetch.
- `producers/gdelt/README.md` documents the end-to-end go-live runbook:
  `migrate → seed-gdelt-demo → orchestrate --once → worker --once → UI Schedules + Dashboard`.
- GDELT stays on the **unstructured** path (reuses the existing `gdelt-gkg-v2` extractor); the
  seed emits **no** `numeric-series-v1` and adds **no** worker/normalizer code.
- `make check` is green (the seed test requires `make up` + the `0002` migration via `make migrate`).
