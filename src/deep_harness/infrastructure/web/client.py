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
from dataclasses import replace

import httpx

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.web.cards import evidence_pack
from deep_harness.infrastructure.web.models import (
    BackendAttempt,
    FetchResult,
    SearchBatch,
    SearchHit,
    SearchOptions,
    Source,
)
from deep_harness.infrastructure.web.providers import (
    USER_AGENT,
    SearchProvider,
    build_providers,
)
from deep_harness.infrastructure.web.relevance import rank_hits, relevance, research_queries
from deep_harness.infrastructure.web.security import normalize_url, validate_public_url
from deep_harness.infrastructure.web.settings import WebSettings
from deep_harness.server_config import number

_SEARCH_EXCLUDED = {"static"}
_DEFAULT_OPTIONS = SearchOptions()


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
            limits=httpx.Limits(
                max_connections=number("web.http_max_connections"),
                max_keepalive_connections=number("web.http_keepalive_connections"),
            ),
        )
        self._public_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            trust_env=False,
            limits=httpx.Limits(
                max_connections=number("web.http_max_connections"),
                max_keepalive_connections=number("web.public_http_keepalive_connections"),
            ),
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
            and getattr(self.providers[name], "search_available", True)
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

    async def _search_hits(self, query: str, limit: int, options: SearchOptions) -> SearchBatch:
        key = ("search", query.strip(), limit, options)
        return await self._coalesced(
            key,
            lambda: self._search_uncached(query, limit, options),
            lambda batch: bool(batch.hits)
            and all(
                attempt.status in {"ok", "merged", "empty", "low_relevance"}
                for attempt in batch.attempts
            ),
        )

    async def _search_uncached(
        self, query: str, limit: int, options: SearchOptions
    ) -> SearchBatch:
        groups: list[tuple[str, list[SearchHit]]] = []
        attempts: list[BackendAttempt] = []
        chain = sorted(
            self.search_chain,
            key=lambda provider: len(_ignored_filters(provider, options)),
        )
        for provider in chain[:self.settings.max_search_backends]:
            started = time.monotonic()
            ignored = _ignored_filters(provider, options)
            confident = False
            try:
                async with asyncio.timeout(self.settings.provider_timeout_seconds):
                    specialized = getattr(provider, "search_with_options", None)
                    hits = (
                        await specialized(query, limit, options)
                        if specialized is not None
                        else await provider.search(query, limit)
                    )
            except (DomainError, TimeoutError) as exc:
                status = exc.code if isinstance(exc, DomainError) else "web_timeout"
            except Exception:
                status = "web_backend"
            else:
                relevant = []
                for hit in hits:
                    try:
                        url = validate_public_url(hit.url, allow_private=False)
                    except DomainError:
                        continue
                    score = relevance(query, hit)
                    if score is None or score >= self.settings.relevance_threshold:
                        relevant.append(
                            replace(hit, url=url, provider=hit.provider or provider.name)
                        )
                        confident |= score is None or score >= self.settings.relevance_fast_path
                groups.append((query, relevant))
                if relevant:
                    status = "filters_unsupported" if ignored else "ok" if confident else "merged"
                else:
                    status = "low_relevance" if hits else "empty"
            attempts.append(BackendAttempt(provider.name, status, _elapsed_ms(started)))
            if confident:
                break
        return SearchBatch(tuple(rank_hits(groups, limit)), tuple(attempts), bool(groups))

    async def _fetch_result(self, url: str) -> FetchResult:
        key = ("fetch", url)
        return await self._coalesced(
            key,
            lambda: self._fetch_uncached(url),
            lambda result: bool(result.text) and result.status in {"ok", "partial"},
        )

    async def _fetch_uncached(self, url: str) -> FetchResult:
        errors: list[str] = []
        attempts: list[BackendAttempt] = []
        empty_result = None
        for provider in self.fetch_chain:
            started = time.monotonic()
            result = None
            try:
                async with asyncio.timeout(self.settings.provider_timeout_seconds):
                    result = await provider.fetch(url)
            except (DomainError, TimeoutError) as exc:
                code = exc.code if isinstance(exc, DomainError) else "web_timeout"
                if code in {"web_url", "web_private_url"}:
                    raise
                errors.append(f"{provider.name}:{code}")
            except Exception:
                code = "web_backend"
                errors.append(f"{provider.name}:{code}")
            else:
                code = result.status if result is not None else "empty"
            attempts.append(BackendAttempt(provider.name, code, _elapsed_ms(started)))
            if result is not None:
                if result.text.strip():
                    return replace(result, attempts=tuple(attempts))
                if empty_result is None:
                    empty_result = result
        if empty_result is not None:
            return replace(empty_result, attempts=tuple(attempts))
        detail = f"（{'、'.join(errors[:2])}）" if errors else "：无可用读取后端"
        return FetchResult(
            url=url, status="error", note="未能读取页面" + detail, attempts=tuple(attempts)
        )

    async def search(
        self, query: str, limit: int, max_chars: int, options: SearchOptions = _DEFAULT_OPTIONS
    ) -> str:
        max_chars = min(max_chars, self.settings.max_response_chars)
        batch = await self._search_hits(query, limit, options)
        if not batch.reachable:
            raise DomainError(
                "web_unavailable",
                f"检索后端均不可用（{_describe_attempts(batch.attempts)}）",
            )
        sources = [_source_from_hit(hit) for hit in batch.hits]
        provider = batch.hits[0].provider if batch.hits else "none"
        skipped = [f"{a.backend}:{a.status}" for a in batch.attempts if a.status != "ok"]
        return evidence_pack(
            f"联网检索：{query[: number('web.heading_preview_chars')]}",
            sources,
            budget=max_chars,
            meta=f"后端={provider}；回退={','.join(skipped) or '无'}",
            status=_search_status(sources, batch.attempts),
            diagnostics=_describe_attempts(batch.attempts),
        )

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
                status="unavailable" if result.status in {"error", "blocked"} else "no_results",
                diagnostics=_describe_attempts(result.attempts),
            )
        remaining = result.text[offset:]
        truncated = len(remaining) > max_chars or result.truncated
        next_offset = offset + max_chars if len(remaining) > max_chars else None
        source = Source(
            title=result.title,
            url=result.url or url,
            text=remaining[:max_chars],
            provider=result.provider,
            original_url=url,
            published_at=result.published_at,
            note=(
                f"offset={offset}；"
                + (
                    f"内容已截断，next_offset={next_offset}"
                    if next_offset is not None
                    else "已达正文提取上限"
                    if truncated
                    else "完整读取"
                )
            ),
        )
        return evidence_pack(
            "网页读取",
            [source],
            budget=max_chars + number("web.fetch_evidence_overhead_chars"),
            meta=f"URL={url}；后端={result.provider}",
            status="partial" if truncated else "ok",
            diagnostics=_describe_attempts(result.attempts),
        )

    async def extract(self, urls: list[str], max_chars: int) -> str:
        max_chars = min(max_chars, self.settings.max_response_chars)
        validated = [
            validate_public_url(url, allow_private=self.settings.allow_private_urls) for url in urls
        ]
        deadline = asyncio.get_running_loop().time() + self.settings.total_timeout_seconds
        results = await _collect([self._safe_fetch(url) for url in validated], deadline)
        sources = []
        failures = []
        attempts = []
        for url, result in zip(validated, results, strict=True):
            if result is not None and result.attempts:
                attempts.append(_describe_attempts(result.attempts))
            if result is None or not result.text.strip():
                failures.append(f"{url}（{result.note if result else '超时'}）")
                continue
            sources.append(_source_from_page(url, result))
        status = "partial" if sources and failures else "ok" if sources else "unavailable"
        return evidence_pack(
            "网页批量读取",
            _distinct_sources(sources),
            budget=max_chars + number("web.fetch_evidence_overhead_chars"),
            status=status,
            diagnostics="；".join([*failures, *attempts]),
        )

    async def _safe_fetch(self, url: str) -> FetchResult:
        try:
            return await self._fetch_result(url)
        except (DomainError, TimeoutError) as exc:
            note = exc.code if isinstance(exc, DomainError) else "web_timeout"
            return FetchResult(url=url, status="error", note=note)

    async def research(
        self,
        question: str,
        queries: list[str],
        max_sources: int,
        max_chars: int,
        options: SearchOptions = _DEFAULT_OPTIONS,
    ) -> str:
        """Search and enrich within one deadline, retaining completed evidence."""
        effective = research_queries(question, queries, self.settings.max_queries)
        max_sources = min(max_sources, self.settings.max_sources)
        max_chars = min(max_chars, self.settings.max_response_chars)
        limit = max(
            number("web.min_results_per_research_query"),
            min(number("web.max_results_per_research_query"), max_sources),
        )
        now = asyncio.get_running_loop().time()
        total = self.settings.total_timeout_seconds
        deadline = now + total
        async def search_query(query: str) -> SearchBatch:
            try:
                return await self._search_hits(query, limit, options)
            except TimeoutError:
                return SearchBatch((), (BackendAttempt("search", "web_timeout", 0),), False)

        batches = await _collect(
            [search_query(query) for query in effective],
            now + total * self.settings.research_search_fraction,
        )
        if not any(batch and batch.reachable for batch in batches):
            detail = _describe_attempts(
                tuple(attempt for batch in batches if batch for attempt in batch.attempts)
            )
            raise DomainError("web_unavailable", f"所有研究子查询的检索后端均不可用（{detail}）")

        groups = [batch.hits for batch in batches if batch is not None]
        relevant_groups = [
            (question, [hit for hit in hits if relevance(question, hit) != 0])
            for hits in groups
        ]
        merged = rank_hits(relevant_groups, max_sources)
        fetches = []
        for hit in merged:
            if not hit.text.strip():
                fetches.append(self._safe_fetch(hit.url))
        fetched = iter(await _collect(fetches, deadline))
        sources = []
        gaps = []
        fetch_attempts = []
        for hit in merged:
            if hit.text.strip():
                sources.append(_source_from_hit(hit))
                continue
            page = next(fetched)
            if page is not None and page.attempts:
                fetch_attempts.append(_describe_attempts(page.attempts))
            if page is not None and page.text.strip():
                sources.append(_source_from_page(hit.url, page, hit=hit))
            else:
                note = page.note if page is not None else "抓取超时"
                gaps.append(f"{hit.url}（{note}）")
                if hit.snippet.strip():
                    sources.append(replace(_source_from_hit(hit), note=note))
        sources = _distinct_sources(sources)
        failures = sum(batch is None or not batch.reachable for batch in batches)
        attempts = [
            _describe_attempts(batch.attempts) if batch is not None else "子查询超时"
            for batch in batches
        ]
        filters_missing = any(
            attempt.status == "filters_unsupported"
            for batch in batches if batch for attempt in batch.attempts
        )
        status = (
            "partial" if sources and (failures or gaps or filters_missing)
            else "ok" if sources
            else "unavailable" if gaps
            else "no_results"
        )
        return evidence_pack(
            f"深度联网研究：{question[: number('web.heading_preview_chars')]}",
            sources,
            budget=max_chars,
            meta=f"子查询={len(effective)}；失败子查询={failures}；去重后来源={len(sources)}",
            status=status,
            diagnostics="；".join([*gaps, *attempts, *fetch_attempts]),
        )

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


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _describe_attempts(attempts) -> str:
    return "，".join(f"{a.backend}:{a.status} {a.elapsed_ms}ms" for a in attempts) or "无可用后端"


