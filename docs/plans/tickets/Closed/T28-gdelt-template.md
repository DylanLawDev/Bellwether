# T28 — GDELT collector-as-template (Stack B)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§4 The template contract; §11 Phase 2 — "GDELT as a template"; K1/K2/K4).
**Depends on:** T12 (the reference GDELT producer), T22 (`templates.py` discovery), T23 (run-harness + `DryRunClient` + `bellweather run-template`).
**Branch:** `ticket/T28-gdelt-template`. **PR, do not merge without approval.**

## Goal
Make the existing **external** GDELT producer (`producers/gdelt/`) a first-class **orchestrator template**: add a `producers/gdelt/template.toml` manifest so `discover_templates("producers")` finds it, and give the producer a canonical `run(params: dict, client) -> dict` entrypoint that the run-harness (`bellweather run-template`) can drive. This proves the Phase-1 orchestrator generalizes the v0 producer pattern (the spec's "GDELT as a template" Phase-2 item, §11) **without writing any new fetch/parse logic** — it wraps `rows_to_submissions(_fetch_lines(...))`, which T12 already verified against the GKG 2.1 codebook.

GDELT stays on the **unstructured** path: submissions keep `kind="unstructured"`, `content_type="gdelt-gkg-v2"`, and flow to the existing `gdelt-gkg-v2` extractor (themes/persons/orgs/locations/tone → tags). This ticket does **not** touch the extractor and does **not** emit `numeric-series-v1` — that canonical structured payload is for the Polymarket path (Stack C), not GDELT.

## ⚠ Verify before building
The fetch/parse layer being wrapped here was verified in T12; this ticket adds **no new network or parsing code**, so there is nothing new to re-verify against external docs. The GKG 2.1 column indices and the live-feed master-file mechanics already carry their "VERIFY against current GDELT docs" caveat inline in `producers/gdelt/producer.py` (the `COL_*` constants block) and in `producers/gdelt/README.md` ("Column verification caveat"). **Do not duplicate or weaken those caveats** — leave them intact. The only network entry point remains `_fetch_lines(path_or_url)` (T12), which the tests in this ticket bypass entirely by passing a local fixture file path; no live GDELT call is ever made in the test suite.

## Files
- **Create:** `producers/gdelt/template.toml` — the manifest (`name="gdelt"`, `entrypoint="producers.gdelt.producer:run"`, one `url` param, `[schedule] default_interval = "15m"`).
- **Modify:** `producers/gdelt/producer.py` — rename the existing CLI helper `run(path_or_url, client=None)` to `run_path(path_or_url, client=None)` (only the `if __name__ == "__main__"` block calls it today — see "Existing callers" below), and add a **new** template entrypoint `run(params: dict, client) -> dict` matching the locked entrypoint contract `def run(params, client) -> dict | None`.
- **Modify (docs):** `producers/gdelt/README.md` — add a short "As an orchestrator template" section documenting the manifest, the `url` param, the 15m default interval, and the `bellweather run-template` invocation.
- **Test:** `tests/test_gdelt_template.py` — pure, no DB/GCS/network; reuses the existing `tests/fixtures/gkg_sample.csv` (3 GKG rows).

## Interface

`producers/gdelt/template.toml` (manifest shape per spec §4 / T22's `_parse_manifest`):
```toml
name        = "gdelt"
entrypoint  = "producers.gdelt.producer:run"
description = "GDELT GKG 2.1 collector (unstructured): fetch a GKG batch, parse to gdelt-gkg-v2 payloads, submit."

[params]
url = { type = "str", required = true, help = "GKG file URL or local path (a master-file entry, e.g. http://data.gdeltproject.org/gdeltv2/<ts>.gkg.csv)" }

[schedule]
default_interval = "15m"   # GDELT publishes a new GKG batch every 15 minutes
```

`producers/gdelt/producer.py` — after this ticket:
```python
def run(params: dict, client) -> dict:               # NEW: the orchestrator template entrypoint
    """Template entrypoint: fetch+parse the GKG batch at params['url'], submit via client."""
    subs = rows_to_submissions(_fetch_lines(params["url"]))
    results = client.ingest_batch(subs)
    return {"submitted": len(results)}

def run_path(path_or_url: str, client: BellwetherClient | None = None) -> list[IngestResult]:
    ...                                               # RENAMED from the old run(); CLI/manual-use helper
```

Locked contract this consumes (do not redefine — provided by T22/T23):
- `discover_templates(templates_dir) -> dict[str, Template]` (T22) — scans `<dir>/*/template.toml`; `Template.default_interval_seconds == 900` for `"15m"`, `Template.params` is a list of `TemplateParam` with `.name`/`.required`/`.type`.
- `bellweather run-template --template <name> --params <json> --dry-run` (T23) — discovers the manifest, validates params, imports the entrypoint, calls `entrypoint(params, DryRunClient())`, and prints one JSON summary line `{"template", "submitted", "dry_run": true, "sample": [Submission.model_dump(mode="json"), ...]}`. The harness reads `BELLWEATHER_TEMPLATES_DIR` to locate manifests. Each `sample` item is a Submission dict, so `sample[0]["content_type"] == "gdelt-gkg-v2"`.
- `DryRunClient.ingest_batch(subs)` (T23) returns one `IngestResult(status="created")` per submission and captures every `Submission` — so `run`'s returned `{"submitted": len(results)}` equals the number of GKG rows, and the harness's `sample`/`submitted` reflect the same captured submissions.

**Existing callers of the old `run`:** the only in-repo caller is the `if __name__ == "__main__"` block of `producer.py` itself. `tests/test_gdelt_producer.py` imports only `_default_client, parse_gkg_line, rows_to_submissions` (not `run`), so the rename does not touch that test. (Confirm before editing: `grep -rn "producer import run\|producer\.run\b" tests/ producers/`.)

## Steps

> No DB, no GCS, no network in this ticket. The fixture is a local file, `DryRunClient` performs zero I/O, and discovery only parses TOML. **`make up`/`make migrate` are NOT required.** (`uv run pytest` alone runs the whole suite for this ticket.)

- [ ] **Step 1: Read first.** Read `producers/gdelt/producer.py` and `producers/gdelt/README.md` end-to-end. Confirm: the old `run(path_or_url, client=None)` is only invoked from the file's own `__main__` block, and `rows_to_submissions`/`_fetch_lines`/`_default_client` already exist and are unchanged by this ticket. Confirm `tests/fixtures/gkg_sample.csv` has 3 tab-separated GKG rows (`wc -l` reports 3, no trailing blank line).

- [ ] **Step 2: Create the manifest** `producers/gdelt/template.toml` (exact bytes from the Interface section above). Note `entrypoint = "producers.gdelt.producer:run"` — the manifest must point at the **new** `run(params, client)` entrypoint added in Step 5, not at `run_path`.

- [ ] **Step 3: Failing test** `tests/test_gdelt_template.py` — pure (no DB/GCS/network); discovery + a dry-run drive of the harness over the existing GKG fixture. The fixture path and templates dir are resolved relative to the test file so it works from any CWD.
```python
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
            "--template", "gdelt",
            "--dry-run",
            "--params", json.dumps({"url": str(GKG_FIXTURE)}),
        ],
    )
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["dry_run"] is True
    assert summary["submitted"] == GKG_ROWS  # one submission per GKG row (3)
    assert summary["sample"][0]["content_type"] == "gdelt-gkg-v2"
    assert summary["sample"][0]["kind"] == "unstructured"
    assert summary["sample"][0]["source"] == "gdelt.gkg"
```

- [ ] **Step 4: Run → FAIL** (`uv run pytest tests/test_gdelt_template.py -v`). Two expected failures: discovery finds no `"gdelt"` (manifest exists but `producer.run` does not yet match the `run(params, client)` entrypoint contract the harness imports — the harness will `TypeError` calling the old two-positional `run(path_or_url, client=None)` with `(validated_params, DryRunClient())`), and the dry-run drive errors out. (Discovery itself will pass once Step 2's manifest exists, but the dry-run test fails until Step 5.)

- [ ] **Step 5: Modify `producers/gdelt/producer.py`.** Rename the existing helper and add the new template entrypoint. Both wrap the **unchanged** `rows_to_submissions`/`_fetch_lines`.

  5a. Rename the existing `run` to `run_path` (signature and body otherwise unchanged):
  ```python
  def run_path(path_or_url: str, client: BellwetherClient | None = None) -> list[IngestResult]:
      """Fetch a GKG batch, normalize it, and ingest via the Bellwether client.

      Manual/CLI helper (used by ``__main__`` below). The orchestrator template
      entrypoint is ``run(params, client)``.
      """
      client = client or _default_client()
      subs = rows_to_submissions(_fetch_lines(path_or_url))
      return client.ingest_batch(subs)
  ```

  5b. Add the new canonical template entrypoint (place it directly above `run_path`):
  ```python
  def run(params: dict, client) -> dict:
      """Orchestrator template entrypoint (manifest: producers/gdelt/template.toml).

      Wraps the existing fetch+parse logic in the locked entrypoint contract
      ``def run(params: dict, client) -> dict | None``. ``params["url"]`` is a GKG
      file URL or local path (a master-file entry); ``client`` is injected by the
      run-harness (a real ``BellwetherClient`` on a scheduled run, a ``DryRunClient``
      for a preview). GDELT stays UNSTRUCTURED (``content_type="gdelt-gkg-v2"``),
      handled by the existing extractor — no numeric-series-v1 here.
      """
      subs = rows_to_submissions(_fetch_lines(params["url"]))
      results = client.ingest_batch(subs)
      return {"submitted": len(results)}
  ```

  5c. Update the `if __name__ == "__main__"` block to call `run_path` instead of `run` (the new `run(params, client)` takes a params dict, not a path string):
  ```python
  if __name__ == "__main__":
      if len(sys.argv) != 2:
          print("usage: python -m producers.gdelt.producer <path-or-url>", file=sys.stderr)
          raise SystemExit(2)
      results = run_path(sys.argv[1])
      by_status: dict[str, int] = {}
      for r in results:
          by_status[r.status] = by_status.get(r.status, 0) + 1
      summary = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
      print(f"ingested {len(results)} record(s): {summary or 'none'}")
  ```

- [ ] **Step 6: Run → PASS** (`uv run pytest tests/test_gdelt_template.py -v`). Discovery finds `gdelt` with the `url` param and `default_interval_seconds == 900`; the dry-run summary reports `submitted == 3` (one per fixture row) with `sample[0]["content_type"] == "gdelt-gkg-v2"`. Also confirm the unchanged T12 suite still passes: `uv run pytest tests/test_gdelt_producer.py -v`.

- [ ] **Step 7: Update `producers/gdelt/README.md`** — add a section after the "Run" section. Keep the existing "Column verification caveat" intact:
  ```markdown
  ## As an orchestrator template

  `producers/gdelt/template.toml` registers this producer with the Bellwether
  orchestrator (`docs/specs/2026-06-01-producer-orchestrator-design.md` §4). The
  manifest declares one parameter, `url` (a GKG file URL or local path — a
  master-file entry), and a default schedule interval of `15m` (GDELT publishes a
  new GKG batch every 15 minutes).

  The template entrypoint is `run(params, client)` (the locked
  `def run(params: dict, client) -> dict | None` contract); the older
  `run_path(path_or_url, client=None)` helper is what the `python -m` CLI above
  uses. GDELT is an **unstructured** feed — submissions keep
  `content_type="gdelt-gkg-v2"` and flow to the existing extractor (themes /
  persons / orgs / locations / tone → tags); it does **not** emit
  `numeric-series-v1`.

  Dry-run it through the run-harness without any datastore (points the harness at
  this repo's `producers/` dir and uses a local GKG file, so no network call):

  ```bash
  BELLWEATHER_TEMPLATES_DIR=producers \
    uv run bellweather run-template --template gdelt --dry-run \
    --params '{"url": "tests/fixtures/gkg_sample.csv"}'
  ```

  The summary line reports `submitted` (one per GKG row) and a `sample` of the
  would-be submissions; nothing is committed and no HTTP is made (`DryRunClient`).
  ```

- [ ] **Step 8: `make check`** — `ruff check . && ruff format --check . && pytest` green. (No DB/GCS tests added here, so the suite passes without `make up`.) Fix any ruff lint/format findings (e.g. import ordering, line length) before committing.

- [ ] **Step 9: Commit** (`feat: register GDELT as an orchestrator template`).

## Acceptance criteria
- `producers/gdelt/template.toml` exists with `name = "gdelt"`, `entrypoint = "producers.gdelt.producer:run"`, a required `url` param of type `str`, and `[schedule] default_interval = "15m"`.
- `discover_templates("producers")` includes `"gdelt"`; the discovered `Template` has `entrypoint == "producers.gdelt.producer:run"`, `default_interval_seconds == 900`, and a `url` param with `required is True` / `type == "str"`. Discovery parses TOML only — it does not import the entrypoint (it is the T22 contract being consumed, not re-tested here).
- `producers/gdelt/producer.py` exposes a `run(params: dict, client) -> dict` template entrypoint that does `rows_to_submissions(_fetch_lines(params["url"]))` → `client.ingest_batch(...)` → `return {"submitted": len(results)}`, and a renamed `run_path(path_or_url, client=None)` helper for the CLI / manual use. The `__main__` block calls `run_path`. No new fetch/parse logic, and the `gdelt-gkg-v2` extractor is untouched.
- GDELT remains **unstructured**: emitted submissions keep `kind="unstructured"`, `content_type="gdelt-gkg-v2"`, `source="gdelt.gkg"`. No `numeric-series-v1` payload is produced.
- `bellweather run-template --template gdelt --dry-run --params '{"url": <tests/fixtures/gkg_sample.csv>}'` (with `BELLWEATHER_TEMPLATES_DIR=producers`) prints a JSON summary with `dry_run` true, `submitted == 3` (the number of GKG fixture rows), and `sample[0]["content_type"] == "gdelt-gkg-v2"`. The test drives this via `typer.testing.CliRunner`; no network (local fixture file), no DB, no GCS.
- The existing `tests/test_gdelt_producer.py` (T12) still passes unchanged. The inline "VERIFY against current GDELT docs" caveats in `producer.py` and `README.md` are preserved.
- `make check` is green.
