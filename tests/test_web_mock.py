"""Mock backend returns the column/key shapes the data-access contract promises."""

import pandas as pd

from bellweather.web.data import mock, source


def test_tracked_symbols_shape():
    df = mock.get_tracked_symbols()
    assert list(df.columns) == source.TRACKED_SYMBOL_COLUMNS
    assert not df.empty
    assert (df["kind"] == "coverage").all()


def test_observations_shape_and_filter():
    all_keys = mock.get_tracked_symbols()["key"].tolist()
    df = mock.get_observations(all_keys[:2])
    assert list(df.columns) == source.OBSERVATION_COLUMNS
    assert not df.empty
    assert set(df["key"].unique()).issubset(set(all_keys[:2]))
    # value mirrors sample_count (coverage increments both per event)
    assert (df["value"] == df["sample_count"]).all()


def test_observations_unknown_key_is_empty():
    df = mock.get_observations(["theme:DOES_NOT_EXIST"])
    assert list(df.columns) == source.OBSERVATION_COLUMNS
    assert df.empty


def test_raw_records_shape_and_status_filter():
    df = mock.query_raw_records()
    assert list(df.columns) == source.RAW_RECORD_COLUMNS
    assert not df.empty
    failed = mock.query_raw_records(status="failed")
    assert (failed["status"] == "failed").all()


def test_raw_records_pagination():
    page1 = mock.query_raw_records(limit=10, offset=0)
    page2 = mock.query_raw_records(limit=10, offset=10)
    assert len(page1) == 10
    assert set(page1["id"]).isdisjoint(set(page2["id"]))


def test_tags_shape():
    df = mock.query_tags()
    assert list(df.columns) == source.TAG_COLUMNS
    assert not df.empty


def test_queue_stats_keys():
    stats = mock.get_queue_stats()
    assert set(stats) == set(source.QUEUE_STATES)
    assert all(isinstance(v, int) for v in stats.values())


def test_worker_runs_shape():
    df = mock.get_worker_runs()
    assert list(df.columns) == source.WORKER_RUN_COLUMNS
    assert not df.empty


def test_ingestion_rate_shape():
    df = mock.get_ingestion_rate()
    assert list(df.columns) == source.INGESTION_RATE_COLUMNS
    assert not df.empty


def test_settings_view_shape():
    rows = mock.get_settings_view()
    assert isinstance(rows, list) and rows
    for r in rows:
        assert set(r) == {"key", "value", "note"}


def test_deterministic_across_builds():
    # Built once at import under a fixed seed → stable on every launch.
    assert mock.get_tracked_symbols().equals(mock.get_tracked_symbols())
    assert isinstance(mock.get_observations(["theme:ECON_STOCKMARKET"]), pd.DataFrame)
