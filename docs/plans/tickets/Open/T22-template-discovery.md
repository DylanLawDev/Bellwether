# T22 — Template manifest contract + discovery (`templates.py`)

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` (§4 The template contract; K2). **Depends on:** T01. **Branch:** `ticket/T22-template-discovery`. **PR, do not merge without approval.**

## Goal
Give Bellwether the **template-registry** unit (spec §3.1): discover collector manifests from the templates dir, parse them into `Template`/`TemplateParam` models, validate caller-supplied params against the schema, and translate human intervals to seconds — **all without executing any template code**. Discovery uses stdlib `tomllib` and must never import a manifest's entrypoint module; importing happens only at run/preview time via `load_entrypoint`. This is pure infrastructure: no DB, no GCS, no network, testable with a fixture template directory.

## Files
- Create: `src/bellweather/templates.py`
- Modify: `src/bellweather/config.py` — add `bellweather_templates_dir: str = "producers"` to `Settings`.
- Test: `tests/test_templates.py`
- Test fixture: `tests/fixtures/templates/echo/template.toml`
- Test fixture: `tests/fixtures/templates/echo/handler.py` (dummy entrypoint with an import side-effect marker)

## Interface
Copied verbatim from the build plan ("Locked interfaces", `templates.py`):
```python
@dataclass
class TemplateParam:
    name: str; type: str = "str"; required: bool = False
    default: object | None = None; choices: list | None = None; help: str | None = None

@dataclass
class Template:
    name: str; entrypoint: str; description: str = ""
    params: list[TemplateParam] = field(default_factory=list)
    default_interval_seconds: int | None = None

def discover_templates(templates_dir: str | None = None) -> dict[str, Template]: ...  # scan */template.toml via tomllib; DO NOT import entrypoints
def get_template(name: str, templates_dir: str | None = None) -> Template | None: ...
def validate_params(template: Template, params: dict) -> dict: ...   # defaults + required + choices + coercion; ValueError on bad
def load_entrypoint(entrypoint: str): ...   # "module.path:function" -> callable (run-time only)
def parse_interval(s: str) -> int: ...       # "30m"|"6h"|"1d"|"45s" -> seconds
```
Manifest shape (TOML): `name`, `entrypoint = "module:func"`, `description`, `[params]` table of `{type, required, default, choices, help}`, `[schedule] default_interval = "30m"`. Entrypoint contract: `def run(params: dict, client) -> dict | None`.

## Steps

- [ ] **Step 1: Fixture entrypoint with an import marker** `tests/fixtures/templates/echo/handler.py` — a dummy entrypoint that records, *at module import time*, that it was imported. Discovery must never trigger this side effect.
```python
# tests/fixtures/templates/echo/handler.py
# Module-level side effect: importing this module appends to IMPORT_LOG.
# discover_templates() must NEVER import it (no code execution to list templates);
# only load_entrypoint() (run/preview time) may.
IMPORT_LOG: list[str] = []
IMPORT_LOG.append("imported")


def run(params: dict, client) -> dict | None:
    return {"submitted": 0, "echo": params}
```

- [ ] **Step 2: Fixture manifest** `tests/fixtures/templates/echo/template.toml` — exercises every manifest feature: a required param, a param with a default + `choices`, and a default interval.
```toml
name        = "echo"
entrypoint  = "tests.fixtures.templates.echo.handler:run"
description = "Echo template for tests"

[params]
url      = { type = "str", required = true, help = "Some URL" }
mode     = { type = "str", default = "recent", choices = ["all", "recent"] }
limit    = { type = "int", default = 10 }

[schedule]
default_interval = "30m"
```

- [ ] **Step 3: Failing test** `tests/test_templates.py` — pure, no DB/GCS. The fixtures dir is resolved relative to the test file so it works from any CWD.
```python
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
    assert echo.entrypoint == "tests.fixtures.templates.echo.handler:run"
    assert echo.description == "Echo template for tests"
    assert echo.default_interval_seconds == 1800
    by_name = {p.name: p for p in echo.params}
    assert isinstance(by_name["url"], TemplateParam)
    assert by_name["url"].required is True and by_name["url"].type == "str"
    assert by_name["mode"].default == "recent" and by_name["mode"].choices == ["all", "recent"]
    assert by_name["limit"].type == "int" and by_name["limit"].default == 10


def test_discovery_does_not_import_entrypoint():
    # The handler module must NOT be imported by discovery (no code execution to list).
    sys.modules.pop("tests.fixtures.templates.echo.handler", None)
    discover_templates(FIXTURES)
    assert "tests.fixtures.templates.echo.handler" not in sys.modules


def test_get_template_by_name():
    assert get_template("echo", FIXTURES).name == "echo"
    assert get_template("nope", FIXTURES) is None


def test_validate_params_fills_defaults_and_coerces():
    echo = get_template("echo", FIXTURES)
    out = validate_params(echo, {"url": "http://x", "limit": "25"})
    assert out == {"url": "http://x", "mode": "recent", "limit": 25}  # default + int coercion


def test_validate_params_requires_required():
    echo = get_template("echo", FIXTURES)
    with pytest.raises(ValueError):
        validate_params(echo, {"mode": "all"})  # missing required `url`


