from __future__ import annotations

from typing import Any

import pytest

from learning_agent.learning_agent.providers import RawFetchedPage, RawFetchedSection
from learning_agent.learning_agent.services import WebSearchService, WebSearchServiceConfig
from learning_agent.learning_agent.services.web_search_service import (
    _query_terms_for_section_match,
    _score_section_against_query,
)


class _FakeFetchProvider:
    def __init__(self, page: RawFetchedPage) -> None:
        self._page = page

    async def fetch(self, url: str) -> RawFetchedPage:
        del url
        return self._page


class _FakeSearchProvider:
    async def search(self, query: str, *, top_k: int, language: str = "any", freshness: str = "any") -> list:
        del query, top_k, language, freshness
        return []


def _multi_section_page() -> RawFetchedPage:
    """构造类似 docs.python.org/3/library/asyncio-task.html 的多段页面。"""
    sections = (
        RawFetchedSection(
            cursor="s0",
            heading="Coroutines",
            section_path=("Coroutines and Tasks", "Coroutines"),
            content="Coroutines declared with the async/await syntax is the preferred way of writing asyncio applications. " * 8,
        ),
        RawFetchedSection(
            cursor="s1",
            heading="Awaitables",
            section_path=("Coroutines and Tasks", "Awaitables"),
            content="We say that an object is an awaitable object if it can be used in an await expression. " * 8,
        ),
        RawFetchedSection(
            cursor="s2",
            heading="Task groups",
            section_path=("Coroutines and Tasks", "Task groups"),
            content=(
                "Task groups combine a task creation API with a convenient and reliable way "
                "to wait for all tasks in the group to finish. The TaskGroup async context manager "
                "holds a group of tasks. Tasks can be added to the group using create_task(). "
                "All tasks are awaited when the context manager exits via __aexit__()."
            ) * 3,
        ),
        RawFetchedSection(
            cursor="s3",
            heading="Terminating a Task Group",
            section_path=("Coroutines and Tasks", "Task groups", "Terminating a Task Group"),
            content=(
                "While terminating a task group is not natively supported by the standard library, "
                "termination can be achieved by adding an exception-raising task to the group. "
                "TaskGroup terminates all running tasks once any task raises an exception."
            ) * 2,
        ),
        RawFetchedSection(
            cursor="s4",
            heading="Sleeping",
            section_path=("Coroutines and Tasks", "Sleeping"),
            content="asyncio.sleep(delay, result=None) blocks for delay seconds. " * 8,
        ),
    )
    full_content = "\n\n".join(s.content for s in sections)
    return RawFetchedPage(
        url="https://docs.python.org/3/library/asyncio-task.html",
        title="Coroutines and Tasks",
        content=full_content,
        content_type="text/html",
        headings=tuple(s.heading for s in sections),
        sections=sections,
    )


def _service_for(page: RawFetchedPage) -> WebSearchService:
    return WebSearchService(
        search_provider=_FakeSearchProvider(),
        fetch_provider=_FakeFetchProvider(page),
        config=WebSearchServiceConfig(),
    )


# ----- 纯函数测试 -----


def test_query_terms_for_section_match_includes_ascii():
    assert "task" in _query_terms_for_section_match("task group")
    assert "group" in _query_terms_for_section_match("task group")


def test_query_terms_for_section_match_picks_up_cjk():
    terms = _query_terms_for_section_match("任务组 task group")
    assert "任务组" in terms
    assert "task" in terms
    assert "group" in terms


def test_query_terms_for_section_match_empty_query_is_empty():
    assert _query_terms_for_section_match("") == []
    assert _query_terms_for_section_match("   ") == []


def test_score_section_against_query_zero_when_no_terms():
    # query 没有任何 ASCII token / CJK token,score 应为 0
    assert _score_section_against_query("a", "the quick brown fox", "Greeting") == 0


def test_score_section_against_query_heading_weight_higher_than_content():
    # 同一份 query 命中 heading 1 次 vs content 1 次,前者分数更高(3 倍权重)
    heading_score = _score_section_against_query("task", section_content="", section_heading="Task")
    content_score = _score_section_against_query("task", section_content="task", section_heading="")
    assert heading_score > content_score


def test_score_section_against_query_content_density_capped():
    # content 同 term 出现 5 次和 50 次得分应该相同(min(5, c_count) 上限)
    five = _score_section_against_query("task", section_content="task " * 5, section_heading="")
    fifty = _score_section_against_query("task", section_content="task " * 50, section_heading="")
    assert five == fifty


def test_score_section_against_query_longer_terms_score_more():
    # 长 term 更稀有,specificity = len(term) 是乘数
    short = _score_section_against_query("ab", section_content="ab", section_heading="")
    long_ = _score_section_against_query("abcdef", section_content="abcdef", section_heading="")
    assert long_ > short


def test_score_section_against_query_cjk_term_matches_cjk_content():
    score = _score_section_against_query(
        "任务组",
        section_content="任务组 是 asyncio 在 Python 3.11 引入的新原语,任务组 能等所有任务结束。",
        section_heading="任务组",
    )
    assert score > 0


