"""Headless render smoke for the Scrape specs page (web/pages/6_Scrape.py).

Uses Streamlit's AppTest to execute the page in a simulated context against the
mock backend. Guarded by importorskip so it runs only when the optional ``ui``
dependency group is installed (``uv run --group ui pytest``); the default
``make check`` gate has no Streamlit and skips this file. No DB, no network.
"""

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

_PAGE = "src/bellweather/web/pages/6_Scrape.py"


def test_new_spec_view_renders_with_all_fixtures():
    at = AppTest.from_file(_PAGE).run(timeout=20)
    assert not at.exception
    options = list(at.selectbox[0].options)
    # "➕ New spec…" sentinel + the comprehensive fixture specs
    assert options[0].startswith("➕")
    assert {"demo-prices", "fed-speeches", "weather-alerts"} <= set(options)


def test_existing_spec_prefills_with_disabled_name():
    at = AppTest.from_file(_PAGE).run(timeout=20)
    at.selectbox[0].select("weather-alerts").run(timeout=20)
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Edit", "Preview"]
    name_fields = [(ti.value, ti.disabled) for ti in at.text_input if ti.value == "weather-alerts"]
    assert name_fields == [("weather-alerts", True)]  # name immutable on edit


def test_blank_sites_create_surfaces_validation_error():
    at = AppTest.from_file(_PAGE).run(timeout=20)
    at.text_area[0].set_value("")  # sites textarea on the new-spec form
    at.button[0].click().run(timeout=20)
    assert not at.exception
    assert any("site URL is required" in e.value for e in at.error)
