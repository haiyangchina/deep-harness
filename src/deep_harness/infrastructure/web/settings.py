"""Web subsystem settings. Only environment variable *names* live here."""

from pydantic import BaseModel, ConfigDict, Field


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    routing: tuple[str, ...] = (
        "tavily",
        "exa",
        "brave",
        "serper",
        "searxng",
        "bing_cn",
        "ddgs",
        "jina",
    )
    extract_routing: tuple[str, ...] = ("static",)
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    max_response_chars: int = Field(default=8000, ge=500, le=32_000)
    max_sources: int = Field(default=6, ge=1, le=12)
    max_queries: int = Field(default=4, ge=1, le=8)
    max_concurrency: int = Field(default=4, ge=1, le=8)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)
    allow_private_urls: bool = False

    searxng_url: str | None = None
    firecrawl_url: str | None = None

    tavily_api_key_env: str = "TAVILY_API_KEY"
    exa_api_key_env: str = "EXA_API_KEY"
    brave_api_key_env: str = "BRAVE_SEARCH_API_KEY"
    serper_api_key_env: str = "SERPER_API_KEY"
    jina_api_key_env: str = "JINA_API_KEY"
    firecrawl_api_key_env: str = "FIRECRAWL_API_KEY"
