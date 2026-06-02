"""Scrape-spec control-plane API via TestClient (DB tests require `make up` + `make migrate`).

CRUD (create -> get -> list -> patch -> delete + 404s) round-trips against real Postgres
(scrape_specs from migration 0003). Preview monkeypatches the fetch seam and LlmExtractor
to in-process fakes, so it never hits the network or the LLM and commits nothing.
"""

import pytest
from fastapi.testclient import TestClient

import bellweather.api as api
from bellweather.api import app
from bellweather.db import get_conn
from bellweather.fetch import FetchResult
from bellweather.migrate import apply_migrations

client = TestClient(app)

# Spec names this module owns — wiped before/after so reruns are deterministic.
_NAMES = ("t39-prices", "t39-other")

# A minimal but realistic spec body. output_schema is the LLM tool input_schema;
# binding maps the extracted JSON onto (symbol, ts, value) + tags.
_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                    "in_stock": {"type": "boolean"},
                },
            },
        }
    },
}
_BINDING = {
    "records_path": "$.items",
    "symbol_key": "scrape:prices:{category}:{name}",
    "symbol_kind": "scraped-metric",
    "value": "$.price",
    "ts": "fetched_at",
    "unit": "usd",
    "tags": ["category", "in_stock"],
}


def _body(name="t39-prices", **over):
    body = {
        "name": name,
        "description": "T39 fixture spec",
        "sites": ["https://example.com/a", "https://example.com/b"],
        "output_schema": _SCHEMA,
        "binding": _BINDING,
        "fetch_adapter": "httpx",
        "llm_model": None,
        "enabled": True,
    }
    body.update(over)
    return body


@pytest.fixture(autouse=True)
def _clean():
    apply_migrations()

    def _wipe(c):
        c.execute("delete from scrape_specs where name = any(%s)", (list(_NAMES),))
        c.commit()

    with get_conn() as c:
        _wipe(c)
    yield
    with get_conn() as c:
        _wipe(c)


def _create(**over):
    r = client.post("/api/scrape-specs", json=_body(**over))
    assert r.status_code == 200, r.text
    return r.json()


# --- CRUD (DB-backed) -------------------------------------------------------
def test_create_returns_row_with_nested_json():
    created = _create()
    assert created["id"] > 0
    assert created["name"] == "t39-prices"
    assert created["enabled"] is True
    assert created["fetch_adapter"] == "httpx"
    # sites/output_schema/binding round-trip as nested JSON (psycopg jsonb adaption).
    assert created["sites"] == ["https://example.com/a", "https://example.com/b"]
    assert created["binding"]["symbol_key"] == "scrape:prices:{category}:{name}"
    assert created["output_schema"]["type"] == "object"


def test_get_then_list_includes_created():
    created = _create()
    got = client.get(f"/api/scrape-specs/{created['name']}")
    assert got.status_code == 200
    assert got.json()["id"] == created["id"]
    assert got.json()["sites"] == created["sites"]
    rows = client.get("/api/scrape-specs").json()
    assert any(r["id"] == created["id"] and r["name"] == "t39-prices" for r in rows)
    assert all(
        {
            "id",
            "name",
            "description",
            "sites",
            "output_schema",
            "binding",
            "fetch_adapter",
            "llm_model",
            "enabled",
        }
        <= set(r)
        for r in rows
    )


