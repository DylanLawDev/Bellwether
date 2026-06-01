# T01 — Settings + Postgres connection pool

**Spec:** `docs/superpowers/specs/2026-05-31-ingestor-extractor-design.md` (§8 Tech)
**Depends on:** T00. **Branch:** `ticket/T01-config-db`. **PR, do not merge without approval.**

## Goal
Centralize all configuration in one env-driven `Settings` object and provide a shared Postgres connection pool. Every later module imports from here; nothing reads `os.environ` directly.

## Files
- Create: `src/bellweather/config.py`, `src/bellweather/db.py`
- Test: `tests/test_config.py`, `tests/test_db.py`

## Interfaces (referenced by exact name in later tickets)
```python
# config.py
class Settings(BaseSettings):
    database_url: str
    bellweather_bucket: str
    storage_emulator_host: str | None = None
    bellweather_api_url: str = "http://localhost:8000"
    bellweather_obs_bucket: Literal["hour", "15min"] = "hour"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

def get_settings() -> Settings: ...   # cached singleton

# db.py
def get_pool() -> ConnectionPool: ...        # cached psycopg_pool.ConnectionPool
@contextmanager
def get_conn() -> Iterator[Connection]: ...   # borrow a connection, autocommit=False
```

## Steps

- [ ] **Step 1: Failing test for settings** `tests/test_config.py`
```python
from bellweather.config import get_settings

def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("BELLWEATHER_BUCKET", "b")
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url == "postgresql://x/y"
    assert s.bellweather_obs_bucket == "hour"
```
- [ ] **Step 2: Run it** → FAIL (module missing). `uv run pytest tests/test_config.py -v`
- [ ] **Step 3: Implement `config.py`**
```python
from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str
    bellweather_bucket: str
    storage_emulator_host: str | None = None
    bellweather_api_url: str = "http://localhost:8000"
    bellweather_obs_bucket: Literal["hour", "15min"] = "hour"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```
- [ ] **Step 4: Run it** → PASS. Commit (`feat: add env-driven Settings`).

- [ ] **Step 5: Failing test for the pool** `tests/test_db.py` (needs `make up`)
```python
from bellweather.db import get_conn

def test_can_select_one():
    with get_conn() as conn:
        cur = conn.execute("select 1")
        assert cur.fetchone()[0] == 1
```
- [ ] **Step 6: Run it** → FAIL.
- [ ] **Step 7: Implement `db.py`**
```python
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator
from psycopg import Connection
from psycopg_pool import ConnectionPool
from bellweather.config import get_settings

@lru_cache
def get_pool() -> ConnectionPool:
    return ConnectionPool(get_settings().database_url, min_size=1, max_size=8, open=True)

@contextmanager
def get_conn() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn
```
- [ ] **Step 8: Run it** (after `make up`) → PASS. Commit (`feat: add Postgres connection pool`).

## Acceptance criteria
- `make check` green (with `make up` running for the db test).
- No module other than `config.py` reads environment variables.
- `get_conn()` yields a working connection from a pooled, cached pool.
