from datetime import datetime, timezone

from bellweather.gold import bucket_ts


def test_bucket_hour():
    t = datetime(2026, 5, 31, 14, 47, 12, tzinfo=timezone.utc)
    assert bucket_ts(t, "hour") == datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc)


def test_bucket_15min():
    t = datetime(2026, 5, 31, 14, 47, 12, tzinfo=timezone.utc)
    assert bucket_ts(t, "15min") == datetime(2026, 5, 31, 14, 45, tzinfo=timezone.utc)
