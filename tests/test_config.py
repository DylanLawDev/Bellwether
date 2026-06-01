import pytest

from bellweather.config import get_settings


@pytest.fixture
def _isolate_settings():
    # get_settings() is a process-wide @lru_cache. Clear it BEFORE so we read the
    # monkeypatched env, and AFTER so the throwaway DATABASE_URL below does not
    # poison the cache for later DB tests (they would otherwise inherit it and
    # try to connect to host "x"). See PR discussion for the original CI failure.
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_read_from_env(monkeypatch, _isolate_settings):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("BELLWEATHER_BUCKET", "b")
    s = get_settings()
    assert s.database_url == "postgresql://x/y"
    assert s.bellweather_obs_bucket == "hour"
