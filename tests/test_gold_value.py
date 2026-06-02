from datetime import datetime, timezone

import pytest

from bellweather.config import get_settings
from bellweather.db import get_conn
from bellweather.gold import bucket_ts, upsert_value
from bellweather.migrate import apply_migrations
from tests.conftest import clear_observations

# A unique key for this test's symbol so its observation rows never collide with
# other tests' shared coverage symbols.
_KEY = "test:gold-value-symbol"


@pytest.fixture(autouse=True)
def _m():
    apply_migrations()
    # Reset this symbol's observations (and drop the symbol itself) so value /
    # sample_count assertions start from a clean slate regardless of test order.
    with get_conn() as c:
        clear_observations(c, (_KEY,))
        c.execute("delete from tracked_symbols where key = %s", (_KEY,))
        c.commit()


def test_fresh_value_creates_symbol_and_observation():
    ts = datetime(2026, 5, 31, 14, 47, 12, tzinfo=timezone.utc)
    bucket = bucket_ts(ts, get_settings().bellweather_obs_bucket)
    with get_conn() as c:
        sym_id = upsert_value(
            c,
            _KEY,
            "market-probability",
            ts,
            0.37,
            unit="probability",
            description="Will X happen by D? (YES)",
            sample_count=1,
        )
        c.commit()
        kind, unit, desc = c.execute(
            "select kind, unit, description from tracked_symbols where id = %s", (sym_id,)
        ).fetchone()
        assert (kind, unit, desc) == (
            "market-probability",
            "probability",
            "Will X happen by D? (YES)",
        )
        value, sample_count = c.execute(
            "select value, sample_count from observations"
            " where tracked_symbol_id = %s and ts_bucket = %s",
            (sym_id, bucket),
        ).fetchone()
        assert value == pytest.approx(0.37)
        assert sample_count == 1


def test_same_bucket_replaces_not_increments():
    ts = datetime(2026, 5, 31, 14, 47, 12, tzinfo=timezone.utc)
    bucket = bucket_ts(ts, get_settings().bellweather_obs_bucket)
    with get_conn() as c:
        upsert_value(c, _KEY, "market-probability", ts, 0.37, sample_count=1)
        # Same (symbol, bucket), a new value + higher sample_count.
        sym_id = upsert_value(c, _KEY, "market-probability", ts, 0.42, sample_count=3)
        c.commit()
        # Exactly one observation row for this (symbol, bucket) — no extra rows.
        nrows = c.execute(
            "select count(*) from observations where tracked_symbol_id = %s", (sym_id,)
        ).fetchone()[0]
        assert nrows == 1
        value, sample_count = c.execute(
            "select value, sample_count from observations"
            " where tracked_symbol_id = %s and ts_bucket = %s",
            (sym_id, bucket),
        ).fetchone()
        # SET, not increment: last value wins; sample_count set (not 1 + 3).
        assert value == pytest.approx(0.42)
        assert sample_count == 3


def test_two_points_in_one_bucket_collapse_last_wins():
    # Two distinct timestamps inside the same configured bucket map to one row.
    a = datetime(2026, 5, 31, 14, 5, 0, tzinfo=timezone.utc)
    b = datetime(2026, 5, 31, 14, 50, 0, tzinfo=timezone.utc)
    bucket_a = bucket_ts(a, get_settings().bellweather_obs_bucket)
    bucket_b = bucket_ts(b, get_settings().bellweather_obs_bucket)
    # Default granularity is "hour": both fall in the same bucket.
    assert bucket_a == bucket_b
    with get_conn() as c:
        upsert_value(c, _KEY, "market-probability", a, 0.10)
        sym_id = upsert_value(c, _KEY, "market-probability", b, 0.90)
        c.commit()
        rows = c.execute(
            "select value from observations where tracked_symbol_id = %s order by ts_bucket",
            (sym_id,),
        ).fetchall()
        assert [r[0] for r in rows] == pytest.approx([0.90])
