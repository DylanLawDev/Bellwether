from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from bellweather.contracts import IngestResult, Submission
from bellweather.db import get_conn
from bellweather.queue import enqueue
from bellweather.storage import get_bronze_store

# Deliberate second source of truth: this MUST stay in sync with the extractor
# registry (bellweather.extractors.known_content_types()). We hardcode it here so
# the ingest/API process doesn't import the extractors package (and its heavier
# deps); the worker imports them and has a defensive `unroutable` fallback if the
# two ever diverge.
KNOWN_CONTENT_TYPES: set[str] = {"gdelt-gkg-v2", "numeric-series-v1", "scrape-llm-v1"}


def ingest_record(sub: Submission) -> IngestResult:
    # 1. bronze-first: capture the immutable envelope before any fallible step.
    if sub.payload_uri is not None:
        payload_uri = sub.payload_uri
    else:
        payload_uri = get_bronze_store().put(
            sub.source, sub.fetched_at, sub.idempotency_key, sub.model_dump(mode="json")
        )
    routable = sub.content_type in KNOWN_CONTENT_TYPES
    status = "received" if routable else "unroutable"
    with get_conn() as conn:
        try:
            rid = conn.execute(
                """insert into raw_records
                     (source,kind,content_type,idempotency_key,payload_uri,fetched_at,provenance,status)
                   values (%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                (
                    sub.source,
                    sub.kind,
                    sub.content_type,
                    sub.idempotency_key,
                    payload_uri,
                    sub.fetched_at,
                    Jsonb(sub.provenance),
                    status,
                ),
            ).fetchone()[0]
        except UniqueViolation:
            conn.rollback()
            existing = conn.execute(
                "select id, payload_uri from raw_records where source=%s and idempotency_key=%s",
                (sub.source, sub.idempotency_key),
            ).fetchone()
            return IngestResult(
                raw_record_id=existing[0], status="duplicate", payload_uri=existing[1]
            )
        if routable:
            enqueue(conn, rid)
        conn.commit()
    return IngestResult(
        raw_record_id=rid,
        status=("created" if routable else "unroutable"),
        payload_uri=payload_uri,
    )
