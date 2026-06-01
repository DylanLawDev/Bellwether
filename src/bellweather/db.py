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
