import json
import subprocess
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError

from bellweather import reads, schedules, templates
import bellweather.fetch.httpx_fetch  # noqa: F401  # registers the default "httpx" adapter
from bellweather.fetch import get_fetcher, known_fetchers
from bellweather.llm import LlmExtractor
from bellweather.scrape import specs as scrape_specs
from bellweather.scrape.binding import apply_binding
from bellweather.contracts import IngestResult, Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record
from bellweather.orchestrator import _child_env, tick

app = FastAPI(title="Bellweather Ingestion")


# Raw dicts (not list[Submission]): typing the body as list[Submission] makes
# Pydantic validate the whole batch up front, so one malformed record 422s the
# entire request and even the valid records are lost. The batch contract (T07)
# is per-record isolation, so each record is validated individually below.
class BatchRequest(BaseModel):
    records: list[dict]


class BatchResponse(BaseModel):
    results: list[IngestResult]


@app.get("/healthz")
def healthz():
    with get_conn() as conn:
        conn.execute("select 1")
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResult)
def ingest(sub: Submission):
    return ingest_record(sub)


@app.post("/ingest/batch", response_model=BatchResponse)
def ingest_batch(req: BatchRequest):
    results: list[IngestResult] = []
    for raw in req.records:
        try:
            sub = Submission.model_validate(raw)
        except ValidationError as e:
            results.append(IngestResult(status="error", error=str(e)))
            continue
        results.append(ingest_record(sub))
    return BatchResponse(results=results)


# --- read API ---------------------------------------------------------------
# GET routes returning exactly the shapes the web UI's data contract expects
# (bellweather.web.data.source). Reads only; one connection per request.
class SymbolRow(BaseModel):
    id: int
    key: str
    tag_type: str
    raw_value: str
    kind: str
    latest_value: float
    total_samples: float


class ObservationRow(BaseModel):
    ts_bucket: datetime
    key: str
    value: float
    sample_count: int


class RawRecordRow(BaseModel):
    id: int
    source: str
    kind: str
    content_type: str
    idempotency_key: str
    status: str
    fetched_at: datetime
    payload_uri: str


class TagRow(BaseModel):
    id: int
    raw_record_id: int
    source: str
    tag_type: str
    raw_value: str
    observed_at: datetime
    score: dict


class QueueStats(BaseModel):
    pending: int
    leased: int
    done: int
    failed: int


class IngestionRateRow(BaseModel):
    hour: datetime
    records: int


class ConfigRow(BaseModel):
    key: str
    value: str
    note: str


# --- control-plane API (schedules / templates / runs) -----------------------
class ScheduleRow(BaseModel):
    id: int
    name: str
    template: str
    params: dict
    interval_seconds: int
    enabled: bool
    force_run: bool
    last_run_at: datetime | None


class ScheduleCreate(BaseModel):
    name: str
    template: str
    params: dict = {}
    interval_seconds: int
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    params: dict | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    force_run: bool | None = None


class TemplateParamRow(BaseModel):
    name: str
    type: str
    required: bool
    default: object | None = None
    choices: list | None = None
    help: str | None = None


class TemplateRow(BaseModel):
    name: str
    entrypoint: str
    description: str
    params: list[TemplateParamRow]
    default_interval_seconds: int | None = None


class RunRow(BaseModel):
    id: int
    schedule_id: int | None
    template: str
    params: dict
    started_at: datetime
    finished_at: datetime | None
    status: str
    submitted: int | None
    error: str | None


class TickResult(BaseModel):
    started_run_ids: list[int]


# --- scrape-spec control plane (read / CRUD / preview) ----------------------
class ScrapeSpecRow(BaseModel):
    id: int
    name: str
    description: str | None = None
    sites: list
    output_schema: dict
    binding: dict
    fetch_adapter: str
    llm_model: str | None = None
    enabled: bool


class ScrapeSpecCreate(BaseModel):
    name: str
    sites: list = []
    output_schema: dict
    binding: dict
    description: str | None = None
    fetch_adapter: str = "httpx"
    llm_model: str | None = None
    enabled: bool = True


class ScrapeSpecPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    sites: list | None = None
    output_schema: dict | None = None
    binding: dict | None = None
    fetch_adapter: str | None = None
    llm_model: str | None = None
    enabled: bool | None = None


class ScrapePreviewRequest(BaseModel):
    url: str | None = None


class ScrapePreviewSampleRow(BaseModel):
    symbol_key: str
    ts: datetime
    value: float


class ScrapePreviewTagRow(BaseModel):
    tag_type: str
    raw_value: str


class ScrapePreviewResult(BaseModel):
    extracted: dict
    symbols: list[str]
    sample: list[ScrapePreviewSampleRow]
    tags: list[ScrapePreviewTagRow]


