# T02 — Migration runner + initial schema (6 tables)

**Spec:** `docs/specs/2026-05-31-ingestor-extractor-design.md` (§6 Data model)
**Depends on:** T01. **Branch:** `ticket/T02-migrations`. **PR, do not merge without approval.**

## Goal
A dead-simple, forward-only SQL migration runner, plus migration `0001` creating the six spine tables exactly as the spec defines them.

## Files
- Create: `src/bellweather/migrate.py`, `src/bellweather/migrations/0001_initial.sql`
- Add CLI command later (T07) — for now `bellweather migrate` may be wired via a tiny typer stub if needed; tests call `apply_migrations()` directly.
- Test: `tests/test_migrate.py`

## Interface
```python
# migrate.py
def apply_migrations() -> list[str]: ...  # returns names of migrations newly applied
```

## Steps

- [ ] **Step 1: Failing test** `tests/test_migrate.py` (requires `make up`)
```python
from bellweather.migrate import apply_migrations
from bellweather.db import get_conn

def test_migrations_create_tables_and_are_idempotent():
    apply_migrations()
    second = apply_migrations()           # already applied → no-op
    assert second == []
    with get_conn() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema='public'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert {"raw_records", "work_queue", "tags", "entities",
            "tracked_symbols", "observations", "schema_migrations"} <= names
```
- [ ] **Step 2: Run it** → FAIL.

- [ ] **Step 3: Write `migrations/0001_initial.sql`** (exact schema)
```sql
create table if not exists schema_migrations (
  name text primary key,
  applied_at timestamptz not null default now()
);

create table if not exists raw_records (
  id            bigserial primary key,
  source        text not null,
  kind          text not null check (kind in ('unstructured','structured')),
  content_type  text not null,
  idempotency_key text not null,
  payload_uri   text not null,
  fetched_at    timestamptz not null,
  ingested_at   timestamptz not null default now(),
  provenance    jsonb not null default '{}'::jsonb,
  status        text not null default 'received'
                check (status in ('received','processed','unroutable','failed')),
  unique (source, idempotency_key)
);

create table if not exists work_queue (
  id           bigserial primary key,
  raw_record_id bigint not null references raw_records(id),
  state        text not null default 'pending'
               check (state in ('pending','leased','done','failed')),
  attempts     int not null default 0,
  lease_until  timestamptz not null default now(),
  last_error   text,
  enqueued_at  timestamptz not null default now()
);
create index if not exists work_queue_pending_idx
  on work_queue (state, lease_until) where state = 'pending';

create table if not exists entities (
  id             bigserial primary key,
  canonical_name text not null,
  entity_type    text not null,
  aliases        jsonb not null default '[]'::jsonb,
  is_tracked_symbol boolean not null default false,
  unique (canonical_name, entity_type)
);

create table if not exists tags (
  id            bigserial primary key,
  raw_record_id bigint not null references raw_records(id),
  source        text not null,
  observed_at   timestamptz not null,
  tag_type      text not null,
  raw_value     text not null,
  canonical_entity_id bigint references entities(id),
  score         jsonb not null default '{}'::jsonb
);
create index if not exists tags_type_value_idx on tags (tag_type, raw_value);
create index if not exists tags_observed_idx on tags (observed_at);

create table if not exists tracked_symbols (
  id          bigserial primary key,
  key         text not null unique,
  kind        text not null,
  entity_id   bigint references entities(id),
  unit        text,
  description text
);

create table if not exists observations (
  tracked_symbol_id bigint not null references tracked_symbols(id),
  ts_bucket   timestamptz not null,
  value       double precision not null,
  sample_count int not null default 0,
  primary key (tracked_symbol_id, ts_bucket)
);
```

- [ ] **Step 4: Implement `migrate.py`**
```python
from pathlib import Path
from bellweather.db import get_conn

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

def apply_migrations() -> list[str]:
    applied: list[str] = []
    with get_conn() as conn:
        conn.execute(
            "create table if not exists schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        conn.commit()
        done = {r[0] for r in conn.execute("select name from schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("insert into schema_migrations(name) values (%s)", (path.name,))
            conn.commit()
            applied.append(path.name)
    return applied
```
- [ ] **Step 5: Run it** → PASS (idempotent re-run returns `[]`). Commit (`feat: add migration runner + initial schema`).

## Acceptance criteria
- All 7 tables (6 spine + `schema_migrations`) exist after `apply_migrations()`.
- Re-running is a no-op (forward-only, tracked in `schema_migrations`).
- `make check` green with `make up`.
