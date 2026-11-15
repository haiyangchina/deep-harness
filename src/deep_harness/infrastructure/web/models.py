"""Pure result shapes; these types do not depend on any framework."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchHit:
    """One ranked result. ``text`` holds clean page text when a backend provides it."""

    title: str
    url: str
    snippet: str = ""
    text: str = ""
    provider: str = ""


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


@dataclass(frozen=True)
class Source:
    """A citation-ready source used when shaping an evidence pack."""

    title: str
    url: str
    text: str
    provider: str = ""
    note: str = ""
