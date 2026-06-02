import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError

from bellweather import reads, schedules, templates
from bellweather.config import get_settings
from bellweather.contracts import IngestResult, Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record
from bellweather.orchestrator import tick

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


def _preview_subprocess(template: str, params: dict) -> dict:
    """Spawn `bellweather run-template --dry-run` with a minimal env (K4/K9).

    Never runs the template in-process: an in-process import would hand the
    customer script the API's DB/bucket credentials. The subprocess gets only
    BELLWEATHER_API_URL + PATH; its last stdout line is a JSON summary
    ({"submitted": int, "sample": [...]}).
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bellweather.cli",
            "run-template",
            "--template",
            template,
            "--params",
            json.dumps(params),
            "--dry-run",
        ],
        env={
            "BELLWEATHER_API_URL": get_settings().bellweather_api_url,
            "PATH": os.environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=proc.stderr.strip() or "preview failed")
    return json.loads(proc.stdout.strip().splitlines()[-1])


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


app.include_router(api_router)