def _ignored_filters(provider, options: SearchOptions) -> set[str]:
    detect = getattr(provider, "supported_filters", None)
    supported = detect(options) if detect is not None else set()
    return {
        name for name in ("language", "region", "freshness") if getattr(options, name)
    } - supported


def _search_status(sources, attempts) -> str:
    if not sources:
        return "no_results"
    return "partial" if any(a.status == "filters_unsupported" for a in attempts) else "ok"


def _distinct_sources(sources: list[Source]) -> list[Source]:
    result = {}
    for source in sources:
        key = normalize_url(source.url)
        current = result.get(key)
        if current is None or source.kind == "正文" and current.kind != "正文":
            result[key] = source
    return list(result.values())


async def _collect(calls, deadline: float) -> list:
    """Return completed items in input order, cancelling only this request's waiters."""
    tasks = [asyncio.create_task(call) for call in calls]
    if not tasks:
        return []
    try:
        timeout = max(0, deadline - asyncio.get_running_loop().time())
        done, _ = await asyncio.wait(tasks, timeout=timeout)
        return [task.result() if task in done else None for task in tasks]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _source_from_hit(hit: SearchHit) -> Source:
    return Source(
        title=hit.title,
        url=hit.url,
        text=hit.text or hit.snippet,
        provider=hit.provider,
        published_at=hit.published_at,
        kind="正文" if hit.text.strip() else "搜索摘要",
    )


def _source_from_page(url: str, page: FetchResult, *, hit: SearchHit | None = None) -> Source:
    provider = page.provider
    if hit is not None and hit.provider and hit.provider != page.provider:
        provider = f"{hit.provider} / {page.provider}" if page.provider else hit.provider
    limit = number("web.enriched_source_chars")
    return Source(
        title=page.title or (hit.title if hit else ""),
        url=page.url or url,
        text=page.text[:limit],
        provider=provider,
        note="正文已截断" if page.truncated or len(page.text) > limit else "",
        original_url=url,
        published_at=page.published_at or (hit.published_at if hit else ""),
    )