# ----- service.fetch(query=...) 集成测试 -----


@pytest.mark.asyncio
async def test_query_aware_picks_relevant_sections():
    """query="task group" 时应优先选 "Task groups" / "Terminating a Task Group" 两段,
    而不是从 s0 开始切。"""
    service = _service_for(_multi_section_page())
    result = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query="task group",
        limit_chars=8000,
    )
    assert result["pagination_mode"] == "query"
    assert result["query"] == "task group"
    # 至少命中 "Task groups" 那段 (s2)
    assert "s2" in result["selected_sections"]
    # 不应包含完全无关的 "Sleeping" 段
    assert "s4" not in result["selected_sections"]
    # 段路径在 selected_section_paths 里
    assert result["selected_section_paths"]
    # content 包含 task group 相关正文
    assert "TaskGroup" in result["content"] or "Task groups" in result["content"]
    # query 模式应该不推荐 next_offset / next_section_cursor
    assert result["next_offset"] is None
    assert result["next_section_cursor"] is None


@pytest.mark.asyncio
async def test_query_aware_empty_query_falls_back_to_section_mode():
    """query=None / "" 时应走原有 section 路径,不进 query 分支。"""
    service = _service_for(_multi_section_page())

    result_none = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query=None,
    )
    assert result_none["pagination_mode"] == "section"
    assert result_none.get("query") is None or "query" not in result_none

    result_blank = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query="   ",
    )
    assert result_blank["pagination_mode"] == "section"


@pytest.mark.asyncio
async def test_query_aware_zero_score_falls_back_to_section_mode():
    """query 完全不命中任何段时,应降级到 section/offset 原路径,不返回空。"""
    service = _service_for(_multi_section_page())
    result = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query="zzzzz nonexistent token xyzqwerty",
    )
    # 降级到 section 模式
    assert result["pagination_mode"] == "section"
    # 仍有 content
    assert result["content"].strip()


@pytest.mark.asyncio
async def test_query_aware_respects_limit_chars():
    """top-N 累计字符不应超过 bounded_limit(允许超 1 段以保证不空回)。"""
    service = _service_for(_multi_section_page())
    # limit 设小一点,触发裁剪
    result = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query="task group taskgroup",
        limit_chars=1500,  # bounded_limit 实际会被 clamp 到至少 1000
    )
    assert result["pagination_mode"] == "query"
    # content 长度不超过 limit_chars(最后兜底 content[:bounded_limit])
    assert len(result["content"]) <= result["limit_chars"]


@pytest.mark.asyncio
async def test_query_aware_truncated_flag_when_some_sections_dropped():
    """命中的段超过 limit_chars 容纳能力时,truncated 应为 True。"""
    # 构造命中很多段的页面
    sections = tuple(
        RawFetchedSection(
            cursor=f"s{i}",
            heading=f"Task Group Section {i}",
            section_path=(f"Task Group Section {i}",),
            content=f"This section talks about task group operations. " * 50,
        )
        for i in range(6)
    )
    page = RawFetchedPage(
        url="https://example.com/long",
        title="Long",
        content="\n\n".join(s.content for s in sections),
        sections=sections,
    )
    service = _service_for(page)
    result = await service.fetch(
        url="https://example.com/long",
        query="task group",
        limit_chars=1500,
    )
    assert result["pagination_mode"] == "query"
    assert result["truncated"] is True
    # 至少选了 1 段,但不是全部
    assert 1 <= len(result["selected_sections"]) < len(sections)


@pytest.mark.asyncio
async def test_query_aware_cjk_query_works_end_to_end():
    """中文 query 也能走 query 路径并命中段。"""
    sections = (
        RawFetchedSection(
            cursor="s0",
            heading="任务组",
            section_path=("协程与任务", "任务组"),
            content="任务组 是 asyncio 在 Python 3.11 引入的新原语,任务组 把多个任务的生命周期捆绑在一起,能等所有任务结束。",
        ),
        RawFetchedSection(
            cursor="s1",
            heading="协程",
            section_path=("协程与任务", "协程"),
            content="协程是用 async/await 语法声明的函数,是编写 asyncio 应用的首选方式。",
        ),
    )
    page = RawFetchedPage(
        url="https://example.cn/asyncio",
        title="协程与任务",
        content="\n\n".join(s.content for s in sections),
        sections=sections,
    )
    service = _service_for(page)
    result = await service.fetch(
        url="https://example.cn/asyncio",
        query="任务组",
    )
    assert result["pagination_mode"] == "query"
    assert "s0" in result["selected_sections"]
    assert "任务组" in result["content"]


@pytest.mark.asyncio
async def test_query_aware_guidance_should_stop_true():
    """query 模式 guidance.should_stop 应为 True(query 已经精选过,推荐 answer_now)。"""
    service = _service_for(_multi_section_page())
    result = await service.fetch(
        url="https://docs.python.org/3/library/asyncio-task.html",
        query="task group",
    )
    guidance = result["guidance"]
    assert guidance.get("should_stop_after_this") is True
    assert guidance.get("recommended_next_action") == "answer_now"
