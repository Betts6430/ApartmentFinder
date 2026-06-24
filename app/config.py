from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_maps_api_key: str = ""
    cache_dir: Path = Path("./data")
    search_cache_ttl_hours: int = 3


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)
