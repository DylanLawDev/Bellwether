# Producer Orchestrator & Structured Feed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Bellwether a producer **orchestrator** (schedule external collector scripts + call them with parameters) and the **structured (numeric) ingestion path** its first real feed needs — built as reusable infrastructure, testable with fixtures, before any real scraper.

**Architecture:** A thin `bellweather-orchestrator` Cloud Run Job (pinged every minute, like the existing worker-drain) reads due `producer_schedules`, claims them, and spawns each **template** (a TOML manifest + a script) as a subprocess with only `BELLWEATHER_API_URL` — the script scrapes and `POST`s to `/ingest`. Numeric feeds emit a canonical `numeric-series-v1` payload that a generic normalizer lands directly in gold via a new set-semantics `upsert_value`; the worker routes by `raw_records.kind`.

**Tech Stack:** Python 3.12 + `uv`, FastAPI, `psycopg` v3 (sync), `pydantic` v2, `tomllib` (stdlib — manifests), `typer`, Streamlit (UI), Terraform (Cloud Run Job + Scheduler). No new runtime dependency.

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md`.

---

## How to run a ticket (lifecycle)

Tickets live in `docs/plans/tickets/{Open, In Progress, Closed}/`. To work one: move it `Open → In Progress`, branch `ticket/T<NN>-<slug>`, follow TDD, get `make check` green, open one PR. **Merge gate:** a ticket's contents may merge to `main` only when it is in `In Progress/` (work underway) or `Closed/` (done) — never from `Open/`. Move it to `Closed/` when merged. (Mirrors `CLAUDE.md` Conventions.)

Each ticket is self-contained — spec ref, prerequisites, exact files, interfaces, tests, acceptance criteria.

---

## Module layout (locked — new + modified for this epic)

```
src/bellweather/
├── config.py            # MODIFY: + bellweather_templates_dir                  [T22]
├── gold.py              # MODIFY: + upsert_value() (set-semantics)             [T18]
├── normalizers/
│   ├── __init__.py      # CREATE: NormalizedPoint, Normalizer, registry        [T19]
│   └── numeric_series.py# CREATE: NumericSeriesNormalizer ("numeric-series-v1") [T19]
├── worker.py            # MODIFY: branch process_job on raw_records.kind       [T20]
├── ingest.py            # MODIFY: KNOWN_CONTENT_TYPES += "numeric-series-v1"   [T20]
├── migrations/
│   └── 0002_orchestrator.sql  # CREATE: producer_schedules, producer_runs      [T21]
├── schedules.py         # CREATE: schedule registry CRUD + due/claim/runs      [T21]
├── templates.py         # CREATE: manifest discovery + params + interval parse [T22]
├── client.py            # MODIFY: + DryRunClient                               [T23]
├── orchestrator.py      # CREATE: tick(), _run_subprocess(), run_orchestrator  [T24]
├── cli.py               # MODIFY: + run-template, + orchestrate                [T23/T24]
├── api.py               # MODIFY: + /api/schedules,/templates,/orchestrator,…  [T25]
└── web/
    ├── data/source.py   # MODIFY: + SCHEDULE/TEMPLATE/RUN column contracts     [T26]
    ├── data/mock.py     # MODIFY: + schedules/templates/runs (in-memory)       [T26]
    ├── data/live.py     # MODIFY: + schedules/templates/runs (httpx)           [T26]
    └── pages/5_Schedules.py  # CREATE: Schedules control-plane page            [T26]
infra/main.tf            # MODIFY: orchestrator Job + Scheduler + run.invoker   [T27]
Dockerfile               # MODIFY: bake templates dir; BELLWEATHER_TEMPLATES_DIR [T27]
```

---

## Locked interfaces (use these exact names/signatures across tickets)

**config.py** — add to `Settings`:
```python
bellweather_templates_dir: str = "producers"   # dir scanned for */template.toml
```

**gold.py** — add (idempotent set; D1 last-value-wins):
```python
def upsert_value(conn, symbol_key: str, symbol_kind: str, ts: datetime, value: float,
                 *, unit: str | None = None, description: str | None = None,
                 sample_count: int = 1) -> int:
    # 1. insert into tracked_symbols(key,kind,unit,description) on conflict (key)
    #    do update set kind=excluded.kind,
    #                  unit=coalesce(excluded.unit, tracked_symbols.unit),
    #                  description=coalesce(excluded.description, tracked_symbols.description)
    #    returning id
    # 2. bucket = bucket_ts(ts, get_settings().bellweather_obs_bucket)
    # 3. insert into observations(tracked_symbol_id,ts_bucket,value,sample_count) values(...)
    #    on conflict (tracked_symbol_id, ts_bucket)
    #    do update set value = excluded.value, sample_count = excluded.sample_count
    # returns tracked_symbol id. NEVER commits (caller owns txn).
