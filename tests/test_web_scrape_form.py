"""Pure validation for the Scrape-specs authoring form (web.pages._scrape_form).

Locks the three guards the UI applies before calling create_scrape_spec:
- spec names must be URL-path-safe (no '/', '?', '#', whitespace);
- output_schema / binding must parse to JSON *objects*, not arrays/scalars.
No Streamlit, no DB, no network.
"""

import pytest

from bellweather.web.pages import _scrape_form as form


# --- validate_spec_name -----------------------------------------------------
def test_blank_name_rejected():
    assert form.validate_spec_name("   ") is not None


@pytest.mark.parametrize("name", ["a/b", "a?b", "a#b", "a b", "a%b", "../x"])
def test_path_reserved_names_rejected(name):
    # These characters change the URL path/query/fragment of
    # /api/scrape-specs/{name}, so a created spec becomes unmanageable.
    assert form.validate_spec_name(name) is not None


@pytest.mark.parametrize("name", ["my-spec", "demo.prices_v2", "ABC123"])
def test_path_safe_names_accepted(name):
    assert form.validate_spec_name(name) is None


# --- validate_json_object ---------------------------------------------------
@pytest.mark.parametrize("value", [[], ["x"], "x", 3, 3.5, True, None])
def test_non_object_json_rejected(value):
    # The live API models require dicts for output_schema/binding; a non-object
    # would raise an uncaught 422 instead of a form error.
    assert form.validate_json_object("Output schema", value) is not None


def test_object_json_accepted():
    assert form.validate_json_object("Binding", {"symbol_key": "x"}) is None


# --- parse_json (unchanged behaviour, moved into the helper) ----------------
def test_parse_json_reports_invalid():
    value, err = form.parse_json("Output schema", "{not json}")
    assert value is None
    assert "not valid JSON" in err


def test_parse_json_passes_through_valid():
    value, err = form.parse_json("Binding", '{"a": 1}')
    assert err is None
    assert value == {"a": 1}
