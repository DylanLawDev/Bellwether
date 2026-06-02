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