```

**normalizers/__init__.py**:
```python
@dataclass
class NormalizedPoint:
    symbol_key: str
    symbol_kind: str
    ts: datetime
    value: float
    unit: str | None = None
    description: str | None = None

@runtime_checkable
class Normalizer(Protocol):
    content_type: str
    def normalize(self, envelope: dict) -> list["NormalizedPoint"]: ...

def register(n: Normalizer) -> None: ...
def get_normalizer(content_type: str) -> Normalizer | None: ...
def known_content_types() -> set[str]: ...
```

**normalizers/numeric_series.py** — `NumericSeriesNormalizer.content_type = "numeric-series-v1"`. Reads `envelope["payload"]` (the bronze envelope is `Submission.model_dump(mode="json")`, so the payload dict is under `"payload"`) with keys `symbol_key, symbol_kind, unit?, description?, points:[{ts, value}]`; yields one `NormalizedPoint` per point (`datetime.fromisoformat(ts)`, `float(value)`). Calls `register(NumericSeriesNormalizer())` at import.

**worker.py** — `process_job` selects `kind` too and branches:
```python
source, kind, content_type, payload_uri, fetched_at = row  # add kind to the SELECT
if kind == "structured":
    n = get_normalizer(content_type)
    if n is None: -> status='unroutable'; ack; return
    for pt in n.normalize(get_bronze_store().get(payload_uri)):
        upsert_value(conn, pt.symbol_key, pt.symbol_kind, pt.ts, pt.value,
                     unit=pt.unit, description=pt.description)
    -> status='processed'; ack; return
# else: existing unstructured/extractor path (unchanged)
```
Import `bellweather.normalizers.numeric_series  # noqa: F401` (registers) + `get_normalizer` + `upsert_value`.

**ingest.py** — `KNOWN_CONTENT_TYPES = {"gdelt-gkg-v2", "numeric-series-v1"}` (else structured records park as `unroutable` and never enqueue).

**migrations/0002_orchestrator.sql**:
```sql
create table if not exists producer_schedules (
  id bigserial primary key,
  name text not null,
  template text not null,
  params jsonb not null default '{}'::jsonb,
  interval_seconds int not null check (interval_seconds > 0),
  enabled boolean not null default true,
  force_run boolean not null default false,
  last_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create table if not exists producer_runs (
  id bigserial primary key,
  schedule_id bigint references producer_schedules(id),
  template text not null,
  params jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running' check (status in ('running','ok','error')),
  submitted int,
  error text
);
create index if not exists producer_runs_schedule_idx on producer_runs (schedule_id, started_at desc);
```

**schedules.py** — never commit (caller owns txn); dict_row shapes:
```python
def list_schedules(conn) -> list[dict]: ...
def get_schedule(conn, schedule_id: int) -> dict | None: ...
def create_schedule(conn, *, name, template, params: dict, interval_seconds: int, enabled: bool = True) -> int: ...
def update_schedule(conn, schedule_id: int, **fields) -> None: ...   # name|params|interval_seconds|enabled|force_run; bumps updated_at
def delete_schedule(conn, schedule_id: int) -> None: ...
def set_force_run(conn, schedule_id: int, value: bool = True) -> None: ...
def due_schedules(conn) -> list[dict]: ...   # enabled AND (force_run OR last_run_at IS NULL OR last_run_at + interval_seconds*'1s' <= now())
def claim(conn, schedule_id: int) -> None: ...   # set last_run_at=now(), force_run=false
def start_run(conn, *, schedule_id: int, template: str, params: dict) -> int: ...
def finish_run(conn, run_id: int, *, status: str, submitted: int | None = None, error: str | None = None) -> None: ...
def list_runs(conn, *, schedule_id: int | None = None, limit: int = 50) -> list[dict]: ...
```

