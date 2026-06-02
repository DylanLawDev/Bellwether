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
            conn,
            name="t34-renamed",
            sites=["https://x"],
            output_schema=_SCHEMA,
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
        specs.create_spec(conn, name="t34-dup", sites=[], output_schema=_SCHEMA, binding=_BINDING)
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            specs.create_spec(
                conn, name="t34-dup", sites=[], output_schema=_SCHEMA, binding=_BINDING
            )
        conn.rollback()
