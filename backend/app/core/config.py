"""Environment configuration for Loan Lens AI (SPEC §5)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_files() -> tuple[str, ...]:
    """Repo-root `.env`, then `backend/.env`, when those files exist."""
    backend_dir = Path(__file__).resolve().parents[2]
    repo_root = backend_dir.parent
    return tuple(
        str(path)
        for path in (repo_root / ".env", backend_dir / ".env")
        if path.is_file()
    )


# SPEC §6.7 and §22. Analyze HTTP cap stays fixed. The vision call timeout is
# `loanlens_vision_timeout_seconds` (default below): a ceiling, not a fixed wait.
ANALYZE_HTTP_TIMEOUT_SECONDS = 120
VISION_REQUEST_TIMEOUT_SECONDS = 60


class Settings(BaseSettings):
    """Server settings. Field names match SPEC §5 environment variables.

    `VITE_API_BASE_URL` is frontend-only and is not loaded here.
    """

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    loanlens_env: Literal["development", "test", "production"] = Field(
        default="development",
        description="Runtime environment: development, test, or production.",
    )
    loanlens_database_url: str = Field(
        default="sqlite:///./data/loanlens.db",
        description="SQLite URL for the V1 store.",
    )
    loanlens_tmp_dir: str = Field(
        default="data/tmp/images",
        description="Directory for temporary uploaded images.",
    )
    loanlens_max_images_per_analysis: int = Field(
        default=12,
        description="Maximum images accepted for one analysis.",
    )
    loanlens_max_image_bytes: int = Field(
        default=10_485_760,
        description="Maximum size of one image in bytes (10 MiB).",
    )
    loanlens_analysis_ttl_hours: int = Field(
        default=24,
        description="Hours before an analysis and its images expire.",
    )
    vision_provider: Literal["fake", "openai_compatible"] = Field(
        default="fake",
        description="Vision adapter name. Default is fake so tests do not call a live API.",
    )
    vision_api_key: str = Field(
        default="",
        description="Server-only vision API key. Never log or return this value.",
    )
    vision_base_url: str = Field(
        default="",
        description="Optional OpenAI-compatible base URL override. Empty uses the adapter default.",
    )
    vision_model: str = Field(
        default="",
        description="Vision model name. Empty until a live adapter is configured.",
    )
    loanlens_log_level: str = Field(
        default="INFO",
        description="Log level name.",
    )
    loanlens_cors_origins: str = Field(
        default="http://127.0.0.1:5175,http://localhost:5175",
        description=(
            "Comma-separated browser origins allowed by CORS. "
            "An allowlist, including in production. Never `*`."
        ),
    )
    loanlens_worker_enabled: bool = Field(
        default=True,
        description=(
            "When true, one in-process worker runs the oldest queued draft. "
            "When false, analyze still enqueues and the draft stays queued."
        ),
    )
    loanlens_vision_timeout_seconds: int = Field(
        default=VISION_REQUEST_TIMEOUT_SECONDS,
        description=(
            "Maximum seconds for one vision HTTP call. Default 60. "
            "The call returns as soon as the provider completes. "
            "Raise for local CPU Ollama (for example 1200). "
            "Does not block the Analyze HTTP request."
        ),
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.loanlens_cors_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
