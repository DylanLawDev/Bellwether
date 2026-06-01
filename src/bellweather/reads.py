"""Read-only query functions backing the web UI's data contract.

Each function takes a psycopg ``Connection``, runs parameterized SQL, and returns
plain ``dict``/``list`` shapes — the same column/key contract the web UI expects
(see ``bellweather.web.data.source``). Per repo convention these helpers **never
commit**; the caller owns the transaction. Reads only — no writes, no schema
changes to the six-table spine.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from psycopg import Connection
from psycopg.rows import dict_row

from bellweather.config import get_settings

# Work-queue states, zero-filled by get_queue_stats(). Defined here rather than
# imported from bellweather.web.data.source so the API runtime never pulls in the
# UI data package (its __init__ imports a pandas-backed backend, and pandas is a
# dev/ui-only dependency). tests/test_reads.py asserts this stays in sync with
# the UI contract's QUEUE_STATES.
QUEUE_STATES = ("pending", "leased", "done", "failed")

# Settings fields surfaced (in order) by /api/config, with operator notes. The
# value comes from the live Settings; database_url is masked in get_config().
_CONFIG_FIELDS = [
    ("database_url", "Postgres spine (raw index, queue, silver, gold)."),
    ("bellweather_bucket", "GCS bucket for immutable raw bytes."),
    ("storage_emulator_host", "Set to fake-gcs only for local tests; unset in prod."),
    ("bellweather_api_url", "Ingestion/read API base URL used by clients and the UI."),
    ("bellweather_obs_bucket", "Gold observation bucket granularity (hour | 15min)."),
]


def _rows(conn: Connection, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def get_symbols(conn: Connection) -> list[dict]:
    """Tracked symbols joined to their gold observations.

    ``latest_value`` is the value at the most recent bucket; ``total_samples`` is
    ``sum(value)`` across buckets. ``tag_type``/``raw_value`` split ``key`` on the
    first ``:`` (matching ``gold.upsert_coverage``'s ``f"{tag_type}:{raw_value}"``).
    """
    return _rows(
        conn,
        """
        select s.id,
               s.key,
               split_part(s.key, ':', 1) as tag_type,
               substr(s.key, strpos(s.key, ':') + 1) as raw_value,
               s.kind,
               coalesce(latest.value, 0)::float8 as latest_value,
               coalesce(agg.total, 0)::float8 as total_samples
        from tracked_symbols s
        left join lateral (
            select o.value from observations o
            where o.tracked_symbol_id = s.id
            order by o.ts_bucket desc
            limit 1
        ) latest on true
        left join (
            select tracked_symbol_id, sum(value) as total
            from observations group by tracked_symbol_id
        ) agg on agg.tracked_symbol_id = s.id
        order by s.id
        """,
    )


def get_observations(
    conn: Connection,
    keys: list[str],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    if not keys:
        return []
    conds = ["s.key = any(%s)"]
    params: list = [list(keys)]
    if start is not None:
        conds.append("o.ts_bucket >= %s")
        params.append(start)
    if end is not None:
        conds.append("o.ts_bucket <= %s")
        params.append(end)
    return _rows(
        conn,
        f"""
        select o.ts_bucket, s.key, o.value, o.sample_count
        from observations o
        join tracked_symbols s on s.id = o.tracked_symbol_id
        where {" and ".join(conds)}
        order by o.ts_bucket
        """,
        tuple(params),
    )


def query_raw_records(
    conn: Connection,
    source: str | None = None,
    content_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    conds: list[str] = []
    params: list = []
    if source:
        conds.append("source = %s")
        params.append(source)
    if content_type:
        conds.append("content_type = %s")
        params.append(content_type)
    if status:
        conds.append("status = %s")
        params.append(status)
    if search:
        # Literal substring, case-insensitive (the UI labels this a substring
        # search): position() avoids treating %/_/regex metachars as wildcards.
        conds.append("position(lower(%s) in lower(idempotency_key)) > 0")
        params.append(search)
    if start is not None:
        conds.append("fetched_at >= %s")
        params.append(start)
    if end is not None:
        conds.append("fetched_at <= %s")
        params.append(end)
    where = f"where {' and '.join(conds)}" if conds else ""
    params.extend([limit, offset])
    return _rows(
        conn,
        f"""
        select id, source, kind, content_type, idempotency_key,
               status, fetched_at, payload_uri
        from raw_records
        {where}
        order by fetched_at desc, id desc
        limit %s offset %s
        """,
        tuple(params),
    )


def query_tags(
    conn: Connection,
    tag_type: str | None = None,
    search: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    conds: list[str] = []
    params: list = []
    if tag_type:
        conds.append("tag_type = %s")
        params.append(tag_type)
    if search:
        conds.append("position(lower(%s) in lower(raw_value)) > 0")
        params.append(search)
    if start is not None:
        conds.append("observed_at >= %s")
        params.append(start)
    if end is not None:
        conds.append("observed_at <= %s")
        params.append(end)
    where = f"where {' and '.join(conds)}" if conds else ""
    params.extend([limit, offset])
    return _rows(
        conn,
        f"""
        select id, raw_record_id, source, tag_type, raw_value, observed_at, score
        from tags
        {where}
        order by observed_at desc, id desc
        limit %s offset %s
        """,
        tuple(params),
    )


def get_queue_stats(conn: Connection) -> dict[str, int]:
    counts = {
        r["state"]: r["n"]
        for r in _rows(conn, "select state, count(*) as n from work_queue group by state")
    }
    # Zero-fill any state with no rows so the shape is always the four keys.
    return {state: int(counts.get(state, 0)) for state in QUEUE_STATES}


def get_ingestion_rate(conn: Connection, hours: int = 48) -> list[dict]:
    return _rows(
        conn,
        """
        select date_trunc('hour', fetched_at) as hour, count(*) as records
        from raw_records
        where fetched_at >= now() - make_interval(hours => %s)
        group by 1
        order by 1
        """,
        (hours,),
    )


def get_config(conn: Connection | None = None) -> list[dict]:
    """Redacted view of ``Settings`` for the UI. Masks ``database_url``; never
    returns raw secrets. ``conn`` is accepted for a uniform signature but unused.
    """
    settings = get_settings()
    out: list[dict] = []
    for key, note in _CONFIG_FIELDS:
        value = getattr(settings, key, None)
        if key == "database_url":
            value = _mask_dsn(str(value))
        out.append({"key": key, "value": "" if value is None else str(value), "note": note})
    return out


def _mask_dsn(dsn: str) -> str:
    """Hide every credential of a Postgres DSN.

    Returns a credential-free shape like ``postgresql://***@<host>:<port>/<db>``
    so the UI can confirm a DB is configured without leaking the connection
    string. Drops the userinfo (``user:pass@``) *and* the query string — secrets
    can also ride in query params (e.g. ``?password=...``), so those are removed
    too rather than only stripping ``user:pass@``.
    """
    if "://" not in dsn:
        return "***"
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://***@{host}{port}{parts.path}"
