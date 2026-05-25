from __future__ import annotations

from typing import Any

import pytest

from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.ai import ToolCall
from learning_agent.learning_agent.extension_manager import ExtensionContext
from learning_agent.learning_agent.extensions.web_search_tools import (
    create_web_search_tools_extension,
)
from learning_agent.learning_agent.providers import RawFetchedPage, RawSearchResult
from learning_agent.learning_agent.services import (
    DEFAULT_ALLOWED_SOURCE_TYPES,
    WebSearchService,
    WebSearchServiceConfig,
)
from learning_agent.learning_agent.tool_registry import ToolRegistry


class FakeSearchProvider:
    def __init__(self, results: list[RawSearchResult]) -> None:
        self._results = results

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        language: str = "any",
        freshness: str = "any",
    ) -> list[RawSearchResult]:
        del query, top_k, language, freshness
        return self._results


class FakeFetchProvider:
    def __init__(self, page: RawFetchedPage) -> None:
        self._page = page

    async def fetch(self, url: str) -> RawFetchedPage:
        del url
        return self._page


class FakeService:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        return {"kind": "search", **kwargs}

    async def fetch(self, **kwargs: Any) -> dict[str, Any]:
        return {"kind": "fetch", **kwargs}


@pytest.mark.asyncio
async def test_web_search_service_dedups_and_prioritizes_official_docs():
    service = WebSearchService(
        FakeSearchProvider(
            [
                RawSearchResult(
                    title="Python Docs",
                    url="https://docs.python.org/3/?utm_source=test",
                    snippet="Official documentation",
                    score=0.7,
                ),
                RawSearchResult(
                    title="Example Blog",
                    url="https://example.com/blog/python-guide",
                    snippet="Blog post",
                    score=0.95,
                ),
                RawSearchResult(
                    title="Python Docs Duplicate",
                    url="https://docs.python.org/3/",
                    snippet="Duplicate official documentation",
                    score=0.8,
                ),
            ]
        ),
        FakeFetchProvider(
            RawFetchedPage(
                url="https://docs.python.org/3/",
                title="Python Docs",
                content="content",
            )
        ),
        config=WebSearchServiceConfig(
            default_top_k=5,
            max_top_k=5,
            default_limit_chars=12000,
            allowed_source_types=tuple(DEFAULT_ALLOWED_SOURCE_TYPES),
        ),
    )

    result = await service.search(
        query="python docs",
        source_preferences=["official_docs"],
    )

    assert result["total_returned"] == 2
    assert result["results"][0]["source_type"] == "official_docs"
    assert result["results"][0]["url"] == "https://docs.python.org/3/"
    assert result["results"][1]["source_type"] == "official_blog"


@pytest.mark.asyncio
async def test_web_fetch_truncates_and_returns_next_offset():
    long_text = "A" * 15000
    service = WebSearchService(
        FakeSearchProvider([]),
        FakeFetchProvider(
            RawFetchedPage(
                url="https://example.com/page",
                title="Example Page",
                content=long_text,
                content_type="text/html",
            )
        ),
        config=WebSearchServiceConfig(
            default_top_k=5,
            max_top_k=5,
            default_limit_chars=12000,
            allowed_source_types=tuple(DEFAULT_ALLOWED_SOURCE_TYPES),
        ),
    )

    result = await service.fetch(url="https://example.com/page", limit_chars=12000)

    assert len(result["content"]) == 12000
    assert result["truncated"] is True
    assert result["next_offset"] == 12000


@pytest.mark.asyncio
async def test_web_search_extension_registers_tools_and_handles_inputs(monkeypatch: pytest.MonkeyPatch):
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(config={"web_search": {"enabled": True}})
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=HookSystem(),
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    assert registry.get("web_search") is not None
    assert registry.get("web_fetch") is not None

    search_result = await registry.execute(
        ToolCall(tool_id="web_search", arguments={"query": "fastapi docs", "top_k": 3})
    )
    fetch_result = await registry.execute(
        ToolCall(tool_id="web_fetch", arguments={"url": "https://example.com/doc", "offset": 10})
    )
    invalid_result = await registry.execute(
        ToolCall(tool_id="web_search", arguments={"query": "   "})
    )

    assert search_result["kind"] == "search"
    assert search_result["top_k"] == 3
    assert fetch_result["kind"] == "fetch"
    assert fetch_result["offset"] == 10
    assert invalid_result["error"]["code"] == "INVALID_QUERY"
