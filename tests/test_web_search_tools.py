from __future__ import annotations

import io
import ssl
from urllib import error
from typing import Any

import pytest

from learning_agent.agent.event_bus import EventBus
from learning_agent.agent.hook_system import HookSystem
from learning_agent.ai import (
    AfterToolExecuteInput,
    BeforeToolExecuteInput,
    HookDecision,
    HookContext,
    ToolCall,
)
from learning_agent.learning_agent.extension_manager import ExtensionContext
from learning_agent.learning_agent.extensions.web_search_tools import (
    _build_web_search_service,
    create_web_search_tools_extension,
)
from learning_agent.learning_agent.providers import RawFetchedPage, RawFetchedSection, RawSearchResult
from learning_agent.learning_agent.providers.adapters.builtin_web_fetch import (
    BuiltinWebFetchProvider,
    _extract_readable_html,
    _extract_structured_readable_html,
)
from learning_agent.learning_agent.providers.adapters.tls_utils import build_web_tls_context
from learning_agent.learning_agent.services import (
    DEFAULT_ALLOWED_SOURCE_TYPES,
    WebSearchService,
    WebSearchServiceConfig,
    classify_source,
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


class _FakeSSLContext:
    pass


class _FakeHTTPResponse:
    def __init__(
        self,
        *,
        url: str,
        body: str,
        content_type: str,
        charset: str = "utf-8",
    ) -> None:
        self._url = url
        self._body = body.encode(charset)
        self.status = 200
        self.headers = self
        self._content_type = content_type
        self._charset = charset

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit: int | None = None) -> bytes:
        if limit is None:
            return self._body
        return self._body[:limit]

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str:
        return self._charset

    def geturl(self) -> str:
        return self._url


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
    assert "official documentation" in result["results"][0]["snippet"].lower()
    assert result["results"][1]["source_type"] == "official_blog"


@pytest.mark.asyncio
async def test_web_search_service_builds_query_aware_excerpt():
    service = WebSearchService(
        FakeSearchProvider(
            [
                RawSearchResult(
                    title="FastAPI Response Guide",
                    url="https://fastapi.tiangolo.com/advanced/custom-response/",
                    snippet=(
                        "FastAPI supports many response classes. "
                        "StreamingResponse sends streamed bodies from generators. "
                        "Additional deployment notes."
                    ),
                    score=0.8,
                )
            ]
        ),
        FakeFetchProvider(
            RawFetchedPage(
                url="https://fastapi.tiangolo.com/advanced/custom-response/",
                title="FastAPI Response Guide",
                content="content",
            )
        ),
    )

    result = await service.search(query="fastapi streamingresponse", source_preferences=["official_docs"])

    assert result["results"][0]["snippet"] == "StreamingResponse sends streamed bodies from generators."
    assert result["guidance"]["recommended_next_action"] == "fetch_top_authoritative_result_then_answer"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://fastapi.tiangolo.com/tutorial/response-model/", "official_docs"),
        ("https://www.starlette.dev/responses/", "official_docs"),
        ("https://openai.com/index/introducing-gpt-5/", "official_blog"),
        ("https://arxiv.org/abs/1706.03762", "paper"),
        ("https://raw.githubusercontent.com/encode/starlette/master/docs/responses.md", "github_repo"),
    ],
)
def test_classify_source_prioritizes_high_value_learning_sources(url: str, expected: str):
    assert classify_source(url) == expected


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
    assert result["pagination_mode"] == "offset"


