"""Pure result shapes; these types do not depend on any framework."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchOptions:
    language: str | None = None
    region: str | None = None
    freshness: str | None = None


@dataclass(frozen=True)
class SearchHit:
    """One ranked result. ``text`` holds clean page text when a backend provides it."""

    title: str
    url: str
    snippet: str = ""
    text: str = ""
    provider: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class FetchResult:
    """One page read. Status is ok, partial, blocked or error."""

    url: str
    title: str = ""
    text: str = ""
    provider: str = ""
    status: str = "ok"
    note: str = ""
    truncated: bool = False
    published_at: str = ""
    attempts: tuple[BackendAttempt, ...] = ()


@dataclass(frozen=True)
class Source:
    """A citation-ready source used when shaping an evidence pack."""

    title: str
    url: str
    text: str
    provider: str = ""
    note: str = ""
    original_url: str = ""
    published_at: str = ""
    kind: str = "正文"


@dataclass(frozen=True)
class BackendAttempt:
    backend: str
    status: str
    elapsed_ms: int


@dataclass(frozen=True)
class SearchBatch:
    hits: tuple[SearchHit, ...]
    attempts: tuple[BackendAttempt, ...]
    reachable: bool
