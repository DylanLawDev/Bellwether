"""Mock backend returns the column/key shapes the data-access contract promises."""

import pandas as pd

from bellweather.web.data import mock, source


def test_tracked_symbols_shape():
    df = mock.get_tracked_symbols()
    assert list(df.columns) == source.TRACKED_SYMBOL_COLUMNS
    assert not df.empty
    assert (df["kind"] == "coverage").all()


def test_symbol_key_is_composed_from_parts():
    # Must match gold.upsert_coverage's f"{tag_type}:{raw_value}" keying.
    df = mock.get_tracked_symbols()
    composed = df["tag_type"] + ":" + df["raw_value"]
    assert (df["key"] == composed).all()


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


def test_raw_records_search_and_content_type_filters():
    # search matches idempotency_key (case-insensitive); content_type narrows rows.
    all_rows = mock.query_raw_records(limit=1000)
    needle = all_rows.iloc[0]["idempotency_key"][:10].upper()
    hits = mock.query_raw_records(search=needle, limit=1000)
    assert not hits.empty
    assert hits["idempotency_key"].str.contains(needle, case=False).all()
    assert (
        mock.query_raw_records(content_type="gdelt-gkg-v2", limit=1000).shape[0]
        == all_rows.shape[0]
    )
    assert mock.query_raw_records(content_type="nope", limit=1000).empty


def test_records_use_the_real_gdelt_identifiers():
    # Must mirror producers/gdelt + GdeltGkgExtractor so live mode shows the same rows.
    df = mock.query_raw_records(limit=1000)
    assert (df["content_type"] == "gdelt-gkg-v2").all()
    assert (df["source"] == "gdelt.gkg").all()


def test_search_is_literal_not_regex():
    # Regex metacharacters must be treated literally — never crash, never over-match.
    assert mock.query_raw_records(search="[", limit=1000).empty
    assert mock.query_tags(search="[", limit=1000).empty
    assert mock.query_raw_records(search="zzz.zzz", limit=1000).empty


def test_tags_shape():
    df = mock.query_tags()
    assert list(df.columns) == source.TAG_COLUMNS
    assert not df.empty


def test_tags_type_and_search_filters():
    themes = mock.query_tags(tag_type="theme", limit=1000)
    assert not themes.empty
    assert (themes["tag_type"] == "theme").all()
    hits = mock.query_tags(search="Ukraine", limit=1000)
    assert not hits.empty
    assert hits["raw_value"].str.contains("Ukraine", case=False).all()


def test_tags_score_is_a_mapping():
    # `score` mirrors the tags.score jsonb column — a dict, not a scalar.
    df = mock.query_tags(limit=1000)
    assert df["score"].map(lambda s: isinstance(s, dict)).all()


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
