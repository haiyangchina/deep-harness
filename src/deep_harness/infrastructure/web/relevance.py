"""Bounded lexical ranking for multilingual search candidates."""

import re
import unicodedata
from collections.abc import Iterable

from deep_harness.infrastructure.web.models import SearchHit
from deep_harness.infrastructure.web.security import normalize_url

_CJK = re.compile(r"[\u3400-\u9fff]+")
_WORD = re.compile(r"[^\W\d_]+[^\W_]*|\d+(?:[.-]\d+)*")
_FILLER = re.compile(
    r"^(?:请(?:帮我)?(?:查询|查找|搜索)?|帮我(?:查询|查找|搜索)?|如何|怎么)"
    r"|(?:的?(?:是什么|有哪些|怎么样|介绍一下))$"
)
_STOP_WORDS = {"a", "an", "and", "for", "how", "in", "is", "of", "on", "the", "to", "what"}
_TIME = re.compile(r"(?:未来|最近|近)[一二两三四五六七八九十\d]+(?:天|周|月)|未来一周")
_EXACT = re.compile(r"\b\d+(?:[.-]\d+)+\b")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _terms(text: str) -> set[str]:
    text = _normalize(text)
    words = set(_WORD.findall(_CJK.sub(" ", text))) - _STOP_WORDS
    for run in _CJK.findall(text):
        words.update(run[index:index + 2] for index in range(len(run) - 1))
    return words


def relevance(query: str, hit: SearchHit) -> float | None:
    """Compare query coverage in title, URL and summary. None signals too little context."""
    query = _normalize(query)
    terms = _terms(_TIME.sub(" ", _FILLER.sub("", query)))
    if len(terms) < 2:
        return None
    title = _terms(f"{hit.title} {hit.url}")
    summary = _terms(f"{hit.snippet} {hit.text}")
    score = (len(terms & title) + 0.75 * len((terms - title) & summary)) / len(terms)
    exact = set(_EXACT.findall(query))
    if exact and not exact.issubset(title | summary):
        score *= 0.55
    return score


def rank_hits(
    groups: Iterable[tuple[str, Iterable[SearchHit]]], limit: int
) -> list[SearchHit]:
    """Merge canonical copies and break relevance ties with cross-query rank agreement."""
    candidates: dict[str, tuple[SearchHit, float, float]] = {}
    for query, hits in groups:
        seen = set()
        for position, hit in enumerate(hits, 1):
            key = normalize_url(hit.url)
            if key in seen:
                continue
            seen.add(key)
            score = relevance(query, hit)
            quality = score if score is not None else 0.5
            best, maximum, votes = candidates.get(key, (hit, -1.0, 0.0))
            if (quality, bool(hit.text), hit.published_at) > (
                maximum, bool(best.text), best.published_at
            ):
                best, maximum = hit, quality
            candidates[key] = best, maximum, votes + 1 / position
    ranked = sorted(candidates.values(), key=lambda row: (row[1], row[2]), reverse=True)
    return [hit for hit, _, _ in ranked[:limit]]


def research_queries(question: str, supplied: list[str], limit: int) -> list[str]:
    """Keep the original and add a compact variant without dropping its constraints."""
    compact = _FILLER.sub("", question.strip()).strip(" ?？。")
    compact = re.sub(
        r"^(?:what (?:is|are)|how (?:do|can) i|please (?:find|search for))\s+",
        "",
        compact,
        flags=re.IGNORECASE,
    )
    separated = _TIME.sub(lambda match: f" {match[0]} ", compact)
    queries = {}
    for value in (question, *supplied, separated, compact):
        value = " ".join(value.split())
        if value:
            queries.setdefault(_normalize(value), value)
    return list(queries.values())[:limit]