**templates.py**:
```python
@dataclass
class TemplateParam:
    name: str; type: str = "str"; required: bool = False
    default: object | None = None; choices: list | None = None; help: str | None = None

@dataclass
class Template:
    name: str; entrypoint: str; description: str = ""
    params: list[TemplateParam] = field(default_factory=list)
    default_interval_seconds: int | None = None

def discover_templates(templates_dir: str | None = None) -> dict[str, Template]: ...  # scan */template.toml via tomllib; DO NOT import entrypoints
def get_template(name: str, templates_dir: str | None = None) -> Template | None: ...
def validate_params(template: Template, params: dict) -> dict: ...   # defaults + required + choices + coercion; ValueError on bad
def load_entrypoint(entrypoint: str): ...   # "module.path:function" -> callable (run-time only)
def parse_interval(s: str) -> int: ...       # "30m"|"6h"|"1d"|"45s" -> seconds
```
Manifest shape (TOML): `name`, `entrypoint = "module:func"`, `description`, `[params]` table of `{type, required, default, choices, help}`, `[schedule] default_interval = "30m"`.
Entrypoint contract: `def run(params: dict, client) -> dict | None` (returns optional `{"submitted": int, ...}` summary).

**client.py** — `DryRunClient` (same surface as `BellwetherClient`, no I/O):
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

**cli.py**:
```python
@app.command("run-template")
def run_template(template: str, params: str = "{}", dry_run: bool = False) -> None: ...
    # discover->validate(json.loads(params))->load_entrypoint->client=(DryRunClient if dry_run else BellwetherClient)
    # ->summary=entrypoint(p,client)->print(json.dumps({"submitted":..., "sample":[...] if dry_run}))
@app.command()
def orchestrate(once: bool = False) -> None:
    from bellweather.orchestrator import run_orchestrator; run_orchestrator(once=once)
```

**orchestrator.py**:
```python
def tick(conn) -> list[int]: ...   # for s in due_schedules: claim+commit; rid=start_run+commit;
                                   #   try: summary=_run_subprocess(...); finish_run('ok', submitted)
                                   #   except: finish_run('error', error); commit
def _run_subprocess(template: str, params: dict, *, timeout: int = 600) -> dict: ...
    # subprocess.run(["bellweather","run-template","--template",template,"--params",json.dumps(params)],
    #   env={"BELLWEATHER_API_URL": get_settings().bellweather_api_url, "PATH": os.environ["PATH"]},  # NO db/bucket
    #   capture_output=True, text=True, timeout=timeout) ; json.loads(last stdout line)
def run_orchestrator(once: bool = False) -> None: ...   # loop tick() with get_conn(); sleep when idle
```

**api.py** — add to `api_router` (prefix `/api`):
`GET /templates`, `POST /templates/{name}/preview` (spawns `run-template --dry-run` minimal-env subprocess; returns sample), `GET /schedules`, `POST /schedules`, `PATCH /schedules/{id}`, `DELETE /schedules/{id}`, `POST /schedules/{id}/force`, `POST /orchestrator/run` (trigger a tick now), `GET /runs`. Pydantic row models mirror `schedules.py`/`templates.py` dict shapes.

**web/data** — add column contracts + functions (mock + live identical shapes):
`SCHEDULE_COLUMNS = [id, name, template, interval_seconds, enabled, force_run, last_run_at]`;
`RUN_COLUMNS = [id, schedule_id, template, started_at, finished_at, status, submitted, error]`;
`get_schedules()`, `get_templates()`, `get_runs(schedule_id=None)`, `create_schedule(...)`, `update_schedule(id, **)`, `delete_schedule(id)`, `force_schedule(id)`, `run_orchestrator_now()`, `preview_template(name, params)`.

---

## Build order & dependency graph

