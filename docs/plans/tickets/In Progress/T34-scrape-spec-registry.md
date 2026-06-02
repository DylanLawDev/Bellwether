# T34 — Scrape-spec registry: migration 0003 + `scrape/specs.py`

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§5 The scrape-spec registry; K2/K3). **Depends on:** T01 (config), T02 (db + migrate). **Branch:** `ticket/T34-scrape-spec-registry`. **PR, do not merge without approval.**

## Goal
Add the scrape engine's control-plane state: a forward-only migration that creates the `scrape_specs` table, and a new `scrape/specs.py` helper module with CRUD over it (`list_specs`/`get_spec`/`create_spec`/`update_spec`/`delete_spec`). A scrape spec is the **data** that parameterizes the one generic `LlmScrapeExtractor` (K3) — a `{sites, output_schema, binding}` triple keyed by a unique `name`, referenced from a record's `provenance.scrape_spec`. Keeping per-source mapping as a UI-editable table row instead of bespoke Python (K2/K3) is the whole point. Like `schedules.py`, `queue.py`, and `reads.py`, every helper takes a psycopg `Connection`, runs parameterized SQL, returns `dict`/`list` shapes via `dict_row`, wraps the jsonb columns (`sites`/`output_schema`/`binding`) with `psycopg.types.json.Json`, and **never commits** — the caller owns the transaction.

