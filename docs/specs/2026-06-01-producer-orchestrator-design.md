# Bellwether — Producer Orchestrator & Structured Feed Path Design

| | |
|---|---|
| **Status** | Draft — approved in brainstorm, pending spec review |
| **Date** | 2026-06-01 |
| **Owner** | Dylan |
| **Scope** | Two tracks on one shared backbone: (1) a **producer orchestrator** that schedules customer-authored collector scripts ("templates") and calls them with parameters, and (2) the **structured (numeric) ingestion path** the orchestrator's first real feed (Polymarket) needs. **This spec covers the full epic but is explicitly phased** — see §11. Only **Phase 1 (infrastructure)** is ticketed now; the Polymarket template and the GDELT/demo go-live are **Phase 2**. |
| **Related** | `docs/specs/2026-05-31-ingestor-extractor-design.md` (the v0 spine this builds on), `README.md` §3–§5 (Collector, two ingestion paths, v1 structured feed), `docs/plans/2026-05-31-ingestor-extractor.md` (locked module layout) |

---

## 1. Goal

Give Bellwether a way to **schedule and run external collector scripts** on a cadence, with parameters, and to **land the numeric feeds they emit** in the gold time series.

Two things are built together because they share infra and only make sense as a pair:

1. **Producer orchestrator** — Bellwether becomes the conductor for collector scripts that live in a Git repo loaded onto the instance. A **template** is such a script plus a manifest declaring how it may be *called with parameters*; a **schedule** (usage) binds a template to concrete parameters and an interval. The orchestrator fires due schedules; the script does the scraping and `POST`s to `/ingest` itself.
2. **Structured ingestion path** — the worker today only handles *unstructured → extractor*. Numeric feeds (the orchestrator's first real target, Polymarket) need the **structured → normalize → gold** path, which is a stub in the v0 spine. This spec makes it real.

The deliverable proves the orchestration + structured backbone end to end **with fixtures** (Phase 1), so the Polymarket template and the GDELT/demo go-live (Phase 2) are pure additions on a tested substrate — the same way the GDELT extractor (T10) and worker (T11) were built and tested before the real GDELT producer (T12) existed.

---

## 2. Key decisions (from brainstorm)

| # | Decision | Rationale |
|---|---|---|
| K1 | **Bellwether is the orchestrator, not the scraper.** Collection logic stays in customer-authored scripts; Bellwether *schedules* them and *calls them with parameters*. | Keeps scraping concerns (auth, navigation, rate limits) out of the core. Evolves — does not break — the v0 "collection is external" stance (see §10 delta D-a). |
| K2 | **A template = a manifest + a script.** The manifest (TOML) declares `name`, `entrypoint` (`module:function`), a **params schema**, and a default interval. The script holds the fetch/parse logic and hands data to `/ingest`. | The user's "declarative manifest utilizing python scripts": declarative *what/when*, Python *how*. Manifests are enumerable **without executing code**, and the UI can build a parameter form from the schema. |
| K3 | **A schedule (usage) lives in the app DB; templates/scripts live in the Git repo.** `producer_schedules` rows bind `(template, params, interval)`; the repo is the source of truth for logic. | Resolves the control-plane question cleanly: logic is reviewed/versioned in Git; usages are app state, UI-manageable, and seedable. |
| K4 | **Templates run with the ingest URL only — never the spine's DB/bucket creds.** Each invocation gets a minimal env (`BELLWEATHER_API_URL`), exactly as `producers/gdelt`'s `_default_client()` already does. | Even though we run customer code, it can only `POST /ingest`; it cannot touch Postgres or GCS directly. Defuses the RCE concern within the trust model (your scripts, your instance). |
| K5 | **Execution = a subprocess per due schedule, dispatched by a thin orchestrator tick.** One `bellweather-orchestrator` Cloud Run Job is pinged every minute by Cloud Scheduler (mirrors the existing `bellweather-worker-drain`). | Process isolation: a hung/crashing script cannot stall the orchestrator. Reuses the proven worker-drain infra pattern; no per-schedule Terraform. Per-execution *isolated Cloud Run Jobs* + sandboxing are a later hardening (§12). |
| K6 | **Structured feeds emit a canonical payload (`numeric-series-v1`) handled by one generic normalizer.** Source-specific normalizers remain possible behind the registry, but the canonical shape means most structured producers (incl. Polymarket) need **zero worker-side code**. | Pushes per-source shaping into the script (where the orchestrator philosophy already puts the work) and keeps the worker generic. Makes Phase 1 testable with a fixture payload and Phase 2 Polymarket trivial on the worker side. |
| K7 | **Gold writes for structured series are set-semantics (`upsert_value`), not increment.** | Idempotent by construction — re-processing a record (expired lease, replay) re-sets the same values; safe. (Contrast the existing `upsert_coverage`, which increments — see §10 delta D-c.) |
| K8 | **Backfill is stateless.** Each run fetches the full available window; idempotent dedup fills past gaps *and* adds new points. `backfill` is a *parameter the script interprets*, not orchestrator logic. | No watermark/state to maintain. Polymarket's history endpoint returns the whole series cheaply; "new data" and "missing past data" both fall out of re-fetching + dedup. |
| K9 | **Dry-run preview runs trusted template code with a capturing client, committing nothing.** | Gives the "does this link work?" feedback loop in the UI without executing *pasted* code and without any side effects (no HTTP, no bronze, no DB). |
| K10 | **First customer = you; one scripts-repo.** Multi-tenant onboarding, dynamic multi-repo loading, and per-script dependency isolation are explicitly out (§12). | YAGNI. Build the mechanism, not a platform business, in this epic. |

---

## 3. Architecture

Everything inside the box is Bellwether. Collector **scripts** are external (in the loaded Git repo); the orchestrator *invokes* them.

```
                         ┌──────────────────────────────────────────────┐
   Scheduler (1m) ─POST─▶ │  Orchestrator  (bellweather orchestrate --tick)│
                         │   1. read due schedules (producer_schedules)   │
                         │   2. claim (set last_run_at = now())           │
                         │   3. subprocess: bellweather run-template       │
                         │      env = {BELLWEATHER_API_URL}  (K4)          │
                         │   4. record producer_runs                      │
                         └───────────────────────┬────────────────────────┘
                                                 │ spawns
                                  ┌──────────────▼───────────────┐
                                  │  Template script (Git repo)   │  ← external logic
                                  │  entrypoint(params, client)   │
                                  │  scrape → build submissions   │
                                  └──────────────┬───────────────┘
                                                 │ POST /ingest  (numeric-series-v1)
                         ┌───────────────────────▼────────────────────────┐
   Scheduler (1m) ─POST─▶ │  Ingestion API → bronze + raw_records + queue  │  (exists)
                         └───────────────────────┬────────────────────────┘
                                                 │
   Scheduler (1m) ─POST─▶ │  Worker (bellweather worker --once)            │  (exists, extended)
                         │   route by raw_records.kind:                   │
                         │    ├─ unstructured → Extractor → tags + gold    │  (exists)
                         │    └─ structured   → Normalizer → gold (value)  │  (NEW, §6)
                         └───────────────────────┬────────────────────────┘
                                                 ▼
                                gold: tracked_symbols + observations
```

Three small, single-purpose scheduled processes result — the orchestrator drives **producers** (front of the pipe); the worker drains the **queue** (back of the pipe); both are tiny Cloud Run Jobs pinged every minute. They are decoupled so a hung scraper cannot stall queue-draining, and vice versa.

### 3.1 Units (each independently testable)

| Unit | Responsibility | New? | Owning module |
|---|---|---|---|
| `template-registry` | Discover template manifests from the templates dir; parse `entrypoint` + params schema; validate without executing. | NEW | `templates.py` |
| `run-harness` | Load a manifest, import the entrypoint, validate params, build a client, call it, emit a JSON summary. Runs in the subprocess. | NEW | `cli.py` (`run-template`) |
| `dry-run client` | Same surface as `BellwetherClient`; captures submissions, performs no I/O. | NEW | `client.py` (`DryRunClient`) |
| `schedule-registry` | CRUD + "which schedules are due" query + claim. Never commits (caller owns the txn). | NEW | `schedules.py` |
| `orchestrator` | Tick: find due → claim → subprocess → record run. | NEW | `orchestrator.py` |
| `normalizer-registry` | Map `content_type` → normalizer (structured). Mirrors the extractor registry. | NEW | `normalizers/__init__.py` |
| `numeric-series normalizer` | Parse the canonical `numeric-series-v1` payload → `NormalizedPoint`s. | NEW | `normalizers/numeric_series.py` |
| `gold value write` | `upsert_value()` — ensure a tracked symbol, set an observation (idempotent). | NEW | `gold.py` |
| `worker routing` | Branch on `raw_records.kind`: structured → normalizer, else extractor. | MODIFY | `worker.py` |
| `control-plane API` | `/api/schedules` (CRUD, run-now), `/api/templates` (+ preview), `/api/runs`. | NEW | `api.py` |
| `UI control plane` | Schedules page: list/add/edit usages, run-now, dry-run preview, run history. | NEW | `web/pages/5_Schedules.py`, `web/data/*` |
| `orchestrator infra` | `bellweather-orchestrator` Cloud Run Job + Scheduler; scripts repo baked into the image. | NEW | `infra/main.tf`, `Dockerfile` |

**Isolation contract (extends the v0 one):** an external script breaking, hanging, or being malicious must never corrupt the spine. It runs in a subprocess with no DB/bucket credentials (K4); its only capability is `POST /ingest`. The orchestrator records the failure and moves on.

---

## 4. The template contract

A **template** is a directory in the loaded scripts repo containing a manifest and the script it points to.

```toml
# producers/polymarket/template.toml   (Phase 2 example; Phase 1 uses fixtures)
name        = "polymarket"
entrypoint  = "producers.polymarket.producer:run"   # "module.path:function"
description = "Polymarket event price-history collector"

[params]
url      = { type = "str", required = true, help = "Polymarket event URL" }
backfill = { type = "str", default = "all", choices = ["all", "recent"] }

[schedule]
default_interval = "30m"   # human duration; the UI/seed converts to interval_seconds
```

**Entrypoint contract.** The function named by `entrypoint` has the signature:

```python
def run(params: dict, client: BellwetherClient) -> dict | None: ...
```

- `params` is the schedule's params, validated against the manifest `[params]` schema.
- `client` is injected by the run-harness — a real `BellwetherClient` for a scheduled run, a `DryRunClient` for a preview. **The script never constructs its own DB/bucket access**; it only uses `client.ingest()/ingest_batch()`.
- The optional return is a summary (e.g. `{"submitted": 412, "symbols": 7}`) recorded on the run. The existing `producers/gdelt`'s `run()` is already one line away from this shape.

**Manifest format = TOML** via stdlib `tomllib` — **no new dependency**. (YAML is a possible future alternative if a dep is accepted; not now.)

**Discovery** (`templates.py`): scan `BELLWEATHER_TEMPLATES_DIR` (default: the repo's `producers/`) for `template.toml` files, parse them into a `Template` model (name, entrypoint, params schema, default interval). Discovery **must not import the entrypoint** (no code execution to list templates) — import happens only at run/preview time, inside the subprocess.

---

## 5. The schedule registry (control plane)

`migrations/0002_orchestrator.sql` (next sequential migration; the runner auto-discovers `*.sql` in order):

```sql
create table if not exists producer_schedules (
  id              bigserial primary key,
  name            text not null,                 -- human label for this usage
  template        text not null,                 -- template name from the manifest
  params          jsonb not null default '{}'::jsonb,
  interval_seconds int not null check (interval_seconds > 0),
  enabled         boolean not null default true,
  force_run       boolean not null default false, -- one-shot: run on the next tick regardless of
                                                  -- interval; the tick consumes it (resets to false)
  last_run_at     timestamptz,                   -- set at dispatch (claim) time
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create table if not exists producer_runs (
  id           bigserial primary key,
  schedule_id  bigint references producer_schedules(id),
  template     text not null,
  params       jsonb not null default '{}'::jsonb,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  status       text not null default 'running'
               check (status in ('running','ok','error')),
  submitted    int,                              -- from the script's summary
  error        text
);
create index if not exists producer_runs_schedule_idx
  on producer_runs (schedule_id, started_at desc);
```

`schedules.py` (helpers never commit — caller owns the txn, per the `queue.py` convention):

```python
def list_schedules(conn) -> list[dict]: ...
def create_schedule(conn, name, template, params, interval_seconds, enabled=True) -> int: ...
def update_schedule(conn, id, **fields) -> None: ...
def delete_schedule(conn, id) -> None: ...
def due_schedules(conn) -> list[dict]:    # enabled AND (force_run OR last_run_at IS NULL
                                          # OR last_run_at + interval <= now())
def claim(conn, id) -> None:              # set last_run_at = now(), force_run = false —
                                          # consumes the one-shot force AND prevents double-fire
def set_force_run(conn, id, value=True) -> None: ...   # the UI "Force Run" toggle
def start_run(conn, schedule_id, template, params) -> int: ...
def finish_run(conn, run_id, status, submitted=None, error=None) -> None: ...
```

**Interval, not cron** (K8 / "sync at x interval"): schedules store `interval_seconds`; due = "interval elapsed since `last_run_at`." The API/UI accept human durations ("30m", "6h") and convert. Full cron is a later option, not needed now. **Claim-on-dispatch** (`last_run_at = now()` before the subprocess starts) means a long-running script won't be re-fired on the next minute tick until its interval elapses again. A schedule's **`force_run`** flag (set from the UI's *Force Run* toggle) makes the next tick run it regardless of interval; the claim **consumes** the flag (resets it to `false`), so after the run the toggle reads off again on refresh.

---

## 6. The structured ingestion path

### 6.1 Canonical payload — `numeric-series-v1`

A structured submission's `payload` (or `payload_uri` target) is:

```jsonc
{
  "symbol_key":  "polymarket:us-x-iran-permanent-peace-deal-by:<variant>",
  "symbol_kind": "market-probability",   // tracked_symbols.kind
  "unit":        "probability",
  "description": "Will X happen by D? (YES)",
  "points": [ { "ts": "2026-05-31T14:00:00Z", "value": 0.37 }, ... ]
}
```

Submitted with `kind="structured"`, `content_type="numeric-series-v1"`.

**Structured idempotency** (producer-side guidance, enforced in Phase 2 scripts): submit **one record per (symbol, fetch)** carrying all points, with `idempotency_key = "<symbol_key>:<sha1(points)>"`. Re-fetching identical data dedups (no-op, no re-store); any new/gap-filled point changes the hash → a new immutable bronze snapshot → re-normalized. Gold stays correct because `upsert_value` is set-semantics (K7). Bronze keeps every snapshot — that is the point (replayability, README §6.4).

### 6.2 Normalizer registry — mirrors extractors

```python
# normalizers/__init__.py
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

def register(n): ...
def get_normalizer(content_type) -> Normalizer | None: ...
```

`normalizers/numeric_series.py` ships the generic `numeric-series-v1` normalizer: validate the payload shape, yield one `NormalizedPoint` per point.

### 6.3 Worker routing (the one modification to existing code)

`worker.process_job` currently always calls `get_extractor(content_type)`. It will branch on `raw_records.kind` (now also selected from the row):

- **`structured`** → `get_normalizer(content_type)`; `None` → `status='unroutable'`, ack (no data lost — same rule as unknown extractor). Else for each `NormalizedPoint` → `gold.upsert_value(...)`. Set `status='processed'`, ack.
- **`unstructured`** → existing extractor path, unchanged.

### 6.4 Gold value write

```python
# gold.py  (alongside upsert_coverage)
def upsert_value(conn, symbol_key, symbol_kind, ts, value, *,
                 unit=None, description=None, sample_count=1) -> None:
    # 1. ensure tracked_symbols row (key unique) -> id, fill unit/description/kind
    # 2. bucket ts by get_settings().bellweather_obs_bucket
    # 3. INSERT ... ON CONFLICT (tracked_symbol_id, ts_bucket)
    #    DO UPDATE SET value = excluded.value, sample_count = excluded.sample_count
    #    (SET, not increment -> idempotent)
```

No gold-schema migration is needed — `tracked_symbols`/`observations` already carry `kind`, `unit`, `description`, `value`, `sample_count`. Multiple points in one bucket are last-value-wins (see OQ-1).

---

## 7. The orchestrator

`orchestrator.py`:

```python
def tick(conn) -> list[int]:           # returns started run ids
    runs = []
    for s in schedules.due_schedules(conn):
        schedules.claim(conn, s["id"]); conn.commit()
        run_id = schedules.start_run(conn, s["id"], s["template"], s["params"]); conn.commit()
        try:
            summary = _run_subprocess(s["template"], s["params"])   # K4/K5
            schedules.finish_run(conn, run_id, "ok", submitted=summary.get("submitted"))
        except Exception as e:
            schedules.finish_run(conn, run_id, "error", error=str(e))
        conn.commit(); runs.append(run_id)
    return runs

def _run_subprocess(template, params) -> dict:
    # subprocess.run(["bellweather", "run-template", "--template", template,
    #                 "--params", json.dumps(params)],
    #                env={"BELLWEATHER_API_URL": ...} + minimal PATH,  # NO db/bucket
    #                capture_output=True, timeout=...) ; parse JSON summary from stdout
```

`cli.py` gains:
- `bellweather orchestrate [--once]` — one tick (Cloud Run Job) or loop (local).
- `bellweather run-template --template NAME --params JSON [--dry-run]` — the harness (§4): discover manifest, import entrypoint, validate params, build `BellwetherClient` (or `DryRunClient` if `--dry-run`), call it, print JSON summary.

**Repo loading (Phase 1):** the scripts repo is baked into the orchestrator image at build/deploy from a configured repo URL + ref; `BELLWEATHER_TEMPLATES_DIR` points at it (default: this repo's `producers/`, so the demo runs without an external repo). Runtime git-clone and per-script dependency isolation are out (§12).

---

## 8. Control-plane API + UI

New endpoints under the existing `/api` router (`api.py`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/templates` | List discovered templates + their params schemas (for the UI form). |
| POST | `/api/templates/{name}/preview` | Dry-run (K9): the API spawns `bellweather run-template --dry-run` as a **minimal-env subprocess** (same K4 isolation as the orchestrator — *not* in-process, which would hand the script the API's DB/bucket creds), with a `DryRunClient`; returns discovered symbols + a sample of would-be submissions. **Commits nothing, makes no HTTP.** |
| GET/POST | `/api/schedules` | List / create a usage. |
| PATCH/DELETE | `/api/schedules/{id}` | Edit / delete (incl. toggling `enabled` and `force_run`). |
| POST | `/api/schedules/{id}/force` | Set the schedule's `force_run` flag — it runs on the next tick regardless of interval; the tick consumes it. (The UI's *Force Run* toggle.) |
| POST | `/api/orchestrator/run` | Trigger an orchestrator tick **now** instead of waiting for the every-minute scheduler. In GCP, invokes the orchestrator Cloud Run Job; locally, runs one `--once` tick. (The UI's *Run now* button.) |
| GET | `/api/runs` | Recent `producer_runs` (per schedule). |

UI: a new **Schedules** page (`web/pages/5_Schedules.py`) — list usages, an "Add usage" form generated from a template's params schema (paste link, set interval), a **Preview** button (dry-run), a per-row **Force Run** toggle (one-shot — sets `force_run`; reads back **off** after the orchestrator consumes it on the next run), a **Run now** button that triggers an immediate orchestrator tick, and recent-run history. Follows the seam: pages read from `web.data`, which gains `get_schedules()/get_templates()/get_runs()` (+ column contracts in `web/data/source.py`) implemented in both `mock.py` and `live.py`. **This is the first UI write-path** (POST/PATCH), so `live.py` gains write helpers; `mock.py` keeps an in-memory list for offline use.

---

## 9. Deployment

`infra/main.tf` gains (mirroring the existing worker Job + drain scheduler exactly):

- `google_cloud_run_v2_job "orchestrator"` — `command = ["bellweather", "orchestrate", "--once"]`, runtime SA, `BELLWEATHER_API_URL` = the **in-project** `bellweather-api` service URL (D2 — never a public/third-party endpoint; the orchestrator authenticates as the runtime SA and passes a short-lived, ingest-scoped token to spawned scripts), `BELLWEATHER_TEMPLATES_DIR`, `DATABASE_URL` secret (the orchestrator reads the schedule registry — note: the *orchestrator* needs DB; the *scripts it spawns* do not, K4).
- `google_cloud_scheduler_job "orchestrate"` — `schedule = "* * * * *"`, OAuth-invokes the orchestrator Job. The API service's SA also gets `run.invoker` on this Job so the UI's *Run now* (D3) can trigger an immediate execution.

`Dockerfile`: bake the scripts repo / `producers/` into the image and ensure `BELLWEATHER_TEMPLATES_DIR` resolves. Stays within the `<$40/mo`, scale-to-zero envelope (one more tiny scheduled Job; no always-on cost).

---

## 10. Relationship to existing design — deltas & contradictions

Per the request to "call out what contradicts." Each item is a deliberate evolution, not an accident:

| # | Existing statement | This design | Resolution |
|---|---|---|---|
| **D-a** | 2026-05-31 spec **D1** / README §4: "collection is external… the only coupling is the ingestion contract," diagram shows "(nothing internal)" before `/ingest`. | Bellwether now **schedules and invokes** external scripts. There is a *second* coupling: the template-manifest contract. | **Evolution, called out.** Collection *logic* stays external and unprivileged (K1/K4); Bellwether adds *orchestration of execution*. The README's "Collector… isolated, scheduled job" (§3/§4) is actually *realized* by this — it was always foreseen, just not as a Bellwether-run thing. **Recommend amending** the 2026-05-31 spec D1 + README §4 with a one-line pointer to this spec. |
| **D-b** | 2026-05-31 spec §10 + §3: "the **structured market feed** … is out of scope; a normalizer seam exists but no structured extractor ships." | This spec **builds the structured path** (generic normalizer + value gold write + worker routing). | **Scope advance.** This is the start of README §5.3 (v1). No contradiction in mechanism — it fills the seam the v0 spec deliberately left. |
| **D-c** | `gold.upsert_coverage` **increments** a counter on conflict. | New `gold.upsert_value` **sets** the value (idempotent, K7). | **No conflict** — different function for a different semantic (coverage counts vs. metric values). Worth noting: the *coverage* path is not strictly idempotent under re-processing; out of scope to fix here, flagged for awareness. |
| **D-d** | `worker.process_job` routes **everything** via `get_extractor(content_type)`; never reads `kind`. | Worker branches on `raw_records.kind`. | **Modification** to one existing function (§6.3). Unstructured path behavior is unchanged. |
| **D-e** | README frames Bellwether as a single-user personal pipeline. | K1 introduces "customer scripts in a repo we load onto an instance." | **Conceptual expansion**, scoped down by K10: build the mechanism with you as the first/only customer; **no** multi-tenancy this epic. Called out so the README's framing isn't silently broadened. |

**Per review (2026-06-01) the root docs are amended to match:** the `2026-05-31` spec's D1 (D-a), its §3 diagram + §10 out-of-scope (D-b/D-d), and README §3/§4/§5.3 (D-a/D-e) now carry the orchestrator + structured-path reality and cross-reference this spec.

---

## 11. Phasing

**Phase 1 — Infrastructure (ticketed now, `T18+`).** Everything in §4–§9 that does *not* require real scraping or a real GDELT pull. Each is TDD-able with fixtures (a fake echo template; a fixture `numeric-series-v1` payload):

1. Gold value write (`upsert_value`) — set-semantics, idempotent.
2. Normalizer registry + generic `numeric-series-v1` normalizer + worker `kind` routing.
3. Schedule registry — migration `0002` + `schedules.py`.
4. Template manifest contract + discovery (`templates.py`).
5. Run-harness + `DryRunClient` + `bellweather run-template`.
6. Orchestrator tick + `bellweather orchestrate`.
7. Control-plane API + Schedules UI (with a fake template exercising preview/run-now).
8. Infra: orchestrator Cloud Run Job + Scheduler + scripts-repo image bake.

**End-to-end "done" for Phase 1:** with a trivial fixture template registered and a schedule due, `bellweather orchestrate --once` spawns the script (minimal env), the script's fixture `numeric-series-v1` submission flows API → queue → worker → an `observations` row keyed to a `tracked_symbol`; the UI lists the schedule, previews it (committing nothing), and shows the run. `make check` green.

**Phase 2 — Producers & demo (later, separate tickets — NOT now).**

- **Polymarket template** (`producers/polymarket/`): manifest + script — resolve event URL → discover contract variants (Gamma API) → fetch price history (CLOB), shaping into `numeric-series-v1` (no worker code needed, K6). *Verify endpoints against current Polymarket docs first*, exactly like GDELT's caveat.
- **GDELT as a template**: a `template.toml` for the existing `producers/gdelt` producer (unstructured path, existing extractor) — proves the orchestrator generalizes the v0 producer.
- **Demo config**: seed schedules (GDELT + Polymarket) + a starter watch-list; `make demo` / a CLI to bootstrap; live end-to-end verification.

---

## 12. Out of scope (this epic)

- **In-UI script *authoring*** (a browser code editor that saves+runs pasted Python). That is "remote code execution by design" on a publicly-invokable service and needs auth + a sandboxed executor + versioned script artifacts — a separate, gated epic. Phase 1 gives the *dry-run preview* of *trusted* templates instead (K9).
- **Per-execution isolated Cloud Run Jobs + sandboxing** (gVisor/nsjail, egress limits, resource caps). Phase 1 uses subprocess isolation + minimal-creds (K4/K5); harden later.
- **Multi-tenant customer onboarding**, **dynamic multi-repo loading**, and **per-script dependency installation** (K10). One baked-in scripts repo this epic.
- **Full cron schedules** — interval-based only (§5).
- **News-vs-price divergence research** (README §5.3 research half) — this epic delivers the structured *feed*, not the comparison.
- **Fixing `upsert_coverage` idempotency** (D-c) — flagged, not addressed.

---

## 13. Decisions (resolved in review) & remaining tuning

- **D1 — structured series resolution → last-value-wins.** Bucket structured points by the global `bellweather_obs_bucket`; on a bucket collision the **latest value wins**. The design's real job is to **capture data we miss** when scraping is imperfect — re-fetch + idempotent dedup fills gaps (K8) — not high-fidelity intra-bucket resolution. Native sub-bucket resolution is a later option if a feed needs it.
- **D2 — orchestrator → API stays in-project.** The orchestrator targets **this project's own `bellweather-api` service**, never a public/third-party endpoint. It authenticates as the runtime service account; the in-project API URL (plus a short-lived, ingest-scoped identity token if the service requires auth) is passed to spawned scripts via env — still creds-minimal (no DB/bucket; K4). All jobs are scoped to the project.
- **D3 — run controls** (both routed through the orchestrator; the API never spawns ingesting runs itself):
  - **Force Run (per item)** — a one-shot `force_run` flag on the schedule; the next tick runs it regardless of interval and **consumes** the flag (resets to `false`), so the UI toggle reads off after the run.
  - **Run now (orchestrator)** — triggers an immediate tick instead of waiting for the minute scheduler (invokes the orchestrator Job in GCP; `--once` locally).
- **Remaining tuning** (decided in the tickets, not blocking — this is the old "OQ-2," now split in two so it's unambiguous):
  - **Preview cap** *(this is the "previewing data" half)* — a dry-run can produce thousands of would-be points; the UI shows only the first ~N per symbol (and the first few symbols).
  - **Run timeout** *(this governs real scheduled runs, not preview)* — a scheduled run is a subprocess; if a script hangs it is killed after a per-run wall-clock timeout and the run is recorded as `error`.
