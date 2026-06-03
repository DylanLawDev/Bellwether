"""Deterministic, schema-accurate mock data for the UI prototype.

Shapes mirror the six-table Postgres spine (``migrations/0001_initial.sql``) so a
later ``live.py`` can return identical frames from real read-endpoints. The dataset
is built once at import under a fixed seed, so the prototype looks the same on every
launch; query functions return filtered copies.
"""

from __future__ import annotations

import hashlib
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

# Match what the real GDELT path writes (producers/gdelt + GdeltGkgExtractor), so the
# prototype exercises the same source/content_type the live read API would return.
_SOURCE = "gdelt.gkg"
_CONTENT_TYPE = "gdelt-gkg-v2"


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
                "source": _SOURCE,
                "kind": "unstructured",
                "content_type": _CONTENT_TYPE,
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
                "source": _SOURCE,
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
        # regex=False: the UI labels this substring search, so metachars are literal
        # (a "[" must not crash; "." must not match any char).
        df = df[df["idempotency_key"].str.contains(search, case=False, na=False, regex=False)]
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
        df = df[df["raw_value"].str.contains(search, case=False, na=False, regex=False)]
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


# --- producer orchestrator control plane (T26) ------------------------------
# Two fixture templates so the UI's "Add usage" form + preview have a schema to
# render offline. `echo` exercises the structured (numeric-series-v1) path.
_TEMPLATES = [
    {
        "name": "gdelt",
        "description": "GDELT GKG collector (unstructured).",
        "default_interval_seconds": 1800,
        "params": [
            {
                "name": "url",
                "type": "str",
                "required": True,
                "default": None,
                "choices": None,
                "help": "GKG file URL or local path.",
            },
            {
                "name": "backfill",
                "type": "str",
                "required": False,
                "default": "all",
                "choices": ["all", "recent"],
                "help": "How far back to fetch.",
            },
        ],
    },
    {
        "name": "echo",
        "description": "Fixture numeric-series-v1 producer (Phase-1 demo).",
        "default_interval_seconds": 3600,
        "params": [
            {
                "name": "n",
                "type": "int",
                "required": False,
                "default": 1,
                "choices": None,
                "help": "How many points to emit.",
            },
        ],
    },
]

_SCHEDULES_STATE: list[dict] = [
    {
        "id": 1,
        "name": "gdelt-hourly",
        "template": "gdelt",
        "params": {"url": "http://data.gdeltproject.org/...", "backfill": "all"},
        "interval_seconds": 3600,
        "enabled": True,
        "force_run": False,
        "last_run_at": _now_hour() - timedelta(minutes=20),
    }
]
_RUNS_STATE: list[dict] = [
    {
        "id": 1,
        "schedule_id": 1,
        "template": "gdelt",
        "started_at": _now_hour() - timedelta(minutes=20),
        "finished_at": _now_hour() - timedelta(minutes=19),
        "status": "ok",
        "submitted": 412,
        "error": None,
    }
]
_NEXT_ID = {"schedule": 2, "run": 2}


def _schedules_frame() -> pd.DataFrame:
    rows = [{c: s[c] for c in contract.SCHEDULE_COLUMNS} for s in _SCHEDULES_STATE]
    return pd.DataFrame(rows, columns=contract.SCHEDULE_COLUMNS)


def get_schedules() -> pd.DataFrame:
    return _schedules_frame()


def get_templates() -> list[dict]:
    return [dict(t, params=[dict(p) for p in t["params"]]) for t in _TEMPLATES]


def get_runs(schedule_id=None) -> pd.DataFrame:
    rows = [r for r in _RUNS_STATE if schedule_id is None or r["schedule_id"] == schedule_id]
    rows = [{c: r[c] for c in contract.RUN_COLUMNS} for r in rows]
    df = pd.DataFrame(rows, columns=contract.RUN_COLUMNS)
    return df.sort_values("started_at", ascending=False, ignore_index=True) if not df.empty else df


