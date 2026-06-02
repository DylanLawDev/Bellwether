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
