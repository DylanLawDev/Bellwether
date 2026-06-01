from bellweather.migrate import apply_migrations
from bellweather.db import get_conn


def test_migrations_create_tables_and_are_idempotent():
    apply_migrations()
    second = apply_migrations()  # already applied → no-op
    assert second == []
    with get_conn() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema='public'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert {
        "raw_records",
        "work_queue",
        "tags",
        "entities",
        "tracked_symbols",
        "observations",
        "schema_migrations",
    } <= names
