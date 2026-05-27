from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


class SearchProviderError(Exception):
    """Web search provider base error."""


class SearchTimeoutError(TimeoutError, SearchProviderError):
    """Transient timeout while searching the web."""


class SearchUnavailableError(ConnectionError, SearchProviderError):
    """Transient upstream failure while searching the web."""


@dataclass(slots=True)
class RawSearchResult:
    title: str
    url: str
    snippet: str = ""
    published_at: Optional[str] = None
    score: float = 0.0


class WebSearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        top_k: int,
        language: str = "any",
        freshness: str = "any",
    ) -> list[RawSearchResult]:
        ...
