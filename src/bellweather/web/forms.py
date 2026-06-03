"""Pure validation helpers for the Scrape/Extract page forms.

Lives OUTSIDE web/pages/ on purpose: Streamlit lists every .py file in pages/
in the sidebar (the old leading-underscore trick did not hide it), and keeping
the logic Streamlit-free makes it importable and unit-testable. The pages wire
these into `st.error(...)`; everything here is side-effect free.
"""

from __future__ import annotations

import json
import re

# Spec names address the live backend as a URL *path* segment
# (`/api/scrape-specs/{name}` for get/patch/delete/preview, built by
# str-interpolation with no encoding in web.data.live). Characters like `/`, `?`
# and `#` would change the path/query/fragment and make a created spec
# unmanageable from the UI, so we restrict names to a path-safe allowlist.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_json(label: str, raw: str) -> tuple[object | None, str | None]:
    """Parse a JSON text area; return (value, error_message)."""
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{label} is not valid JSON: {exc}"


def validate_spec_name(name: str) -> str | None:
    """Return an error message for an unusable spec name, else None."""
    if not name.strip():
        return "Spec name is required."
    if not _NAME_RE.fullmatch(name):
        return (
            "Spec name may only contain letters, digits, '.', '-' and '_' "
            "(it addresses the spec in a URL path)."
        )
    return None


def validate_json_object(label: str, value: object) -> str | None:
    """Return an error message if a parsed JSON value is not an object, else None."""
    if not isinstance(value, dict):
        return f"{label} must be a JSON object (e.g. {{...}}), not {type(value).__name__}."
    return None


def build_source_payload(
    *,
    name: str,
    description: str,
    sites_raw: str,
    fetch_adapter: str,
    require_name: bool = True,
) -> tuple[dict | None, list[str]]:
    """Parse + validate the Scrape page's source form; return (payload, errors).

    Pure and Streamlit-free so the page's new-vs-existing branch stays testable.
    ``require_name=False`` on the edit path, where the name comes from the
    selector and is immutable. Blank ``description`` collapses to ``None``;
    blank ``fetch_adapter`` defaults to ``"httpx"``.
    """
    errors: list[str] = []
    sites = [line.strip() for line in sites_raw.splitlines() if line.strip()]

    if require_name:
        err = validate_spec_name(name)
        if err:
            errors.append(err)

    if not sites:
        errors.append("At least one site URL is required.")

    if errors:
        return None, errors

    return {
        "name": name.strip(),
        "description": description.strip() or None,
        "sites": sites,
        "fetch_adapter": fetch_adapter or "httpx",
    }, []


def build_extraction_payload(
    *,
    name: str,
    description: str,
    output_schema_raw: str,
    binding_raw: str,
    llm_model: str,
    require_name: bool = True,
) -> tuple[dict | None, list[str]]:
    """Parse + validate the Extract page's extractor form; return (payload, errors).

    Same contract as :func:`build_source_payload` but for the parse half:
    ``output_schema``/``binding`` must parse to JSON objects; blank
    ``description``/``llm_model`` collapse to ``None``.
    """
    errors: list[str] = []

    output_schema, err_schema = parse_json("Output schema", output_schema_raw)
    if err_schema:
        errors.append(err_schema)
    else:
        err = validate_json_object("Output schema", output_schema)
        if err:
            errors.append(err)

    binding, err_binding = parse_json("Binding", binding_raw)
    if err_binding:
        errors.append(err_binding)
    else:
        err = validate_json_object("Binding", binding)
        if err:
            errors.append(err)

    if require_name:
        err = validate_spec_name(name)
        if err:
            errors.append(err)

    if errors:
        return None, errors

    return {
        "name": name.strip(),
        "description": description.strip() or None,
        "output_schema": output_schema,
        "binding": binding,
        "llm_model": llm_model.strip() or None,
    }, []
