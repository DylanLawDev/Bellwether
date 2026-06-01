import pytest
from bellweather.migrate import apply_migrations
from bellweather.db import get_conn
from bellweather import queue
from tests.conftest import clear_records


@pytest.fixture(autouse=True)
def _migrated():
    apply_migrations()
    # These tests insert source='s' raw_records + work_queue rows but never clean
    # them up. Without this, stale pending jobs accumulate across local runs and
    # push newly-enqueued (highest-id) jobs out of lease()'s oldest-10 batch,
    # flaking test_enqueue_then_lease_then_ack. Start each test from a clean queue.
    with get_conn() as conn:
        clear_records(conn, "s")
        conn.commit()
    yield
    with get_conn() as conn:
        clear_records(conn, "s")
        conn.commit()


def _insert_raw(conn) -> int:
    return conn.execute(
        "insert into raw_records(source,kind,content_type,idempotency_key,payload_uri,fetched_at)"
        " values('s','unstructured','c',%s,'gs://b/x',now()) returning id",
        (f"k-{conn.execute('select gen_random_uuid()').fetchone()[0]}",),
    ).fetchone()[0]


def test_enqueue_then_lease_then_ack():
    with get_conn() as conn:
        rid = _insert_raw(conn)
        jid = queue.enqueue(conn, rid)
        conn.commit()
        jobs = queue.lease(conn, limit=10)
        conn.commit()
        assert any(j.id == jid for j in jobs)
        queue.ack(conn, jid)
        conn.commit()
        again = queue.lease(conn, limit=10)
        conn.commit()
        assert all(j.id != jid for j in again)


def test_lease_skips_already_leased():
    with get_conn() as c1, get_conn() as c2:
        rid = _insert_raw(c1)
        jid = queue.enqueue(c1, rid)
        c1.commit()
        leased1 = queue.lease(c1, limit=10)
        c1.commit()
        leased2 = queue.lease(c2, limit=10)
        c2.commit()
        ids1 = {j.id for j in leased1}
        ids2 = {j.id for j in leased2}
        assert jid in ids1 and jid not in ids2  # no double-lease


def test_lease_reclaims_expired_lease():
    # A worker that leases a job and then dies (no ack/fail) must not orphan it:
    # once its lease window elapses, the job is eligible to be leased again.
    with get_conn() as conn:
        rid = _insert_raw(conn)
        jid = queue.enqueue(conn, rid)
        conn.commit()
        # lease with an already-expired window (lease_until = now() - 1s)
        first = queue.lease(conn, limit=10, lease_seconds=-1)
        conn.commit()
        assert any(j.id == jid for j in first)
        # the orphaned-but-expired job is reclaimed on the next lease
        second = queue.lease(conn, limit=10)
        conn.commit()
        assert any(j.id == jid for j in second)
        attempts = conn.execute("select attempts from work_queue where id=%s", (jid,)).fetchone()[0]
        assert attempts >= 2  # incremented once per (re)lease


def test_active_lease_is_not_reclaimed():
    # The flip side: a still-valid lease must NOT be re-handed-out.
    with get_conn() as conn:
        rid = _insert_raw(conn)
        jid = queue.enqueue(conn, rid)
        conn.commit()
        first = queue.lease(conn, limit=10, lease_seconds=60)
        conn.commit()
        assert any(j.id == jid for j in first)
        second = queue.lease(conn, limit=10)
        conn.commit()
        assert all(j.id != jid for j in second)  # lease still active → skipped


def test_fail_retries_then_dead_letters():
    with get_conn() as conn:
        rid = _insert_raw(conn)
        jid = queue.enqueue(conn, rid)
        conn.commit()
        for _ in range(5):
            queue.lease(conn, limit=10)
            conn.commit()
            queue.fail(conn, jid, "boom", max_attempts=5)
            conn.commit()
        state = conn.execute(
            "select state, attempts from work_queue where id=%s", (jid,)
        ).fetchone()
        assert state[0] == "failed" and state[1] >= 5
