# T18 — Gold value write — `upsert_value` (set-semantics)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§6.4 Gold value write; K7 / D1 last-value-wins).
**Depends on:** T11. **Branch:** `ticket/T18-gold-value-write`. **PR, do not merge without approval.**

## Goal
Add `gold.upsert_value(...)` — the gold write for the structured (numeric) path. It ensures a
`tracked_symbols` row for a symbol key (filling `kind`/`unit`/`description`), buckets the
timestamp by the global `bellweather_obs_bucket`, and writes one `observations` row with
**set-semantics** (`value = excluded.value`, `sample_count = excluded.sample_count`). Unlike the
neighbouring `upsert_coverage` (which *increments*), this is idempotent / last-value-wins, so a
replayed structured record re-sets the same value rather than double-counting. Like every other DB
helper, it **never commits** — the caller owns the transaction.

## Files
- Modify: `src/bellweather/gold.py` — add `upsert_value(...)` alongside `bucket_ts` /
  `upsert_coverage`. No new imports beyond what the module already has (`datetime`,
  `get_settings`).
- Test: `tests/test_gold_value.py` — DB-backed test (needs `make up` + `make migrate`).

## Interface
Copied verbatim from the build plan's "Locked interfaces" (`gold.py`):
```python
def upsert_value(conn, symbol_key: str, symbol_kind: str, ts: datetime, value: float,
                 *, unit: str | None = None, description: str | None = None,
                 sample_count: int = 1) -> int:
    # 1. insert into tracked_symbols(key,kind,unit,description) on conflict (key)
    #    do update set kind=excluded.kind,
    #                  unit=coalesce(excluded.unit, tracked_symbols.unit),
    #                  description=coalesce(excluded.description, tracked_symbols.description)
    #    returning id
    # 2. bucket = bucket_ts(ts, get_settings().bellweather_obs_bucket)
    # 3. insert into observations(tracked_symbol_id,ts_bucket,value,sample_count) values(...)
    #    on conflict (tracked_symbol_id, ts_bucket)
    #    do update set value = excluded.value, sample_count = excluded.sample_count
    # returns tracked_symbol id. NEVER commits (caller owns txn).
```
Existing schema this writes into (`migrations/0001_initial.sql`, no migration needed):
`tracked_symbols(id, key unique, kind, entity_id, unit, description)` and
`observations(tracked_symbol_id, ts_bucket, value, sample_count, primary key (tracked_symbol_id, ts_bucket))`.

## Steps

- [ ] **Step 0: Bring up infra.** `make up` (Postgres 16 + fake-gcs) then `make migrate` (applies
  the existing 6-table schema; this ticket adds **no** new migration — `tracked_symbols` and
  `observations` already exist).

- [ ] **Step 1: Failing test** `tests/test_gold_value.py` — three DB cases: a fresh value lands an
  observations row + creates the tracked_symbol with `kind`/`unit`/`description`; re-upserting the
  same `(symbol, bucket)` with a new value **replaces** it (not incremented) and sets
  `sample_count`; bucketing matches the configured granularity. Use a unique symbol key + a
  fixture that clears it so the test is order-independent and re-runnable.
```python
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
            c, _KEY, "market-probability", ts, 0.37,
            unit="probability", description="Will X happen by D? (YES)", sample_count=1,
        )
        c.commit()
        kind, unit, desc = c.execute(
            "select kind, unit, description from tracked_symbols where id = %s", (sym_id,)
        ).fetchone()
        assert (kind, unit, desc) == (
            "market-probability", "probability", "Will X happen by D? (YES)",
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
```

- [ ] **Step 2: Run → FAIL** (with `make up`/`make migrate` already done):
  `uv run pytest tests/test_gold_value.py -v` → `ImportError: cannot import name 'upsert_value'`.

- [ ] **Step 3: Implement** `upsert_value` in `src/bellweather/gold.py`, after `upsert_coverage`.
  Verbatim from the locked interface — set-semantics on the observation, `coalesce` so a later
  call that omits `unit`/`description` does not blank an existing value:
```python
def upsert_value(
    conn,
    symbol_key: str,
    symbol_kind: str,
    ts: datetime,
    value: float,
    *,
    unit: str | None = None,
    description: str | None = None,
    sample_count: int = 1,
) -> int:
    sym_id = conn.execute(
        """insert into tracked_symbols(key, kind, unit, description)
           values (%s, %s, %s, %s)
           on conflict (key) do update
             set kind = excluded.kind,
                 unit = coalesce(excluded.unit, tracked_symbols.unit),
                 description = coalesce(excluded.description, tracked_symbols.description)
           returning id""",
        (symbol_key, symbol_kind, unit, description),
    ).fetchone()[0]
    bucket = bucket_ts(ts, get_settings().bellweather_obs_bucket)
    conn.execute(
        """insert into observations(tracked_symbol_id, ts_bucket, value, sample_count)
           values (%s, %s, %s, %s)
           on conflict (tracked_symbol_id, ts_bucket)
           do update set value = excluded.value,
                         sample_count = excluded.sample_count""",
        (sym_id, bucket, value, sample_count),
    )
    return sym_id
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_gold_value.py -v` → 3 passed.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green
  with `make up` running.

- [ ] **Step 6: Commit** (`feat: add gold.upsert_value set-semantics gold write`).

## Acceptance criteria
- `upsert_value(conn, symbol_key, symbol_kind, ts, value, *, unit=None, description=None, sample_count=1) -> int`
  exactly matches the locked signature and returns the `tracked_symbols.id`.
- A fresh call creates the `tracked_symbols` row (with `kind`/`unit`/`description`) and one
  `observations` row at `bucket_ts(ts, get_settings().bellweather_obs_bucket)` carrying that value.
- Re-upserting the same `(symbol, bucket)` **replaces** `value` and **sets** `sample_count` (last
  value wins; no increment, no extra row) — idempotent by construction (K7 / D1).
- Two timestamps inside one configured bucket collapse to a single row (last write wins).
- `upsert_value` never commits (caller owns the txn); `unit`/`description` use `coalesce` so a
  later call omitting them does not blank existing metadata.
- No new migration; `make check` green.
