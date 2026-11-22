"""Web retrieval subsystem tests: every external backend is a deterministic fake."""

import asyncio

import httpx
import pytest

from deep_harness.application.capabilities import WEB_TOOLS
from deep_harness.application.config import load_web_settings
from deep_harness.application.tools import (
    TOOL_ARGUMENTS,
    TOOL_DESCRIPTIONS,
    ToolGateway,
    tool_definitions,
)
from deep_harness.application.web_tools import WebResearchArgs, execute_web_tool
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.infrastructure.web import FakeProvider, WebClient
from deep_harness.infrastructure.web.cards import evidence_pack
from deep_harness.infrastructure.web.extract import extract_text
from deep_harness.infrastructure.web.models import FetchResult, SearchHit, SearchOptions, Source
from deep_harness.infrastructure.web.providers import (
    BraveProvider,
    JinaProvider,
    StaticFetchProvider,
)
from deep_harness.infrastructure.web.relevance import research_queries
from deep_harness.infrastructure.web.security import validate_public_url
from deep_harness.infrastructure.web.settings import WebSettings
from deep_harness.runtime.models import DiagnosticModel


def web_settings(**overrides):
    values = {"routing": ("fake",), "extract_routing": ("fake",), "cache_ttl_seconds": 60}
    values.update(overrides)
    return WebSettings(**values)


def hits():
    return [
        SearchHit(
            title="Alpha", url="https://example.org/a", snippet="short summary", provider="fake"
        ),
        SearchHit(title="Beta", url="https://example.org/b", text="B" * 300, provider="fake"),
    ]


async def test_search_shapes_bounded_cited_evidence():
    fake = FakeProvider(hits=hits())
    web = WebClient(web_settings(), providers={"fake": fake})
    try:
        result = await web.search("q", 5, 1200)
        assert "[1]" in result and "[2]" in result
        assert "https://example.org/a" in result
        assert "<untrusted-web-content>" in result
        assert "不可信" in result
        assert len(result) <= 1400
        assert fake.calls == [("search", "q", 5)]
    finally:
        await web.close()


async def test_router_falls_back_to_the_next_backend():
    broken = FakeProvider(fail=True, name="broken")
    good = FakeProvider(hits=hits(), name="good")
    web = WebClient(
        web_settings(routing=("broken", "good")),
        providers={"broken": broken, "good": good},
    )
    try:
        result = await web.search("q", 3, 2000)
        assert "Alpha" in result and len(good.calls) == 1
    finally:
        await web.close()


async def test_fetch_keeps_empty_page_diagnostics_without_caching():
    url = "https://example.org/empty"
    fake = FakeProvider(pages={url: FetchResult(url, status="partial", note="empty page")})
    web = WebClient(web_settings(), providers={"fake": fake})
    try:
        for _ in range(2):
            result = await web.fetch(url, 1200)
            assert "partial" in result
            assert "empty page" in result
        assert fake.calls == [("fetch", url), ("fetch", url)]
    finally:
        await web.close()


@pytest.mark.parametrize(
    ("query", "unrelated", "related"),
    [
        ("上海未来一周天气预报", "上海市人民政府", "上海七天天气预报"),
        ("杭州未来一周天气预报", "杭州市地图", "杭州七天天气预报"),
        ("Python 3.12 asyncio timeout", "Python 2.7 download", "Python 3.12 asyncio timeout"),
        ("武汉近期公交票价政策", "武汉城市地图", "武汉公交票价政策发布"),
    ],
)
async def test_low_relevance_from_first_backend_triggers_fallback(query, unrelated, related):
    poor = FakeProvider(name="poor", hits=[SearchHit(unrelated, "https://example.org/poor")])
    good = FakeProvider(name="good", hits=[SearchHit(related, "https://example.org/good")])
    settings = web_settings(routing=("poor", "good"))
    web = WebClient(settings, providers={"poor": poor, "good": good})
    try:
        result = await web.search(query, 3, 2000)
        assert "https://example.org/good" in result
        assert "poor:low_relevance" in result
        assert "https://example.org/poor" not in result
    finally:
        await web.close()


async def test_research_keeps_finished_evidence_after_enrichment_deadline():
    fast, slow = "https://example.org/fast", "https://example.org/slow"

    class SlowPageProvider(FakeProvider):
        async def fetch(self, url):
            if url == slow:
                await asyncio.sleep(10)
            return await super().fetch(url)

    fake = SlowPageProvider(
        hits=[SearchHit("Library", fast), SearchHit("Library update", slow, snippet="summary")],
        pages={fast: FetchResult(fast, text="First paragraph\n\nSecond paragraph")},
    )
    web = WebClient(web_settings(total_timeout_seconds=0.25), providers={"fake": fake})
    try:
        result = await execute_web_tool(
            "web_research", WebResearchArgs(question="library"), web
        )
        assert "检索状态: partial" in result
        assert "First paragraph\n\nSecond paragraph" in result
        assert "抓取超时" in result and "证据类型: 搜索摘要" in result
    finally:
        await web.close()


