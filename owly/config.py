"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    xai_api_key: str = ""
    xai_model: str = "grok-4.5"
    xai_base_url: str = "https://api.x.ai/v1"

    output_dir: Path = PROJECT_ROOT / "editions"
    data_dir: Path = PROJECT_ROOT / "data"

    max_output_tokens: int = 4096
    ingestion_hours: int = 12

    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8741
    owly_api_key: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "owly.db"

    def ensure_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
