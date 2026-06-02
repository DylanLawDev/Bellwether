from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str | None = None
    bellweather_bucket: str | None = None
    storage_emulator_host: str | None = None
    bellweather_api_url: str = "http://localhost:8000"
    bellweather_obs_bucket: Literal["hour", "15min"] = "hour"
    bellweather_templates_dir: str = "producers"  # dir scanned for */template.toml
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class UISettings(BaseSettings):
    """Minimal settings for the web UI running as a thin API client.

    Only needs the read-API base URL. Deliberately does NOT require the
    pipeline's ``database_url`` / ``bellweather_bucket``, so the UI can run in a
    client-only environment (``BELLWEATHER_UI_SOURCE=live``) against a remote API
    without carrying the server's DB/GCS secrets.
    """

    bellweather_api_url: str = "http://localhost:8000"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_ui_settings() -> UISettings:
    return UISettings()
