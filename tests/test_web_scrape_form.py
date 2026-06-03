"""Pure validation for the Scrape-specs authoring form (web.forms).

Locks the three guards the UI applies before calling create_scrape_spec:
- spec names must be URL-path-safe (no '/', '?', '#', whitespace);
- output_schema / binding must parse to JSON *objects*, not arrays/scalars.
No Streamlit, no DB, no network.
"""

import pytest

from bellweather.web import forms as form


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


# --- build_source_payload (Scrape page: fetch half) --------------------------
def _src_kw(**over):
    base = dict(
        name="my-source",
        description="",
        sites_raw="https://a\n  \nhttps://b\n",
        fetch_adapter="httpx",
    )
    base.update(over)
    return base


def test_source_payload_happy_path():
    payload, errors = form.build_source_payload(**_src_kw())
    assert errors == []
    assert payload["name"] == "my-source"
    assert payload["sites"] == ["https://a", "https://b"]  # blanks stripped
    assert payload["description"] is None  # blank → None
    assert payload["fetch_adapter"] == "httpx"


def test_source_payload_edit_path_skips_name_check():
    # require_name=False: an empty name is fine because the selector owns it
    _, errors = form.build_source_payload(**_src_kw(name="", require_name=False))
    assert errors == []


def test_source_payload_requires_name_on_create():
    _, errors = form.build_source_payload(**_src_kw(name="bad/name"))
    assert any("name" in e.lower() for e in errors)


def test_source_payload_requires_sites():
    _, errors = form.build_source_payload(**_src_kw(sites_raw="   \n  "))
    assert any("site" in e.lower() for e in errors)


def test_source_payload_blank_adapter_defaults_httpx():
    payload, errors = form.build_source_payload(**_src_kw(fetch_adapter=""))
    assert errors == []
    assert payload["fetch_adapter"] == "httpx"


# --- build_extraction_payload (Extract page: parse half) ----------------------
_OK_SCHEMA = '{"type": "object", "properties": {"price": {"type": "number"}}}'
_OK_BINDING = '{"symbol_key": "s:{x}", "symbol_kind": "k", "value": "$.price", "ts": "fetched_at"}'


def _ex_kw(**over):
    base = dict(
        name="my-extractor",
        description="",
        output_schema_raw=_OK_SCHEMA,
        binding_raw=_OK_BINDING,
        llm_model="",
    )
    base.update(over)
    return base


def test_extraction_payload_happy_path():
    payload, errors = form.build_extraction_payload(**_ex_kw())
    assert errors == []
    assert payload["name"] == "my-extractor"
    assert payload["llm_model"] is None  # blank → None
    assert payload["description"] is None
    assert payload["output_schema"] == {
        "type": "object",
        "properties": {"price": {"type": "number"}},
    }
    assert payload["binding"]["symbol_key"] == "s:{x}"


def test_extraction_payload_edit_path_skips_name_check():
    _, errors = form.build_extraction_payload(**_ex_kw(name="", require_name=False))
    assert errors == []


def test_extraction_payload_requires_name_on_create():
    _, errors = form.build_extraction_payload(**_ex_kw(name="bad/name"))
    assert any("name" in e.lower() for e in errors)


def test_extraction_payload_rejects_non_object_schema():
    _, errors = form.build_extraction_payload(**_ex_kw(output_schema_raw="[1, 2]"))
    assert any("Output schema" in e for e in errors)


def test_extraction_payload_rejects_invalid_json_binding():
    _, errors = form.build_extraction_payload(**_ex_kw(binding_raw="{nope}"))
    assert any("Binding" in e for e in errors)
