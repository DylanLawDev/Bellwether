"""Headless render smokes for the split Scrape/Extract pages.

Uses Streamlit's AppTest to execute both pages in a simulated context against
the mock backend. Guarded by importorskip so they run only when the optional
``ui`` dependency group is installed (``uv run --group ui pytest``); the default
``make check`` gate has no Streamlit and skips this file. No DB, no network.
"""

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

_SCRAPE = "src/bellweather/web/pages/6_Scrape.py"
_EXTRACT = "src/bellweather/web/pages/7_Extract.py"


# --- Scrape page (sources) ----------------------------------------------------
def test_scrape_new_source_view_renders_with_fixtures():
    at = AppTest.from_file(_SCRAPE).run(timeout=20)
    assert not at.exception
    options = list(at.selectbox[0].options)
    assert options[0].startswith("➕")
    assert {"demo-prices", "fed-speeches", "weather-alerts"} <= set(options)


def test_scrape_existing_source_prefills_with_disabled_name():
    at = AppTest.from_file(_SCRAPE).run(timeout=20)
    at.selectbox[0].select("demo-prices").run(timeout=20)
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Edit", "Captures"]
    name_fields = [(ti.value, ti.disabled) for ti in at.text_input if ti.value == "demo-prices"]
    assert name_fields == [("demo-prices", True)]  # name immutable on edit


def test_scrape_captures_tab_renders_raw_content():
    at = AppTest.from_file(_SCRAPE).run(timeout=20)
    at.selectbox[0].select("demo-prices").run(timeout=20)
    assert not at.exception
    # the Captures tab renders the capture in st.code
    assert at.code
    assert "<h1>" in at.code[0].value


def test_scrape_blank_sites_create_surfaces_validation_error():
    at = AppTest.from_file(_SCRAPE).run(timeout=20)
    at.text_area[0].set_value("")  # sites textarea on the new-source form
    at.button[0].click().run(timeout=20)
    assert not at.exception
    assert any("site URL is required" in e.value for e in at.error)


# --- Extract page (extraction specs) -------------------------------------------
def test_extract_new_extractor_view_renders_with_fixtures():
    at = AppTest.from_file(_EXTRACT).run(timeout=20)
    assert not at.exception
    options = list(at.selectbox[0].options)
    assert options[0].startswith("➕")
    assert {"product-prices", "page-sentiment"} <= set(options)


def test_extract_existing_extractor_prefills_with_links():
    at = AppTest.from_file(_EXTRACT).run(timeout=20)
    at.selectbox[0].select("page-sentiment").run(timeout=20)
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Edit", "Test"]
    name_fields = [(ti.value, ti.disabled) for ti in at.text_input if ti.value == "page-sentiment"]
    assert name_fields == [("page-sentiment", True)]
    # the M2M edit surface shows the current links as the multiselect default
    assert at.multiselect[0].value == ["demo-prices", "fed-speeches"]


def test_extract_test_tab_shows_capture_and_runs_extraction():
    at = AppTest.from_file(_EXTRACT).run(timeout=20)
    at.selectbox[0].select("product-prices").run(timeout=20)
    assert not at.exception
    # Test tab shows the linked source's capture read-only
    assert at.code and "<h1>" in at.code[0].value
    # run the extraction (the Test tab's button is after the form submit)
    run_buttons = [b for b in at.button if "extraction" in b.label.lower()]
    assert run_buttons
    run_buttons[0].click().run(timeout=20)
    assert not at.exception


def test_extract_invalid_binding_surfaces_validation_error():
    at = AppTest.from_file(_EXTRACT).run(timeout=20)
    at.text_area[1].set_value("{nope}")  # binding textarea on the new-extractor form
    at.button[0].click().run(timeout=20)
    assert not at.exception
    assert any("Binding" in e.value for e in at.error)
