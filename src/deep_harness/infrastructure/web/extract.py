"""HTML to compact readable text, preferring trafilatura when installed."""

import re
from html.parser import HTMLParser

from deep_harness.server_config import number

MAX_HTML_BYTES = number("web.max_http_body_bytes")
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"}


class _ReadableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._depth += 1
        if tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "div", "section", "article"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._depth:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", joined)).strip()


def fallback_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def extract_text(
    html: str, *, max_chars: int = number("web.max_response_chars_max")
) -> tuple[str, str]:
    """Return (clean_text, title). Never raises on malformed HTML."""
    if not isinstance(html, str) or not html:
        return "", ""
    sample = html[:MAX_HTML_BYTES]
    clean = ""
    try:
        import trafilatura

        clean = trafilatura.extract(sample, output_format="markdown", favor_recall=False) or ""
        if not clean:
            clean = trafilatura.extract(sample, output_format="txt", favor_recall=True) or ""
    except Exception:  # optional dependency or malformed input; use the built-in reader
        clean = ""
    parser = _ReadableParser()
    try:
        parser.feed(sample)
        parser.close()
    except Exception:  # malformed markup still yields whatever was collected
        pass
    title = parser.title.strip() or fallback_title(sample)
    if not clean:
        clean = parser.text()
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean[:max_chars], title[: number("web.provider_title_chars")]
