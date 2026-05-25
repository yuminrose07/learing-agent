"""TeachJudge 单元测试（B5 E3）。

覆盖：
- happy path passed / needs_review → 返回正确 verdict + reason
- 未知 verdict → 强制降级为 needs_review
- reason 缺失 → 按 verdict 兜底文案
- reason 超长 → 截断
- 空作答 → 直接 needs_review，不调 provider
- provider 抛错 → needs_review + 错误兜底文案
- 解析失败 → needs_review
- markdown 围栏容忍
- model_name 透传 / fallback default_model / 都没有时降级
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.learning_unit import TeachQuestion
from learning_agent.ai.models import ChatChunk, ChatParams
from learning_agent.learning_agent.teach_judge import TeachJudge


def _make_provider(
    raw_response: str,
    *,
    default_model: str | None = "kimi-2.6",
) -> MagicMock:
    provider = MagicMock()
    provider.default_model = default_model
    provider.chat = AsyncMock(return_value=ChatChunk(content=raw_response))
    return provider


def _make_question(
    *,
    stem: str = "解释 attention 的作用",
    rubric: str = "必须提到 query/key/value 与权重分配",
) -> TeachQuestion:
    return TeachQuestion(
        concept_id="cpt-000",
        kind="sa",
        stem=stem,
        rubric=rubric,
    )


class TestTeachJudgeParsing:
    @pytest.mark.asyncio
    async def test_passed_verdict_round_trips(self):
        provider = _make_provider(
            '{"verdict": "passed", "reason": "命中了 query/key/value"}'
        )
        judge = TeachJudge(provider)

        verdict, reason = await judge.judge(
            question=_make_question(),
            user_answer="attention 把 query 投到 key 上算权重然后加权 value",
        )
        assert verdict == "passed"
        assert "query/key/value" in reason

    @pytest.mark.asyncio
    async def test_needs_review_verdict_round_trips(self):
        provider = _make_provider(
            '{"verdict": "needs_review", "reason": "没提到 key"}'
        )
        judge = TeachJudge(provider)

        verdict, reason = await judge.judge(
            question=_make_question(),
            user_answer="attention 是一种权重",
        )
        assert verdict == "needs_review"
        assert "key" in reason

    @pytest.mark.asyncio
    async def test_unknown_verdict_is_coerced_to_needs_review(self):
        provider = _make_provider(
            '{"verdict": "kinda", "reason": "差不多吧"}'
        )
        judge = TeachJudge(provider)

        verdict, _ = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "needs_review"

    @pytest.mark.asyncio
    async def test_missing_reason_falls_back_to_default(self):
        provider = _make_provider('{"verdict": "passed"}')
        judge = TeachJudge(provider)

        verdict, reason = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "passed"
        assert reason  # 非空兜底

    @pytest.mark.asyncio
    async def test_overlong_reason_is_truncated(self):
        long_reason = "啊" * 500
        provider = _make_provider(
            '{"verdict": "passed", "reason": "' + long_reason + '"}'
        )
        judge = TeachJudge(provider)

        _, reason = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert len(reason) <= 241  # 240 + 省略号
        assert reason.endswith("…")

    @pytest.mark.asyncio
    async def test_markdown_fence_is_tolerated(self):
        provider = _make_provider(
            "好的，结果是：\n"
            "```json\n"
            '{"verdict": "passed", "reason": "ok"}\n'
            "```"
        )
        judge = TeachJudge(provider)

        verdict, _ = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "passed"

    @pytest.mark.asyncio
    async def test_non_object_root_returns_needs_review(self):
        provider = _make_provider('["passed"]')
        judge = TeachJudge(provider)

        verdict, _ = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "needs_review"

    @pytest.mark.asyncio
    async def test_garbage_response_returns_needs_review(self):
        provider = _make_provider("我不知道")
        judge = TeachJudge(provider)

        verdict, _ = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "needs_review"


class TestTeachJudgeShortCircuit:
    @pytest.mark.asyncio
    async def test_empty_answer_short_circuits_to_needs_review(self):
        provider = _make_provider('{"verdict": "passed"}')
        judge = TeachJudge(provider)

        verdict, reason = await judge.judge(
            question=_make_question(), user_answer="   "
        )
        assert verdict == "needs_review"
        assert "空" in reason
        provider.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_exception_falls_back_to_needs_review(self):
        provider = MagicMock()
        provider.default_model = "kimi-2.6"
        provider.chat = AsyncMock(side_effect=RuntimeError("boom"))
        judge = TeachJudge(provider)

        verdict, reason = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "needs_review"
        assert "失败" in reason or "暂不可用" in reason


class TestTeachJudgeModelRouting:
    @pytest.mark.asyncio
    async def test_explicit_model_name_is_passed_to_provider(self):
        provider = _make_provider('{"verdict": "passed"}')
        judge = TeachJudge(provider, model_name="claude-haiku-4-5-20251001")

        await judge.judge(question=_make_question(), user_answer="x")

        (params,), _ = provider.chat.call_args
        assert isinstance(params, ChatParams)
        assert params.model == "claude-haiku-4-5-20251001"
        assert params.stream is False
        assert params.temperature == 0.0

    @pytest.mark.asyncio
    async def test_model_name_none_falls_back_to_provider_default(self):
        provider = _make_provider('{"verdict": "passed"}', default_model="kimi-2.6")
        judge = TeachJudge(provider, model_name=None)

        await judge.judge(question=_make_question(), user_answer="x")
        (params,), _ = provider.chat.call_args
        assert params.model == "kimi-2.6"

    @pytest.mark.asyncio
    async def test_no_model_resolved_falls_back_to_needs_review(self):
        provider = _make_provider("[]", default_model=None)
        judge = TeachJudge(provider, model_name=None)

        verdict, reason = await judge.judge(
            question=_make_question(), user_answer="x"
        )
        assert verdict == "needs_review"
        assert reason
        provider.chat.assert_not_awaited()
