from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _jvcapture_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_artifact_dir() -> Path:
    return _jvcapture_project_root() / ".files" / "jvcapture_artifacts"


_JVCAPTURE_ROOT_ENV = _jvcapture_project_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JVCAPTURE_",
        extra="ignore",
        env_file=_JVCAPTURE_ROOT_ENV if _JVCAPTURE_ROOT_ENV.is_file() else None,
        env_file_encoding="utf-8",
    )

    max_active_jobs: int = 1000
    max_concurrent_captures: int = 2
    max_job_duration_seconds: int = 600
    completed_job_retention_hours: int = 24
    failed_webhook_retention_days: int = 7
    webhook_max_concurrent: int = 8
    webhook_read_timeout_seconds: int = 120
    webhook_max_retries: int = 5
    artifact_dir: Path = Field(default_factory=_default_artifact_dir)
    public_base_url: Optional[str] = None
    cors_origins: Optional[str] = None

    @field_validator("artifact_dir", mode="after")
    @classmethod
    def _resolve_artifact_dir(cls, v: Path) -> Path:
        expanded = v.expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (_jvcapture_project_root() / expanded).resolve()

    def resolved_cors_origins(self) -> List[str]:
        default_dev = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
        raw = (self.cors_origins or "").strip()
        if not raw:
            return default_dev
        parsed = [x.strip() for x in raw.split(",") if x.strip()]
        return parsed if parsed else default_dev


@lru_cache
def get_settings() -> Settings:
    return Settings()