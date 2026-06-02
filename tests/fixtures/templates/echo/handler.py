# tests/fixtures/templates/echo/handler.py
# Module-level side effect: importing this module appends to IMPORT_LOG.
# discover_templates() must NEVER import it (no code execution to list templates);
# only load_entrypoint() (run/preview time) may.
IMPORT_LOG: list[str] = []
IMPORT_LOG.append("imported")


def run(params: dict, client) -> dict | None:
    return {"submitted": 0, "echo": params}