## Files
- Create: `src/bellweather/migrations/0003_scrape_specs.sql` — the `scrape_specs` table (after the orchestrator's `0002`).
- Create: `src/bellweather/scrape/__init__.py` — package marker.
- Create: `src/bellweather/scrape/specs.py` — `scrape_specs` CRUD helpers (never commit).
- Test: `tests/test_scrape_specs.py` — DB-backed round-trip test (needs `make up` + `make migrate`).

## Interface
Migration (`migrations/0003_scrape_specs.sql`) — copied verbatim from the build plan's "Locked interfaces":
```sql
create table if not exists scrape_specs (
  id            bigserial primary key,
  name          text not null unique,        -- referenced by a record's provenance.scrape_spec
  description   text,
  sites         jsonb not null default '[]'::jsonb,   -- list of URLs
  output_schema jsonb not null,              -- JSON Schema → LLM tool input_schema
  binding       jsonb not null,              -- see binding contract below
  fetch_adapter text not null default 'httpx',
  llm_model     text,                        -- per-spec model override; null → settings default
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
```

`scrape/specs.py` — locked signatures copied verbatim from the build plan (never commit; caller owns the txn; `dict_row` shapes; `sites`/`output_schema`/`binding` come back as Python `list`/`dict` via psycopg jsonb adaption):
```python
def list_specs(conn) -> list[dict]: ...
def get_spec(conn, name: str) -> dict | None: ...
def create_spec(conn, *, name: str, sites: list, output_schema: dict, binding: dict,
                description: str | None = None, fetch_adapter: str = "httpx",
                llm_model: str | None = None, enabled: bool = True) -> int: ...   # returns id
def update_spec(conn, name: str, **fields) -> None: ...   # name|description|sites|output_schema|
                                                          # binding|fetch_adapter|llm_model|enabled; bumps updated_at
def delete_spec(conn, name: str) -> None: ...
```

## Steps

- [ ] **Step 0: Bring up infra.** `make up` (Postgres 16 + fake-gcs) then `make migrate`. This ticket adds the new `0003_scrape_specs.sql` migration — after you create it in Step 3 you re-run `make migrate` (the test's `_migrated` fixture also calls `apply_migrations()`, which auto-discovers `*.sql` in sorted order and applies `0003` after `0002`).

- [ ] **Step 1: Failing test** `tests/test_scrape_specs.py` — with the local Postgres up (`make up`) and migrations applied (`make migrate`). Each case inserts spec rows under stable, test-owned names and the `_migrated` fixture deletes them by name first, so the test is order-independent and re-runnable. Cover the full round-trip: `create_spec` → `get_spec` round-trips the nested JSON (`sites` a `list`, `output_schema`/`binding` `dict`s); `list_specs` includes the created spec; `update_spec` changes a field and bumps `updated_at`; `delete_spec` removes it; a duplicate `name` raises `UniqueViolation`.
```python
import time

import psycopg
import pytest

from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from bellweather.scrape import specs

# Test-owned spec names; the fixture clears exactly these before each test so
# assertions never collide with other rows and the test re-runs cleanly.
_NAMES = (
    "t34-prices",
    "t34-renamed",
    "t34-list-a",
    "t34-list-b",
    "t34-dup",
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                },
                "required": ["name", "price"],
            },
        }
    },
    "required": ["items"],
}

_BINDING = {
    "records_path": "$.items",
    "symbol_key": "scrape:prices:{name}",
    "symbol_kind": "scraped-metric",
    "value": "$.price",
    "ts": "fetched_at",
    "unit": "usd",
    "tags": ["name"],
}


@pytest.fixture(autouse=True)
def _migrated():
    # Applies forward-only migrations (incl. 0003_scrape_specs). Clears this
    # test's spec rows by name up front so order/re-runs are deterministic.
    apply_migrations()
    with get_conn() as conn:
        conn.execute("delete from scrape_specs where name = any(%s)", (list(_NAMES),))
        conn.commit()


def test_create_get_roundtrips_nested_json():
    with get_conn() as conn:
        spec_id = specs.create_spec(
            conn,
            name="t34-prices",
            sites=["https://example.com/a", "https://example.com/b"],
            output_schema=_SCHEMA,
            binding=_BINDING,
            description="example prices",
        )
        conn.commit()
        assert isinstance(spec_id, int)
        row = specs.get_spec(conn, "t34-prices")
        assert row["id"] == spec_id
        assert row["name"] == "t34-prices"
        assert row["description"] == "example prices"
        # jsonb columns adapt back to native Python list/dict.
        assert row["sites"] == ["https://example.com/a", "https://example.com/b"]
        assert isinstance(row["sites"], list)
        assert row["output_schema"] == _SCHEMA
        assert isinstance(row["output_schema"], dict)
        assert row["binding"] == _BINDING
        assert isinstance(row["binding"], dict)
        assert row["binding"]["records_path"] == "$.items"
        # defaults
        assert row["fetch_adapter"] == "httpx"
        assert row["llm_model"] is None
        assert row["enabled"] is True
        assert row["created_at"] is not None
        assert row["updated_at"] is not None


def test_get_missing_returns_none():
    with get_conn() as conn:
        assert specs.get_spec(conn, "t34-does-not-exist") is None


def test_list_specs_includes_created():
    with get_conn() as conn:
        specs.create_spec(
            conn, name="t34-list-a", sites=[], output_schema=_SCHEMA, binding=_BINDING
        )
        specs.create_spec(
            conn, name="t34-list-b", sites=[], output_schema=_SCHEMA, binding=_BINDING
        )
        conn.commit()
        names = {s["name"] for s in specs.list_specs(conn)}
        assert {"t34-list-a", "t34-list-b"} <= names


def test_update_changes_field_and_bumps_updated_at():
    with get_conn() as conn:
        specs.create_spec(
            conn, name="t34-renamed", sites=["https://x"], output_schema=_SCHEMA,
            binding=_BINDING,
        )
        conn.commit()
        before = specs.get_spec(conn, "t34-renamed")["updated_at"]
        time.sleep(0.01)
        specs.update_spec(
            conn,
            "t34-renamed",
            description="now described",
            sites=["https://y", "https://z"],
            enabled=False,
            llm_model="claude-haiku-4-5-20251001",
        )
        conn.commit()
        row = specs.get_spec(conn, "t34-renamed")
        assert row["description"] == "now described"
        assert row["sites"] == ["https://y", "https://z"]
        assert row["enabled"] is False
        assert row["llm_model"] == "claude-haiku-4-5-20251001"
        assert row["updated_at"] > before


def test_update_with_no_fields_is_noop():
    with get_conn() as conn:
        specs.create_spec(
            conn, name="t34-prices", sites=[], output_schema=_SCHEMA, binding=_BINDING
        )
        conn.commit()
        specs.update_spec(conn, "t34-prices")  # no fields -> no-op, no error
        conn.commit()
        assert specs.get_spec(conn, "t34-prices")["name"] == "t34-prices"


def test_delete_spec():
    with get_conn() as conn:
        specs.create_spec(
            conn, name="t34-prices", sites=[], output_schema=_SCHEMA, binding=_BINDING
        )
        conn.commit()
        specs.delete_spec(conn, "t34-prices")
        conn.commit()
        assert specs.get_spec(conn, "t34-prices") is None


def test_duplicate_name_raises():
    with get_conn() as conn:
        specs.create_spec(
            conn, name="t34-dup", sites=[], output_schema=_SCHEMA, binding=_BINDING
        )
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            specs.create_spec(
                conn, name="t34-dup", sites=[], output_schema=_SCHEMA, binding=_BINDING
            )
        conn.rollback()
```

- [ ] **Step 2: Run → FAIL** (no `0003` applied / no `scrape` package):
  `uv run pytest tests/test_scrape_specs.py -v`. Expect a `ModuleNotFoundError: No module named 'bellweather.scrape'` (and, once the package exists but the migration is not applied, a `psycopg.errors.UndefinedTable: relation "scrape_specs" does not exist`).

- [ ] **Step 3: Implement.**

  First, the migration `src/bellweather/migrations/0003_scrape_specs.sql` — paste the SQL from the Interface section verbatim. The runner (`bellweather.migrate.apply_migrations`) auto-discovers `*.sql` in sorted order, so the `0003_` prefix sequences it after `0002_orchestrator.sql`. Re-run `make migrate` after creating it. The jsonb columns (`sites`/`output_schema`/`binding`) must be passed from Python as JSON via `psycopg.types.json.Json`, so `specs.py` wraps them with `Json(...)`.
```sql
create table if not exists scrape_specs (
  id            bigserial primary key,
  name          text not null unique,        -- referenced by a record's provenance.scrape_spec
  description   text,
  sites         jsonb not null default '[]'::jsonb,   -- list of URLs
  output_schema jsonb not null,              -- JSON Schema → LLM tool input_schema
  binding       jsonb not null,              -- see binding contract below
  fetch_adapter text not null default 'httpx',
  llm_model     text,                        -- per-spec model override; null → settings default
  enabled       boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
```

  Next, the package marker `src/bellweather/scrape/__init__.py` — empty file:
```python
```

  Then `src/bellweather/scrape/specs.py` — mirror `schedules.py` exactly (helpers take `conn`, `dict_row` via private `_rows`/`_one`, never commit). Wrap the three jsonb columns with `psycopg.types.json.Json` on write; `update_spec` builds a whitelisted dynamic SET clause and always bumps `updated_at`:
```python
"""Scrape-spec registry (the LLM scrape engine's control-plane state).

CRUD over ``scrape_specs`` — the ``{sites, output_schema, binding}`` triple,
keyed by a unique ``name``, that parameterizes the one generic
``LlmScrapeExtractor`` (K3). Mirrors the repo conventions: every helper takes a
psycopg ``Connection``, runs parameterized SQL, returns ``dict``/``list`` shapes
via ``dict_row``, wraps the jsonb columns with ``Json``, and **never commits** —
the caller owns the transaction (see ``schedules.py`` and ``queue.py``).
"""

from __future__ import annotations

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json

# Columns update_spec() may set. The three jsonb columns are wrapped with Json()
# on write; everything else binds as-is.
_UPDATABLE = {
    "name",
    "description",
    "sites",
    "output_schema",
    "binding",
    "fetch_adapter",
    "llm_model",
    "enabled",
}
_JSONB = {"sites", "output_schema", "binding"}

_COLUMNS = """
    id, name, description, sites, output_schema, binding,
    fetch_adapter, llm_model, enabled, created_at, updated_at
"""


def _rows(conn: Connection, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _one(conn: Connection, sql: str, params: tuple = ()) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchone()


def list_specs(conn: Connection) -> list[dict]:
    return _rows(conn, f"select {_COLUMNS} from scrape_specs order by id")


def get_spec(conn: Connection, name: str) -> dict | None:
    return _one(conn, f"select {_COLUMNS} from scrape_specs where name = %s", (name,))


def create_spec(
    conn: Connection,
    *,
    name: str,
    sites: list,
    output_schema: dict,
    binding: dict,
    description: str | None = None,
    fetch_adapter: str = "httpx",
    llm_model: str | None = None,
    enabled: bool = True,
) -> int:
    return conn.execute(
        """
        insert into scrape_specs
            (name, description, sites, output_schema, binding,
             fetch_adapter, llm_model, enabled)
        values (%s, %s, %s, %s, %s, %s, %s, %s) returning id
        """,
        (
            name,
            description,
            Json(sites),
            Json(output_schema),
            Json(binding),
            fetch_adapter,
            llm_model,
            enabled,
        ),
    ).fetchone()[0]


def update_spec(conn: Connection, name: str, **fields) -> None:
    cols = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not cols:
        return
    sets = []
    vals: list = []
    for k, v in cols.items():
        sets.append(f"{k} = %s")
        vals.append(Json(v) if k in _JSONB else v)
    sets.append("updated_at = now()")
    vals.append(name)
    conn.execute(
        f"update scrape_specs set {', '.join(sets)} where name = %s",
        tuple(vals),
    )


def delete_spec(conn: Connection, name: str) -> None:
    conn.execute("delete from scrape_specs where name = %s", (name,))
```

- [ ] **Step 4: Run → PASS.** With `make up` running and `0003` applied (`make migrate`):
  `uv run pytest tests/test_scrape_specs.py -v`. All cases green.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green with `make up` running so the DB tests execute rather than erroring.

- [ ] **Step 6: Commit** (`feat: scrape-spec registry — migration 0003 + scrape/specs.py`).

## Acceptance criteria
- `migrations/0003_scrape_specs.sql` creates `scrape_specs` exactly as locked in the build plan — `name text not null unique`, `sites`/`output_schema`/`binding` jsonb (sites defaulting `'[]'::jsonb`, schema/binding `not null`), `fetch_adapter` defaulting `'httpx'`, nullable `llm_model`, `enabled` default true, and `created_at`/`updated_at` timestamps — and is auto-applied in order after `0002` by `apply_migrations()`.
- `scrape/specs.py` exposes every locked signature; helpers take a `conn`, return `dict`/`list` shapes via `dict_row`, and **never commit** (caller owns the txn), mirroring `schedules.py`/`queue.py`.
- `create_spec` → `get_spec` round-trips `sites` as a Python `list` and `output_schema`/`binding` as `dict`s (psycopg jsonb adaption), and defaults `fetch_adapter="httpx"`, `llm_model=None`, `enabled=true`.
- `list_specs` returns the created specs ordered by `id`; `get_spec` returns `None` for an unknown name.
- `update_spec` whitelists the locked fields, wraps the three jsonb columns with `Json`, no-ops on empty, and bumps `updated_at`; `delete_spec` removes the row by name.
- A duplicate `name` raises `psycopg.errors.UniqueViolation` (the `unique` constraint holds).
- `make check` is green (DB tests require `make up` + the `0003` migration).
