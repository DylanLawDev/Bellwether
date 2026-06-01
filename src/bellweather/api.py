from fastapi import FastAPI
from pydantic import BaseModel

from bellweather.contracts import IngestResult, Submission
from bellweather.db import get_conn
from bellweather.ingest import ingest_record

app = FastAPI(title="Bellweather Ingestion")


class BatchRequest(BaseModel):
    records: list[Submission]


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
    return BatchResponse(results=[ingest_record(s) for s in req.records])
