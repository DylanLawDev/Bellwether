import time

from psycopg.types.json import Jsonb

import bellweather.extractors.gdelt_gkg  # noqa: F401  (registers the extractor)
import bellweather.normalizers.numeric_series  # noqa: F401  (registers the normalizer)
from bellweather.db import get_conn
from bellweather.extractors import get_extractor
from bellweather.gold import upsert_coverage, upsert_value
from bellweather.normalizers import get_normalizer
from bellweather.queue import Job, ack, fail, lease
from bellweather.storage import get_bronze_store


def process_job(conn, job: Job) -> None:
    row = conn.execute(
        "select source, kind, content_type, payload_uri, fetched_at from raw_records where id=%s",
        (job.raw_record_id,),
    ).fetchone()
    source, kind, content_type, payload_uri, fetched_at = row

    if kind == "structured":
        normalizer = get_normalizer(content_type)
        if normalizer is None:
            conn.execute(
                "update raw_records set status='unroutable' where id=%s", (job.raw_record_id,)
            )
            ack(conn, job.id)
            return
        envelope = get_bronze_store().get(payload_uri)
        for pt in normalizer.normalize(envelope):
            upsert_value(
                conn,
                pt.symbol_key,
                pt.symbol_kind,
                pt.ts,
                pt.value,
                unit=pt.unit,
                description=pt.description,
            )
        conn.execute("update raw_records set status='processed' where id=%s", (job.raw_record_id,))
        ack(conn, job.id)
        return

    extractor = get_extractor(content_type)
    if extractor is None:
        conn.execute("update raw_records set status='unroutable' where id=%s", (job.raw_record_id,))
        ack(conn, job.id)
        return
    envelope = get_bronze_store().get(payload_uri)
    for t in extractor.extract(envelope):
        conn.execute(
            "insert into tags(raw_record_id, source, observed_at, tag_type, raw_value, score)"
            " values (%s,%s,%s,%s,%s,%s)",
            (job.raw_record_id, source, fetched_at, t.tag_type, t.raw_value, Jsonb(t.score)),
        )
        if t.tag_type != "tone":
            upsert_coverage(conn, source, t.tag_type, t.raw_value, fetched_at)
    conn.execute("update raw_records set status='processed' where id=%s", (job.raw_record_id,))
    ack(conn, job.id)


def run_worker(once: bool = False) -> None:
    while True:
        with get_conn() as conn:
            jobs = lease(conn, limit=20)
            conn.commit()
            for job in jobs:
                try:
                    process_job(conn, job)
                    conn.commit()
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    with get_conn() as c2:
                        fail(c2, job.id, str(e))
                        c2.commit()
        if once:
            return
        if not jobs:
            time.sleep(2)