def test_patch_updates_fields():
    created = _create()
    r = client.patch(
        f"/api/scrape-specs/{created['name']}",
        json={
            "enabled": False,
            "sites": ["https://example.com/only"],
            "llm_model": "claude-haiku-4-5-20251001",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["sites"] == ["https://example.com/only"]
    assert body["llm_model"] == "claude-haiku-4-5-20251001"
    # Persisted: a fresh GET reflects the patch.
    assert client.get(f"/api/scrape-specs/{created['name']}").json()["enabled"] is False


def test_patch_explicit_null_clears_nullable_field():
    # An explicit null on a nullable field must persist (not be dropped): set
    # llm_model, then PATCH it back to null to fall to the settings default.
    created = _create(llm_model="claude-haiku-4-5-20251001")
    assert created["llm_model"] == "claude-haiku-4-5-20251001"
    r = client.patch(f"/api/scrape-specs/{created['name']}", json={"llm_model": None})
    assert r.status_code == 200
    assert r.json()["llm_model"] is None
    # Omitted fields are untouched; the explicit null landed in the DB.
    fresh = client.get(f"/api/scrape-specs/{created['name']}").json()
    assert fresh["llm_model"] is None
    assert fresh["enabled"] is True  # never sent → unchanged


def test_delete_removes_spec():
    created = _create()
    r = client.delete(f"/api/scrape-specs/{created['name']}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}
    assert client.get(f"/api/scrape-specs/{created['name']}").status_code == 404
    rows = client.get("/api/scrape-specs").json()
    assert all(x["id"] != created["id"] for x in rows)


def test_get_unknown_is_404():
    assert client.get("/api/scrape-specs/t39-nope").status_code == 404


def test_patch_unknown_is_404():
    assert client.patch("/api/scrape-specs/t39-nope", json={"enabled": False}).status_code == 404


def test_delete_unknown_is_404():
    assert client.delete("/api/scrape-specs/t39-nope").status_code == 404


# --- in-process preview (fakes; no network, no LLM, no commit) --------------
class _FakeFetcher:
    name = "httpx"

    def __init__(self):
        self.urls = []

    def fetch(self, url, **opts):
        self.urls.append(url)
        return FetchResult(
            content="<html>raw page</html>",
            status=200,
            content_type="text/html",
            final_url=url,
        )


class _FakeLlm:
    """Stand-in for LlmExtractor: returns canned JSON, never builds an Anthropic client."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    def extract(self, content, output_schema, *, model=None):
        self.calls.append((content, model))
        return {
            "items": [
                {"category": "fruit", "name": "apple", "price": 1.5, "in_stock": True},
                {"category": "fruit", "name": "pear", "price": 2.0, "in_stock": False},
            ]
        }


def test_preview_returns_extracted_observations_and_tags(monkeypatch):
    created = _create()
    fetcher = _FakeFetcher()
    fake_llm = _FakeLlm()
    # Patch the seams used inside the preview endpoint (the api module's references).
    monkeypatch.setattr(api, "get_fetcher", lambda name: fetcher)
    monkeypatch.setattr(api, "LlmExtractor", lambda *a, **k: fake_llm)

    r = client.post(f"/api/scrape-specs/{created['name']}/preview", json={"url": None})
    assert r.status_code == 200, r.text
    body = r.json()

    # Default url = first site; the fetcher saw exactly that one URL (one fetch only).
    assert fetcher.urls == ["https://example.com/a"]
    # The LLM was called with the raw page content + the spec's per-spec model (None here).
    assert fake_llm.calls and fake_llm.calls[0][0] == "<html>raw page</html>"

    # extracted = the raw LLM JSON instance.
    assert body["extracted"] == {
        "items": [
            {"category": "fruit", "name": "apple", "price": 1.5, "in_stock": True},
            {"category": "fruit", "name": "pear", "price": 2.0, "in_stock": False},
        ]
    }
    # symbols = distinct symbol_keys from the binding.
    assert body["symbols"] == [
        "scrape:prices:fruit:apple",
        "scrape:prices:fruit:pear",
    ]
    # sample = flat {symbol_key, ts, value} rows; values came from $.price.
    assert {s["symbol_key"]: s["value"] for s in body["sample"]} == {
        "scrape:prices:fruit:apple": 1.5,
        "scrape:prices:fruit:pear": 2.0,
    }
    assert all({"symbol_key", "ts", "value"} == set(s) for s in body["sample"])
    # tags = {tag_type, raw_value} per bound field.
    tag_pairs = {(t["tag_type"], t["raw_value"]) for t in body["tags"]}
    assert ("category", "fruit") in tag_pairs
    assert all({"tag_type", "raw_value"} == set(t) for t in body["tags"])

    # Preview commits nothing: no scrape-llm-v1 raw_records, no tracked_symbols.
    with get_conn() as c:
        n_recs = c.execute(
            "select count(*) from raw_records where content_type = %s", ("scrape-llm-v1",)
        ).fetchone()[0]
        n_syms = c.execute(
            "select count(*) from tracked_symbols where key like %s", ("scrape:prices:%",)
        ).fetchone()[0]
    assert n_recs == 0
    assert n_syms == 0


def test_preview_explicit_url_overrides_first_site(monkeypatch):
    created = _create()
    fetcher = _FakeFetcher()
    monkeypatch.setattr(api, "get_fetcher", lambda name: fetcher)
    monkeypatch.setattr(api, "LlmExtractor", lambda *a, **k: _FakeLlm())
    r = client.post(
        f"/api/scrape-specs/{created['name']}/preview",
        json={"url": "https://example.com/explicit"},
    )
    assert r.status_code == 200
    assert fetcher.urls == ["https://example.com/explicit"]


def test_preview_unknown_spec_is_404():
    assert client.post("/api/scrape-specs/t39-nope/preview", json={"url": None}).status_code == 404