def _preview_shape(summary: dict) -> dict:
    """Flatten a dry-run summary into the control-plane preview contract.

    ``run-template --dry-run`` emits ``{"submitted": int, "sample": [<Submission
    dict>, ...]}``. The UI wants ``{"submitted", "symbols", "sample"}`` where
    ``symbols`` is the distinct symbol keys and ``sample`` is one flat row per
    point: ``{"symbol_key", "ts", "value"}``. Submissions without a numeric
    ``symbol_key``/``points`` payload (e.g. unstructured) count toward
    ``submitted`` but contribute no symbols/sample rows.
    """
    symbols: list[str] = []
    sample: list[dict] = []
    for sub in summary.get("sample", []):
        payload = sub.get("payload") or {}
        key = payload.get("symbol_key")
        if key is None:
            continue
        if key not in symbols:
            symbols.append(key)
        for pt in payload.get("points", []):
            sample.append({"symbol_key": key, "ts": pt.get("ts"), "value": pt.get("value")})
    return {"submitted": summary.get("submitted", 0), "symbols": symbols, "sample": sample}


def _preview_subprocess(template: str, params: dict) -> dict:
    """Spawn `bellweather run-template --dry-run` with a minimal env (K4/K9).

    Uses the installed ``bellweather`` console script (NOT ``python -m
    bellweather.cli`` — that imports the module without a ``__main__`` guard
    and emits nothing) and the shared ``orchestrator._child_env()`` so the
    child can discover/import the template while never receiving the API's
    DB/bucket credentials. Reshapes the summary into the preview contract.
    """
    proc = subprocess.run(
        [
            "bellweather",
            "run-template",
            "--template",
            template,
            "--params",
            json.dumps(params),
            "--dry-run",
        ],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip() or "preview failed")
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=502, detail="preview produced no output")
    return _preview_shape(json.loads(lines[-1]))


SCRAPE_PREVIEW_SAMPLE_LIMIT = 50  # cap flat sample/symbol rows a dry-run preview returns


def _scrape_preview(spec: dict, url: str | None) -> dict:
    """In-process K10 dry-run: fetch ONE url, LLM-extract against the spec's
    output_schema, apply the spec's binding, and return the extracted JSON +
    would-be observations/tags. Commits NOTHING — no bronze, no /ingest, no DB.

    The API is the trusted surface that holds the LLM key (the collector does
    not), so this runs in-process rather than spawning a subprocess. Reuses the
    same units the worker path uses: get_fetcher, LlmExtractor, apply_binding.
    """
    target = url or (spec["sites"][0] if spec["sites"] else None)
    if not target:
        raise HTTPException(status_code=400, detail="spec has no sites and no url given")
    fetcher = get_fetcher(spec["fetch_adapter"])
    if fetcher is None:
        raise HTTPException(
            status_code=400, detail=f"unknown fetch adapter: {spec['fetch_adapter']}"
        )
    fetched = fetcher.fetch(target)
    instance = LlmExtractor().extract(
        fetched.content, spec["output_schema"], model=spec.get("llm_model")
    )
    obs, tags = apply_binding(instance, spec["binding"], fetched_at=datetime.now(timezone.utc))
    symbols: list[str] = []
    for o in obs:
        if o.symbol_key not in symbols:
            symbols.append(o.symbol_key)
    sample = [{"symbol_key": o.symbol_key, "ts": o.ts, "value": o.value} for o in obs]
    return {
        "extracted": instance,
        "symbols": symbols[:SCRAPE_PREVIEW_SAMPLE_LIMIT],
        "sample": sample[:SCRAPE_PREVIEW_SAMPLE_LIMIT],
        "tags": [{"tag_type": t.tag_type, "raw_value": t.raw_value} for t in tags],
    }


api_router = APIRouter(prefix="/api")


@api_router.get("/symbols", response_model=list[SymbolRow])
def api_symbols():
    with get_conn() as conn:
        return reads.get_symbols(conn)


@api_router.get("/observations", response_model=list[ObservationRow])
def api_observations(
    keys: Annotated[list[str], Query()] = [],
    start: datetime | None = None,
    end: datetime | None = None,
):
    with get_conn() as conn:
        return reads.get_observations(conn, keys, start=start, end=end)


