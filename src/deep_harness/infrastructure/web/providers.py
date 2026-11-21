"""Pluggable search and extraction backends behind one provider seam.

Every backend returns the same shapes. Credentials are read from environment
variable *names* in settings, so no secret ever enters a session snapshot.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.web.bing_cn import BROWSER_UA, parse_bing_results
from deep_harness.infrastructure.web.extract import MAX_HTML_BYTES, extract_text
from deep_harness.infrastructure.web.models import FetchResult, SearchHit
from deep_harness.infrastructure.web.security import resolve_fetch_addresses, validate_public_url

USER_AGENT = "deep-harness/1.0 (bounded web research)"
MAX_BACKEND_TEXT = 64_000
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


@runtime_checkable
class SearchProvider(Protocol):
    name: str
    available: bool

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...

    async def fetch(self, url: str) -> FetchResult | None: ...


def _key(env_name: str) -> str | None:
    return os.environ.get(env_name, "").strip() or None


def _hit(row: dict, *, title: str, url: str, snippet: str, text: str = "", provider: str = ""):
    return SearchHit(
        title=str(row.get(title) or "")[:200],
        url=str(row.get(url) or ""),
        snippet=str(row.get(snippet) or "")[:600],
        text=str(row.get(text) or "")[:MAX_BACKEND_TEXT] if text else "",
        provider=provider,
    )


class HttpProvider:
    """Shared HTTP error normalization: provider failures never leak credentials."""

    name = "base"
    available = False

    def __init__(self, client: httpx.AsyncClient, key: str | None = None):
        self.client = client
        self.key = key
        self.available = key is not None

    async def _request(self, method: str, url: str, **kwargs):
        try:
            async with self.client.stream(method, url, **kwargs) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(content) + len(chunk) > MAX_HTML_BYTES:
                        raise DomainError(
                            "web_response_large", f"{self.name} response exceeded limit"
                        )
                    content.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers={"content-type": response.headers.get("content-type", "")},
                    content=bytes(content),
                    request=response.request,
                )
        except httpx.TimeoutException as exc:
            raise DomainError("web_timeout", f"{self.name} request timed out") from exc
        except httpx.HTTPError as exc:
            raise DomainError("web_backend", f"{self.name} request failed") from exc

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        return []

    async def fetch(self, url: str) -> FetchResult | None:
        return None


class TavilyProvider(HttpProvider):
    name = "tavily"

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "POST",
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        rows = response.json().get("results", [])
        return [
            _hit(row, title="title", url="url", snippet="content", provider=self.name)
            for row in rows[:limit]
            if row.get("url")
        ]


class ExaProvider(HttpProvider):
    name = "exa"

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "POST",
            "https://api.exa.ai/search",
            headers={"x-api-key": self.key or ""},
            json={
                "query": query,
                "numResults": limit,
                "contents": {"text": {"maxCharacters": 4000}},
            },
        )
        rows = response.json().get("results", [])
        return [
            _hit(
                row,
                title="title",
                url="url",
                snippet="summary",
                text="text",
                provider=self.name,
            )
            for row in rows[:limit]
            if row.get("url")
        ]


class BraveProvider(HttpProvider):
    name = "brave"

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={"X-Subscription-Token": self.key or "", "Accept": "application/json"},
        )
        rows = response.json().get("web", {}).get("results", [])
        return [
            _hit(row, title="title", url="url", snippet="description", provider=self.name)
            for row in rows[:limit]
            if row.get("url")
        ]


class SerperProvider(HttpProvider):
    name = "serper"

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "POST",
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.key or ""},
            json={"q": query, "num": limit},
        )
        rows = response.json().get("organic", [])
        return [
            _hit(row, title="title", url="link", snippet="snippet", provider=self.name)
            for row in rows[:limit]
            if row.get("link")
        ]


class SearxngProvider(HttpProvider):
    """Operator-owned metasearch endpoint; deliberately not public-URL validated."""

    name = "searxng"

    def __init__(self, client: httpx.AsyncClient, base_url: str | None):
        super().__init__(client, key=base_url)
        self.base_url = (base_url or "").rstrip("/")
        self.available = bool(self.base_url)

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "GET",
            f"{self.base_url}/search",
            params={"q": query, "format": "json", "language": "all"},
        )
        rows = response.json().get("results", [])
        return [
            _hit(row, title="title", url="url", snippet="content", provider=self.name)
            for row in rows[:limit]
            if row.get("url")
        ]


class BingCnProvider(HttpProvider):
    """Keyless Bing China SERP; reachable from mainland China networks.

    A browser User-Agent is mandatory: Bing serves a challenge/empty SERP to
    short or custom UAs, so the client-level ``deep-harness`` UA is overridden.
    """

    name = "bing_cn"

    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client, key=None)
        self.available = True

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "GET",
            "https://cn.bing.com/search",
            params={"q": query, "count": max(limit * 2, 10), "setlang": "zh-CN"},
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        return parse_bing_results(response.text, limit, self.name)


def _parse_jina_search(text: str, limit: int, provider: str) -> list[SearchHit]:
    """Jina search returns markdown rather than JSON; parse it leniently."""
    lines = [line.strip() for line in text.splitlines()]
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        for raw in _URL_RE.findall(line):
            url = raw.rstrip(".,;)")
            if url in seen or "r.jina.ai" in url or "s.jina.ai" in url:
                continue
            seen.add(url)
            previous = next(
                (
                    lines[j]
                    for j in range(index - 1, -1, -1)
                    if lines[j] and not _URL_RE.search(lines[j])
                ),
                "",
            )
            following = next(
                (
                    lines[j]
                    for j in range(index + 1, min(len(lines), index + 4))
                    if lines[j] and not _URL_RE.search(lines[j])
                ),
                "",
            )
            hits.append(
                SearchHit(
                    title=re.sub(r"^\[\d+\]\s*", "", previous)[:160],
                    url=url,
                    snippet=following[:400],
                    provider=provider,
                )
            )
            if len(hits) >= limit:
                return hits
    return hits


class JinaProvider(HttpProvider):
    """Keyless-capable search plus the r.jina.ai reader."""

    name = "jina"

    def __init__(self, client: httpx.AsyncClient, key: str | None = None):
        super().__init__(client, key=None)
        self.api_key = key
        self.available = True

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/plain"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "GET", f"https://s.jina.ai/{quote(query, safe='')}", headers=self._headers()
        )
        return _parse_jina_search(response.text[:MAX_BACKEND_TEXT], limit, self.name)

    async def fetch(self, url: str) -> FetchResult | None:
        response = await self._request("GET", f"https://r.jina.ai/{url}", headers=self._headers())
        text = response.text[:MAX_BACKEND_TEXT].strip()
        if not text:
            return None
        return FetchResult(url=url, text=text, provider=self.name, status="ok")


class FirecrawlProvider(HttpProvider):
    name = "firecrawl"

    def __init__(self, client: httpx.AsyncClient, key: str | None, base_url: str | None):
        super().__init__(client, key)
        self.base_url = (base_url or "https://api.firecrawl.dev").rstrip("/")
        self.available = bool(key or base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._request(
            "POST",
            f"{self.base_url}/v1/search",
            headers=self._headers(),
            json={"query": query, "limit": limit},
        )
        rows = response.json().get("data", [])
        if isinstance(rows, dict):
            rows = rows.get("web", [])
        return [
            _hit(row, title="title", url="url", snippet="description", provider=self.name)
            for row in rows[:limit]
            if isinstance(row, dict) and row.get("url")
        ]

    async def fetch(self, url: str) -> FetchResult | None:
        response = await self._request(
            "POST",
            f"{self.base_url}/v1/scrape",
            headers=self._headers(),
            json={"url": url, "formats": ["markdown"]},
        )
        data = response.json().get("data", {})
        text = (data.get("markdown") or "")[:MAX_BACKEND_TEXT] if isinstance(data, dict) else ""
        if not text:
            return None
        return FetchResult(
            url=url,
            title=str(data.get("metadata", {}).get("title") or "")[:200],
            text=text,
            provider=self.name,
            status="ok",
        )


class DdgsProvider:
    """Keyless DuckDuckGo fallback; the optional package is imported lazily."""

    name = "ddgs"

    def __init__(self):
        self._worker: asyncio.Task | None = None
        self._slot = asyncio.Lock()
        try:
            from ddgs import DDGS

            self._engine = DDGS
            self.available = True
        except ImportError:
            self._engine = None
            self.available = False

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        if not self.available:
            return []

        def run():
            engine = self._engine(timeout=10)
            try:
                return list(engine.text(query, max_results=limit))
            finally:
                close = getattr(engine, "close", None)
                if close:
                    close()

        try:
            async with self._slot:
                if self._worker is not None and not self._worker.done():
                    raise DomainError("web_backend", "ddgs worker is still busy")
                self._worker = asyncio.create_task(asyncio.to_thread(run))
                self._worker.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                worker = self._worker
            rows = await asyncio.wait_for(asyncio.shield(worker), timeout=15)
        except TimeoutError as exc:
            raise DomainError("web_timeout", "ddgs search timed out") from exc
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("web_backend", "ddgs search failed") from exc
        return [
            _hit(row, title="title", url="href", snippet="body", provider=self.name)
            for row in rows[:limit]
            if isinstance(row, dict) and row.get("href")
        ]

    async def fetch(self, url: str) -> FetchResult | None:
        return None


class StaticFetchProvider:
    """Always-available reader: direct GET plus local HTML cleanup."""

    name = "static"
    available = True

    def __init__(
        self, client: httpx.AsyncClient, *, max_chars: int = 32_000, allow_private: bool = False
    ):
        self.client = client
        self.max_chars = max_chars
        self.allow_private = allow_private

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        return []

    async def fetch(self, url: str) -> FetchResult | None:
        target = httpx.URL(validate_public_url(url, allow_private=self.allow_private))
        try:
            for _ in range(6):
                validate_public_url(str(target), allow_private=self.allow_private)
                addresses = await resolve_fetch_addresses(
                    target.raw_host.decode("ascii"),
                    target.port or (443 if target.scheme == "https" else 80),
                    allow_private=self.allow_private,
                )
                response = await self._connect(target, addresses)
                try:
                    if response.has_redirect_location:
                        target = target.join(response.headers["location"])
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and "html" not in content_type and "text" not in content_type:
                        return FetchResult(
                            url=url, status="blocked", note="非文本响应", provider=self.name
                        )
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise DomainError("web_backend", "Unexpected compressed page response")
                    data = bytearray()
                    truncated = False
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        remaining = MAX_HTML_BYTES - len(data)
                        data.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    html = data.decode(response.encoding or "utf-8", errors="replace")
                    break
                finally:
                    await response.aclose()
            else:
                raise DomainError("web_backend", "Page exceeded the redirect limit")
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise DomainError("web_timeout", "page fetch timed out") from exc
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise DomainError("web_backend", "Page request failed") from exc
        text, title = await asyncio.to_thread(extract_text, html, max_chars=self.max_chars)
        return FetchResult(
            url=str(target),
            title=title,
            text=text,
            provider=self.name,
            status="ok" if text else "partial",
            note="" if text else "页面未提取到正文",
            truncated=truncated or len(text) >= self.max_chars,
        )

    async def _connect(self, target, addresses):
        for index, address in enumerate(addresses):
            request = httpx.Request(
                "GET",
                target.copy_with(host=address),
                headers={
                    "Host": target.netloc.decode("ascii"),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                },
                extensions={"sni_hostname": target.raw_host.decode("ascii")},
            )
            try:
                return await self.client.send(request, stream=True, follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise


class FakeProvider:
    """Deterministic in-memory backend for tests; never touches the network."""

    name = "fake"
    available = True

    def __init__(self, hits=None, pages=None, *, fail=False, name="fake"):
        self.name = name
        self.hits = list(hits or [])
        self.pages = dict(pages or {})
        self.fail = fail
        self.calls: list[tuple] = []

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.calls.append(("search", query, limit))
        if self.fail:
            raise DomainError("web_backend", "fake backend failed")
        return self.hits[:limit]

    async def fetch(self, url: str) -> FetchResult | None:
        self.calls.append(("fetch", url))
        return self.pages.get(url)


def build_providers(
    settings, client: httpx.AsyncClient, public_client: httpx.AsyncClient | None = None
) -> dict[str, SearchProvider]:
    """Create every backend; unavailable ones stay in the dict but leave the chain."""
    return {
        "tavily": TavilyProvider(client, _key(settings.tavily_api_key_env)),
        "exa": ExaProvider(client, _key(settings.exa_api_key_env)),
        "brave": BraveProvider(client, _key(settings.brave_api_key_env)),
        "serper": SerperProvider(client, _key(settings.serper_api_key_env)),
        "searxng": SearxngProvider(client, settings.searxng_url),
        "bing_cn": BingCnProvider(client),
        "jina": JinaProvider(client, _key(settings.jina_api_key_env)),
        "firecrawl": FirecrawlProvider(
            client, _key(settings.firecrawl_api_key_env), settings.firecrawl_url
        ),
        "ddgs": DdgsProvider(),
        "static": StaticFetchProvider(
            public_client or client,
            max_chars=settings.max_response_chars * 4,
            allow_private=settings.allow_private_urls,
        ),
    }
