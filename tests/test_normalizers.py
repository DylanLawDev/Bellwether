from datetime import datetime, timezone

import pytest

from bellweather.normalizers import (
    NormalizedPoint,
    register,
    get_normalizer,
    known_content_types,
    _REGISTRY,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class _Fake:
    content_type = "fake-series-v1"

    def normalize(self, envelope):
        p = envelope["payload"]
        return [
            NormalizedPoint(
                symbol_key=p["symbol_key"],
                symbol_kind=p["symbol_kind"],
                ts=datetime.fromisoformat(p["points"][0]["ts"]),
                value=float(p["points"][0]["value"]),
            )
        ]


def test_register_and_lookup():
    register(_Fake())
    n = get_normalizer("fake-series-v1")
    assert n is not None
    pts = n.normalize(
        {
            "payload": {
                "symbol_key": "k",
                "symbol_kind": "m",
                "points": [{"ts": "2026-05-31T14:00:00+00:00", "value": "0.5"}],
            }
        }
    )
    assert pts[0].symbol_key == "k" and pts[0].value == 0.5
    assert "fake-series-v1" in known_content_types()


def test_unknown_returns_none():
    assert get_normalizer("does-not-exist") is None


def test_numeric_series_normalizer_is_registered_on_import():
    n = get_normalizer("numeric-series-v1")
    assert n is not None
    assert n.content_type == "numeric-series-v1"


def test_numeric_series_yields_one_point_per_entry():
    from bellweather.normalizers.numeric_series import NumericSeriesNormalizer

    envelope = {
        "payload": {
            "symbol_key": "polymarket:demo:yes",
            "symbol_kind": "market-probability",
            "unit": "probability",
            "description": "Will X happen by D? (YES)",
            "points": [
                {"ts": "2026-05-31T14:00:00+00:00", "value": 0.37},
                {"ts": "2026-05-31T15:00:00+00:00", "value": "0.42"},
            ],
        }
    }
    pts = NumericSeriesNormalizer().normalize(envelope)
    assert len(pts) == 2
    assert pts[0] == NormalizedPoint(
        symbol_key="polymarket:demo:yes",
        symbol_kind="market-probability",
        ts=datetime(2026, 5, 31, 14, 0, tzinfo=timezone.utc),
        value=0.37,
        unit="probability",
        description="Will X happen by D? (YES)",
    )
    assert pts[1].value == 0.42 and isinstance(pts[1].value, float)
    assert pts[1].ts == datetime(2026, 5, 31, 15, 0, tzinfo=timezone.utc)
    assert pts[1].unit == "probability"


def test_numeric_series_optional_fields_default_to_none():
    envelope = {
        "payload": {
            "symbol_key": "src:bare",
            "symbol_kind": "counter",
            "points": [{"ts": "2026-05-31T14:00:00+00:00", "value": 1}],
        }
    }
    pts = get_normalizer("numeric-series-v1").normalize(envelope)
    assert pts[0].unit is None and pts[0].description is None
    assert pts[0].value == 1.0


def test_numeric_series_empty_points_yields_empty_list():
    envelope = {"payload": {"symbol_key": "src:bare", "symbol_kind": "counter", "points": []}}
    assert get_normalizer("numeric-series-v1").normalize(envelope) == []
