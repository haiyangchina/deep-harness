"""Web subsystem settings. Only environment variable *names* live here."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deep_harness.server_config import number, setting


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = setting("web.enabled")
    routing: tuple[str, ...] = tuple(setting("web.routing"))
    pi_providers: tuple[Literal["exa", "parallel-mcp", "bocha", "brave", "tavily"], ...] = Field(
        default=tuple(setting("web.pi_providers")), min_length=1, max_length=5
    )
    extract_routing: tuple[str, ...] = tuple(setting("web.extract_routing"))
    request_timeout_seconds: float = Field(
        default=number("web.request_timeout_seconds"),
        gt=0,
        le=number("web.request_timeout_seconds_max"),
    )
    max_response_chars: int = Field(
        default=number("web.max_response_chars"),
        ge=number("web.max_response_chars_min"),
        le=number("web.max_response_chars_max"),
    )
    max_sources: int = Field(
        default=number("web.max_sources"), ge=1, le=number("web.max_sources_max")
    )
    max_queries: int = Field(
        default=number("web.max_queries"), ge=1, le=number("web.max_queries_max")
    )
    max_concurrency: int = Field(
        default=number("web.max_concurrency"), ge=1, le=number("web.max_concurrency_max")
    )
    total_timeout_seconds: float = Field(
        default=number("web.total_timeout_seconds"),
        gt=0,
        le=number("web.total_timeout_seconds_max"),
    )
    max_cache_entries: int = Field(
        default=number("web.max_cache_entries"), ge=1, le=number("web.max_cache_entries_max")
    )
    cache_ttl_seconds: int = Field(
        default=number("web.cache_ttl_seconds"), ge=0, le=number("web.cache_ttl_seconds_max")
    )
    allow_private_urls: bool = setting("web.allow_private_urls")
    provider_timeout_seconds: float = Field(
        default=number("web.provider_timeout_seconds"),
        gt=0,
        le=number("web.request_timeout_seconds_max"),
    )
    max_search_backends: int = Field(
        default=number("web.max_search_backends"), ge=1, le=number("web.max_queries_max")
    )
    research_search_fraction: float = Field(
        default=number("web.research_search_fraction"), gt=0, lt=1
    )
    relevance_threshold: float = Field(default=number("web.relevance_threshold"), gt=0, le=1)
    relevance_fast_path: float = Field(default=number("web.relevance_fast_path"), gt=0, le=1)

    searxng_url: str | None = setting("web.searxng_url")
    firecrawl_url: str | None = setting("web.firecrawl_url")
    bocha_api_key_env: str = setting("web.bocha_api_key_env")

    tavily_api_key_env: str = setting("web.tavily_api_key_env")
    exa_api_key_env: str = setting("web.exa_api_key_env")
    brave_api_key_env: str = setting("web.brave_api_key_env")
    serper_api_key_env: str = setting("web.serper_api_key_env")
    jina_api_key_env: str = setting("web.jina_api_key_env")
    firecrawl_api_key_env: str = setting("web.firecrawl_api_key_env")

    @field_validator("pi_providers")
    @classmethod
    def distinct_pi_providers(cls, providers):
        return tuple(dict.fromkeys(providers))
