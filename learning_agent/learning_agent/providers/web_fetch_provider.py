from __future__ import annotations

from dataclasses import dataclass
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
class RawFetchedPage:
    url: str
    title: str
    content: str
    content_type: str = "text/html"
    published_at: Optional[str] = None


class WebFetchProvider(Protocol):
    async def fetch(self, url: str) -> RawFetchedPage:
        ...