def create_schedule(name, template, params, interval_seconds, enabled=True) -> int:
    sid = _NEXT_ID["schedule"]
    _NEXT_ID["schedule"] += 1
    _SCHEDULES_STATE.append(
        {
            "id": sid,
            "name": name,
            "template": template,
            "params": dict(params),
            "interval_seconds": int(interval_seconds),
            "enabled": bool(enabled),
            "force_run": False,
            "last_run_at": None,
        }
    )
    return sid


def update_schedule(id, **fields) -> None:
    allowed = {"name", "params", "interval_seconds", "enabled"}
    for s in _SCHEDULES_STATE:
        if s["id"] == id:
            s.update({k: v for k, v in fields.items() if k in allowed})


def delete_schedule(id) -> None:
    _SCHEDULES_STATE[:] = [s for s in _SCHEDULES_STATE if s["id"] != id]


def force_schedule(id) -> None:
    for s in _SCHEDULES_STATE:
        if s["id"] == id:
            s["force_run"] = True


def run_orchestrator_now() -> dict:
    # Mimic the orchestrator tick: any enabled schedule that is forced (or never
    # run) gets a recorded run; the claim consumes force_run (resets to False).
    started = []
    now = _now_hour()
    for s in _SCHEDULES_STATE:
        if not s["enabled"]:
            continue
        if s["force_run"] or s["last_run_at"] is None:
            s["force_run"] = False
            s["last_run_at"] = now
            rid = _NEXT_ID["run"]
            _NEXT_ID["run"] += 1
            _RUNS_STATE.append(
                {
                    "id": rid,
                    "schedule_id": s["id"],
                    "template": s["template"],
                    "started_at": now,
                    "finished_at": now,
                    "status": "ok",
                    "submitted": 1,
                    "error": None,
                }
            )
            started.append(rid)
    return {"started_run_ids": started}


def preview_template(name, params) -> dict:
    # Deterministic dry-run shape: one fictitious symbol + a single sample point.
    return {
        "symbols": [f"{name}:demo"],
        "sample": [{"symbol_key": f"{name}:demo", "ts": _now_hour().isoformat(), "value": 0.5}],
    }


# --- scrape/extract split (sources ⟷ extraction specs, M2M) ------------------
# docs/specs/2026-06-03-scrape-extract-split-design.md. Sources own the fetch
# half (sites + adapter → raw captures); extraction specs own the parse half
# (schema + binding + model) and apply to sources via _LINKS_STATE. Captures
# are derived deterministically from (source, url) — no stored capture state.
_SOURCES_STATE: list[dict] = [
    {
        "id": 1,
        "name": "demo-prices",
        "description": "Demo product pages.",
        "sites": ["https://example.com/products/a", "https://example.com/products/b"],
        "fetch_adapter": "httpx",
        "enabled": True,
    },
    {
        "id": 2,
        "name": "fed-speeches",
        "description": "FOMC speeches + testimony.",
        "sites": [
            "https://www.federalreserve.gov/newsevents/speeches.htm",
            "https://www.federalreserve.gov/newsevents/testimony.htm",
        ],
        "fetch_adapter": "httpx",
        "enabled": True,
    },
    {
        "id": 3,
        "name": "weather-alerts",
        "description": "Active NWS severe-weather alerts by region.",
        "sites": [
            "https://www.weather.gov/alerts/west",
            "https://www.weather.gov/alerts/central",
            "https://www.weather.gov/alerts/east",
        ],
        "fetch_adapter": "httpx",
        "enabled": True,
    },
    {
        "id": 4,
        "name": "crypto-funding",
        "description": "Perp funding rates (disabled until rate-limit cleared).",
        "sites": ["https://example-exchange.test/funding/btc-perp"],
        "fetch_adapter": "httpx",
        "enabled": False,
    },
    {
        "id": 5,
        "name": "job-postings",
        "description": "Open-req counts on a few careers pages.",
        "sites": ["https://example-co.test/careers", "https://another-co.test/jobs"],
        "fetch_adapter": "httpx",
        "enabled": True,
    },
]

