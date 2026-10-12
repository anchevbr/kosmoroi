"""HTML-only news collection with configurable AI processing."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./news.db"
    redis_url: str = "redis://redis:6379/0"
    geo_register_id: str = "world-geonames-3.0.2"
    admin_token: str = ""
    editor_token: str = ""
    public_base_url: str = "http://localhost:8000"
    draft_only: bool = False
    publication_paused: bool = False
    fixture_mode: bool = False
    llm_provider: str = "openai"
    llm_model: str = "gpt-5.4-mini"
    llm_api_key: str = ""
    llm_endpoint: str = "https://api.openai.com/v1/responses"
    llm_timeout_seconds: int = Field(default=120, ge=10, le=600)
    reasoning_effort: str = "low"
    web_search_enabled: bool = False
    web_search_interval_minutes: int = Field(default=180, ge=30)
    web_search_queries_per_tick: int = Field(default=2, ge=1, le=10)
    web_search_daily_limit: int = Field(default=8, ge=0)
    web_search_call_cost_usd: float = Field(default=0.01, ge=0)
    model_path: str = "/models/news.gguf"
    model_sha256: str = ""
    model_context_tokens: int = Field(default=32768, ge=8192)
    model_gpu_layers: int = 0
    model_threads: int = Field(default=4, ge=1)
    model_chat_format: str | None = None
    output_language: str = "el"
    publisher_name: str = "Κοσμοροή"
    publisher_logo_url: str | None = None
    source_catalog_path: str = "fixtures/source_catalog.json"
    source_discovery_interval_minutes: int = Field(default=60, ge=1)
    source_discovery_max_per_tick: int = Field(default=10, ge=0)
    public_html_enabled: bool = True
    auto_discover_publishers: bool = True
    maximum_sources: int = Field(default=1000, ge=1)
    crawl_delay_seconds: float = Field(default=2, ge=1)
    crawl_links_per_page: int = Field(default=50, ge=1, le=200)
    crawl_pages_per_source: int = Field(default=12, ge=1, le=100)
    crawl_articles_per_tick: int = Field(default=20, ge=1, le=200)
    crawl_max_depth: int = Field(default=2, ge=0, le=4)
    article_refetch_hours: int = Field(default=6, ge=1)
    article_watch_days: int = Field(default=3, ge=1, le=30)
    raw_text_retention_days: int = Field(default=7, ge=1)
    max_source_chars: int = Field(default=16000, ge=2000, le=30000)
    max_evidence_claims: int = Field(default=8, ge=1, le=12)
    daily_call_limit: int = Field(default=600, ge=0)
    daily_token_limit: int = Field(default=4000000, ge=0)
    daily_generation_limit: int = Field(default=100, ge=0)
    national_selection_max: int = 5
    regional_selection_max: int = 2
    international_selection_max: int = Field(default=10, ge=0)
    general_selection_max: int = Field(default=3, ge=0)
    selection_min_score: int = 4
    max_tokens_per_call: int = Field(default=6000, ge=1)
    verification_max_tokens: int = Field(default=2500, ge=256)
    extraction_max_tokens: int = Field(default=2500, ge=256)
    job_lease_seconds: int = Field(default=1800, ge=60)
    tick_rounds: int = Field(default=8, ge=1, le=20)
    tick_jobs_per_round: int = Field(default=10, ge=1, le=100)
    daily_spend_limit_usd: float | None = Field(default=10, ge=0)
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
