from datetime import datetime

from bellweather.config import get_settings


def bucket_ts(when: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return when.replace(minute=0, second=0, microsecond=0)
    if granularity == "15min":
        minute = (when.minute // 15) * 15
        return when.replace(minute=minute, second=0, microsecond=0)
    raise ValueError(f"unknown granularity {granularity}")


def upsert_coverage(conn, source, tag_type, raw_value, observed_at):
    key = f"{tag_type}:{raw_value}"
    sym_id = conn.execute(
        """insert into tracked_symbols(key, kind) values(%s,'coverage')
           on conflict (key) do update set key=excluded.key returning id""",
        (key,),
    ).fetchone()[0]
    bucket = bucket_ts(observed_at, get_settings().bellweather_obs_bucket)
    conn.execute(
        """insert into observations(tracked_symbol_id, ts_bucket, value, sample_count)
           values (%s,%s,1,1)
           on conflict (tracked_symbol_id, ts_bucket)
           do update set value = observations.value + 1,
                         sample_count = observations.sample_count + 1""",
        (sym_id, bucket),
    )
