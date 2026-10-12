from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _default_searxng_url() -> str:
    # SEARXNG_URL is the guide's name (set by docker-compose.yml). When running this package next to
    # the FTSE pipeline, fall back to that project's existing SEARXNG_BASE_URL variable.
    return os.getenv("SEARXNG_URL") or os.getenv("SEARXNG_BASE_URL") or "http://searxng:8080"


@dataclass(frozen=True)
class Settings:
    """All runtime settings, read from environment variables (see docker-compose.yml)."""

    searxng_url: str = field(default_factory=_default_searxng_url)
    valkey_url: str = field(default_factory=lambda: _env("VALKEY_URL", "redis://valkey:6379/1"))
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", "http://qdrant:6333"))
    qdrant_collection: str = field(default_factory=lambda: _env("QDRANT_COLLECTION", "research_chunks"))

    # Any OpenAI-compatible, self-hosted server: Ollama, vLLM, llama.cpp server, LM Studio...
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://ollama:11434/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "qwen2.5:14b-instruct"))
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 300.0))

    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "BAAI/bge-m3"))
    rerank_model: str = field(default_factory=lambda: _env("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"))
    device: str = field(default_factory=lambda: _env("MODEL_DEVICE", "cpu"))  # "cuda" if you have a GPU

    # Cache TTLs (seconds)
    search_cache_ttl: int = field(default_factory=lambda: _env_int("SEARCH_CACHE_TTL", 6 * 3600))
    news_cache_ttl: int = field(default_factory=lambda: _env_int("NEWS_CACHE_TTL", 15 * 60))
    page_cache_ttl: int = field(default_factory=lambda: _env_int("PAGE_CACHE_TTL", 7 * 24 * 3600))

    # Agent loop budget
    max_rounds: int = field(default_factory=lambda: _env_int("MAX_ROUNDS", 3))
    queries_per_round: int = field(default_factory=lambda: _env_int("QUERIES_PER_ROUND", 5))
    results_per_query: int = field(default_factory=lambda: _env_int("RESULTS_PER_QUERY", 8))
    max_pages_per_round: int = field(default_factory=lambda: _env_int("MAX_PAGES_PER_ROUND", 15))
    max_per_domain: int = field(default_factory=lambda: _env_int("MAX_PER_DOMAIN", 2))
    top_k_passages: int = field(default_factory=lambda: _env_int("TOP_K_PASSAGES", 14))

    max_total_queries: int = field(default_factory=lambda: _env_int("MAX_TOTAL_QUERIES", 10))
    search_timeout: float = field(default_factory=lambda: _env_float("SEARCH_TIMEOUT", 60.0))  # per query, all retries
    fetch_timeout: float = field(default_factory=lambda: _env_float("FETCH_TIMEOUT", 60.0))    # per page
    coverage_min_passages: int = field(default_factory=lambda: _env_int("COVERAGE_MIN_PASSAGES", 2))
    coverage_min_sources: int = field(default_factory=lambda: _env_int("COVERAGE_MIN_SOURCES", 2))
    # bge-reranker scores for broad questions are low (best passage ~0.2 for "major developments over the past
    # month"), so the default equals the reranker's own relevance floor (0.05): a passage must survive reranking.
    coverage_min_score: float = field(default_factory=lambda: _env_float("COVERAGE_MIN_SCORE", 0.05))
    max_snippet_passages: int = field(default_factory=lambda: _env_int("MAX_SNIPPET_PASSAGES", 5))

    # Background jobs (RabbitMQ broker + Celery workers; results live in Valkey DB 2)
    rabbitmq_host: str = field(default_factory=lambda: _env("RABBITMQ_HOST", "rabbitmq"))
    rabbitmq_user: str = field(default_factory=lambda: _env("RABBITMQ_DEFAULT_USER", "research"))
    rabbitmq_password: str = field(default_factory=lambda: _env("RABBITMQ_DEFAULT_PASS", ""), repr=False)
    result_backend_url: str = field(default_factory=lambda: _env("CELERY_RESULT_BACKEND", "redis://valkey:6379/2"))
    job_soft_time_limit: int = field(default_factory=lambda: _env_int("JOB_SOFT_TIME_LIMIT", 2400))
    job_time_limit: int = field(default_factory=lambda: _env_int("JOB_TIME_LIMIT", 2700))
    job_max_retries: int = field(default_factory=lambda: _env_int("JOB_MAX_RETRIES", 2))
    job_result_ttl: int = field(default_factory=lambda: _env_int("JOB_RESULT_TTL", 24 * 3600))
    sync_wait_seconds: int = field(default_factory=lambda: _env_int("SYNC_WAIT_SECONDS", 1500))

    # Politeness
    search_concurrency: int = field(default_factory=lambda: _env_int("SEARCH_CONCURRENCY", 3))
    fetch_concurrency: int = field(default_factory=lambda: _env_int("FETCH_CONCURRENCY", 6))
    per_host_delay: float = field(default_factory=lambda: _env_float("PER_HOST_DELAY", 1.5))
    user_agent: str = field(
        default_factory=lambda: _env(
            "FETCH_USER_AGENT",
            "Mozilla/5.0 (compatible; Yantra3lokaResearchBot/0.1; +mailto:dev@example.com)",
        )
    )


settings = Settings()
