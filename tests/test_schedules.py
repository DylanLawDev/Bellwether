import time

import pytest

from bellweather import schedules
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations


@pytest.fixture(autouse=True)
def _migrated():
    # Applies forward-only migrations (incl. 0002). Each test inserts its own
    # schedules and asserts only on the rows it created, so no global cleanup is
    # needed — but delete this test's rows on teardown to keep due_schedules()
    # assertions in other tests deterministic across local re-runs.
    apply_migrations()
    yield
    with get_conn() as conn:
        conn.execute("delete from producer_runs where template like 't21-%'")
        conn.execute("delete from producer_schedules where template like 't21-%'")
        conn.commit()


def test_create_get_list_roundtrip():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="echo usage",
            template="t21-echo",
            params={"url": "http://x"},
            interval_seconds=300,
        )
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["id"] == sid
        assert row["name"] == "echo usage"
        assert row["template"] == "t21-echo"
        assert row["params"] == {"url": "http://x"}
        assert row["interval_seconds"] == 300
        assert row["enabled"] is True
        assert row["force_run"] is False
        assert row["last_run_at"] is None
        listed = schedules.list_schedules(conn)
        assert any(s["id"] == sid for s in listed)


def test_get_missing_returns_none():
    with get_conn() as conn:
        assert schedules.get_schedule(conn, -1) is None


def test_interval_seconds_must_be_positive():
    import psycopg

    with get_conn() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            schedules.create_schedule(
                conn,
                name="bad",
                template="t21-bad",
                params={},
                interval_seconds=0,
            )
        conn.rollback()


def test_update_schedule_changes_fields_and_bumps_updated_at():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="x",
            template="t21-upd",
            params={},
            interval_seconds=60,
        )
        conn.commit()
        before = schedules.get_schedule(conn, sid)["updated_at"]
        time.sleep(0.01)
        schedules.update_schedule(
            conn,
            sid,
            name="renamed",
            interval_seconds=120,
            params={"a": 1},
            enabled=False,
        )
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["name"] == "renamed"
        assert row["interval_seconds"] == 120
        assert row["params"] == {"a": 1}
        assert row["enabled"] is False
        assert row["updated_at"] > before


def test_update_with_no_fields_is_noop():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="x",
            template="t21-noop",
            params={},
            interval_seconds=60,
        )
        conn.commit()
        schedules.update_schedule(conn, sid)  # no fields -> no-op, no error
        conn.commit()
        assert schedules.get_schedule(conn, sid)["name"] == "x"


def test_delete_schedule():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="x",
            template="t21-del",
            params={},
            interval_seconds=60,
        )
        conn.commit()
        schedules.delete_schedule(conn, sid)
        conn.commit()
        assert schedules.get_schedule(conn, sid) is None


def test_due_includes_never_run_and_elapsed_excludes_disabled_and_not_yet_due():
    with get_conn() as conn:
        # never run -> due
        never = schedules.create_schedule(
            conn,
            name="never",
            template="t21-never",
            params={},
            interval_seconds=60,
        )
        # ran long ago -> interval elapsed -> due
        elapsed = schedules.create_schedule(
            conn,
            name="elapsed",
            template="t21-elapsed",
            params={},
            interval_seconds=60,
        )
        # ran just now, long interval -> NOT due
        fresh = schedules.create_schedule(
            conn,
            name="fresh",
            template="t21-fresh",
            params={},
            interval_seconds=3600,
        )
        # disabled, never run -> NOT due
        off = schedules.create_schedule(
            conn,
            name="off",
            template="t21-off",
            params={},
            interval_seconds=60,
            enabled=False,
        )
        conn.execute(
            "update producer_schedules set last_run_at = now() - interval '1 hour' where id=%s",
            (elapsed,),
        )
        conn.execute(
            "update producer_schedules set last_run_at = now() where id=%s",
            (fresh,),
        )
        conn.commit()
        due_ids = {s["id"] for s in schedules.due_schedules(conn)}
        assert never in due_ids
        assert elapsed in due_ids
        assert fresh not in due_ids
        assert off not in due_ids


def test_force_run_makes_a_fresh_schedule_due():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="forced",
            template="t21-force",
            params={},
            interval_seconds=3600,
        )
        conn.execute("update producer_schedules set last_run_at = now() where id=%s", (sid,))
        conn.commit()
        assert sid not in {s["id"] for s in schedules.due_schedules(conn)}
        schedules.set_force_run(conn, sid, True)
        conn.commit()
        assert schedules.get_schedule(conn, sid)["force_run"] is True
        assert sid in {s["id"] for s in schedules.due_schedules(conn)}


def test_claim_sets_last_run_at_and_clears_force_run():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="claimed",
            template="t21-claim",
            params={},
            interval_seconds=3600,
        )
        schedules.set_force_run(conn, sid, True)
        conn.commit()
        schedules.claim(conn, sid)
        conn.commit()
        row = schedules.get_schedule(conn, sid)
        assert row["last_run_at"] is not None
        assert row["force_run"] is False
        # consuming the force AND advancing last_run_at -> no longer due
        assert sid not in {s["id"] for s in schedules.due_schedules(conn)}


def test_start_then_finish_run_transitions_running_to_ok_with_submitted():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="runs",
            template="t21-runs",
            params={"k": "v"},
            interval_seconds=60,
        )
        conn.commit()
        rid = schedules.start_run(
            conn,
            schedule_id=sid,
            template="t21-runs",
            params={"k": "v"},
        )
        conn.commit()
        runs = schedules.list_runs(conn, schedule_id=sid)
        running = next(r for r in runs if r["id"] == rid)
        assert running["status"] == "running"
        assert running["template"] == "t21-runs"
        assert running["params"] == {"k": "v"}
        assert running["finished_at"] is None
        assert running["submitted"] is None
        schedules.finish_run(conn, rid, status="ok", submitted=7)
        conn.commit()
        done = next(r for r in schedules.list_runs(conn, schedule_id=sid) if r["id"] == rid)
        assert done["status"] == "ok"
        assert done["submitted"] == 7
        assert done["finished_at"] is not None
        assert done["error"] is None


def test_finish_run_records_error():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="err",
            template="t21-err",
            params={},
            interval_seconds=60,
        )
        rid = schedules.start_run(conn, schedule_id=sid, template="t21-err", params={})
        conn.commit()
        schedules.finish_run(conn, rid, status="error", error="boom")
        conn.commit()
        row = next(r for r in schedules.list_runs(conn, schedule_id=sid) if r["id"] == rid)
        assert row["status"] == "error"
        assert row["error"] == "boom"
        assert row["submitted"] is None


def test_list_runs_orders_newest_first_and_honors_limit():
    with get_conn() as conn:
        sid = schedules.create_schedule(
            conn,
            name="ord",
            template="t21-order",
            params={},
            interval_seconds=60,
        )
        conn.commit()
        rids = []
        for _ in range(3):
            rids.append(schedules.start_run(conn, schedule_id=sid, template="t21-order", params={}))
            conn.commit()
            time.sleep(0.01)
        runs = schedules.list_runs(conn, schedule_id=sid)
        ordered = [r["id"] for r in runs]
        assert ordered == sorted(rids, reverse=True)  # newest started_at first
        assert len(schedules.list_runs(conn, schedule_id=sid, limit=1)) == 1
