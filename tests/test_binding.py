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
