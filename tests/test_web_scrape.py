"""Scrape-spec control-plane backends build matching shapes (mock + live).

live.* is exercised against a fake API via pytest-httpserver (mirrors
tests/test_web_schedules.py); mock.* returns in-memory shapes. Both match the
bellweather.web.data.source.SCRAPE_SPEC_COLUMNS contract. No DB, no network.
"""

import pytest

from bellweather.config import get_ui_settings
from bellweather.web.data import live, mock, source as contract

_SCHEMA = {
    "type": "object",
    "properties": {"price": {"type": "number"}, "title": {"type": "string"}},
}
_BINDING = {
    "symbol_key": "scrape:demo:{title}",
    "symbol_kind": "scraped-metric",
    "value": "$.price",
    "ts": "fetched_at",
    "unit": "usd",
    "tags": ["title"],
}

_SPEC_ROW = {
    "id": 1,
    "name": "demo-prices",
    "description": "Fixture scrape spec.",
    "fetch_adapter": "httpx",
    "llm_model": None,
    "enabled": True,
}
_SPEC_FULL = dict(
    _SPEC_ROW,
    sites=["https://example.com/a", "https://example.com/b"],
    output_schema=_SCHEMA,
    binding=_BINDING,
)
_PREVIEW = {
    "extracted": {"price": 12.5, "title": "Widget"},
    "symbols": ["scrape:demo:Widget"],
    "sample": [
        {"symbol_key": "scrape:demo:Widget", "ts": "2026-06-02T11:00:00+00:00", "value": 12.5}
    ],
    "tags": [{"tag_type": "title", "raw_value": "Widget"}],
}


# --- live: fake API via pytest-httpserver -----------------------------------
@pytest.fixture()
def _api(httpserver, monkeypatch):
    httpserver.expect_request("/api/scrape-specs", method="GET").respond_with_json([_SPEC_ROW])
    httpserver.expect_request("/api/scrape-specs", method="POST").respond_with_json(
        dict(_SPEC_ROW, id=7)
    )
    httpserver.expect_request("/api/scrape-specs/demo-prices", method="GET").respond_with_json(
        _SPEC_FULL
    )
    httpserver.expect_request("/api/scrape-specs/demo-prices", method="PATCH").respond_with_json(
        dict(_SPEC_ROW, enabled=False)
    )
    httpserver.expect_request("/api/scrape-specs/demo-prices", method="DELETE").respond_with_json(
        {"status": "deleted"}
    )
    # The preview body carries the url unwrapped under "url".
    httpserver.expect_request(
        "/api/scrape-specs/demo-prices/preview",
        method="POST",
        json={"url": "https://example.com/a"},
    ).respond_with_json(_PREVIEW)
    httpserver.expect_request("/api/fetch-adapters", method="GET").respond_with_json(
        {"adapters": ["httpx"]}
    )
    monkeypatch.setenv("BELLWEATHER_API_URL", httpserver.url_for("").rstrip("/"))
    get_ui_settings.cache_clear()
    yield
    get_ui_settings.cache_clear()


def test_live_get_scrape_specs(_api):
    df = live.get_scrape_specs()
    assert list(df.columns) == contract.SCRAPE_SPEC_COLUMNS
    assert df.iloc[0]["name"] == "demo-prices"
    assert df.iloc[0]["fetch_adapter"] == "httpx"


def test_live_get_scrape_spec_full(_api):
    spec = live.get_scrape_spec("demo-prices")
    assert spec["sites"] == ["https://example.com/a", "https://example.com/b"]
    assert spec["output_schema"] == _SCHEMA
    assert spec["binding"] == _BINDING


def test_live_create_scrape_spec_returns_id(_api):
    new_id = live.create_scrape_spec(
        "demo-prices", _SPEC_FULL["sites"], _SCHEMA, _BINDING, description="x"
    )
    assert new_id == 7


def test_live_write_paths_do_not_raise(_api):
    live.update_scrape_spec("demo-prices", enabled=False)
    live.delete_scrape_spec("demo-prices")


