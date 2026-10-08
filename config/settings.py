"""Centralized, validated configuration for the FTSE 50 Market News Research Agent.

All settings are read from environment variables (and optionally a local `.env`
file). Secrets are never logged.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Override any value with an environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ Ollama
    ollama_api_key: str = Field(default="", description="Paid Ollama API key.")
    ollama_base_url: str = Field(
        default="https://ollama.com/v1",
        description="Base URL of the Ollama OpenAI-compatible endpoint.",
    )
    # NOTE: intentionally no hard default model — the user must choose one.
    ollama_model: str = Field(default="", description="Ollama model id, e.g. 'qwen3:8b'.")

    # ------------------------------------------------------------------ SearXNG
    searxng_base_url: str = Field(
        default="http://localhost:8080",
        description="Base URL of the (self-hosted or remote) SearXNG instance.",
    )
    searxng_timeout_s: float = 20.0

    # ------------------------------------------------------------------ You.com
    you_api_key: str = Field(default="", description="You.com API key (fallback search only).")
    you_timeout_s: float = 20.0

    # ------------------------------------------------------- pipeline behaviour
    max_articles_per_company: int = 10
    top_n_articles: int = 3
    dedup_similarity_threshold: float = 0.85
    news_lookback_days: int = 14
    min_useful_search_results: int = 4

    # --------------------------------------------------------------- concurrency
    max_search_concurrency: int = 4
    max_crawl_concurrency: int = 3
    max_llm_concurrency: int = 2

    # ------------------------------------------------------------------- models
    embedding_model: str = "all-MiniLM-L6-v2"

    # -------------------------------------------------------------------- paths
    cache_dir: Path = Path("data/cache")
    output_dir: Path = Path("data/output")
    mock_data_path: Path = Path("data/mock/mock_news.json")

    # ------------------------------------------------------------------ retries
    llm_max_retries: int = 3
    crawl_max_retries: int = 2
    crawl_timeout_s: float = 45.0
    extraction_min_chars: int = 400

    @field_validator("dedup_similarity_threshold")
    @classmethod
    def _threshold_in_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("dedup_similarity_threshold must be in (0, 1)")
        return v

    # ------------------------------------------------------------- derived API
    def require_live_llm(self) -> None:
        """Raise a clear error if live LLM analysis was requested without config."""
        missing = []
        if not self.ollama_api_key:
            missing.append("OLLAMA_API_KEY")
        if not self.ollama_base_url:
            missing.append("OLLAMA_BASE_URL")
        if not self.ollama_model:
            missing.append("OLLAMA_MODEL")
        if missing:
            raise RuntimeError(
                "Live LLM analysis requested but not configured. Set in .env: "
                + ", ".join(missing)
            )

    @property
    def searxng_search_url(self) -> str:
        return self.searxng_base_url.rstrip("/") + "/search"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings instance."""
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
