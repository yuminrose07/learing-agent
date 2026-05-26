"""Final Answer Guard — 回放测试。

锁住"工具调用成功导向架构 / Final Answer Guarantee"修复的两条核心契约：

1. 纯函数层 ``_classify_incomplete_answer`` / ``_final_answer_verdict``
   对真实泄漏案例返回正确的 reason_code，对正常长答案不误判。
2. ``stream_session_chat`` 守门会抑制 ``stream_error`` chunk、当判定为
   "答案对用户不可用"时追加 rescue chunk，并把失败落账本。

回放的 4 条真实泄漏案例（修复前曾把这些原样泄漏给前端）：
- provider_error: provider 400/auth/invalid，``_inner()`` 只 yield 一条
  stream_error chunk，可见内容为空。
- single_pass_failed: SINGLE_PASS 直答路径异常，``_inner()`` 只 yield 一条
  stream_error chunk，可见内容为空。
- incomplete_answer: 模型说"让我尝试其他来源："后没有再发起 tool call，
  ReAct 把这条悬挂答案当成终态。
- partial_after_error: SSL 短可见片段（<= 120 chars）+ 末尾 stream_error，
  把残片视为"片段不可用"。

伴随的 false-positive guard 用例（正常答案绝不能被误判进 rescue）：
- 长答案（数百字）+ 末尾才出现一次延迟 stream_error；
- 普通的完整短句（句号结尾）；
- "是的。" 这种合法极短答案；
- 不带 stream_error 的完整长答案。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from learning_agent.ai import (
    AgentMode,
    ChatChunk,
    LearningSession,
)
from learning_agent.learning_agent.main import (
    LearningAgentSystem,
    _PARTIAL_AFTER_ERROR_MAX_LEN,
    _classify_incomplete_answer,
    _final_answer_verdict,
)
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    build_turn_profile,
)


# ──────────────────────────────────────────────────────────────────────────
# Section 1 — 纯函数：_classify_incomplete_answer
# ──────────────────────────────────────────────────────────────────────────


class TestClassifyIncompleteAnswer:
    """逐项锁住"短残句"判定的边界。"""

    def test_empty_string_is_empty_stream(self):
        assert _classify_incomplete_answer("") == "empty_stream"

    def test_whitespace_only_is_empty_stream(self):
        # 不能因为前端拿到换行 / 空格就判为可用；都视为空。
        assert _classify_incomplete_answer("   \n\t  ") == "empty_stream"

    def test_dangling_chinese_colon_is_incomplete(self):
        # 真实事故：sess-8f458ab2 turn 8 — "让我尝试其他来源："
        assert (
            _classify_incomplete_answer("让我尝试其他来源：") == "incomplete_answer"
        )

    def test_dangling_ascii_colon_is_incomplete(self):
        assert _classify_incomplete_answer("Let me try other sources:") == "incomplete_answer"

    def test_dangling_chinese_comma_is_incomplete(self):
        assert _classify_incomplete_answer("先看一下，") == "incomplete_answer"

    def test_dangling_ellipsis_is_incomplete(self):
        assert _classify_incomplete_answer("稍等…") == "incomplete_answer"

    def test_dangling_em_dash_is_incomplete(self):
        assert _classify_incomplete_answer("结论是—") == "incomplete_answer"

    def test_short_complete_with_period_is_usable(self):
        # 短答案 + 完整收尾标点：合法。"是的。" 是真实可能场景。
        assert _classify_incomplete_answer("是的。") is None
        assert _classify_incomplete_answer("Done.") is None

    def test_short_question_mark_is_usable(self):
        # 反问也是合法答案。
        assert _classify_incomplete_answer("是吗？") is None

    def test_long_answer_with_trailing_colon_is_not_misjudged(self):
        # 长答案极少以悬挂标点收尾；即使收尾也不该被判残：
        # 上限 _INCOMPLETE_MAX_LEN (48) 之外不触发。
        long_text = "在 Python 3.12 里，类型参数有了新语法。完整说明见文档示例如下：" * 3
        assert len(long_text) > 48
        assert _classify_incomplete_answer(long_text) is None

    def test_long_normal_answer_is_usable(self):
        long_text = "Python 3.12 引入了 PEP 695 的新泛型语法和 PEP 698 的 override 装饰器。"
        assert _classify_incomplete_answer(long_text) is None


# ──────────────────────────────────────────────────────────────────────────
# Section 2 — 纯函数：_final_answer_verdict（含 4 条泄漏案例 + FP 守卫）
# ──────────────────────────────────────────────────────────────────────────


class TestFinalAnswerVerdictLeakCases:
    """4 条真实泄漏案例，必须返回非 None reason_code 触发 rescue。"""

    def test_provider_error_empty_stream(self):
        # 案例 1：provider 400/auth/invalid。stream_error chunk 已被守门抑制，
        # visible_text="", stream_error_reason="provider_error"。
        reason = _final_answer_verdict(
            visible_text="",
            stream_error_reason="provider_error",
            inner_reason=None,
        )
        assert reason == "provider_error"

    def test_single_pass_failed_empty_stream(self):
        # 案例 2：SINGLE_PASS 直答路径异常，整轮空。
        reason = _final_answer_verdict(
            visible_text="",
            stream_error_reason="single_pass_failed",
            inner_reason=None,
        )
        assert reason == "single_pass_failed"

    def test_dangling_preamble_no_stream_error(self):
        # 案例 3："让我尝试其他来源：" — 模型说要再尝试但没发起 tool call。
        # 流本身没有 stream_error；纯靠"残句"判定拦下。
        reason = _final_answer_verdict(
            visible_text="让我尝试其他来源：",
            stream_error_reason=None,
            inner_reason=None,
        )
        assert reason == "incomplete_answer"

    def test_ssl_partial_after_error(self):
        # 案例 4：SSL 错误把流打断，可见内容是 88 字的开头片段。
        # 在 _PARTIAL_AFTER_ERROR_MAX_LEN (120) 内 + 有 stream_error → rescue。
        partial = "Python 3.12 引入了 PEP 695 的新泛型语法，主要变化包括：" + "简化" * 5
        assert 0 < len(partial) <= _PARTIAL_AFTER_ERROR_MAX_LEN
        reason = _final_answer_verdict(
            visible_text=partial,
            stream_error_reason="provider_error",
            inner_reason=None,
        )
        assert reason == "provider_error"


class TestFinalAnswerVerdictFalsePositiveGuards:
    """正常答案绝不能被误判进 rescue（这些是修复时手动校准过的反例）。"""

    def test_long_complete_answer_with_late_stream_error_is_usable(self):
        # 完整长答案 + 末尾才出现一次延迟 stream_error → 用户已经收到完整答案，
        # 不该再叠一段 rescue 文本（_PARTIAL_AFTER_ERROR_MAX_LEN 上限的存在原因）。
        long_text = (
            "Python 3.12 的新特性包括 PEP 695 的新泛型语法、PEP 698 的 override 装饰器、"
            "f-string 解析器统一进 CPython 编译器、CPython 解释器子解释器隔离 GIL 改进、"
            "asyncio 性能提升等。这些都已经在官方 What's New 文档里列出。"
        )
        assert len(long_text) > _PARTIAL_AFTER_ERROR_MAX_LEN
        reason = _final_answer_verdict(
            visible_text=long_text,
            stream_error_reason="provider_error",
            inner_reason=None,
        )
        assert reason is None

    def test_normal_complete_answer_no_error_is_usable(self):
        reason = _final_answer_verdict(
            visible_text="这是一段完整、自洽的回答。",
            stream_error_reason=None,
            inner_reason=None,
        )
        assert reason is None

    def test_very_short_period_answer_is_usable(self):
        # "是的。" — 极短但是合法完整答案。
        reason = _final_answer_verdict(
            visible_text="是的。",
            stream_error_reason=None,
            inner_reason=None,
        )
        assert reason is None

    def test_inner_exception_with_empty_visible_uses_inner_reason(self):
        # _inner() 自己捕获了异常但没产 stream_error chunk（例如 prepare 阶段）；
        # 整轮可见为空，verdict 用 inner_reason。
        reason = _final_answer_verdict(
            visible_text="",
            stream_error_reason=None,
            inner_reason="inner_exception",
        )
        assert reason == "inner_exception"

    def test_empty_with_no_reason_falls_back_to_empty_stream(self):
        # 没有任何上游线索：仍然 rescue，但 reason_code 退化为 empty_stream。
        reason = _final_answer_verdict(
            visible_text="",
            stream_error_reason=None,
            inner_reason=None,
        )
        assert reason == "empty_stream"


# ──────────────────────────────────────────────────────────────────────────
# Section 3 — stream_session_chat 守门集成测试
# ──────────────────────────────────────────────────────────────────────────


async def _drain(agen) -> list[ChatChunk]:
    out: list[ChatChunk] = []
    async for chunk in agen:
        out.append(chunk)
    return out


def _make_session() -> LearningSession:
    return LearningSession(
        id="sess-final-guard",
        mode=AgentMode.CHAT,
        learning_unit_id=None,
    )


def _stub_stream_system(
    *,
    inner_chunks: list[ChatChunk],
    rescue_text: str = "RESCUE_FALLBACK",
) -> tuple[LearningAgentSystem, AsyncMock]:
    """构造一个能驱动 stream_session_chat 的最小 LearningAgentSystem stub。

    `inner_chunks` 是 ``agent_loop.run`` 要 yield 的 chunks（顺序、metadata 完全照搬）。
    返回 (system, logger_mock)，logger_mock 用于断言失败账本写入。
    """
    session = _make_session()
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.agent_loop = MagicMock()

    async def fake_agent_loop_run(*args, **kwargs):
        for ch in inner_chunks:
            yield ch

    system.agent_loop.run = fake_agent_loop_run

    system.get_session = MagicMock(return_value=session)

    # learning_unit 路径短路：返回 None 即跳过 teach flow
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    system.learning_unit_store = learning_unit_store

    profile = build_turn_profile(AgentMode.CHAT)
    prepared = PreparedSessionTurn(
        effective_mode=AgentMode.CHAT,
        runtime_input="user_input",
        profile=profile,
        stream_metadata={"mode": AgentMode.CHAT.value},
        compaction_plan=None,
    )
    system._prepare_session_turn = AsyncMock(return_value=(session, prepared))
    system._maybe_emit_first_value = MagicMock()
    system._maybe_fire_concept_extraction = MagicMock()

    # rescue answer：直接返回静态文本，避免依赖 provider
    system._build_rescue_answer = AsyncMock(return_value=(rescue_text, "rescue_llm"))

    logger_mock = MagicMock()
    logger_mock.record = AsyncMock()
    system.unresolved_failure_logger = logger_mock

    return system, logger_mock


class TestStreamSessionChatGuard:
    """4 条泄漏案例在 stream_session_chat 守门处必须被拦住。"""

    @pytest.mark.asyncio
    async def test_provider_error_empty_triggers_rescue(self):
        # 修复前：用户拿到 "[Error] LLM stream failed: 400 ..."
        # 修复后：stream_error chunk 被抑制，rescue 文本顶上。
        chunks = [
            ChatChunk(
                content="\n[Error] 400 invalid request\n",
                metadata={
                    "stream_error": True,
                    "stream_error_reason": "provider_error",
                },
            ),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "请搜索 Python 3.12", AgentMode.CHAT)
        )

        # 用户能看到的内容只有 rescue，绝不能含原始 [Error]
        assert len(out) == 1
        assert "[Error]" not in out[0].content
        assert "400" not in out[0].content
        assert out[0].content == "RESCUE_FALLBACK"
        assert out[0].metadata["rescue"] is True
        assert out[0].metadata["rescue_reason"] == "provider_error"

        # 账本记录了这次失败 + rescue_used=True
        logger.record.assert_awaited_once()
        kwargs = logger.record.await_args.kwargs
        assert kwargs["layer"] == "stream_session_chat"
        assert kwargs["reason_code"] == "provider_error"
        assert kwargs["rescue_used"] is True

    @pytest.mark.asyncio
    async def test_single_pass_failed_empty_triggers_rescue(self):
        chunks = [
            ChatChunk(
                content="\n[Error] Unable to continue: timeout\n",
                metadata={
                    "stream_error": True,
                    "stream_error_reason": "single_pass_failed",
                },
            ),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "u", AgentMode.CHAT)
        )

        assert len(out) == 1
        assert "[Error]" not in out[0].content
        assert "Unable to continue" not in out[0].content
        assert out[0].metadata["rescue_reason"] == "single_pass_failed"
        logger.record.assert_awaited_once()
        assert logger.record.await_args.kwargs["reason_code"] == "single_pass_failed"

    @pytest.mark.asyncio
    async def test_dangling_preamble_triggers_rescue(self):
        # 真实事故：模型只输出 "让我尝试其他来源：" 就以 finish_reason=stop 结束。
        chunks = [
            ChatChunk(content="让我尝试其他来源：", metadata={}),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "搜一下", AgentMode.CHAT)
        )

        # 残句本身被先 yield 出去（模型实际产出），但 rescue chunk 紧随其后兜底。
        # 前端会把后续 chunk 拼接显示，至少最后用户能拿到可用答案。
        contents = [c.content for c in out]
        rescue_chunks = [c for c in out if c.metadata.get("rescue")]
        assert "让我尝试其他来源：" in contents
        assert len(rescue_chunks) == 1
        assert rescue_chunks[0].metadata["rescue_reason"] == "incomplete_answer"

        logger.record.assert_awaited_once()
        assert logger.record.await_args.kwargs["reason_code"] == "incomplete_answer"
        assert logger.record.await_args.kwargs["rescue_used"] is True

    @pytest.mark.asyncio
    async def test_ssl_partial_then_error_triggers_rescue(self):
        # 短可见片段 + 流错误：rescue 拦下，原始 [Error] 不外露。
        # 注意：partial 末尾不能是悬挂标点，否则会被先判定为 incomplete_answer，
        # 我们要锁的是"短片段 + 系统级流错误"这条独立分支。
        partial = "Python 3.12 引入了 PEP 695 新泛型语法"
        chunks = [
            ChatChunk(content=partial, metadata={}),
            ChatChunk(
                content="\n[Error] SSL connection closed\n",
                metadata={
                    "stream_error": True,
                    "stream_error_reason": "provider_error",
                },
            ),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "u", AgentMode.CHAT)
        )

        contents = [c.content for c in out]
        # partial 已经流给用户（在 stream_error 之前），但 stream_error chunk 被抑制
        assert partial in contents
        for c in contents:
            assert "[Error]" not in c
            assert "SSL connection closed" not in c

        rescue_chunks = [c for c in out if c.metadata.get("rescue")]
        assert len(rescue_chunks) == 1
        assert rescue_chunks[0].metadata["rescue_reason"] == "provider_error"
        logger.record.assert_awaited_once()


class TestStreamSessionChatNoFalsePositive:
    """正常答案不能触发 rescue。这是修复"过度兜底"的反向锁。"""

    @pytest.mark.asyncio
    async def test_long_answer_with_late_stream_error_no_rescue(self):
        # 答案完整 + 末尾才打一次 stream_error → 不要叠 rescue。
        # 但本轮确实出过错，应该用 rescue_used=False 落账本（方便后续优化）。
        long_text = (
            "Python 3.12 引入了 PEP 695 的新泛型语法、PEP 698 的 override 装饰器、"
            "更快的 CPython 解释器、子解释器隔离 GIL 改进，以及 asyncio 的多项性能优化。"
            "另外标准库的 typing / pathlib 也获得了一系列易用性改善。"
        )
        assert len(long_text) > _PARTIAL_AFTER_ERROR_MAX_LEN
        chunks = [
            ChatChunk(content=long_text, metadata={}),
            ChatChunk(
                content="\n[Error] late stream error\n",
                metadata={
                    "stream_error": True,
                    "stream_error_reason": "provider_error",
                },
            ),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "u", AgentMode.CHAT)
        )

        rescue_chunks = [c for c in out if c.metadata.get("rescue")]
        assert rescue_chunks == [], "完整长答案不应触发 rescue"

        # stream_error 仍然被抑制，但要落非 rescue 账本
        contents = [c.content for c in out]
        for c in contents:
            assert "[Error]" not in c
            assert "late stream error" not in c

        logger.record.assert_awaited_once()
        kwargs = logger.record.await_args.kwargs
        assert kwargs["rescue_used"] is False
        assert kwargs["reason_code"] == "provider_error"

    @pytest.mark.asyncio
    async def test_normal_complete_answer_no_rescue_no_log(self):
        chunks = [
            ChatChunk(content="这是一段完整、自洽的回答。", metadata={}),
        ]
        system, logger = _stub_stream_system(inner_chunks=chunks)

        out = await _drain(
            system.stream_session_chat("sess-final-guard", "u", AgentMode.CHAT)
        )

        rescue_chunks = [c for c in out if c.metadata.get("rescue")]
        assert rescue_chunks == []
        # 没有 stream_error 也没有 rescue → 账本完全不写
        logger.record.assert_not_awaited()