_EXTRACTION_SPECS_STATE: list[dict] = [
    {
        "id": 1,
        "name": "product-prices",
        "description": "Title + price from product pages.",
        "output_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "price": {"type": "number"}},
        },
        "binding": {
            "symbol_key": "scrape:prices:{title}",
            "symbol_kind": "scraped-metric",
            "value": "$.price",
            "ts": "fetched_at",
            "unit": "usd",
            "tags": ["title"],
        },
        "llm_model": None,
    },
    {
        "id": 2,
        "name": "fed-tone",
        "description": "Hawkish/dovish tone of FOMC remarks.",
        "output_schema": {
            "type": "object",
            "properties": {
                "speaker": {"type": "string"},
                "tone": {"type": "number"},
                "topic": {"type": "string"},
            },
        },
        "binding": {
            "symbol_key": "scrape:fed-tone:{speaker}",
            "symbol_kind": "sentiment",
            "value": "$.tone",
            "ts": "fetched_at",
            "unit": "score",
            "tags": ["speaker", "topic"],
        },
        "llm_model": "claude-haiku-4-5-20251001",
    },
    {
        "id": 3,
        "name": "alert-counts",
        "description": "Active alert counts by region.",
        "output_schema": {
            "type": "object",
            "properties": {"region": {"type": "string"}, "active_alerts": {"type": "number"}},
        },
        "binding": {
            "symbol_key": "scrape:wx-alerts:{region}",
            "symbol_kind": "count",
            "value": "$.active_alerts",
            "ts": "fetched_at",
            "unit": "alerts",
            "tags": ["region"],
        },
        "llm_model": None,
    },
    {
        "id": 4,
        "name": "funding-rate",
        "description": "Perp funding rate per symbol.",
        "output_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "funding_rate": {"type": "number"}},
        },
        "binding": {
            "symbol_key": "scrape:funding:{symbol}",
            "symbol_kind": "rate",
            "value": "$.funding_rate",
            "ts": "fetched_at",
            "unit": "bps",
            "tags": ["symbol"],
        },
        "llm_model": None,
    },
    {
        "id": 5,
        "name": "job-counts",
        "description": "Open-req counts per company.",
        "output_schema": {
            "type": "object",
            "properties": {"company": {"type": "string"}, "open_roles": {"type": "number"}},
        },
        "binding": {
            "symbol_key": "scrape:hiring:{company}",
            "symbol_kind": "count",
            "value": "$.open_roles",
            "ts": "fetched_at",
            "unit": "roles",
            "tags": ["company"],
        },
        "llm_model": None,
    },
    {
        "id": 6,
        "name": "page-sentiment",
        "description": "Overall page sentiment (reusable across sources).",
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "sentiment": {"type": "number"}},
        },
        "binding": {
            "symbol_key": "scrape:sentiment:{summary}",
            "symbol_kind": "sentiment",
            "value": "$.sentiment",
            "ts": "fetched_at",
            "unit": "score",
            "tags": ["summary"],
        },
        "llm_model": None,
    },
]

# (source_name, extractor_name) pairs — the M2M junction. page-sentiment applies
# to two sources so the many-to-many is visible in the fixtures.
_LINKS_STATE: list[tuple[str, str]] = [
    ("demo-prices", "product-prices"),
    ("demo-prices", "page-sentiment"),
    ("fed-speeches", "fed-tone"),
    ("fed-speeches", "page-sentiment"),
    ("weather-alerts", "alert-counts"),
    ("crypto-funding", "funding-rate"),
    ("job-postings", "job-counts"),
]
_NEXT_SPLIT_ID = {"source": 6, "extractor": 7}

# Registered fetch adapters offered in the Edit form's dropdown. The live
# backend reads these from GET /api/fetch-adapters; offline we mirror the one
# adapter the registry ships with.
_FETCH_ADAPTERS = ["httpx"]


