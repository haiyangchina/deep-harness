import json
import shutil

import pytest

from deep_harness.infrastructure.web import pi_provider
from deep_harness.infrastructure.web.client import WebClient
from deep_harness.infrastructure.web.models import FetchResult, SearchHit, SearchOptions
from deep_harness.infrastructure.web.providers import FakeProvider
from deep_harness.infrastructure.web.settings import WebSettings


@pytest.fixture
def mocked_worker(tmp_path, monkeypatch):
    bundle = pi_provider._BUNDLE
    if not (shutil.which("node") and bundle.is_file()):
        pytest.skip("Requires Node and the built Pi search worker")
    shim = tmp_path / "mock-search.cjs"
    shim.write_text(
        """
        globalThis.fetch = async (url, options) => {
          const request = JSON.parse(options.body).params.arguments;
          const host = new URL(url).hostname;
          if (host === "mcp.exa.ai") {
            if (request.query.includes("fallback")) {
              await new Promise((_, reject) => {
                options.signal.addEventListener("abort", () => reject(options.signal.reason));
              });
            }
            return Response.json({ result: { content: [{ type: "text", text:
              "Title: Python 3.12 reference\\nURL: https://example.org/python\\n"
              + "Published: 2026-11-20T00:00:00Z\\nHighlights:\\nPython 3.12 search snippet"
            }] } });
          }
          if (host === "search.parallel.ai") {
            return Response.json({ result: { content: [{ type: "text", text: JSON.stringify({
              results: [{
                title: "Python fallback", url: "https://example.org/fallback",
                excerpts: ["Python fallback search snippet"], publish_date: "2026-11-21"
              }]
            }) }] } });
          }
          throw new Error("Unexpected outbound request");
        };
        require(BUNDLE);
        """.replace("BUNDLE", json.dumps(str(bundle))),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEP_HARNESS_PI_BUNDLE", str(shim))
    monkeypatch.delenv("DEEP_HARNESS_PI_ELECTRON", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)


async def test_pi_search_and_research_use_one_worker_and_fetch_cited_text(mocked_worker):
    url = "https://example.org/fallback"
    settings = WebSettings(routing=("pi",), extract_routing=("static",), provider_timeout_seconds=3)
    web = WebClient(settings)
    web.fetch_chain = [FakeProvider(pages={
        url: FetchResult(url, text="Python fallback verified paragraph", provider="static"),
    })]
    pi = web.providers["pi"]
    try:
        plain = await web.search("Python 3.12", 3, 4000)
        assert "pi/exa" in plain and "2026-11-20" in plain and "search snippet" in plain
        process = pi._process
        research = await web.research("Python fallback", ["fallback Python"], 3, 4000)
        assert "pi/parallel-mcp / static" in research
        assert "Python fallback verified paragraph" in research
        assert "2026-11-21" in research
        assert pi._process is process
    finally:
        await web.close()
    assert process.returncode is not None


async def test_pi_deduplicates_configured_backends():
    settings = WebSettings(
        routing=("pi", "exa", "ddgs"),
        pi_providers=("exa", "exa", "tavily"),
    )
    web = WebClient(settings, providers={
        name: FakeProvider(name=name) for name in ("pi", "exa", "ddgs")
    })
    try:
        assert settings.pi_providers == ("exa", "tavily")
        assert web.health()["search_backends"] == ["pi", "ddgs"]
    finally:
        await web.close()


@pytest.mark.parametrize("pi_fails", [False, True])
async def test_default_pi_order_with_language_and_region(pi_fails):
    calls = []

    class RecordingProvider(FakeProvider):
        async def search(self, query, limit):
            calls.append(self.name)
            return await super().search(query, limit)

    class LocalizedProvider(RecordingProvider):
        def supported_filters(self, options):
            return {"language", "region"}

    hits = [SearchHit("Python 3.12", "https://example.org/python")]
    web = WebClient(providers={
        "pi": RecordingProvider(hits=hits, name="pi", fail=pi_fails),
        "searxng": LocalizedProvider(hits=hits, name="searxng"),
    })
    try:
        await web.search("Python 3.12", 3, 3000, SearchOptions(language="zh", region="CN"))
        assert calls == (["pi", "searxng"] if pi_fails else ["pi"])
    finally:
        await web.close()


async def test_pi_worker_inherits_windows_system_proxy(tmp_path, mocked_worker, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setattr(pi_provider, "getproxies", lambda: {
        "https": "http://127.0.0.1:1234", "no": "localhost,127.0.0.1",
    })
    shim = tmp_path / "mock-search.cjs"
    shim.write_text(
        'if (process.env.HTTPS_PROXY !== "http://127.0.0.1:1234" || '
        'process.env.NO_PROXY !== "localhost,127.0.0.1") process.exit(1);\n'
        + shim.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    web = WebClient(WebSettings(routing=("pi",), pi_providers=("exa",)))
    try:
        assert "pi/exa" in await web.search("Python 3.12", 3, 3000)
    finally:
        await web.close()
