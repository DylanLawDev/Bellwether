"""Read API endpoints via TestClient (requires `make up` + migrations).

Asserts each /api route returns status 200, JSON whose keys match the
bellweather.web.data.source column/key contract, and that /api/config masks
the database_url.
"""

import pytest
from fastapi.testclient import TestClient

from bellweather.api import app
from bellweather.db import get_conn
from bellweather.migrate import apply_migrations
from bellweather.web.data import source as contract
from tests.conftest import clear_observations, clear_records
from tests.test_reads import _SOURCE, _SYMS, _seed

client = TestClient(app)


@pytest.fixture(autouse=True)
def _seeded():
    apply_migrations()
    with get_conn() as c:
        _seed(c)
    yield
    with get_conn() as c:
        clear_records(c, _SOURCE)
        clear_observations(c, _SYMS)
        c.execute("delete from tracked_symbols where key = any(%s)", (list(_SYMS),))
        c.commit()


def test_symbols_endpoint():
    rows = client.get("/api/symbols").json()
    assert rows and all(set(r) == set(contract.TRACKED_SYMBOL_COLUMNS) for r in rows)


def test_observations_endpoint():
    r = client.get("/api/observations", params={"keys": [_SYMS[0]]})
    assert r.status_code == 200
    rows = r.json()
    assert rows and all(set(x) == set(contract.OBSERVATION_COLUMNS) for x in rows)
    assert all(x["key"] == _SYMS[0] for x in rows)


def test_records_endpoint():
    rows = client.get("/api/records", params={"source": _SOURCE}).json()
    assert len(rows) == 3
    assert all(set(r) == set(contract.RAW_RECORD_COLUMNS) for r in rows)


def test_tags_endpoint():
    rows = client.get("/api/tags", params={"search": "ukrai"}).json()
    assert len(rows) == 1 and set(rows[0]) == set(contract.TAG_COLUMNS)
    assert isinstance(rows[0]["score"], dict)


def test_queue_endpoint():
    stats = client.get("/api/queue").json()
    assert set(stats) == set(contract.QUEUE_STATES)


def test_ingestion_rate_endpoint():
    rows = client.get("/api/ingestion-rate", params={"hours": 48}).json()
    assert rows and all(set(r) == set(contract.INGESTION_RATE_COLUMNS) for r in rows)


def test_config_endpoint_masks_database_url():
    rows = client.get("/api/config").json()
    assert all(set(r) == {"key", "value", "note"} for r in rows)
    db = next(r for r in rows if r["key"] == "database_url")
    assert "***" in db["value"] and "bellweather:bellweather" not in db["value"]