def test_live_preview_scrape_spec(_api):
    out = live.preview_scrape_spec("demo-prices", url="https://example.com/a")
    assert set(out) == {"extracted", "symbols", "sample", "tags"}
    assert out["extracted"]["price"] == 12.5
    assert out["symbols"] == ["scrape:demo:Widget"]
    assert out["sample"][0]["value"] == 12.5
    assert out["tags"][0]["tag_type"] == "title"


# --- mock: in-memory, no API -------------------------------------------------
def test_mock_get_scrape_specs_shape():
    df = mock.get_scrape_specs()
    assert list(df.columns) == contract.SCRAPE_SPEC_COLUMNS


def test_mock_get_scrape_spec_has_nested_json():
    spec = mock.get_scrape_spec(mock.get_scrape_specs().iloc[0]["name"])
    assert isinstance(spec["sites"], list)
    assert isinstance(spec["output_schema"], dict)
    assert isinstance(spec["binding"], dict)


def test_mock_create_then_get_roundtrip():
    new_id = mock.create_scrape_spec(
        "round-trip", ["https://x"], _SCHEMA, _BINDING, description="rt"
    )
    df = mock.get_scrape_specs()
    assert new_id in df["id"].tolist()
    spec = mock.get_scrape_spec("round-trip")
    assert spec["sites"] == ["https://x"]
    assert spec["output_schema"] == _SCHEMA
    assert spec["binding"] == _BINDING
    assert spec["description"] == "rt"


def test_mock_update_enabled():
    mock.create_scrape_spec("toggle-me", ["https://x"], _SCHEMA, _BINDING)
    mock.update_scrape_spec("toggle-me", enabled=False)
    df = mock.get_scrape_specs().set_index("name")
    assert bool(df.loc["toggle-me", "enabled"]) is False


def test_mock_update_nested_json():
    mock.create_scrape_spec("edit-binding", ["https://x"], _SCHEMA, _BINDING)
    new_binding = dict(_BINDING, unit="eur")
    mock.update_scrape_spec("edit-binding", binding=new_binding)
    assert mock.get_scrape_spec("edit-binding")["binding"]["unit"] == "eur"


def test_mock_delete_removes_row():
    mock.create_scrape_spec("delete-me", ["https://x"], _SCHEMA, _BINDING)
    mock.delete_scrape_spec("delete-me")
    assert "delete-me" not in mock.get_scrape_specs()["name"].tolist()


def test_mock_get_unknown_spec_returns_none():
    assert mock.get_scrape_spec("nope-does-not-exist") is None


def test_mock_preview_scrape_spec_shape():
    spec_name = mock.get_scrape_specs().iloc[0]["name"]
    out = mock.preview_scrape_spec(spec_name)
    assert set(out) == {"extracted", "symbols", "sample", "tags"}
    assert isinstance(out["extracted"], dict)
    assert isinstance(out["symbols"], list)
    assert isinstance(out["sample"], list)
    assert isinstance(out["tags"], list)


def test_mock_preview_varies_by_url():
    name = mock.get_scrape_specs().iloc[0]["name"]
    a = mock.preview_scrape_spec(name, url="https://example.com/products/a")
    b = mock.preview_scrape_spec(name, url="https://example.com/products/b")
    assert a["symbols"] != b["symbols"]
    assert a["sample"][0]["value"] != b["sample"][0]["value"]
    # deterministic: same url → same result
    assert mock.preview_scrape_spec(name, url="https://example.com/products/a") == a


def test_mock_preview_url_none_uses_first_site():
    name = mock.get_scrape_specs().iloc[0]["name"]
    first_site = mock.get_scrape_spec(name)["sites"][0]
    assert mock.preview_scrape_spec(name) == mock.preview_scrape_spec(name, url=first_site)


def test_mock_has_several_fixture_specs():
    # comprehensive offline data: the selector should have plenty to browse
    assert len(mock.get_scrape_specs()) >= 4


# --- fetch-adapter choices (Edit-form dropdown) -----------------------------
def test_mock_fetch_adapter_choices():
    assert mock.get_fetch_adapter_choices() == ["httpx"]


def test_live_fetch_adapter_choices(_api):
    assert live.get_fetch_adapter_choices() == ["httpx"]
