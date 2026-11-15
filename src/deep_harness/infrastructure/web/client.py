"""Host-side retrieval orchestration: routing, caching, evidence shaping.

The multi-hop loop lives here rather than in the model: one tool call fans out
several queries, fetches pages concurrently, de-duplicates and returns one
bounded, cited evidence pack. This keeps a small local model out of a long
search/read/think loop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.web.cards import evidence_pack
from deep_harness.infrastructure.web.models import FetchResult, SearchHit, Source
from deep_harness.infrastructure.web.providers import (
    USER_AGENT,
    SearchProvider,
    build_providers,
)
from deep_harness.infrastructure.web.security import normalize_url, validate_public_url
from deep_harness.infrastructure.web.settings import WebSettings

_SEARCH_EXCLUDED = {"static"}


class WebClient:
    """Single owner of the web backends, HTTP client and in-memory caches."""

    def __init__(
        self,
        settings: WebSettings | None = None,
        *,
        providers: dict[str, SearchProvider] | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings or WebSettings()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.providers = (
            providers if providers is not None else build_providers(self.settings, self.client)
        )
        self.search_chain = [
            self.providers[name]
            for name in self.settings.routing
            if name not in _SEARCH_EXCLUDED
            and name in self.providers
            and self.providers[name].available
        ]
        self.fetch_chain = [
            self.providers[name]
            for name in self.settings.extract_routing
            if name in self.providers and self.providers[name].available
        ]
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _cache_get(self, key: tuple):
        if self.settings.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        stamp, value = entry
        if time.monotonic() - stamp > self.settings.cache_ttl_seconds:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: tuple, value) -> None:
        if self.settings.cache_ttl_seconds > 0:
            self._cache[key] = (time.monotonic(), value)

    async def _coalesced(self, key: tuple, factory: Callable[[], Awaitable]):
        """Share one in-flight backend request across concurrent callers."""
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def _search_hits(self, query: str, limit: int):
        key = ("search", query.strip().lower(), limit)
        result = await self._coalesced(key, lambda: self._search_uncached(query, limit))
        self._cache_set(key, result)
        return result

    async def _search_uncached(self, query: str, limit: int):
        errors: list[str] = []
        clean_empty = False
        for provider in self.search_chain:
            try:
                hits = await provider.search(query, limit)
            except DomainError as exc:
                errors.append(f"{provider.name}:{exc.code}")
                continue
            except Exception:
                errors.append(f"{provider.name}:error")
                continue
            hits = [hit for hit in hits if hit.url][:limit]
            if hits:
                return hits, provider.name, errors
            clean_empty = True
        if not self.search_chain:
            raise DomainError(
                "web_unavailable",
                "没有可用的联网检索后端：请配置 API key、SearXNG 地址，或安装免密钥 ddgs 后端",
            )
        if errors and not clean_empty:
            raise DomainError("web_unavailable", f"检索后端均不可用（{'、'.join(errors[:3])}）")
        return [], "none", errors

    async def _fetch_result(self, url: str) -> FetchResult:
        key = ("fetch", url)
        result = await self._coalesced(key, lambda: self._fetch_uncached(url))
        if result.status in {"ok", "partial"}:
            self._cache_set(key, result)
        return result

    async def _fetch_uncached(self, url: str) -> FetchResult:
        errors: list[str] = []
        for provider in self.fetch_chain:
            try:
                result = await provider.fetch(url)
            except DomainError as exc:
                errors.append(f"{provider.name}:{exc.code}")
                continue
            except Exception:
                errors.append(f"{provider.name}:error")
                continue
            if result is not None and result.text:
                return result
        detail = f"（{'、'.join(errors[:2])}）" if errors else "：无可用读取后端"
        return FetchResult(url=url, status="error", note="未能读取页面" + detail)

    async def search(self, query: str, limit: int, max_chars: int) -> str:
        hits, provider, skipped = await self._search_hits(query, limit)
        sources = [
            Source(
                title=hit.title,
                url=hit.url,
                text=hit.text or hit.snippet,
                provider=hit.provider or provider,
            )
            for hit in hits
        ]
        meta = f"后端={provider}；回退={','.join(skipped) or '无'}"
        return evidence_pack(f"联网检索：{query[:80]}", sources, budget=max_chars, meta=meta)

    async def fetch(self, url: str, max_chars: int, offset: int = 0) -> str:
        url = validate_public_url(url, allow_private=self.settings.allow_private_urls)
        result = await self._fetch_result(url)
        if not result.text:
            return evidence_pack(
                "网页读取",
                [],
                budget=max_chars,
                meta=f"URL={url}；状态={result.status}；{result.note}",
            )
        remaining = result.text[offset:]
        truncated = len(remaining) > max_chars
        source = Source(
            title=result.title,
            url=url,
            text=remaining[:max_chars],
            provider=result.provider,
            note=(
                f"offset={offset}；{'内容已截断，可增大 offset 续读' if truncated else '完整读取'}"
            ),
        )
        return evidence_pack(
            "网页读取",
            [source],
            budget=max_chars + 800,
            meta=f"URL={url}；后端={result.provider}",
        )

    async def extract(self, urls: list[str], max_chars: int) -> str:
        validated = [
            validate_public_url(url, allow_private=self.settings.allow_private_urls)
            for url in urls
        ]
        results = await asyncio.gather(
            *(self._safe_fetch(url) for url in validated), return_exceptions=False
        )
        per_page = max(300, max_chars // max(1, len(results)))
        sources = [
            Source(
                title=result.title,
                url=result.url,
                text=result.text[:per_page],
                provider=result.provider,
                note=result.status if result.status != "ok" else "",
            )
            for result in results
        ]
        return evidence_pack("网页批量读取", sources, budget=max_chars + 800, meta="批量抓取")

    async def _safe_fetch(self, url: str) -> FetchResult:
        try:
            return await self._fetch_result(url)
        except DomainError as exc:
            return FetchResult(url=url, status="error", note=exc.code)

    async def research(
        self,
        question: str,
        queries: list[str],
        max_sources: int,
        max_chars: int,
    ) -> str:
        """One-call deep research: fan out, de-duplicate, enrich, pack evidence."""
        effective = [query.strip() for query in (queries or [question]) if query.strip()]
        if not effective:
            effective = [question]
        effective = effective[: self.settings.max_queries]
        limit = max(3, min(8, max_sources))
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def one(query: str):
            async with semaphore:
                try:
                    return await self._search_hits(query, limit)
                except DomainError:
                    return [], "unavailable", ["all backends failed"]

        bundles = await asyncio.gather(*(one(query) for query in effective))
        ranked = [list(bundle[0]) for bundle in bundles]
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for position in range(limit):
            for hits in ranked:
                if position >= len(hits):
                    continue
                hit = hits[position]
                key = normalize_url(hit.url)
                if not hit.url or key in seen:
                    continue
                seen.add(key)
                merged.append(hit)
                if len(merged) >= max_sources:
                    break
            if len(merged) >= max_sources:
                break

        async def enrich(hit: SearchHit) -> SearchHit:
            if hit.text.strip():
                return hit
            try:
                url = validate_public_url(
                    hit.url, allow_private=self.settings.allow_private_urls
                )
            except DomainError:
                return hit
            async with semaphore:
                try:
                    result = await self._fetch_result(url)
                except DomainError:
                    return hit
            if not result.text:
                return hit
            return SearchHit(
                title=hit.title or result.title,
                url=hit.url,
                snippet=hit.snippet,
                text=result.text[:8000],
                provider=hit.provider or result.provider,
            )

        enriched = await asyncio.gather(*(enrich(hit) for hit in merged))
        sources = [
            Source(
                title=hit.title,
                url=hit.url,
                text=hit.text or hit.snippet,
                provider=hit.provider,
            )
            for hit in enriched
        ]
        providers = ",".join(dict.fromkeys(bundle[1] for bundle in bundles))
        meta = (
            f"问题={question[:100]}；子查询={len(effective)}；"
            f"后端={providers}；去重后来源={len(sources)}"
        )
        return evidence_pack(f"深度联网研究：{question[:80]}", sources, budget=max_chars, meta=meta)

    def health(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "search_backends": [provider.name for provider in self.search_chain],
            "extract_backends": [provider.name for provider in self.fetch_chain],
            "configured_backends": sorted(
                name for name, provider in self.providers.items() if provider.available
            ),
            "cache_entries": len(self._cache),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
