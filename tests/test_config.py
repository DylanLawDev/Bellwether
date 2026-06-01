from bellweather.config import get_settings


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("BELLWEATHER_BUCKET", "b")
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url == "postgresql://x/y"
    assert s.bellweather_obs_bucket == "hour"
