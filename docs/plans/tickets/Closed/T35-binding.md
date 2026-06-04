# T35 — `apply_binding` — JSON → observations + tags (pure)

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` (§4.3 Binding; K2 arbitrary user JSON + declarative binding).
**Depends on:** T09 (extractor registry / `ExtractedTag`), T19 (normalizer registry / `NormalizedPoint`).
**Branch:** `ticket/T35-binding`. **PR, do not merge without approval.**

## Goal
Add `apply_binding(...)` — the **pure, stdlib-only** translator that maps a single LLM-extracted JSON
instance onto Bellwether's gold/silver shapes using the spec's declarative `binding` (K2). It walks the
binding's `records_path` to a list of records, builds each `symbol_key` from a `str.format` template,
resolves `value`/`ts`/`unit`/`description`/`tags` with a minimal field-reference resolver, and returns
`(list[NormalizedPoint], list[ExtractedTag])`. It reuses the existing gold-value point shape
(`NormalizedPoint` from T19) and the tag shape (`ExtractedTag` from T09) so the worker can later write
them with the helpers it already has — this ticket adds **no** I/O, no DB, no LLM, no network. It is the
highest-TDD-value unit in the epic (§4.3), so it is built test-first against fixtures. A record that is
missing its `value` or a `symbol_key` field is **skipped, not crashed** (counting/logging is the
extractor's concern in T38), keeping the binding total.

## Files
- Create: `src/bellweather/scrape/binding.py` — `apply_binding` + the private resolver helpers
  (`_resolve`, `_resolve_records`). Pure: imports only `datetime` (stdlib), `NormalizedPoint`,
  `ExtractedTag`.
- Create (only if `src/bellweather/scrape/__init__.py` does not yet exist): an **empty** package
  marker so the module is importable. The build plan assigns `scrape/__init__.py` to **T34** — if T34
  has already landed it, do **not** recreate it; this ticket just import-tests the `scrape.binding`
  module path and depends only on the two leaf shapes, never on T34's `specs.py`.
- Test: `tests/test_binding.py` — pure unit tests (no DB, no LLM, no network); single record, list via
  `$.items`, `symbol_key` templating, `float()` coercion, `ts=fetched_at` vs a `$.field` ISO ts,
  literal-vs-`$.`-ref `unit`/`description`, tag emission, and the skip cases.

## Interface
Copied verbatim from the build plan's "Locked interfaces" (`scrape/binding.py`):
```python
from bellweather.extractors import ExtractedTag
from bellweather.normalizers import NormalizedPoint

def apply_binding(instance: dict, binding: dict, *, fetched_at: datetime
                  ) -> tuple[list[NormalizedPoint], list[ExtractedTag]]: ...
```
**Return order is load-bearing: `(observations, tags)` — observations FIRST.** Every caller unpacks
`obs, tags = apply_binding(...)` (T38 wraps them `ExtractionResult(tags=tags, observations=obs)`; T39's
preview does the same). Do not swap the tuple.

Binding contract (jsonb on the spec; minimal field-reference resolver — flat fields only, enrich later):
```jsonc
{
  "records_path": "$.items",          // absent/None → the whole instance is ONE record;
                                      //   "$.key"   → instance["key"] must be a list of records
  "symbol_key":   "scrape:prices:{category}:{name}",  // str.format over a record's fields
  "symbol_kind":  "scraped-metric",   // literal → NormalizedPoint.symbol_kind
  "value":        "$.price",          // field ref → float(record["price"])
  "ts":           "fetched_at",       // the literal "fetched_at" → the param; else "$.field" parsed ISO
  "unit":         "usd",              // literal, OR "$.field" ref (a value starting "$." is a ref)
  "description":  "$.title",          // optional; literal or "$.field" ref
  "tags":         ["category", "in_stock"]   // field names → ExtractedTag(tag_type=name, raw_value=str(val), score={})
}
```
Resolver rules (locked): a string starting `"$."` is a **field reference** into the current record
(top-level key only); the literal `"fetched_at"` in `ts` resolves to the `fetched_at` arg; any other
string is a **literal**. `symbol_key` is `template.format(**record)` (missing key → that record is
skipped, not crashed — log/count is the extractor's concern). One `NormalizedPoint` per record;
`unit`/`description` resolved per-record (ref or literal). Missing/duplicate handling: a record missing
`value` or `symbol_key` fields is skipped.

Existing leaf shapes this reuses (do **not** redefine):
- `bellweather.normalizers.NormalizedPoint(symbol_key, symbol_kind, ts, value, unit=None, description=None)` (T19).
- `bellweather.extractors.ExtractedTag(tag_type, raw_value, score)` (T09).

## Steps

- [ ] **Step 1: Failing test** `tests/test_binding.py`. Pure unit, no infra — these run without
  `make up`. Covers: single record (no `records_path`), a list via `$.items`, `symbol_key` templating,
  `float()` coercion, `ts="fetched_at"` (→ arg) vs a `$.field` ISO ts, literal-vs-`$.`-ref `unit` and
  `description`, tag emission, and both skip cases (missing `value`, missing `symbol_key` field).
```python
from datetime import datetime, timezone

from bellweather.extractors import ExtractedTag
from bellweather.normalizers import NormalizedPoint
from bellweather.scrape.binding import apply_binding

# Fixed clock used for every "fetched_at"-resolved timestamp below.
FETCHED_AT = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_single_record_no_records_path():
    # No records_path → the whole instance is ONE record. ts="fetched_at" → the arg.
    instance = {"name": "widget", "category": "tools", "price": "12.50"}
    binding = {
        "symbol_key": "scrape:prices:{category}:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
        "unit": "usd",
    }
    obs, tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert obs == [
        NormalizedPoint(
            symbol_key="scrape:prices:tools:widget",
            symbol_kind="scraped-metric",
            ts=FETCHED_AT,
            value=12.50,
            unit="usd",
            description=None,
        )
    ]
    assert tags == []
    # value was coerced to float, not left a string.
    assert isinstance(obs[0].value, float)


