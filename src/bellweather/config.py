from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    bellweather_bucket: str
    storage_emulator_host: str | None = None
    bellweather_api_url: str = "http://localhost:8000"
    bellweather_obs_bucket: Literal["hour", "15min"] = "hour"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