def test_extract_readable_html_prefers_main_content_and_strips_noise():
    html = """
    <html>
      <head><title>FastAPI Docs</title></head>
      <body>
        <header>Top Navigation</header>
        <nav class="sidebar toc">Table of contents</nav>
        <main class="docs-content">
          <article>
            <h1>StreamingResponse</h1>
            <p>Use StreamingResponse for streamed bodies.</p>
            <pre><code>return StreamingResponse(generator())</code></pre>
          </article>
        </main>
        <footer>Footer links</footer>
      </body>
    </html>
    """

    title, content = _extract_readable_html(html)

    assert title == "FastAPI Docs"
    assert "StreamingResponse" in content
    assert "Use StreamingResponse for streamed bodies." in content
    assert "return StreamingResponse(generator())" in content
    assert "Top Navigation" not in content
    assert "Table of contents" not in content
    assert "Footer links" not in content


def test_extract_structured_readable_html_builds_heading_sections():
    html = """
    <html>
      <head><title>FastAPI Docs</title></head>
      <body>
        <main class="docs-content">
          <article>
            <h1>StreamingResponse</h1>
            <p>Use StreamingResponse for streamed bodies.</p>
            <h2>Parameters</h2>
            <p>It accepts an iterator.</p>
            <pre><code>return StreamingResponse(generator())</code></pre>
          </article>
        </main>
      </body>
    </html>
    """

    title, content, sections, headings = _extract_structured_readable_html(html)

    assert title == "FastAPI Docs"
    assert "StreamingResponse" in content
    assert headings == ("StreamingResponse", "Parameters")
    assert len(sections) == 2
    assert sections[0].cursor == "s0"
    assert sections[0].section_path == ("StreamingResponse",)
    assert sections[1].cursor == "s1"
    assert sections[1].section_path == ("StreamingResponse", "Parameters")
    assert "return StreamingResponse(generator())" in sections[1].code_blocks[0]


@pytest.mark.asyncio
async def test_builtin_web_fetch_provider_parses_rss_feed():
    provider = BuiltinWebFetchProvider()
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>OpenAI News</title>
        <item>
          <title>Latest Post</title>
          <link>https://openai.com/index/latest-post/</link>
          <pubDate>Thu, 22 May 2026 00:00:00 GMT</pubDate>
          <description>OpenAI shares the latest update.</description>
        </item>
        <item>
          <title>Earlier Post</title>
          <link>https://openai.com/index/earlier-post/</link>
          <description>Earlier summary.</description>
        </item>
      </channel>
    </rss>
    """

    provider._read_url_sync = lambda url, *, timeout=None: (url, "text/xml", rss.encode("utf-8"), "utf-8")  # type: ignore[method-assign]

    page = await provider.fetch("https://openai.com/news/rss.xml")

    assert page.title == "OpenAI News"
    assert page.content_type == "text/xml"
    assert page.published_at == "Thu, 22 May 2026 00:00:00 GMT"
    assert "Latest Post" in page.content
    assert "OpenAI shares the latest update." in page.content
    assert page.sections[0].heading == "Latest Post"
    assert page.sections[0].section_path == ("OpenAI News", "Latest Post")


@pytest.mark.asyncio
async def test_builtin_web_fetch_provider_falls_back_to_rss_after_403(monkeypatch: pytest.MonkeyPatch):
    provider = BuiltinWebFetchProvider(prefer_system_trust_store=False)
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>OpenAI News</title>
        <item>
          <title>Leader in enterprise coding agents</title>
          <link>https://openai.com/index/gartner-2026-agentic-coding-leader/</link>
          <pubDate>Thu, 22 May 2026 00:00:00 GMT</pubDate>
          <description>OpenAI discusses Gartner recognition and Codex enterprise adoption.</description>
        </item>
      </channel>
    </rss>
    """

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        url = req.full_url
        if url == "https://openai.com/news":
            raise error.HTTPError(
                url=url,
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=io.BytesIO(b"challenge"),
            )
        if url == "https://openai.com/news/rss.xml":
            return _FakeHTTPResponse(
                url=url,
                body=rss,
                content_type="text/xml",
            )
        raise AssertionError(f"Unexpected urlopen call: {url}")

    monkeypatch.setattr(
        "learning_agent.learning_agent.providers.adapters.builtin_web_fetch.request.urlopen",
        _fake_urlopen,
    )

    page = await provider.fetch("https://openai.com/news")

    assert page.title == "OpenAI News"
    assert "Leader in enterprise coding agents" in page.content
    assert page.sections[0].heading == "Leader in enterprise coding agents"


