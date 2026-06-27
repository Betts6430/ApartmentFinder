from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_maps_api_key: str = ""
    cache_dir: Path = Path("./data")
    search_cache_ttl_hours: int = 3

    # --- Saved-search email alerts (all optional; feature is off unless configured) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""           # defaults to smtp_user if blank
    alert_email_to: str = ""      # recipient FALLBACK; the Settings page (DB) overrides this
    alert_poll_minutes: int = 0   # background re-scrape cadence; 0 disables the poller

    @property
    def smtp_configured(self) -> bool:
        """Whether the app can send mail at all (a sending server is set up).
        The recipient is resolved separately (Settings page → DB, else
        `alert_email_to` fallback) — see services/alerts.py."""
        return bool(self.smtp_host)

    @property
    def alert_sender(self) -> str:
        return self.smtp_from or self.smtp_user


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)
