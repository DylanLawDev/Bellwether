"""Deterministic, schema-accurate mock data for the UI prototype.

Shapes mirror the six-table Postgres spine (``migrations/0001_initial.sql``) so a
later ``live.py`` can return identical frames from real read-endpoints. The dataset
is built once at import under a fixed seed, so the prototype looks the same on every
launch; query functions return filtered copies.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from bellweather.web.data import source as contract

_SEED = 20260531
_HOURS = 14 * 24  # ~14 days of hourly buckets

# (tag_type, raw_value, baseline coverage rate). The tracked-symbol key is composed
# as f"{tag_type}:{raw_value}" in _build() — the same rule gold.upsert_coverage uses
# (src/bellweather/gold.py) — so the mock can't desync from the real keying.
_SYMBOLS = [
    ("theme", "ECON_STOCKMARKET", 11.0),
    ("theme", "WB_2670_JOBS", 7.0),
    ("person", "jerome powell", 4.0),
    ("person", "vladimir putin", 6.0),
    ("org", "federal reserve", 5.0),
    ("org", "european central bank", 3.0),
    ("location", "Ukraine", 9.0),
    ("location", "China", 8.0),
]

# (symbol index, hours-ago-from-end of the spike center, multiplier) — gives the
# anomaly flag something real to catch.
_SPIKES = [(0, 72, 6.0), (2, 18, 7.0), (6, 120, 4.5)]


def _now_hour() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _build():
    rng = np.random.default_rng(_SEED)
    end = _now_hour()
    times = [end - timedelta(hours=h) for h in range(_HOURS - 1, -1, -1)]

    # --- tracked_symbols + observations -------------------------------------
    sym_rows, obs_rows = [], []
    for sid, (tag_type, raw_value, lam) in enumerate(_SYMBOLS, start=1):
        key = f"{tag_type}:{raw_value}"  # matches gold.upsert_coverage keying
        # mild daily seasonality on top of a Poisson baseline
        season = 1.0 + 0.35 * np.sin(np.arange(_HOURS) * (2 * np.pi / 24))
        counts = rng.poisson(lam * season).astype(int)
        for s_idx, ago, mult in _SPIKES:
            if s_idx == sid - 1:
                center = _HOURS - 1 - ago
                for d in (-1, 0, 1):
                    j = center + d
                    if 0 <= j < _HOURS:
                        counts[j] = int(counts[j] * mult)

        latest_value, total = 0, 0
        for t, c in zip(times, counts):
            if c <= 0:  # coverage rows exist only when there was ≥1 event
                continue
            obs_rows.append({"ts_bucket": t, "key": key, "value": float(c), "sample_count": int(c)})
            latest_value, total = float(c), total + int(c)
        sym_rows.append(
            {
                "id": sid,
                "key": key,
                "tag_type": tag_type,
                "raw_value": raw_value,
                "kind": "coverage",
                "latest_value": latest_value,
                "total_samples": total,
            }
        )

    symbols = pd.DataFrame(sym_rows, columns=contract.TRACKED_SYMBOL_COLUMNS)
    observations = pd.DataFrame(obs_rows, columns=contract.OBSERVATION_COLUMNS)

    # --- raw_records --------------------------------------------------------
    n_records = 140
    statuses = rng.choice(
        ["processed", "received", "failed", "unroutable"],
        size=n_records,
        p=[0.86, 0.08, 0.04, 0.02],
    )
    rec_rows = []
    for i in range(1, n_records + 1):
        fetched = end - timedelta(hours=int(rng.integers(0, _HOURS)))
        stamp = fetched.strftime("%Y%m%d%H%M%S")
        rec_rows.append(
            {
                "id": i,
                "source": "gdelt",
                "kind": "unstructured",
                "content_type": "gdelt.gkg",
                "idempotency_key": f"gkg:{stamp}:{i:04d}",
                "status": statuses[i - 1],
                "fetched_at": fetched,
                "payload_uri": f"gs://bellweather-bronze/gdelt/gkg/{stamp}-{i:04d}.json",
            }
        )
    raw_records = pd.DataFrame(rec_rows, columns=contract.RAW_RECORD_COLUMNS).sort_values(
        "fetched_at", ascending=False, ignore_index=True
    )

    # --- tags ---------------------------------------------------------------
    n_tags = 400
    tag_rows = []
    for i in range(1, n_tags + 1):
        tag_type, raw_value, _ = _SYMBOLS[int(rng.integers(0, len(_SYMBOLS)))]
        observed = end - timedelta(hours=int(rng.integers(0, _HOURS)))
        if tag_type == "person" and rng.random() < 0.4:
            tag_type, raw_value = "tone", "tone"
        score = (
            {"tone": round(float(rng.normal(-0.5, 3.0)), 2)}
            if tag_type == "tone"
            else {"count": int(rng.integers(1, 5))}
        )
        tag_rows.append(
            {
                "id": i,
                "raw_record_id": int(rng.integers(1, n_records + 1)),
                "source": "gdelt",
                "tag_type": tag_type,
                "raw_value": raw_value,
                "observed_at": observed,
                "score": score,
            }
        )
    tags = pd.DataFrame(tag_rows, columns=contract.TAG_COLUMNS).sort_values(
        "observed_at", ascending=False, ignore_index=True
    )

    # --- queue stats + worker runs ------------------------------------------
    queue_stats = {"pending": 12, "leased": 3, "done": 1240, "failed": 5}
    run_rows = []
    for r in range(10):
        run_at = end - timedelta(hours=r * 2)
        leased = int(rng.integers(8, 25))
        failed = int(rng.integers(0, 3))
        run_rows.append(
            {
                "run_at": run_at,
                "leased": leased,
                "processed": leased - failed,
                "failed": failed,
                "duration_s": round(float(rng.uniform(1.5, 9.0)), 1),
            }
        )
    worker_runs = pd.DataFrame(run_rows, columns=contract.WORKER_RUN_COLUMNS)

    # --- ingestion rate (last 48h) ------------------------------------------
    rate_rows = []
    for h in range(48):
        hour = end - timedelta(hours=47 - h)
        rate_rows.append({"hour": hour, "records": int(rng.poisson(6))})
    ingestion_rate = pd.DataFrame(rate_rows, columns=contract.INGESTION_RATE_COLUMNS)

    return symbols, observations, raw_records, tags, queue_stats, worker_runs, ingestion_rate


(
    _SYMBOLS_DF,
    _OBS_DF,
    _RECORDS_DF,
    _TAGS_DF,
    _QUEUE_STATS,
    _WORKER_RUNS_DF,
    _INGESTION_RATE_DF,
) = _build()


# --- public data API (matches bellweather.web.data.source) -----------------
def get_tracked_symbols() -> pd.DataFrame:
    return _SYMBOLS_DF.copy()


def get_observations(keys, start=None, end=None) -> pd.DataFrame:
    df = _OBS_DF[_OBS_DF["key"].isin(list(keys))]
    if start is not None:
        df = df[df["ts_bucket"] >= start]
    if end is not None:
        df = df[df["ts_bucket"] <= end]
    return df.sort_values("ts_bucket", ignore_index=True)


def query_raw_records(
    source=None,
    content_type=None,
    status=None,
    search=None,
    start=None,
    end=None,
    limit=100,
    offset=0,
) -> pd.DataFrame:
    df = _RECORDS_DF
    if source:
        df = df[df["source"] == source]
    if content_type:
        df = df[df["content_type"] == content_type]
    if status:
        df = df[df["status"] == status]
    if search:
        df = df[df["idempotency_key"].str.contains(search, case=False, na=False)]
    if start is not None:
        df = df[df["fetched_at"] >= start]
    if end is not None:
        df = df[df["fetched_at"] <= end]
    return df.iloc[offset : offset + limit].reset_index(drop=True)


def query_tags(
    tag_type=None, search=None, start=None, end=None, limit=100, offset=0
) -> pd.DataFrame:
    df = _TAGS_DF
    if tag_type:
        df = df[df["tag_type"] == tag_type]
    if search:
        df = df[df["raw_value"].str.contains(search, case=False, na=False)]
    if start is not None:
        df = df[df["observed_at"] >= start]
    if end is not None:
        df = df[df["observed_at"] <= end]
    return df.iloc[offset : offset + limit].reset_index(drop=True)


def get_queue_stats() -> dict:
    return dict(_QUEUE_STATS)


def get_worker_runs() -> pd.DataFrame:
    return _WORKER_RUNS_DF.copy()


def get_ingestion_rate() -> pd.DataFrame:
    return _INGESTION_RATE_DF.copy()


def get_settings_view() -> list[dict]:
    # Mirrors bellweather.config.Settings fields (mock values; no live config read).
    return [
        {
            "key": "database_url",
            "value": "postgresql://***@cloud-sql/bellweather",
            "note": "Postgres spine (raw index, queue, silver, gold).",
        },
        {
            "key": "bellweather_bucket",
            "value": "bellweather-bronze",
            "note": "GCS bucket for immutable raw bytes.",
        },
        {
            "key": "storage_emulator_host",
            "value": "(unset — real GCS)",
            "note": "Set to fake-gcs only for local tests.",
        },
        {
            "key": "bellweather_api_url",
            "value": "http://localhost:8000",
            "note": "Ingestion API base URL used by the client/producer.",
        },
        {
            "key": "bellweather_obs_bucket",
            "value": "hour",
            "note": "Gold observation bucket granularity (hour | 15min).",
        },
    ]
