from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


class FetchProviderError(Exception):
    """Web fetch provider base error."""


class FetchTimeoutError(TimeoutError, FetchProviderError):
    """Transient timeout while fetching a web page."""


class FetchUnavailableError(ConnectionError, FetchProviderError):
    """Transient upstream failure while fetching a web page."""


class UnsupportedPageError(FetchProviderError):
    """The requested page cannot be normalized into readable content."""


@dataclass(slots=True)
class RawFetchedSection:
    cursor: str
    heading: str
    section_path: tuple[str, ...] = field(default_factory=tuple)
    content: str = ""
    code_blocks: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class RawFetchedPage:
    url: str
    title: str
    content: str
    content_type: str = "text/html"
    published_at: Optional[str] = None
    headings: tuple[str, ...] = field(default_factory=tuple)
    sections: tuple[RawFetchedSection, ...] = field(default_factory=tuple)
    code_blocks: tuple[str, ...] = field(default_factory=tuple)


class WebFetchProvider(Protocol):
    async def fetch(self, url: str) -> RawFetchedPage:
        ...
