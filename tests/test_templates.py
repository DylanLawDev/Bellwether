import pathlib
import sys

import pytest

from bellweather.templates import (
    Template,
    TemplateParam,
    discover_templates,
    get_template,
    validate_params,
    load_entrypoint,
    parse_interval,
)

FIXTURES = str(pathlib.Path(__file__).parent / "fixtures" / "templates")


def test_discover_finds_echo_with_params():
    found = discover_templates(FIXTURES)
    assert "echo" in found  # sibling fixture templates (e.g. echo_series) may coexist
    echo = found["echo"]
    assert isinstance(echo, Template)
    assert echo.entrypoint == "tests.fixtures.templates.echo.producer:run"
    assert echo.description == "Fixture echo template for control-plane API tests"
    assert echo.default_interval_seconds == 1800
    by_name = {p.name: p for p in echo.params}
    assert isinstance(by_name["url"], TemplateParam)
    assert by_name["url"].required is True and by_name["url"].type == "str"
    assert by_name["backfill"].type == "str" and by_name["backfill"].default == "all"


def test_discovery_does_not_import_entrypoint():
    # The producer module must NOT be imported by discovery (no code execution to list).
    sys.modules.pop("tests.fixtures.templates.echo.producer", None)
    discover_templates(FIXTURES)
    assert "tests.fixtures.templates.echo.producer" not in sys.modules


def test_get_template_by_name():
    assert get_template("echo", FIXTURES).name == "echo"
    assert get_template("nope", FIXTURES) is None


def test_validate_params_fills_defaults_and_coerces():
    echo = get_template("echo", FIXTURES)
    out = validate_params(echo, {"url": "https://example.com", "backfill": "recent"})
    assert out == {"url": "https://example.com", "backfill": "recent"}


def test_validate_params_requires_required():
    echo = get_template("echo", FIXTURES)
    with pytest.raises(ValueError):
        validate_params(echo, {})  # missing required `url`


def test_validate_params_uses_default_value():
    echo = get_template("echo", FIXTURES)
    out = validate_params(echo, {"url": "https://example.com"})
    assert out == {"url": "https://example.com", "backfill": "all"}  # default backfill


def test_load_entrypoint_imports_callable():
    fn = load_entrypoint("tests.fixtures.templates.echo.handler:run")
    assert callable(fn)
    assert fn({"a": 1}, None) == {"submitted": 0, "echo": {"a": 1}}


@pytest.mark.parametrize(
    "s,expected",
    [("45s", 45), ("30m", 1800), ("6h", 21600), ("1d", 86400)],
)
def test_parse_interval(s, expected):
    assert parse_interval(s) == expected


def test_parse_interval_rejects_garbage():
    with pytest.raises(ValueError):
        parse_interval("soon")
