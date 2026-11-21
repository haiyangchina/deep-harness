"""Host-side retrieval orchestration: routing, caching, evidence shaping.

The multi-hop loop lives here rather than in the model: one tool call fans out
several queries, fetches pages concurrently, de-duplicates and returns one
bounded, cited evidence pack. This keeps a small local model out of a long
search/read/think loop.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
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
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._public_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
        )
        self.providers = (
            providers
            if providers is not None
            else build_providers(self.settings, self.client, self._public_client)
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
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(self.settings.max_concurrency)
        self._closed = False

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
        self._cache.move_to_end(key)
        return value

    def _cache_set(self, key: tuple, value) -> None:
        ttl = self.settings.cache_ttl_seconds
        if ttl <= 0:
            return
        now = time.monotonic()
        for expired, (stamp, _) in tuple(self._cache.items()):
            if now - stamp > ttl:
                self._cache.pop(expired)
        self._cache[key] = (now, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self.settings.max_cache_entries:
            self._cache.popitem(last=False)

    async def _produce(self, key, factory, cacheable):
        try:
            async with asyncio.timeout(self.settings.total_timeout_seconds):
                async with self._slots:
                    result = await factory()
            if cacheable(result):
                self._cache_set(key, result)
            return result
        finally:
            self._inflight.pop(key, None)

    async def _coalesced(self, key: tuple, factory: Callable[[], Awaitable], cacheable):
        """Share one request until its producer finishes, even if a waiter is cancelled."""
        async with self._lock:
            if self._closed:
                raise DomainError("web_disabled", "Web client is closed")
            cached = self._cache_get(key)
            if cached is not None:
                return cached
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._produce(key, factory, cacheable))
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _search_hits(self, query: str, limit: int):
        key = ("search", query.strip().lower(), limit)
        return await self._coalesced(
            key, lambda: self._search_uncached(query, limit), lambda _: True
        )

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
        return await self._coalesced(
            key,
            lambda: self._fetch_uncached(url),
            lambda result: result.status in {"ok", "partial"},
        )

    async def _fetch_uncached(self, url: str) -> FetchResult:
        errors: list[str] = []
        for provider in self.fetch_chain:
            try:
                result = await provider.fetch(url)
            except DomainError as exc:
                if exc.code in {"web_url", "web_private_url"}:
                    raise
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
        max_chars = min(max_chars, self.settings.max_response_chars)
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
        max_chars = min(max_chars, self.settings.max_response_chars)
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
        truncated = len(remaining) > max_chars or result.truncated
        next_offset = offset + max_chars if len(remaining) > max_chars else None
        source = Source(
            title=result.title,
            url=result.url or url,
            text=remaining[:max_chars],
            provider=result.provider,
            note=(
                f"offset={offset}；"
                + (
                    f"内容已截断，next_offset={next_offset}"
                    if next_offset is not None
                    else "已达正文提取上限" if truncated else "完整读取"
                )
            ),
        )
        return evidence_pack(
            "网页读取",
            [source],
            budget=max_chars + 800,
            meta=f"URL={url}；后端={result.provider}",
        )

    async def extract(self, urls: list[str], max_chars: int) -> str:
        max_chars = min(max_chars, self.settings.max_response_chars)
        validated = [
            validate_public_url(url, allow_private=self.settings.allow_private_urls) for url in urls
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
        max_sources = min(max_sources, self.settings.max_sources)
        max_chars = min(max_chars, self.settings.max_response_chars)
        limit = max(3, min(8, max_sources))

        async def one(query: str):
            try:
                return await self._search_hits(query, limit)
            except (DomainError, TimeoutError):
                return [], "unavailable", ["all backends failed"]

        bundles = await asyncio.gather(*(one(query) for query in effective))
        if all(bundle[1] == "unavailable" for bundle in bundles):
            raise DomainError("web_unavailable", "所有研究子查询的检索后端均不可用")
        ranked = [list(bundle[0]) for bundle in bundles]
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for position in range(limit):
            for hits in ranked:
                if position >= len(hits):
                    continue
                hit = hits[position]
                try:
                    public = validate_public_url(
                        hit.url, allow_private=self.settings.allow_private_urls
                    )
                except DomainError:
                    continue
                key = normalize_url(public)
                if key in seen:
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
                url = validate_public_url(hit.url, allow_private=self.settings.allow_private_urls)
            except DomainError:
                return hit
            try:
                result = await self._fetch_result(url)
            except (DomainError, TimeoutError):
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
        failures = sum(bundle[1] == "unavailable" for bundle in bundles)
        meta = (
            f"问题={question[:100]}；子查询={len(effective)}；"
            f"后端={providers}；失败子查询={failures}；去重后来源={len(sources)}"
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
        async with self._lock:
            self._closed = True
            tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cache.clear()
        if self._owns_client:
            await asyncio.gather(self.client.aclose(), self._public_client.aclose())
