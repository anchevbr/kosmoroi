"""Runtime settings; absence of credentials is a deliberate fail-closed state."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./news.db"
    redis_url: str = "redis://redis:6379/0"
    geo_register_id: str = "fixture-2026-demo"
    admin_token: str = ""
    editor_token: str = ""
    public_base_url: str = "http://localhost:8000"
    draft_only: bool = False
    publication_paused: bool = False
    fixture_mode: bool = False
    llm_provider: str = "fake"
    llm_endpoint: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 30
    source_catalog_path: str = "fixtures/source_catalog.json"
    source_discovery_interval_minutes: int = Field(default=60, ge=1)
    source_discovery_max_per_tick: int = Field(default=5, ge=0)
    daily_call_limit: int = 20
    daily_token_limit: int = 20000
    daily_generation_limit: int = 10
    national_selection_max: int = 5
    regional_selection_max: int = 2
    selection_min_score: int = 4
    max_tokens_per_call: int = 4000
    daily_spend_limit_usd: float | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