@api_router.get("/records", response_model=list[RawRecordRow])
def api_records(
    source: str | None = None,
    content_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        return reads.query_raw_records(
            conn,
            source=source,
            content_type=content_type,
            status=status,
            search=search,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )


@api_router.get("/tags", response_model=list[TagRow])
def api_tags(
    tag_type: str | None = None,
    search: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        return reads.query_tags(
            conn,
            tag_type=tag_type,
            search=search,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )


@api_router.get("/queue", response_model=QueueStats)
def api_queue():
    with get_conn() as conn:
        return reads.get_queue_stats(conn)


@api_router.get("/ingestion-rate", response_model=list[IngestionRateRow])
def api_ingestion_rate(hours: int = 48):
    with get_conn() as conn:
        return reads.get_ingestion_rate(conn, hours=hours)


@api_router.get("/config", response_model=list[ConfigRow])
def api_config():
    return reads.get_config()


@api_router.get("/fetch-adapters")
def api_fetch_adapters():
    return {"adapters": sorted(known_fetchers())}


@api_router.get("/templates", response_model=list[TemplateRow])
def api_templates():
    out = []
    for t in templates.discover_templates().values():
        out.append(
            TemplateRow(
                name=t.name,
                entrypoint=t.entrypoint,
                description=t.description,
                default_interval_seconds=t.default_interval_seconds,
                params=[
                    TemplateParamRow(
                        name=p.name,
                        type=p.type,
                        required=p.required,
                        default=p.default,
                        choices=p.choices,
                        help=p.help,
                    )
                    for p in t.params
                ],
            )
        )
    return out


@api_router.post("/templates/{name}/preview")
def api_template_preview(name: str, params: dict):
    if name not in templates.discover_templates():
        raise HTTPException(status_code=404, detail="unknown template")
    return _preview_subprocess(name, params)


@api_router.get("/schedules", response_model=list[ScheduleRow])
def api_schedules():
    with get_conn() as conn:
        return schedules.list_schedules(conn)


@api_router.post("/schedules", response_model=ScheduleRow)
def api_create_schedule(body: ScheduleCreate):
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name=body.name,
            template=body.template,
            params=body.params,
            interval_seconds=body.interval_seconds,
            enabled=body.enabled,
        )
        conn.commit()
        return schedules.get_schedule(conn, sid)


@api_router.patch("/schedules/{schedule_id}", response_model=ScheduleRow)
def api_update_schedule(schedule_id: int, body: SchedulePatch):
    fields = body.model_dump(exclude_none=True)
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        if fields:
            schedules.update_schedule(conn, schedule_id, **fields)
            conn.commit()
        return schedules.get_schedule(conn, schedule_id)


@api_router.delete("/schedules/{schedule_id}")
def api_delete_schedule(schedule_id: int):
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        schedules.delete_schedule(conn, schedule_id)
        conn.commit()
    return {"status": "deleted"}


@api_router.post("/schedules/{schedule_id}/force", response_model=ScheduleRow)
def api_force_schedule(schedule_id: int):
    with get_conn() as conn:
        if schedules.get_schedule(conn, schedule_id) is None:
            raise HTTPException(status_code=404, detail="unknown schedule")
        schedules.set_force_run(conn, schedule_id, True)
        conn.commit()
        return schedules.get_schedule(conn, schedule_id)


@api_router.post("/orchestrator/run", response_model=TickResult)
def api_orchestrator_run():
    with get_conn() as conn:
        return TickResult(started_run_ids=tick(conn))


@api_router.get("/runs", response_model=list[RunRow])
def api_runs(schedule_id: int | None = None, limit: int = Query(50, ge=1, le=500)):
    with get_conn() as conn:
        return schedules.list_runs(conn, schedule_id=schedule_id, limit=limit)


@api_router.get("/scrape-specs", response_model=list[ScrapeSpecRow])
def api_scrape_specs():
    with get_conn() as conn:
        return scrape_specs.list_specs(conn)


@api_router.get("/scrape-specs/{name}", response_model=ScrapeSpecRow)
def api_scrape_spec(name: str):
    with get_conn() as conn:
        spec = scrape_specs.get_spec(conn, name)
        if spec is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        return spec


@api_router.post("/scrape-specs", response_model=ScrapeSpecRow)
def api_create_scrape_spec(body: ScrapeSpecCreate):
    with get_conn() as conn:
        scrape_specs.create_spec(
            conn,
            name=body.name,
            sites=body.sites,
            output_schema=body.output_schema,
            binding=body.binding,
            description=body.description,
            fetch_adapter=body.fetch_adapter,
            llm_model=body.llm_model,
            enabled=body.enabled,
        )
        conn.commit()
        return scrape_specs.get_spec(conn, body.name)


@api_router.patch("/scrape-specs/{name}", response_model=ScrapeSpecRow)
def api_update_scrape_spec(name: str, body: ScrapeSpecPatch):
    # exclude_unset (not exclude_none): a PATCH that explicitly sends a nullable
    # field as null (e.g. {"llm_model": null} to fall back to the settings default,
    # or {"description": null} to clear it) must reach update_spec, while omitted
    # fields stay untouched. exclude_none would silently drop those explicit nulls.
    fields = body.model_dump(exclude_unset=True)
    with get_conn() as conn:
        if scrape_specs.get_spec(conn, name) is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        if fields:
            scrape_specs.update_spec(conn, name, **fields)
            conn.commit()
        # A patch may rename the spec; look it up by its (possibly new) name.
        return scrape_specs.get_spec(conn, fields.get("name", name))


@api_router.delete("/scrape-specs/{name}")
def api_delete_scrape_spec(name: str):
    with get_conn() as conn:
        if scrape_specs.get_spec(conn, name) is None:
            raise HTTPException(status_code=404, detail="unknown scrape spec")
        scrape_specs.delete_spec(conn, name)
        conn.commit()
    return {"status": "deleted"}


@api_router.post("/scrape-specs/{name}/preview", response_model=ScrapePreviewResult)
def api_scrape_spec_preview(name: str, body: ScrapePreviewRequest):
    with get_conn() as conn:
        spec = scrape_specs.get_spec(conn, name)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown scrape spec")
    return _scrape_preview(spec, body.url)


app.include_router(api_router)
