import pytest
from typer.testing import CliRunner

from bellweather import schedules
from bellweather.cli import (
    POLYMARKET_DEMO_INTERVAL,
    POLYMARKET_DEMO_NAME,
    POLYMARKET_DEMO_PARAMS,
    POLYMARKET_DEMO_TEMPLATE,
    app,
)
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from bellweather.templates import parse_interval

runner = CliRunner()


@pytest.fixture(autouse=True)
def _migrated_and_clean():
    apply_migrations()  # ensures 0002 (producer_schedules) is present
    _delete()
    yield
    _delete()


def _delete():
    with get_conn() as conn:
        conn.execute(
            "delete from producer_runs where schedule_id in"
            " (select id from producer_schedules where name=%s)",
            (POLYMARKET_DEMO_NAME,),
        )
        conn.execute("delete from producer_schedules where name=%s", (POLYMARKET_DEMO_NAME,))
        conn.commit()


def _rows():
    with get_conn() as conn:
        return [s for s in schedules.list_schedules(conn) if s["name"] == POLYMARKET_DEMO_NAME]


def test_seed_polymarket_demo_inserts_one_row():
    result = runner.invoke(app, ["seed-polymarket-demo"])
    assert result.exit_code == 0, result.output
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["template"] == POLYMARKET_DEMO_TEMPLATE
    assert row["params"] == POLYMARKET_DEMO_PARAMS
    assert row["params"]["url"].startswith("https://polymarket.com/event/")
    assert row["interval_seconds"] == parse_interval(POLYMARKET_DEMO_INTERVAL) == 1800
    assert row["enabled"] is True


def test_seed_polymarket_demo_is_idempotent_on_rerun():
    first = runner.invoke(app, ["seed-polymarket-demo"])
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    second = runner.invoke(app, ["seed-polymarket-demo"])
    assert second.exit_code == 0, second.output
    assert "skip" in second.output

    assert len(_rows()) == 1  # second run is a no-op, not a duplicate
