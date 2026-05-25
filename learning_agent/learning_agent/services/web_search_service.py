from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from learning_agent.learning_agent.providers import (
    RawFetchedPage,
    RawSearchResult,
    UnsupportedPageError,
    WebFetchProvider,
    WebSearchProvider,
)

DEFAULT_ALLOWED_SOURCE_TYPES = [
    "official_docs",
    "official_blog",
    "github_repo",
    "github_issue",
    "community_forum",
    "blog",
    "paper",
    "news",
    "aggregator",
]

_TRACKING_QUERY_KEYS = {
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
    "ref",
    "ref_src",
    "source",
}
_TRUST_SCORES = {
    "official_docs": 1.0,
    "official_blog": 0.92,
    "github_repo": 0.88,
    "github_issue": 0.82,
    "community_forum": 0.76,
    "paper": 0.8,
    "blog": 0.6,
    "news": 0.55,
    "aggregator": 0.25,
}


@dataclass(slots=True)
class WebSearchServiceConfig:
    provider_name: str = "builtin"
    default_top_k: int = 5
    max_top_k: int = 10
    default_limit_chars: int = 12000
    timeout_seconds: float = 12.0
    allowed_source_types: tuple[str, ...] = tuple(DEFAULT_ALLOWED_SOURCE_TYPES)


def build_web_search_config(values: Optional[dict]) -> WebSearchServiceConfig:
    values = values or {}
    allowed = values.get("allowed_source_types") or DEFAULT_ALLOWED_SOURCE_TYPES
    return WebSearchServiceConfig(
        provider_name=str(values.get("provider", "builtin")),
        default_top_k=max(1, int(values.get("default_top_k", 5))),
        max_top_k=max(1, int(values.get("max_top_k", 10))),
        default_limit_chars=max(1000, int(values.get("default_limit_chars", 12000))),
        timeout_seconds=float(values.get("timeout_seconds", 12.0)),
        allowed_source_types=tuple(str(item) for item in allowed),
    )


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    clean_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_QUERY_KEYS
    ]
    normalized = parsed._replace(
        fragment="",
        query=urlencode(clean_query, doseq=True),
        netloc=parsed.netloc.lower(),
    )
    return urlunparse(normalized)


def domain_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()


def classify_source(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    if domain == "github.com":
        if "/issues/" in path or "/discussions/" in path:
            return "github_issue"
        return "github_repo"
    if any(host in domain for host in ("stackoverflow.com", "stackexchange.com", "reddit.com", "discuss.", "forum.")):
        return "community_forum"
    if any(host in domain for host in ("arxiv.org", "acm.org", "ieee.org", "springer.com", "openreview.net")):
        return "paper"
    if domain.startswith("docs.") or "readthedocs" in domain or "/docs/" in path or path.startswith("/docs"):
        return "official_docs"
    if domain.startswith("blog.") or path.startswith("/blog") or "/blog/" in path:
        return "official_blog"
    if any(host in domain for host in ("news.ycombinator.com", "techcrunch.com", "theverge.com", "wired.com")):
        return "news"
    if any(host in domain for host in ("geeksforgeeks.org", "w3schools.com", "tutorialspoint.com")):
        return "aggregator"
    return "blog"


class WebSearchService:
    def __init__(
        self,
        search_provider: WebSearchProvider,
        fetch_provider: WebFetchProvider,
        config: Optional[WebSearchServiceConfig] = None,
    ) -> None:
        self._search_provider = search_provider
        self._fetch_provider = fetch_provider
        self._config = config or WebSearchServiceConfig()

    async def search(
        self,
        *,
        query: str,
        top_k: Optional[int] = None,
        freshness: str = "any",
        source_preferences: Optional[Iterable[str]] = None,
        language: str = "any",
    ) -> dict:
        normalized_query = " ".join((query or "").split()).strip()
        if not normalized_query:
            raise ValueError("Search query cannot be empty.")

        bounded_top_k = max(1, min(int(top_k or self._config.default_top_k), self._config.max_top_k))
        preferences = {item for item in (source_preferences or []) if item in self._config.allowed_source_types}
        started = time.monotonic()
        raw_results = await self._search_provider.search(
            normalized_query,
            top_k=max(bounded_top_k * 2, bounded_top_k + 3),
            language=language,
            freshness=freshness,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        ranked: dict[str, dict] = {}
        for index, raw in enumerate(raw_results):
            canonical_url = canonicalize_url(raw.url)
            if not canonical_url:
                continue
            source_type = classify_source(canonical_url)
            if self._config.allowed_source_types and source_type not in self._config.allowed_source_types:
                continue
            provider_score = raw.score or max(0.0, 1.0 - index * 0.05)
            trust_score = _TRUST_SCORES.get(source_type, 0.4)
            preference_bonus = 0.08 if source_type in preferences else 0.0
            score = round(provider_score * 0.45 + trust_score * 0.45 + preference_bonus, 4)
            candidate = {
                "id": f"r{len(ranked) + 1}",
                "title": " ".join((raw.title or "").split()) or canonical_url,
                "url": canonical_url,
                "snippet": " ".join((raw.snippet or "").split()),
                "domain": domain_from_url(canonical_url),
                "source_type": source_type,
                "published_at": raw.published_at,
                "score": score,
            }
            existing = ranked.get(canonical_url)
            if existing is None or candidate["score"] > existing["score"]:
                ranked[canonical_url] = candidate

        ordered = sorted(ranked.values(), key=lambda item: (-item["score"], item["domain"], item["title"]))
        truncated = len(ordered) > bounded_top_k
        results = ordered[:bounded_top_k]
        for index, item in enumerate(results, start=1):
            item["id"] = f"r{index}"

        return {
            "query": normalized_query,
            "results": results,
            "total_returned": len(results),
            "truncated": truncated,
            "provider": self._config.provider_name,
            "search_time_ms": elapsed_ms,
        }

    async def fetch(
        self,
        *,
        url: str,
        offset: int = 0,
        limit_chars: Optional[int] = None,
    ) -> dict:
        canonical_url = canonicalize_url(url)
        if not canonical_url:
            raise ValueError("Only http/https URLs are supported.")
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1000, min(int(limit_chars or self._config.default_limit_chars), 50000))

        page = await self._fetch_provider.fetch(canonical_url)
        if not page.content.strip():
            raise UnsupportedPageError("Fetched page has no readable content.")
        content = page.content.strip()
        sliced = content[bounded_offset:bounded_offset + bounded_limit]
        truncated = bounded_offset + bounded_limit < len(content)
        next_offset = bounded_offset + bounded_limit if truncated else None

        return {
            "url": canonical_url,
            "title": page.title.strip() or domain_from_url(canonical_url),
            "domain": domain_from_url(canonical_url),
            "content": sliced,
            "content_type": page.content_type,
            "published_at": page.published_at,
            "truncated": truncated,
            "next_offset": next_offset,
            "offset": bounded_offset,
            "limit_chars": bounded_limit,
        }


__all__ = [
    "DEFAULT_ALLOWED_SOURCE_TYPES",
    "WebSearchService",
    "WebSearchServiceConfig",
    "build_web_search_config",
    "canonicalize_url",
    "classify_source",
    "domain_from_url",
]
