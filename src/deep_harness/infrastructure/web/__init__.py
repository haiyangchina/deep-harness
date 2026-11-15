"""Bounded, provider-neutral web retrieval for agent evidence."""

from deep_harness.infrastructure.web.bing_cn import parse_bing_results
from deep_harness.infrastructure.web.client import WebClient
from deep_harness.infrastructure.web.models import FetchResult, SearchHit
from deep_harness.infrastructure.web.providers import FakeProvider, build_providers

__all__ = [
    "FetchResult",
    "FakeProvider",
    "SearchHit",
    "WebClient",
    "build_providers",
    "parse_bing_results",
]
