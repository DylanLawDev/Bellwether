"""reads.* query functions against a seeded DB (requires `make up` + migrations)."""

from datetime import datetime, timezone

import pytest

from bellweather import reads
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from bellweather.web.data import source as contract
from tests.conftest import clear_observations, clear_records

_SOURCE = "test.reads"
_KEYS = ("tr-0001", "tr-0002", "tr-0003")
_SYMS = ("theme:T15ALPHA", "person:t15 powell")
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _seed(conn):
    clear_records(conn, _SOURCE)
    clear_observations(conn, _SYMS)
    conn.execute("delete from tracked_symbols where key = any(%s)", (list(_SYMS),))

    rec_ids = []
    for key, status in zip(_KEYS, ("processed", "received", "failed")):
        rid = conn.execute(
            """insert into raw_records
               (source, kind, content_type, idempotency_key, payload_uri, fetched_at, status)
               values (%s,'unstructured','gdelt-gkg-v2',%s,%s, now(), %s) returning id""",
            (_SOURCE, key, f"gs://b/{key}.json", status),
        ).fetchone()[0]
        rec_ids.append(rid)

    # work_queue: 2 pending, 1 leased, 1 done, 1 failed (across our records)
    for rid, state in zip(
        [rec_ids[0], rec_ids[0], rec_ids[1], rec_ids[2], rec_ids[2]],
        ["pending", "pending", "leased", "done", "failed"],
    ):
        conn.execute("insert into work_queue (raw_record_id, state) values (%s,%s)", (rid, state))

    # tags referencing the seeded records
    tag_rows = [
        (rec_ids[0], "theme", "ECON_STOCKMARKET", '{"count": 2}'),
        (rec_ids[0], "person", "Jerome Powell", '{"count": 1}'),
        (rec_ids[1], "tone", "tone", '{"tone": -1.5}'),
        (rec_ids[2], "location", "Ukraine", '{"count": 3}'),
    ]
    for rid, tag_type, raw_value, score in tag_rows:
        conn.execute(
            """insert into tags (raw_record_id, source, observed_at, tag_type, raw_value, score)
               values (%s,%s, now(), %s,%s,%s::jsonb)""",
            (rid, _SOURCE, tag_type, raw_value, score),
        )

    # tracked_symbols + observations: sym1 has two buckets (latest=5, total=8),
    # sym2 has one bucket (latest=2, total=2).
    sym1 = conn.execute(
        "insert into tracked_symbols (key, kind) values (%s,'coverage') returning id", (_SYMS[0],)
    ).fetchone()[0]
    sym2 = conn.execute(
        "insert into tracked_symbols (key, kind) values (%s,'coverage') returning id", (_SYMS[1],)
    ).fetchone()[0]
    for sid, ts, val, n in [
        (sym1, _NOW.replace(hour=10), 3.0, 3),
        (sym1, _NOW.replace(hour=11), 5.0, 5),
        (sym2, _NOW.replace(hour=11), 2.0, 2),
    ]:
        conn.execute(
            "insert into observations (tracked_symbol_id, ts_bucket, value, sample_count)"
            " values (%s,%s,%s,%s)",
            (sid, ts, val, n),
        )
    conn.commit()
    return rec_ids


@pytest.fixture()
def conn():
    apply_migrations()
    with get_conn() as c:
        _seed(c)
        yield c
        clear_records(c, _SOURCE)
        clear_observations(c, _SYMS)
        c.execute("delete from tracked_symbols where key = any(%s)", (list(_SYMS),))
        c.commit()


def _by_key(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_get_symbols_shape_and_derivations(conn):
    rows = reads.get_symbols(conn)
    ours = [r for r in rows if r["key"] in _SYMS]
    assert {r["key"] for r in ours} == set(_SYMS)
    for r in ours:
        assert set(r) == set(contract.TRACKED_SYMBOL_COLUMNS)
    s1 = _by_key(ours, "theme:T15ALPHA")
    assert s1["tag_type"] == "theme" and s1["raw_value"] == "T15ALPHA"
    assert s1["latest_value"] == 5.0 and s1["total_samples"] == 8.0
    # raw_value keeps everything after the first colon (here a space, no extra colon)
    s2 = _by_key(ours, "person:t15 powell")
    assert s2["tag_type"] == "person" and s2["raw_value"] == "t15 powell"


def test_get_observations_filter_and_shape(conn):
    rows = reads.get_observations(conn, [_SYMS[0]])
    assert rows and all(set(r) == set(contract.OBSERVATION_COLUMNS) for r in rows)
    assert all(r["key"] == _SYMS[0] for r in rows)
    assert [r["value"] for r in rows] == [3.0, 5.0]  # ordered by ts_bucket
    # time window trims the earlier bucket
    windowed = reads.get_observations(conn, [_SYMS[0]], start=_NOW.replace(hour=11))
    assert [r["value"] for r in windowed] == [5.0]
    assert reads.get_observations(conn, []) == []


def test_query_raw_records_filters_and_paging(conn):
    rows = reads.query_raw_records(conn, source=_SOURCE)
    assert len(rows) == 3
    assert all(set(r) == set(contract.RAW_RECORD_COLUMNS) for r in rows)
    assert len(reads.query_raw_records(conn, source=_SOURCE, status="failed")) == 1
    assert len(reads.query_raw_records(conn, source=_SOURCE, content_type="nope")) == 0
    page1 = reads.query_raw_records(conn, source=_SOURCE, limit=2, offset=0)
    page2 = reads.query_raw_records(conn, source=_SOURCE, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 1
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_query_raw_records_search_is_literal(conn):
    assert len(reads.query_raw_records(conn, source=_SOURCE, search="TR-0001")) == 1
    assert reads.query_raw_records(conn, source=_SOURCE, search="[") == []


def test_query_tags_filters_and_score_is_dict(conn):
    rows = reads.query_tags(conn, search="ukrai")
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == set(contract.TAG_COLUMNS)
    assert r["raw_value"] == "Ukraine"
    themes = [t for t in reads.query_tags(conn, tag_type="theme") if t["source"] == _SOURCE]
    assert themes and all(t["tag_type"] == "theme" for t in themes)
    tone = [t for t in reads.query_tags(conn, tag_type="tone") if t["source"] == _SOURCE][0]
    assert tone["score"] == {"tone": -1.5}  # jsonb surfaces as a dict


def test_get_queue_stats_zero_fills(conn):
    stats = reads.get_queue_stats(conn)
    assert set(stats) == set(contract.QUEUE_STATES)
    assert all(isinstance(v, int) for v in stats.values())
    # our seed contributed at least these
    assert stats["pending"] >= 2 and stats["failed"] >= 1


def test_get_ingestion_rate_buckets_by_hour(conn):
    rows = reads.get_ingestion_rate(conn, hours=48)
    assert rows and all(set(r) == set(contract.INGESTION_RATE_COLUMNS) for r in rows)
    assert sum(r["records"] for r in rows) >= 3


def test_get_config_masks_database_url(conn):
    rows = reads.get_config(conn)
    assert all(set(r) == {"key", "value", "note"} for r in rows)
    db = next(r for r in rows if r["key"] == "database_url")
    assert "***" in db["value"]
    # the real password must not leak
    assert "bellweather:bellweather" not in db["value"]
