"""Keyless Bing China search parsing, without a third-party HTML dependency.

``ddgs`` (DuckDuckGo) and ``s.jina.ai`` are unreachable from many mainland China
networks, so a keyless backend that resolves and answers locally matters. Bing
China returns a regular SERP for a browser User-Agent; a short/custom UA gets a
challenge page, so providers must send BROWSER_UA explicitly.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from deep_harness.infrastructure.web.extract import MAX_HTML_BYTES
from deep_harness.infrastructure.web.models import SearchHit
from deep_harness.server_config import number

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_TITLE = number("web.provider_title_chars")
MAX_SNIPPET = number("web.provider_snippet_chars")
_HTTP_URL = re.compile(r"^https?://", re.IGNORECASE)


def _clean(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", unescape(" ".join(parts))).strip()


class _ResultsParser(HTMLParser):
    """Collect one SearchHit per ``li.b_algo`` block, tolerating malformed HTML."""

    def __init__(self, provider: str):
        super().__init__(convert_charrefs=True)
        self.provider = provider
        self.results: list[SearchHit] = []
        self._current: dict[str, str] | None = None
        self._li_depth = 0
        self._in_heading = False
        self._in_paragraph = False
        self._title: list[str] = []
        self._snippet: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "li":
            if self._current is None:
                if "b_algo" in (attributes.get("class") or "").split():
                    self._current = {"url": ""}
                    self._li_depth = 1
                    self._title = []
                    self._snippet = []
            else:
                self._li_depth += 1
            return
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = True
        elif tag == "a" and self._in_heading and not self._current["url"]:
            self._current["url"] = (attributes.get("href") or "").strip()
        elif tag == "p":
            self._in_paragraph = True

    def handle_data(self, data):
        if self._current is None:
            return
        if self._in_heading:
            self._title.append(data)
        elif self._in_paragraph:
            self._snippet.append(data)

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_heading = False
        elif tag == "p":
            self._in_paragraph = False
        elif tag == "li" and self._current is not None:
            self._li_depth -= 1
            if self._li_depth <= 0:
                self._finish()

    def finalize(self) -> None:
        """Flush the last block when the SERP was truncated before ``</li>``."""
        if self._current is not None:
            self._finish()

    def _finish(self):
        row, title, snippet = self._current, self._title, self._snippet
        self._current = None
        self._in_heading = self._in_paragraph = False
        url = (row or {}).get("url", "")
        # Bing occasionally wraps results in its own redirect endpoint.
        if not _HTTP_URL.match(url) or "/ck/a" in url:
            return
        self.results.append(
            SearchHit(
                title=_clean(title)[:MAX_TITLE],
                url=url,
                snippet=_clean(snippet)[:MAX_SNIPPET],
                provider=self.provider,
            )
        )


def parse_bing_results(html: str, limit: int, provider: str = "bing_cn") -> list[SearchHit]:
    """Leniently extract at most ``limit`` hits; partial SERP is better than none."""
    parser = _ResultsParser(provider)
    parser.feed(html[:MAX_HTML_BYTES])
    parser.close()
    parser.finalize()
    return parser.results[:limit]