def test_records_path_list():
    # "$.items" → instance["items"] is a list → one observation per record.
    instance = {
        "items": [
            {"name": "a", "category": "tools", "price": 1},
            {"name": "b", "category": "tools", "price": 2},
        ]
    }
    binding = {
        "records_path": "$.items",
        "symbol_key": "scrape:{category}:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
    }
    obs, _tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert [o.symbol_key for o in obs] == ["scrape:tools:a", "scrape:tools:b"]
    assert [o.value for o in obs] == [1.0, 2.0]


def test_ts_field_ref_parsed_iso():
    # ts is a "$." ref → datetime.fromisoformat(record["observed_at"]).
    observed = "2026-05-31T09:30:00+00:00"
    instance = {"name": "x", "category": "c", "price": 3.5, "observed_at": observed}
    binding = {
        "symbol_key": "s:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "$.observed_at",
    }
    obs, _tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert obs[0].ts == datetime.fromisoformat(observed)
    # Not the fetched_at arg — the field ref won.
    assert obs[0].ts != FETCHED_AT


def test_unit_and_description_literal_vs_ref():
    # unit is a literal ("usd"); description is a "$." ref into the record.
    instance = {"name": "x", "category": "c", "price": 4, "title": "A Widget"}
    binding = {
        "symbol_key": "s:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
        "unit": "usd",
        "description": "$.title",
    }
    obs, _tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert obs[0].unit == "usd"
    assert obs[0].description == "A Widget"


def test_tags_emitted_per_field():
    # Each name in "tags" → ExtractedTag(tag_type=name, raw_value=str(value), score={}).
    instance = {"name": "x", "category": "tools", "price": 5, "in_stock": True}
    binding = {
        "symbol_key": "s:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
        "tags": ["category", "in_stock"],
    }
    _obs, tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert tags == [
        ExtractedTag(tag_type="category", raw_value="tools", score={}),
        ExtractedTag(tag_type="in_stock", raw_value="True", score={}),
    ]


def test_record_missing_value_is_skipped():
    # First record has no "price" → skipped (not crashed); second one lands.
    instance = {
        "items": [
            {"name": "a", "category": "c"},
            {"name": "b", "category": "c", "price": 9},
        ]
    }
    binding = {
        "records_path": "$.items",
        "symbol_key": "s:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
    }
    obs, _tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert [o.symbol_key for o in obs] == ["s:b"]


def test_record_missing_symbol_key_field_is_skipped():
    # symbol_key template needs {category}; the first record lacks it → skipped.
    instance = {
        "items": [
            {"name": "a", "price": 1},
            {"name": "b", "category": "tools", "price": 2},
        ]
    }
    binding = {
        "records_path": "$.items",
        "symbol_key": "scrape:{category}:{name}",
        "symbol_kind": "scraped-metric",
        "value": "$.price",
        "ts": "fetched_at",
    }
    obs, _tags = apply_binding(instance, binding, fetched_at=FETCHED_AT)
    assert [o.symbol_key for o in obs] == ["scrape:tools:b"]
```

- [ ] **Step 2: Run → FAIL.** `uv run pytest tests/test_binding.py -v` →
  `ModuleNotFoundError: No module named 'bellweather.scrape.binding'` (or, if `scrape/__init__.py`
  is missing too, `No module named 'bellweather.scrape'`).

- [ ] **Step 3: Implement.** If `src/bellweather/scrape/__init__.py` does not exist (T34 not yet
  merged), create it **empty** (it is T34's file — leave it empty so the two tickets don't conflict on
  content). Then create `src/bellweather/scrape/binding.py` verbatim (this body is already
  ruff-formatted at `line-length = 100`, so Step 5's `make check` stays green):
```python
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
```

- [ ] **Step 4: Run → PASS.** `uv run pytest tests/test_binding.py -v` → 7 passed.

- [ ] **Step 5: Full gate.** `make check` (`ruff check . && ruff format --check . && pytest`) green.
  (These tests need no `make up` — they touch no Postgres/GCS/LLM — but the wider suite still does, so
  keep the emulator up for the full run.)

- [ ] **Step 6: Commit** (`feat: add pure apply_binding (JSON → observations + tags)`).

## Acceptance criteria
- `apply_binding(instance, binding, *, fetched_at) -> tuple[list[NormalizedPoint], list[ExtractedTag]]`
  matches the locked signature, is **pure** (stdlib + the two leaf shapes only — no DB, GCS, LLM, or
  network import), and reuses `NormalizedPoint` (T19) and `ExtractedTag` (T09) unchanged.
- `records_path` absent/None → the whole `instance` is one record; `"$.key"` → `instance["key"]`
  iterated as a list (one `NormalizedPoint` per record).
- `symbol_key = template.format(**record)`; `value = float(record[ref])`; both resolve the per-record
  `unit`/`description` (literal or `"$."` ref); `ts == fetched_at` when `ts` is the literal
  `"fetched_at"`, else `datetime.fromisoformat` of the `"$."`-referenced field.
- Each field listed in `tags` emits `ExtractedTag(tag_type=name, raw_value=str(value), score={})`.
- Observations are returned in **`records_path` order** — one per surviving record, in list order — so
  order-sensitive consumers (T39 preview's `symbols`/`sample`) rest on a stated guarantee, not luck.
- A record missing its `value` field or a `symbol_key` template field is **skipped, not crashed**, and
  surviving records still land.
- No I/O, no migration, no new dependency; tests run without `make up`; `make check` green.
