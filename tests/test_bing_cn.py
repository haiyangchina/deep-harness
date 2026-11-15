"""Offline tests for the keyless Bing China backend.

The real network is never touched: SERP parsing uses a fixture and HTTP calls
go through httpx.MockTransport. This pins the mainland-China fallback that
replaced the ddgs/jina timeouts seen in production.
"""

import httpx
import pytest

from deep_harness.application.config import load_web_settings
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.web import FakeProvider, WebClient
from deep_harness.infrastructure.web.bing_cn import BROWSER_UA, parse_bing_results
from deep_harness.infrastructure.web.models import SearchHit
from deep_harness.infrastructure.web.providers import build_providers
from deep_harness.infrastructure.web.settings import WebSettings

SERP = """<ol id="b_results">
<li class="b_algo" data-id iid=SERP.1>
  <link rel="stylesheet" href="/rp/x.css"/>
  <h2><a target="_blank" href="https://example.com/one" h="ID=SERP,1.2">
    <strong>First</strong> result</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">A &amp; B&ensp;&#183;&ensp;summary one</p></div>
  <cite>https://example.com › one</cite>
</li>
<li class="b_algo"><h2><a href="https://example.com/two">Second result</a></h2>
  <p>Two summary &lt;tag&gt;</p>
  <ul><li>nested item keeps the parent block open</li></ul>
</li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=abc">Redirect</a></h2>
  <p>must be skipped</p></li>
<li class="b_algo"><h2><a href="javascript:void(0)">Script URL</a></h2>
  <p>must be skipped</p></li>
<li class="b_algo"><p>no heading, no link</p></li>
</ol>"""


def test_parser_extracts_clean_cited_hits_and_skips_bad_urls():
    results = parse_bing_results(SERP, 5)
    assert len(results) == 2
    assert results[0].title == "First result"
    assert results[0].url == "https://example.com/one"
    assert results[0].snippet == "A & B · summary one"
    assert results[0].provider == "bing_cn"
    assert results[1].title == "Second result"
    assert results[1].url == "https://example.com/two"
    assert results[1].snippet == "Two summary <tag>"


def test_parser_respects_limit_and_survives_empty_or_malformed_html():
    assert len(parse_bing_results(SERP, 1)) == 1
    assert parse_bing_results("", 5) == []
    assert parse_bing_results("<li class='b_algo'><h2><a href='https://x.test'>t", 5)[0].url == (
        "https://x.test"
    )


def test_parser_bounds_title_and_snippet():
    html = "<li class='b_algo'><h2><a href='https://x.test'>" + "T" * 500
    html += "</a></h2><p>" + "S" * 900 + "</p></li>"
    hit = parse_bing_results(html, 1)[0]
    assert len(hit.title) == 200
    assert len(hit.snippet) == 600


def test_default_routing_prefers_bing_cn_over_foreign_keyless_backends():
    routing = WebSettings().routing
    assert "bing_cn" in routing
    assert routing.index("bing_cn") < routing.index("ddgs")
    assert routing.index("bing_cn") < routing.index("jina")
    # defaults.toml must not drift from the model again.
    assert load_web_settings().routing == routing


async def test_build_providers_registers_keyless_bing_cn():
    async with httpx.AsyncClient() as client:
        providers = build_providers(WebSettings(), client)
        assert providers["bing_cn"].available
        assert providers["bing_cn"].name == "bing_cn"


async def test_bing_cn_sends_browser_ua_and_short_circuits_foreign_fallback():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=SERP)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": "deep-harness/1.0 (bounded web research)"},
    )
    web = WebClient(
        WebSettings(routing=("bing_cn", "ddgs", "jina"), extract_routing=()), client=client
    )
    try:
        result = await web.search("武汉天气", 2, 1500)
        # A short/custom UA makes Bing serve a challenge page: this is the key regression.
        assert len(seen) == 1
        assert seen[0].headers["User-Agent"] == BROWSER_UA
        assert seen[0].url.params["q"] == "武汉天气"
        assert "后端=bing_cn" in result and "回退=无" in result
        assert "https://example.com/one" in result
        assert "ddgs" not in result and "jina" not in result
        assert web.health()["configured_backends"].count("bing_cn") == 1
    finally:
        await web.close()
        await client.aclose()


class FailingProvider:
    name = "bing_cn"
    available = True

    async def search(self, query, limit):
        raise DomainError("web_timeout", "bing cn timed out")

    async def fetch(self, url):
        return None


async def test_bing_cn_timeout_is_normalized_and_chain_continues():
    fallback = FakeProvider(hits=[SearchHit(title="F", url="https://example.org/f", snippet="s")])
    web = WebClient(
        WebSettings(routing=("bing_cn", "fake"), extract_routing=()),
        providers={"bing_cn": FailingProvider(), "fake": fallback},
    )
    try:
        result = await web.search("q", 2, 1200)
        assert "后端=fake" in result
        assert "回退=bing_cn:web_timeout" in result
        assert fallback.calls == [("search", "q", 2)]
    finally:
        await web.close()


async def test_all_keyless_backends_unavailable_still_reports_actionable_error():
    web = WebClient(
        WebSettings(routing=("bing_cn",), extract_routing=()),
        providers={"bing_cn": FailingProvider()},
    )
    try:
        with pytest.raises(DomainError) as caught:
            await web.search("q", 2, 1200)
        assert caught.value.code == "web_unavailable"
        assert "bing_cn:web_timeout" in str(caught.value)
    finally:
        await web.close()