async def test_batch_extract_counts_only_sources_with_content():
    empty, original, final = (
        "https://example.org/empty", "https://example.org/start", "https://example.org/final"
    )
    fake = FakeProvider(pages={
        empty: FetchResult(empty, status="partial", note="页面未提取到正文"),
        original: FetchResult(final, text="A paragraph", published_at="2026-11-20"),
    })
    web = WebClient(web_settings(), providers={"fake": fake})
    try:
        result = await web.extract([empty, original], 2000)
        assert "检索状态: partial" in result
        assert "来源数：1" in result and "[2]" not in result
        assert "页面未提取到正文" in result
        assert f"原始 URL: {original}" in result and f"URL: {final}" in result
        assert "发布日期: 2026-11-20" in result
    finally:
        await web.close()


async def test_search_options_choose_supporting_provider_and_reach_request():
    sent = []

    def handler(request):
        sent.append(request.url.params)
        return httpx.Response(200, json={"web": {"results": [
            {"title": "Python release notes", "url": "https://example.org/release"},
        ]}})

    fake = FakeProvider(hits=hits())
    settings = web_settings(routing=("fake", "brave"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        brave = BraveProvider(client, key="test-key")
        web = WebClient(settings, providers={"fake": fake, "brave": brave}, client=client)
        try:
            result = await web.search("Python release notes", 3, 2000, SearchOptions(
                language="en", region="GB", freshness="week",
            ))
            assert "后端=brave" in result and not fake.calls
            assert dict(sent[0]) == {
                "q": "Python release notes", "count": "3", "search_lang": "en",
                "country": "GB", "freshness": "pw",
            }
        finally:
            await web.close()


def test_query_variants_preserve_constraints_and_collapse_duplicates():
    question = "请查询上海未来一周天气 Python 3.12"
    variants = research_queries(question, [question, "  " + question], 4)
    assert variants[0] == question and len(variants) == len(set(variants)) > 1
    assert all("上海" in query and "未来一周" in query and "3.12" in query for query in variants)


async def test_jina_without_key_is_a_reader_only():
    def handler(request):
        assert request.url.host == "r.jina.ai"
        return httpx.Response(200, text="Retrieved text")

    settings = web_settings(routing=("jina",), extract_routing=("jina",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        web = WebClient(settings, providers={"jina": JinaProvider(client)}, client=client)
        try:
            assert web.health()["search_backends"] == []
            result = await web.fetch("https://example.org", 1000)
            assert "Retrieved text" in result
        finally:
            await web.close()


async def test_all_backends_failing_raises_domain_error():
    broken = FakeProvider(fail=True, name="broken")
    web = WebClient(web_settings(routing=("broken",)), providers={"broken": broken})
    try:
        with pytest.raises(DomainError, match="检索后端均不可用"):
            await web.search("q", 3, 2000)
    finally:
        await web.close()


async def test_clean_empty_result_is_not_an_error():
    web = WebClient(web_settings(), providers={"fake": FakeProvider(hits=[])})
    try:
        result = await web.search("q", 3, 1000)
        assert "未找到可用结果" in result
    finally:
        await web.close()


async def test_inflight_and_cache_coalesce_identical_searches():
    fake = FakeProvider(hits=hits())
    web = WebClient(web_settings(), providers={"fake": fake})
    try:
        await asyncio.gather(*(web.search("same", 3, 2000) for _ in range(4)))
        assert len(fake.calls) == 1
        await web.search("same", 3, 2000)
        assert len(fake.calls) == 1
    finally:
        await web.close()


async def test_research_fans_out_queries_and_deduplicates():
    fake = FakeProvider(hits=hits())
    web = WebClient(web_settings(), providers={"fake": fake})
    try:
        result = await web.research("compare", ["q1", "q2", "q3"], 4, 3000)
        assert "深度联网研究" in result and "子查询=4" in result
        assert ("search", "compare", 4) in fake.calls
        assert len(fake.calls) >= 3
        assert result.splitlines().count("URL: https://example.org/a") == 1
        assert len(result) <= 3400
    finally:
        await web.close()


async def test_fetch_rejects_private_urls_and_allows_public(tmp_path):
    web = WebClient(web_settings(), providers={"fake": FakeProvider()})
    try:
        result = await web.fetch("https://example.org/a", 1000)
        assert "URL=https://example.org/a" in result
        with pytest.raises(DomainError) as caught:
            await web.fetch("http://127.0.0.1:8888/search", 1000)
        assert caught.value.code == "web_private_url"
    finally:
        await web.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://localhost/x",
        "https://host.local/x",
        "file:///etc/passwd",
        "https://user:pw@example.com/x",
    ],
)
def test_url_boundary_refuses_private_and_credential_urls(url):
    with pytest.raises(DomainError):
        validate_public_url(url)


def test_url_boundary_accepts_public_and_operator_opt_in():
    assert validate_public_url("https://example.com/a?q=1") == "https://example.com/a?q=1"
    assert validate_public_url("http://127.0.0.1:8888", allow_private=True)


def test_extract_text_removes_scripts_and_keeps_title():
    html = (
        "<html><head><title>Hello</title><script>bad()</script></head>"
        "<body><p>Good text</p><nav>menu</nav></body></html>"
    )
    text, title = extract_text(html)
    assert "Good text" in text
    assert "bad()" not in text
    assert "Hello" in title


def test_evidence_pack_is_bounded_cited_and_untrusted():
    sources = [
        Source(title=f"S{index}", url=f"https://e.org/{index}", text="x" * 5000)
        for index in range(4)
    ]
    pack = evidence_pack("T", sources, budget=2000)
    assert len(pack) <= 2000
    assert "[1]" in pack
    assert "TRUNCATED" in pack
    assert "不可信" in pack


def test_load_web_settings_reads_defaults_and_operator_override(tmp_path):
    default = load_web_settings()
    assert default.routing[0] == "pi"
    assert default.max_response_chars == 8000
    path = tmp_path / "config.toml"
    path.write_text(
        '[web]\nrouting = ["fake"]\nmax_response_chars = 1234\n', encoding="utf-8"
    )
    custom = load_web_settings(path)
    assert custom.routing == ("fake",) and custom.max_response_chars == 1234


def test_web_tools_are_registered():
    assert set(WEB_TOOLS) <= set(TOOL_ARGUMENTS)
    assert all(name in TOOL_DESCRIPTIONS for name in WEB_TOOLS)
    assert len(tool_definitions(tuple(WEB_TOOLS))) == 4


async def test_gateway_dispatches_web_search_with_fake_backend(tmp_path):
    fake = FakeProvider(hits=hits())
    web = WebClient(web_settings(), providers={"fake": fake})
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: DiagnosticModel(),
        web_clients={"one": web},
    )
    try:
        app = await host.application("one")
        assert app.web is web
        session = await app.create_session(
            "create",
            "prompt",
            ("web_search",),
            permission_mode="full",
            full_access_acknowledged=True,
        )
        await app.store.admit(session, principal="operator", submission_id="s", text="x")
        claim = await app.executions.claim(session.id, app.owner)
        gateway = ToolGateway(app.scope, session, app.receipts, claim, web_client=web)
        result = await gateway.execute(
            ToolCall("w", "web_search", {"query": "q", "max_results": 2, "max_chars": 1500})
        )
        assert "Alpha" in result and "[1]" in result
        assert fake.calls
    finally:
        await host.close()


async def test_cancelling_one_waiter_preserves_shared_producer_and_bounded_cache():
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedProvider(FakeProvider):
        async def search(self, query, limit):
            started.set()
            await release.wait()
            return await super().search(query, limit)

    fake = DelayedProvider(hits=hits())
    web = WebClient(web_settings(max_cache_entries=1), providers={"fake": fake})
    try:
        first = asyncio.create_task(web.search("same", 3, 2000))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(web.search("same", 3, 2000))
        release.set()
        assert "Alpha" in await second
        assert fake.calls == [("search", "same", 3)]
        await web.search("other", 3, 2000)
        assert web.health()["cache_entries"] == 1
    finally:
        await web.close()


async def test_direct_page_fetch_pins_public_address_and_rechecks_redirect(monkeypatch):
    loop = asyncio.get_running_loop()
    lookups = []

    async def resolve(host, port, **kwargs):
        lookups.append(host)
        address = "8.8.8.8" if host == "public.example" else "127.0.0.1"
        return [(None, None, None, "", (address, port))]

    def transport(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "public.example"
        assert request.extensions["sni_hostname"] == "public.example"
        return httpx.Response(302, headers={"location": "https://redirect.example/page"})

    monkeypatch.setattr(loop, "getaddrinfo", resolve)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(DomainError) as error:
            await StaticFetchProvider(client).fetch("https://public.example/page")
    assert error.value.code == "web_private_url"
    assert lookups == ["public.example", "redirect.example"]


async def test_research_applies_operator_source_cap_and_distinguishes_unavailable():
    fake = FakeProvider(hits=hits())
    web = WebClient(web_settings(max_sources=1), providers={"fake": fake})
    try:
        result = await web.research("compare", [], 12, 2000)
        assert "Alpha" in result and "Beta" not in result
    finally:
        await web.close()

    bad = WebClient(web_settings(), providers={"fake": FakeProvider(fail=True)})
    try:
        with pytest.raises(DomainError, match="所有研究子查询"):
            await bad.research("q", [], 3, 2000)
    finally:
        await bad.close()
