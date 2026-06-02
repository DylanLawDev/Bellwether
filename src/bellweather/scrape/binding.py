from datetime import datetime

from bellweather.extractors import ExtractedTag
from bellweather.normalizers import NormalizedPoint

_REF_PREFIX = "$."


def _resolve(spec, record: dict, *, fetched_at: datetime):
    """Resolve one binding value against a record.

    Rules (locked, §4.3): a string starting "$." is a top-level field reference into
    `record`; the literal "fetched_at" resolves to the `fetched_at` arg; any other
    string is a literal. A "$." ref to a missing key returns None (caller decides
    whether that is fatal for this field).
    """
    if spec == "fetched_at":
        return fetched_at
    if isinstance(spec, str) and spec.startswith(_REF_PREFIX):
        return record.get(spec[len(_REF_PREFIX) :])
    return spec  # literal (str / None / already-resolved)


def _resolve_records(instance: dict, records_path) -> list[dict]:
    """records_path absent/None → the whole instance is ONE record; "$.key" →
    instance["key"] (expected to be a list of records)."""
    if not records_path:
        return [instance]
    if records_path.startswith(_REF_PREFIX):
        value = instance.get(records_path[len(_REF_PREFIX) :])
        return list(value) if isinstance(value, list) else []
    return []


def apply_binding(
    instance: dict, binding: dict, *, fetched_at: datetime
) -> tuple[list[NormalizedPoint], list[ExtractedTag]]:
    observations: list[NormalizedPoint] = []
    tags: list[ExtractedTag] = []

    template = binding["symbol_key"]
    symbol_kind = binding["symbol_kind"]
    value_spec = binding["value"]
    ts_spec = binding["ts"]
    unit_spec = binding.get("unit")
    description_spec = binding.get("description")
    tag_fields = binding.get("tags") or []

    for record in _resolve_records(instance, binding.get("records_path")):
        if not isinstance(record, dict):
            continue
        # symbol_key: missing template field → skip this record (not crash).
        try:
            symbol_key = template.format(**record)
        except (KeyError, IndexError):
            continue
        # value: missing field → skip this record.
        raw_value = _resolve(value_spec, record, fetched_at=fetched_at)
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        ts = _resolve(ts_spec, record, fetched_at=fetched_at)
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(ts)

        unit = _resolve(unit_spec, record, fetched_at=fetched_at) if unit_spec else None
        description = (
            _resolve(description_spec, record, fetched_at=fetched_at) if description_spec else None
        )

        observations.append(
            NormalizedPoint(
                symbol_key=symbol_key,
                symbol_kind=symbol_kind,
                ts=ts,
                value=value,
                unit=unit,
                description=description,
            )
        )
        for name in tag_fields:
            if name in record:
                tags.append(ExtractedTag(tag_type=name, raw_value=str(record[name]), score={}))

    return observations, tags