```
Stack A — INFRA (this push; stacked in dependency order):
  T18 gold.upsert_value ─┐
  T19 normalizer reg ────┴─▶ T20 worker kind-routing
  T21 schedule registry ─┐
  T22 template discovery ┴─▶ T23 run-harness+DryRunClient+CLI ─▶ T24 orchestrator tick+CLI
  T20 + T24 ─────────────────▶ T25 control-plane API ─▶ T26 Schedules UI
  T24 ───────────────────────▶ T27 infra (Job + Scheduler + image bake)

Stack B — GDELT collector-as-template (separate set, authored after the push):
  T28 GDELT template (manifest + run(params,client)) ─▶ T29 GDELT demo schedule + go-live verify

Stack C — Polymarket collector (separate set, independent of B, authored after the push):
  T30 Polymarket fetch helpers (Gamma+CLOB; ⚠ verify endpoints) ─▶ T31 Polymarket template (numeric-series-v1)
  ─▶ T32 Polymarket demo schedule + end-to-end verify
```

Stacks B and C are **independent of each other** and both build on the infra (Stack A). They are two separate sets of stacked PRs.

## Phase-1 ticket index (Stack A — authored now)

| Ticket | Title | Depends on |
|---|---|---|
| [T18](tickets/Open/T18-gold-value-write.md) | Gold value write — `upsert_value` (set-semantics) | T11 |
| [T19](tickets/Open/T19-normalizer-registry.md) | Normalizer registry + generic `numeric-series-v1` | T04 |
| [T20](tickets/Open/T20-worker-structured-routing.md) | Worker routes by `kind` → normalizer → gold | T18, T19 |
| [T21](tickets/Open/T21-schedule-registry.md) | Schedule registry: migration 0002 + `schedules.py` | T02 |
| [T22](tickets/Open/T22-template-discovery.md) | Template manifest contract + discovery (`templates.py`) | T01 |
| [T23](tickets/Open/T23-run-harness.md) | Run-harness + `DryRunClient` + `bellweather run-template` | T08, T22 |
| [T24](tickets/Open/T24-orchestrator.md) | Orchestrator tick + `bellweather orchestrate` | T21, T23 |
| [T25](tickets/Open/T25-control-plane-api.md) | Control-plane API (schedules/templates/runs/preview/force/run) | T15, T24 |
| [T26](tickets/Open/T26-schedules-ui.md) | Schedules UI page + `web.data` backends | T16, T25 |
| [T27](tickets/Open/T27-orchestrator-infra.md) | Infra: orchestrator Job + Scheduler + image bake | T14, T24 |

## Phase-2 roadmap (Stacks B & C — authored after the infra push)

| Ticket | Title | Notes |
|---|---|---|
| T28 | GDELT collector-as-template | `template.toml` for `producers/gdelt` + adapt `run` to `run(params, client)` |
| T29 | GDELT demo schedule + go-live | seed a `producer_schedules` row; verify bronze→tags→gold→UI |
| T30 | Polymarket fetch helpers | Gamma (event→variants) + CLOB (prices-history); ⚠ verify endpoints, network isolated |
| T31 | Polymarket template | manifest + `run(params, client)` → `numeric-series-v1`; snapshot idempotency key |
| T32 | Polymarket demo schedule + verify | seed the `us-x-iran-…` example; schedule→orchestrate→normalizer→observations→UI |

**Phase-1 end-to-end done:** with a fixture template registered and a due schedule, `bellweather orchestrate --once` spawns the script (minimal env), its fixture `numeric-series-v1` submission flows API→queue→worker→an `observations` row; the UI lists/previews/force-runs the schedule; `make check` green.

---

## Self-review notes

- **Spec coverage:** orchestrator (T24), templates (T22/T23), schedules + force_run + run-now (T21/T25), structured path (T18/T19/T20), control-plane API + UI (T25/T26), infra (T27). Phase-2 (Polymarket/GDELT, demo) deferred to Stacks B/C per the spec's phasing and the user's sequencing.
- **Type consistency:** all shared symbols (`upsert_value`, `NormalizedPoint`, `Normalizer`, `numeric-series-v1`, `producer_schedules`/`producer_runs`, `schedules.*`, `Template`/`TemplateParam`, `DryRunClient`, `run-template`/`orchestrate`, `tick`/`_run_subprocess`) are defined once here and referenced by exact name in the tickets.
- **No placeholders:** load-bearing SQL, signatures, the manifest shape, the entrypoint contract, and the minimal-env subprocess are given in full; tickets supply the TDD test code + steps.
- **Isolation invariant:** templates run with `BELLWEATHER_API_URL` only (never DB/bucket creds), in a subprocess (orchestrator) — including preview (API spawns a minimal-env dry-run subprocess, never in-process).
