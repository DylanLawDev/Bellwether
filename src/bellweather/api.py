from fastapi import FastAPI
from pydantic import BaseModel, ValidationError

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
