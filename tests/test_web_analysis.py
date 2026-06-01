"""Anomaly helper flags real spikes and stays quiet on flat/short series."""

import pandas as pd

from bellweather.web.analysis import flag_anomalies, top_movers


def test_flags_injected_spike():
    values = pd.Series([5.0] * 50 + [60.0] + [5.0] * 50)
    mask = flag_anomalies(values)
    assert mask.sum() == 1
    assert bool(mask.iloc[50]) is True


def test_flat_series_has_no_anomalies():
    assert not flag_anomalies(pd.Series([5.0] * 20)).any()


def test_short_series_has_no_anomalies():
    assert not flag_anomalies(pd.Series([1.0, 99.0])).any()


def test_higher_sigma_never_flags_more():
    # Noisy baseline + one spike; raising the bar can only flag fewer points.
    values = pd.Series(([4.0, 6.0, 5.0, 7.0, 3.0] * 10) + [40.0])
    low = flag_anomalies(values, sigma=2.0).sum()
    high = flag_anomalies(values, sigma=5.0).sum()
    assert bool(flag_anomalies(values, sigma=2.0).iloc[-1]) is True
    assert high <= low


def test_top_movers_orders_by_latest_value():
    df = pd.DataFrame(
        {
            "key": ["a", "b", "c"],
            "latest_value": [3.0, 9.0, 1.0],
            "total_samples": [10, 20, 5],
        }
    )
    out = top_movers(df, n=2)
    assert out["key"].tolist() == ["b", "a"]
