import pytest
from typer.testing import CliRunner

from bellweather import schedules
from bellweather.cli import GDELT_DEMO_GKG_URL, GDELT_DEMO_NAME, app
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations

runner = CliRunner()


@pytest.fixture(autouse=True)
def _migrated_and_clean():
    # Ensure 0002 is applied, and remove any pre-existing gdelt-demo row so the
    # idempotency assertions start from a known-empty state. Clean up after too.
    apply_migrations()
    _delete_demo()
    yield
    _delete_demo()


def _delete_demo():
    with get_conn() as conn:
        conn.execute(
            "delete from producer_runs where schedule_id in"
            " (select id from producer_schedules where name=%s)",
            (GDELT_DEMO_NAME,),
        )
        conn.execute("delete from producer_schedules where name=%s", (GDELT_DEMO_NAME,))
        conn.commit()


def _demo_rows():
    with get_conn() as conn:
        return [s for s in schedules.list_schedules(conn) if s["name"] == GDELT_DEMO_NAME]


def test_seed_demo_inserts_one_gdelt_demo_schedule():
    result = runner.invoke(app, ["seed-gdelt-demo"])
    assert result.exit_code == 0, result.output

    rows = _demo_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["template"] == "gdelt"
    assert row["enabled"] is True
    assert row["interval_seconds"] == 15 * 60
    # params round-trips as a real dict (JSONB) under the key T28's manifest
    # declares (`url`), so orchestrate validates + fetches.
    assert row["params"] == {"url": GDELT_DEMO_GKG_URL}


def test_seed_demo_is_idempotent_no_duplicate_on_second_run():
    first = runner.invoke(app, ["seed-gdelt-demo"])
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    second = runner.invoke(app, ["seed-gdelt-demo"])
    assert second.exit_code == 0, second.output
    assert "skip" in second.output

    # exactly one row despite two invocations
    assert len(_demo_rows()) == 1
