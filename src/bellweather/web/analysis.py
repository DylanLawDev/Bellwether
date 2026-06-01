"""Pure analysis helpers for the UI prototype (no Streamlit, no I/O — unit-tested)."""

from __future__ import annotations

import pandas as pd


def flag_anomalies(values: pd.Series, sigma: float = 3.0) -> pd.Series:
    """Boolean mask flagging buckets above mean + ``sigma`` * std.

    Simple z-score gate — the v0 "is anything unusual here?" heuristic. Returns an
    all-False mask for series too small or with zero variance (nothing to flag).
    """
    values = pd.Series(values).astype(float)
    if len(values) < 3:
        return pd.Series([False] * len(values), index=values.index)
    std = values.std(ddof=0)
    if std == 0 or pd.isna(std):
        return pd.Series([False] * len(values), index=values.index)
    threshold = values.mean() + sigma * std
    return values > threshold


def top_movers(symbols: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """Tracked symbols ranked by latest coverage value (descending)."""
    return symbols.sort_values("latest_value", ascending=False).head(n).reset_index(drop=True)
