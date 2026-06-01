from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel, ValidationError

from bellweather import reads
from bellweather.contracts import IngestResult, Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record

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


app.include_router(api_router)