def get_fetch_adapter_choices() -> list[str]:
    return list(_FETCH_ADAPTERS)


def _url_value(url: str) -> float:
    """Stable 5.00–14.99 pseudo-value per url (sha1-seeded, test-friendly)."""
    seed = int(hashlib.sha1(url.encode()).hexdigest()[:6], 16)
    return round(5 + (seed % 1000) / 100.0, 2)


def _url_slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1] or "root"


# Per-source raw-capture templates: (content_type, format template). Captures
# are what scraping *produces*; the Extract page parses them. Doubled braces
# escape str.format in the JSON template.
_CAPTURE_TEMPLATES: dict[str, tuple[str, str]] = {
    "demo-prices": (
        "text/html",
        '<html><body>\n  <h1>{slug}</h1>\n  <span class="price">${value}</span>\n'
        "  <p>In stock. Ships in 2 days.</p>\n</body></html>",
    ),
    "fed-speeches": (
        "text/markdown",
        "# Remarks: {slug}\n\n*Federal Reserve* — prepared text.\n\n"
        "- inflation outlook\n- labor markets\n- tone reading: {value}\n",
    ),
    "weather-alerts": (
        "text/html",
        "<table>\n  <tr><th>region</th><th>active</th></tr>\n"
        "  <tr><td>{slug}</td><td>{value}</td></tr>\n</table>",
    ),
    "crypto-funding": (
        "application/json",
        '{{"symbol": "{slug}", "funding_rate": {value}}}',
    ),
    "job-postings": (
        "text/html",
        '<ul class="openings">\n  <li>Engineer — {slug}</li>\n'
        "  <li>Analyst — {slug}</li>\n</ul>\n<!-- open_roles: {value} -->",
    ),
}
_GENERIC_CAPTURE = (
    "text/html",
    "<html><body>\n  <h1>{slug}</h1>\n  <p>value: {value}</p>\n</body></html>",
)


def _capture(source_name: str, url: str) -> dict:
    ctype, tpl = _CAPTURE_TEMPLATES.get(source_name, _GENERIC_CAPTURE)
    content = tpl.format(slug=_url_slug(url), value=_url_value(url))
    return {
        "url": url,
        "captured_at": _now_hour().isoformat(),
        "content_type": ctype,
        "size_bytes": len(content),
        "content": content,
    }


def get_scrape_sources() -> pd.DataFrame:
    rows = [{c: s[c] for c in contract.SCRAPE_SOURCE_COLUMNS} for s in _SOURCES_STATE]
    return pd.DataFrame(rows, columns=contract.SCRAPE_SOURCE_COLUMNS)


def get_scrape_source(name) -> dict | None:
    for s in _SOURCES_STATE:
        if s["name"] == name:
            return {
                **s,
                "sites": list(s["sites"]),
                "parsed_by": sorted(ex for (src, ex) in _LINKS_STATE if src == name),
            }
    return None


def create_scrape_source(name, sites, *, description=None, fetch_adapter="httpx") -> int:
    sid = _NEXT_SPLIT_ID["source"]
    _NEXT_SPLIT_ID["source"] += 1
    _SOURCES_STATE.append(
        {
            "id": sid,
            "name": name,
            "description": description,
            "sites": list(sites),
            "fetch_adapter": fetch_adapter,
            "enabled": True,
        }
    )
    return sid


def update_scrape_source(name, **fields) -> None:
    allowed = {"description", "sites", "fetch_adapter", "enabled"}
    for s in _SOURCES_STATE:
        if s["name"] == name:
            s.update({k: v for k, v in fields.items() if k in allowed})


def delete_scrape_source(name) -> None:
    _SOURCES_STATE[:] = [s for s in _SOURCES_STATE if s["name"] != name]
    _LINKS_STATE[:] = [(src, ex) for (src, ex) in _LINKS_STATE if src != name]