@pytest.mark.asyncio
async def test_builtin_web_fetch_provider_blog_alias_can_fall_back_to_news_rss(monkeypatch: pytest.MonkeyPatch):
    provider = BuiltinWebFetchProvider(prefer_system_trust_store=False)
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>OpenAI News</title>
        <item>
          <title>Latest News Entry</title>
          <link>https://openai.com/index/latest-news-entry/</link>
          <pubDate>Mon, 25 May 2026 00:00:00 GMT</pubDate>
          <description>Fallback through news RSS succeeds.</description>
        </item>
      </channel>
    </rss>
    """

    def _fake_urlopen(req, timeout=None, context=None):
        del timeout, context
        url = req.full_url
        if url in {"https://openai.com/blog", "https://openai.com/blog/rss.xml", "https://openai.com/blog/feed.xml"}:
            raise error.HTTPError(
                url=url,
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=io.BytesIO(b"challenge"),
            )
        if url == "https://openai.com/news/rss.xml":
            return _FakeHTTPResponse(
                url=url,
                body=rss,
                content_type="text/xml",
            )
        raise AssertionError(f"Unexpected urlopen call: {url}")

    monkeypatch.setattr(
        "learning_agent.learning_agent.providers.adapters.builtin_web_fetch.request.urlopen",
        _fake_urlopen,
    )

    page = await provider.fetch("https://openai.com/blog")

    assert page.title == "OpenAI News"
    assert "Latest News Entry" in page.content
    assert page.sections[0].heading == "Latest News Entry"


@pytest.mark.asyncio
async def test_web_fetch_defaults_to_section_pagination_when_sections_exist():
    service = WebSearchService(
        FakeSearchProvider([]),
        FakeFetchProvider(
            RawFetchedPage(
                url="https://www.starlette.dev/responses/",
                title="Starlette Responses",
                content="Intro\nSection A\nAlpha\nSection B\nBeta",
                sections=(
                    RawFetchedSection(
                        cursor="s0",
                        heading="Responses",
                        section_path=("Responses",),
                        content="Responses\nAlpha",
                    ),
                    RawFetchedSection(
                        cursor="s1",
                        heading="StreamingResponse",
                        section_path=("Responses", "StreamingResponse"),
                        content="StreamingResponse\nBeta",
                        code_blocks=("return StreamingResponse(generator())",),
                    ),
                ),
                headings=("Responses", "StreamingResponse"),
                code_blocks=("return StreamingResponse(generator())",),
            )
        ),
    )

    first = await service.fetch(url="https://www.starlette.dev/responses/")
    second = await service.fetch(url="https://www.starlette.dev/responses/", section_cursor="s1")

    assert first["pagination_mode"] == "section"
    assert first["section_cursor"] == "s0"
    assert first["next_section_cursor"] == "s1"
    assert first["next_section_hint"] == "StreamingResponse"
    assert first["available_sections"][0]["heading"] == "Responses"
    assert first["source_type"] == "official_docs"
    assert first["guidance"]["recommended_next_action"] == "continue_to_one_more_specific_section_only_if_missing_detail"
    assert first["guidance"]["should_stop_after_this"] is False
    assert second["section_cursor"] == "s1"
    assert second["section_path"] == ["Responses", "StreamingResponse"]
    assert second["code_blocks"] == ["return StreamingResponse(generator())"]
    assert second["guidance"]["recommended_next_action"] == "answer_now"
    assert second["guidance"]["should_stop_after_this"] is True


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
        ToolCall(tool_id="web_fetch", arguments={"url": "https://example.com/doc", "offset": 10, "section_cursor": "s1"})
    )
    invalid_result = await registry.execute(
        ToolCall(tool_id="web_search", arguments={"query": "   "})
    )

    assert search_result["kind"] == "search"
    assert search_result["top_k"] == 3
    assert fetch_result["kind"] == "fetch"
    assert fetch_result["offset"] == 10
    assert fetch_result["section_cursor"] == "s1"
    assert invalid_result["error"]["code"] == "INVALID_QUERY"


@pytest.mark.asyncio
async def test_web_search_extension_phase_three_budget_hooks(monkeypatch: pytest.MonkeyPatch):
    hook_system = HookSystem()
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(
        config={
            "web_search": {
                "enabled": True,
                "max_fetch_calls_per_turn": 3,
                "max_same_domain_fetches_per_turn": 2,
                "max_total_fetch_chars_per_turn": 2200,
            }
        }
    )
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=hook_system,
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    context = HookContext(session_id="sess-1", turn_id=3)

    before_first = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="call-1",
            tool_name="web_fetch",
            arguments={"url": "https://docs.python.org/3/library/io.html", "limit_chars": 1200},
            tool_schema={},
            context=context,
        )
    )
    assert before_first.decision == HookDecision.CONTINUE

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="call-1",
            tool_name="web_fetch",
            arguments={"url": "https://docs.python.org/3/library/io.html", "limit_chars": 1200},
            success=True,
            result={"content": "A" * 1200, "domain": "docs.python.org"},
            context=context,
        )
    )

    before_second = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="call-2",
            tool_name="web_fetch",
            arguments={"url": "https://docs.python.org/3/library/asyncio.html", "limit_chars": 1200},
            tool_schema={},
            context=context,
        )
    )
    assert before_second.decision == HookDecision.CONTINUE
    assert before_second.patched_arguments["limit_chars"] == 1000

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="call-2",
            tool_name="web_fetch",
            arguments={"url": "https://docs.python.org/3/library/asyncio.html", "limit_chars": 1000},
            success=True,
            result={"content": "B" * 1000, "domain": "docs.python.org"},
            context=context,
        )
    )

    before_third = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="call-3",
            tool_name="web_fetch",
            arguments={"url": "https://docs.python.org/3/library/pathlib.html", "limit_chars": 1200},
            tool_schema={},
            context=context,
        )
    )
    assert before_third.decision == HookDecision.DENY
    assert "same domain" in (before_third.deny_reason or "").lower()


@pytest.mark.asyncio
async def test_web_search_extension_counts_failed_fetches_into_budget(monkeypatch: pytest.MonkeyPatch):
    hook_system = HookSystem()
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(
        config={
            "web_search": {
                "enabled": True,
                "max_fetch_calls_per_turn": 2,
                "max_same_domain_fetches_per_turn": 5,
                "max_total_fetch_chars_per_turn": 5000,
            }
        }
    )
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=hook_system,
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    context = HookContext(session_id="sess-2", turn_id=5)
    url = "https://www.starlette.dev/responses/"

    for call_id in ("call-f1", "call-f2"):
        before = await hook_system.run_before_tool_execute(
            BeforeToolExecuteInput(
                tool_call_id=call_id,
                tool_name="web_fetch",
                arguments={"url": url, "limit_chars": 1200},
                tool_schema={},
                context=context,
            )
        )
        assert before.decision == HookDecision.CONTINUE
        after = await hook_system.run_after_tool_execute(
            AfterToolExecuteInput(
                tool_call_id=call_id,
                tool_name="web_fetch",
                arguments={"url": url, "limit_chars": 1200},
                success=False,
                result={"error": {"code": "FETCH_FAILED"}},
                context=context,
            )
        )
        assert after.extra_metadata["fetch_calls"] >= 1
        assert after.extra_metadata["failed_fetch_calls"] >= 1

    before_third = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="call-f3",
            tool_name="web_fetch",
            arguments={"url": url, "limit_chars": 1200},
            tool_schema={},
            context=context,
        )
    )
    assert before_third.decision == HookDecision.DENY
    assert "budget reached" in (before_third.deny_reason or "").lower()


@pytest.mark.asyncio
async def test_web_search_extension_blocks_second_search_after_authoritative_fetch(monkeypatch: pytest.MonkeyPatch):
    hook_system = HookSystem()
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(config={"web_search": {"enabled": True}})
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=hook_system,
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    search_context = HookContext(session_id="sess-3", trace_id="trace-a", turn_id=7)
    fetch_context = HookContext(session_id="sess-3", trace_id="trace-a", turn_id=8)
    followup_context = HookContext(session_id="sess-3", trace_id="trace-a", turn_id=9)

    before_search = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "fastapi streamingresponse official docs"},
            tool_schema={},
            context=search_context,
        )
    )
    assert before_search.decision == HookDecision.CONTINUE

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "fastapi streamingresponse official docs"},
            success=True,
            result={"results": [{"source_type": "official_docs"}]},
            context=search_context,
        )
    )

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="fetch-1",
            tool_name="web_fetch",
            arguments={"url": "https://fastapi.tiangolo.com/advanced/custom-response/"},
            success=True,
            result={
                "content": "StreamingResponse lets you stream a response body.",
                "domain": "fastapi.tiangolo.com",
                "guidance": {
                    "should_stop_after_this": False,
                    "recommended_next_action": "continue_to_one_more_specific_section_only_if_missing_detail",
                },
            },
            context=fetch_context,
        )
    )

    before_second_search = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="search-2",
            tool_name="web_search",
            arguments={"query": "starlette responses official docs"},
            tool_schema={},
            context=followup_context,
        )
    )
    assert before_second_search.decision == HookDecision.DENY
    assert "already searched authoritative sources" in (before_second_search.deny_reason or "").lower()


@pytest.mark.asyncio
async def test_web_search_extension_blocks_repeat_search_before_first_fetch(monkeypatch: pytest.MonkeyPatch):
    hook_system = HookSystem()
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(config={"web_search": {"enabled": True}})
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=hook_system,
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    search_context = HookContext(session_id="sess-3b", trace_id="trace-a2", turn_id=7)
    followup_context = HookContext(session_id="sess-3b", trace_id="trace-a2", turn_id=8)

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "fastapi streamingresponse official docs"},
            success=True,
            result={"results": [{"source_type": "official_docs"}]},
            context=search_context,
        )
    )

    before_second_search = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="search-2",
            tool_name="web_search",
            arguments={"query": "fastapi streamingresponse example"},
            tool_schema={},
            context=followup_context,
        )
    )
    assert before_second_search.decision == HookDecision.DENY
    assert "fetch the best current result" in (before_second_search.deny_reason or "").lower()


@pytest.mark.asyncio
async def test_web_search_extension_blocks_more_web_tools_once_evidence_is_ready(monkeypatch: pytest.MonkeyPatch):
    hook_system = HookSystem()
    registry = ToolRegistry()
    ext = create_web_search_tools_extension(config={"web_search": {"enabled": True}})
    ctx = ExtensionContext(
        extension_id=ext.id,
        hook_system=hook_system,
        event_bus=EventBus(),
        tool_registry=registry,
        config=ext.config,
    )

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools._build_web_search_service",
        lambda config: FakeService(),
    )

    await ext.activate(ctx)

    search_context = HookContext(session_id="sess-4", trace_id="trace-b", turn_id=8)
    fetch_context = HookContext(session_id="sess-4", trace_id="trace-b", turn_id=9)
    followup_context = HookContext(session_id="sess-4", trace_id="trace-b", turn_id=10)

    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "fastapi streamingresponse official docs"},
            success=True,
            result={"results": [{"source_type": "official_docs"}]},
            context=search_context,
        )
    )
    await hook_system.run_after_tool_execute(
        AfterToolExecuteInput(
            tool_call_id="fetch-1",
            tool_name="web_fetch",
            arguments={"url": "https://fastapi.tiangolo.com/advanced/custom-response/"},
            success=True,
            result={
                "content": "StreamingResponse example",
                "domain": "fastapi.tiangolo.com",
                "guidance": {
                    "should_stop_after_this": True,
                    "recommended_next_action": "answer_now",
                },
            },
            context=fetch_context,
        )
    )

    before_search = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="search-2",
            tool_name="web_search",
            arguments={"query": "another search"},
            tool_schema={},
            context=followup_context,
        )
    )
    before_fetch = await hook_system.run_before_tool_execute(
        BeforeToolExecuteInput(
            tool_call_id="fetch-2",
            tool_name="web_fetch",
            arguments={"url": "https://www.starlette.dev/responses/"},
            tool_schema={},
            context=followup_context,
        )
    )
    assert before_search.decision == HookDecision.DENY
    assert before_fetch.decision == HookDecision.DENY
    assert "enough authoritative evidence" in (before_search.deny_reason or "").lower()
    assert "enough authoritative evidence" in (before_fetch.deny_reason or "").lower()


def test_build_web_tls_context_uses_explicit_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
):
    bundle = tmp_path / "root.pem"
    bundle.write_text("dummy", encoding="utf-8")
    captured: dict[str, Any] = {}

    def _fake_create_default_context(*, cafile: str | None = None) -> _FakeSSLContext:
        captured["cafile"] = cafile
        return _FakeSSLContext()

    monkeypatch.setattr(
        "learning_agent.learning_agent.providers.adapters.tls_utils.ssl.create_default_context",
        _fake_create_default_context,
    )

    context = build_web_tls_context(
        ca_bundle_path=str(bundle),
        prefer_system_trust_store=False,
    )

    assert isinstance(context, _FakeSSLContext)
    assert captured["cafile"] == str(bundle)


def test_build_web_tls_context_uses_truststore_when_available(monkeypatch: pytest.MonkeyPatch):
    class _FakeTruststoreModule:
        @staticmethod
        def SSLContext(protocol: int) -> tuple[str, int]:
            return ("truststore", protocol)

    monkeypatch.setattr(
        "learning_agent.learning_agent.providers.adapters.tls_utils.importlib.import_module",
        lambda name: _FakeTruststoreModule() if name == "truststore" else None,
    )

    context = build_web_tls_context(
        ca_bundle_path=None,
        prefer_system_trust_store=True,
    )

    assert context == ("truststore", ssl.PROTOCOL_TLS_CLIENT)


def test_build_web_search_service_passes_tls_config_to_builtin_providers(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    class _FakeSearchProvider:
        def __init__(self, **kwargs: Any) -> None:
            captured["search"] = kwargs

    class _FakeFetchProvider:
        def __init__(self, **kwargs: Any) -> None:
            captured["fetch"] = kwargs

    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools.BuiltinWebSearchProvider",
        _FakeSearchProvider,
    )
    monkeypatch.setattr(
        "learning_agent.learning_agent.extensions.web_search_tools.BuiltinWebFetchProvider",
        _FakeFetchProvider,
    )

    service = _build_web_search_service(
        {
            "provider": "builtin",
            "timeout_seconds": 8,
            "tls_ca_bundle_path": "/tmp/custom-ca.pem",
            "prefer_system_trust_store": False,
        }
    )

    assert service is not None
    assert captured["search"] == {
        "timeout_seconds": 8.0,
        "ca_bundle_path": "/tmp/custom-ca.pem",
        "prefer_system_trust_store": False,
    }
    assert captured["fetch"] == {
        "timeout_seconds": 8.0,
        "overall_timeout_seconds": 30.0,
        "ca_bundle_path": "/tmp/custom-ca.pem",
        "prefer_system_trust_store": False,
    }