def test_validate_params_rejects_bad_choice():
    echo = get_template("echo", FIXTURES)
    with pytest.raises(ValueError):
        validate_params(echo, {"url": "http://x", "mode": "weekly"})  # not in choices


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
```

- [ ] **Step 4: Run → FAIL** (`uv run pytest tests/test_templates.py -v`) — `bellweather.templates` does not exist yet. (No `make up` needed — these tests touch no Postgres/GCS.)

- [ ] **Step 5: Add the config field** in `src/bellweather/config.py` — append to `Settings` (only `config.py` reads the environment; everything else imports `get_settings()`):
```python
    bellweather_templates_dir: str = "producers"   # dir scanned for */template.toml
```

- [ ] **Step 6: Implement** `src/bellweather/templates.py`:
```python
import importlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from bellweather.config import get_settings

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")


@dataclass
class TemplateParam:
    name: str
    type: str = "str"
    required: bool = False
    default: object | None = None
    choices: list | None = None
    help: str | None = None


@dataclass
class Template:
    name: str
    entrypoint: str
    description: str = ""
    params: list[TemplateParam] = field(default_factory=list)
    default_interval_seconds: int | None = None


def parse_interval(s: str) -> int:
    """'45s' | '30m' | '6h' | '1d' -> seconds. Raises ValueError on bad input."""
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"invalid interval: {s!r} (expected e.g. '30m', '6h', '1d', '45s')")
    return int(m.group(1)) * _INTERVAL_UNITS[m.group(2)]


def _parse_manifest(path: Path) -> Template:
    with path.open("rb") as f:
        data = tomllib.load(f)
    params = []
    for name, spec in (data.get("params") or {}).items():
        params.append(
            TemplateParam(
                name=name,
                type=spec.get("type", "str"),
                required=bool(spec.get("required", False)),
                default=spec.get("default"),
                choices=spec.get("choices"),
                help=spec.get("help"),
            )
        )
    interval = (data.get("schedule") or {}).get("default_interval")
    return Template(
        name=data["name"],
        entrypoint=data["entrypoint"],
        description=data.get("description", ""),
        params=params,
        default_interval_seconds=parse_interval(interval) if interval else None,
    )


def discover_templates(templates_dir: str | None = None) -> dict[str, Template]:
    """Scan <dir>/*/template.toml into Templates. Does NOT import any entrypoint."""
    base = Path(templates_dir or get_settings().bellweather_templates_dir)
    out: dict[str, Template] = {}
    if not base.is_dir():
        return out
    for manifest in sorted(base.glob("*/template.toml")):
        tpl = _parse_manifest(manifest)
        out[tpl.name] = tpl
    return out


def get_template(name: str, templates_dir: str | None = None) -> Template | None:
    return discover_templates(templates_dir).get(name)


_COERCERS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes"),
}


def validate_params(template: Template, params: dict) -> dict:
    """Apply defaults, enforce required + choices, coerce types. ValueError on bad."""
    out: dict = {}
    for p in template.params:
        if p.name in params and params[p.name] is not None:
            raw = params[p.name]
        elif p.required:
            raise ValueError(f"missing required param: {p.name!r}")
        elif p.default is not None:
            raw = p.default
        else:
            continue
        coerce = _COERCERS.get(p.type, str)
        try:
            value = coerce(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"param {p.name!r} not coercible to {p.type}: {raw!r}") from e
        if p.choices is not None and value not in p.choices:
            raise ValueError(f"param {p.name!r}={value!r} not in choices {p.choices}")
        out[p.name] = value
    return out


def load_entrypoint(entrypoint: str):
    """'module.path:function' -> the callable. Run/preview time only (imports code)."""
    module_path, _, func_name = entrypoint.partition(":")
    if not module_path or not func_name:
        raise ValueError(f"invalid entrypoint: {entrypoint!r} (expected 'module:func')")
    module = importlib.import_module(module_path)
    return getattr(module, func_name)
```

- [ ] **Step 7: Run → PASS** (`uv run pytest tests/test_templates.py -v`). Confirm `test_discovery_does_not_import_entrypoint` passes — discovery parses TOML only, never imports the handler.

- [ ] **Step 8: `make check`** (`ruff check . && ruff format --check . && pytest`) green.

- [ ] **Step 9: Commit** (`feat: template manifest discovery + params validation (templates.py)`).

## Acceptance criteria
- `Settings` gains `bellweather_templates_dir: str = "producers"`; no other module reads the environment for it.
- `discover_templates(dir)` finds the fixture `echo` template with all three params and `default_interval_seconds == 1800`, parsing TOML via stdlib `tomllib`.
- Discovery **does not import** any entrypoint module — proven by the `sys.modules` side-effect-marker test.
- `validate_params` fills defaults, coerces types (`"25" -> 25`), raises `ValueError` on a missing required param and on a value outside `choices`.
- `parse_interval` maps `45s/30m/6h/1d` to seconds and raises `ValueError` on garbage.
- `load_entrypoint("module:func")` imports and returns the dummy callable (run-time only).
- Pure: no Postgres/GCS — tests run without `make up`. `make check` green.