def get_extraction_specs() -> pd.DataFrame:
    rows = [{c: e[c] for c in contract.EXTRACTION_SPEC_COLUMNS} for e in _EXTRACTION_SPECS_STATE]
    return pd.DataFrame(rows, columns=contract.EXTRACTION_SPEC_COLUMNS)


def get_extraction_spec(name) -> dict | None:
    for e in _EXTRACTION_SPECS_STATE:
        if e["name"] == name:
            return {
                **e,
                "output_schema": dict(e["output_schema"]),
                "binding": dict(e["binding"]),
                "sources": sorted(src for (src, ex) in _LINKS_STATE if ex == name),
            }
    return None


def create_extraction_spec(
    name, output_schema, binding, *, description=None, llm_model=None, sources=()
) -> int:
    eid = _NEXT_SPLIT_ID["extractor"]
    _NEXT_SPLIT_ID["extractor"] += 1
    _EXTRACTION_SPECS_STATE.append(
        {
            "id": eid,
            "name": name,
            "description": description,
            "output_schema": dict(output_schema),
            "binding": dict(binding),
            "llm_model": llm_model,
        }
    )
    _LINKS_STATE.extend((src, name) for src in sources)
    return eid


def update_extraction_spec(name, **fields) -> None:
    allowed = {"description", "output_schema", "binding", "llm_model"}
    for e in _EXTRACTION_SPECS_STATE:
        if e["name"] == name:
            e.update({k: v for k, v in fields.items() if k in allowed})
    if "sources" in fields:
        _LINKS_STATE[:] = [(src, ex) for (src, ex) in _LINKS_STATE if ex != name]
        _LINKS_STATE.extend((src, name) for src in fields["sources"])


def delete_extraction_spec(name) -> None:
    _EXTRACTION_SPECS_STATE[:] = [e for e in _EXTRACTION_SPECS_STATE if e["name"] != name]
    _LINKS_STATE[:] = [(src, ex) for (src, ex) in _LINKS_STATE if ex != name]


def get_captures(source_name) -> pd.DataFrame:
    src = get_scrape_source(source_name)
    sites = (src or {}).get("sites") or []
    rows = [{c: _capture(source_name, u)[c] for c in contract.CAPTURE_COLUMNS} for u in sites]
    return pd.DataFrame(rows, columns=contract.CAPTURE_COLUMNS)


def get_capture(source_name, url) -> dict | None:
    src = get_scrape_source(source_name)
    if src is None or url not in src["sites"]:
        return None
    return _capture(source_name, url)


def fetch_capture_now(source_name, url) -> dict:
    # Mock "re-fetch": same deterministic content, captured_at refreshed to the
    # current hour (identical to get_capture, so the page can swap them freely).
    return _capture(source_name, url)


def preview_extraction(extractor_name, source_name, url) -> dict:
    # Deterministic dry-run of one extractor over one capture (no fetch, commits
    # nothing): numeric schema props get the url's stable value, string props the
    # url slug; symbols derive from the binding's symbol_key prefix so different
    # extractors emit different symbol spaces over the same capture.
    spec = get_extraction_spec(extractor_name) or {}
    value, slug = _url_value(url), _url_slug(url)
    props = spec.get("output_schema", {}).get("properties", {})
    extracted: dict = {}
    for key, prop in props.items():
        if prop.get("type") == "number":
            extracted[key] = value
        elif prop.get("type") == "string":
            extracted[key] = slug
        else:
            extracted[key] = True
    prefix = spec.get("binding", {}).get("symbol_key", "scrape:demo:{x}").split("{")[0]
    symbol = f"{prefix.rstrip(':')}:{slug}"
    tags = [
        {"tag_type": t, "raw_value": str(extracted.get(t, slug))}
        for t in spec.get("binding", {}).get("tags", [])
    ]
    return {
        "extracted": extracted,
        "symbols": [symbol],
        "sample": [{"symbol_key": symbol, "ts": _now_hour().isoformat(), "value": value}],
        "tags": tags,
    }
