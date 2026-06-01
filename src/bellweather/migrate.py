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
