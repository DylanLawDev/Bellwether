"""Pure validation helpers for the Scrape-specs authoring form (6_Scrape.py).

Kept in a non-page module (leading underscore → Streamlit does not list it as a
page) so the logic is importable and unit-testable without importing Streamlit.
The page wires these into `st.error(...)`; everything here is side-effect free.
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
