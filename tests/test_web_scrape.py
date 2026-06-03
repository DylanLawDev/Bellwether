"""Scrape-source / extraction-spec backends build matching shapes (mock + live).

The scrape/extract split (docs/specs/2026-06-03-scrape-extract-split-design.md):
sources (what to fetch → raw captures) relate many-to-many to extraction specs
(how to parse). live.* is exercised against a fake API via pytest-httpserver —
those tests are the executable contract for the T43+ backend tickets; mock.*
returns in-memory shapes. No DB, no network.
"""

from bellweather.web.data import mock, source as contract


# --- mock: sources -----------------------------------------------------------
def test_mock_sources_frame_shape():
    df = mock.get_scrape_sources()
    assert list(df.columns) == contract.SCRAPE_SOURCE_COLUMNS
    assert len(df) >= 4  # comprehensive fixtures


def test_mock_source_full_includes_sites_and_parsed_by():
    src = mock.get_scrape_source("demo-prices")
    assert src["sites"] and isinstance(src["sites"], list)
    # M2M visible from the source side (sorted)
    assert src["parsed_by"] == ["page-sentiment", "product-prices"]


def test_mock_source_crud_roundtrip():
    sid = mock.create_scrape_source("rt-src", ["https://x"], description="rt")
    assert sid in mock.get_scrape_sources()["id"].tolist()
    mock.update_scrape_source("rt-src", enabled=False, sites=["https://y"])
    src = mock.get_scrape_source("rt-src")
    assert src["enabled"] is False
    assert src["sites"] == ["https://y"]
    mock.delete_scrape_source("rt-src")
    assert mock.get_scrape_source("rt-src") is None


def test_mock_get_unknown_source_returns_none():
    assert mock.get_scrape_source("nope-does-not-exist") is None


# --- mock: extraction specs --------------------------------------------------
def test_mock_extractors_frame_shape():
    df = mock.get_extraction_specs()
    assert list(df.columns) == contract.EXTRACTION_SPEC_COLUMNS
    assert "page-sentiment" in df["name"].tolist()


def test_mock_extractor_full_includes_links():
    spec = mock.get_extraction_spec("page-sentiment")
    # one parser, many sources (sorted)
    assert spec["sources"] == ["demo-prices", "fed-speeches"]
    assert isinstance(spec["output_schema"], dict)
    assert isinstance(spec["binding"], dict)


def test_mock_extractor_sources_update_replaces_links():
    mock.create_extraction_spec(
        "rt-ex",
        {"type": "object", "properties": {"x": {"type": "number"}}},
        {"symbol_key": "s:{x}", "value": "$.x", "ts": "fetched_at", "tags": []},
        sources=["demo-prices"],
    )
    assert "rt-ex" in mock.get_scrape_source("demo-prices")["parsed_by"]
    mock.update_extraction_spec("rt-ex", sources=["job-postings"])
    assert "rt-ex" not in mock.get_scrape_source("demo-prices")["parsed_by"]
    assert "rt-ex" in mock.get_scrape_source("job-postings")["parsed_by"]
    mock.delete_extraction_spec("rt-ex")
    assert "rt-ex" not in mock.get_scrape_source("job-postings")["parsed_by"]
    assert mock.get_extraction_spec("rt-ex") is None


def test_mock_extractor_nested_update_roundtrip():
    mock.create_extraction_spec(
        "rt-edit",
        {"type": "object", "properties": {"v": {"type": "number"}}},
        {"symbol_key": "s:{v}", "value": "$.v", "ts": "fetched_at", "unit": "usd", "tags": []},
    )
    mock.update_extraction_spec("rt-edit", binding={"symbol_key": "s:{v}", "unit": "eur"})
    assert mock.get_extraction_spec("rt-edit")["binding"]["unit"] == "eur"
    mock.delete_extraction_spec("rt-edit")


# --- mock: captures (derived raw bronze) --------------------------------------
def test_mock_captures_listing_and_content():
    df = mock.get_captures("demo-prices")
    assert list(df.columns) == contract.CAPTURE_COLUMNS
    assert len(df) == len(mock.get_scrape_source("demo-prices")["sites"])
    cap = mock.get_capture("demo-prices", df.iloc[0]["url"])
    assert cap["content"]
    assert cap["size_bytes"] == len(cap["content"])
    assert mock.get_capture("demo-prices", "https://not-a-site") is None


def test_mock_fetch_capture_now_matches_get():
    url = mock.get_scrape_source("demo-prices")["sites"][0]
    assert mock.fetch_capture_now("demo-prices", url) == mock.get_capture("demo-prices", url)


# --- mock: extraction preview (parse a capture, no fetch) ---------------------
def test_mock_preview_extraction_varies_by_url_and_extractor():
    sites = mock.get_scrape_source("demo-prices")["sites"]
    a = mock.preview_extraction("product-prices", "demo-prices", sites[0])
    b = mock.preview_extraction("product-prices", "demo-prices", sites[1])
    assert set(a) == {"extracted", "symbols", "sample", "tags"}
    assert a["symbols"] != b["symbols"]
    assert a["sample"][0]["value"] != b["sample"][0]["value"]
    # different extractor over the same capture → different symbol space
    c = mock.preview_extraction("page-sentiment", "demo-prices", sites[0])
    assert c["symbols"] != a["symbols"]
    # deterministic
    assert a == mock.preview_extraction("product-prices", "demo-prices", sites[0])


def test_mock_preview_extraction_fills_schema_props():
    url = mock.get_scrape_source("demo-prices")["sites"][0]
    out = mock.preview_extraction("product-prices", "demo-prices", url)
    assert set(out["extracted"]) == {"title", "price"}
    assert isinstance(out["extracted"]["price"], float)
    assert out["tags"] and out["tags"][0]["tag_type"] == "title"


# --- fetch-adapter choices (Edit-form dropdown) -------------------------------
def test_mock_fetch_adapter_choices():
    assert mock.get_fetch_adapter_choices() == ["httpx"]
