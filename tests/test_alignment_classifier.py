"""``AlignmentClassifier`` 单元测试。

覆盖：
- 档位映射：单候选→none / 多候选 low→suggested / 多候选 high→active
- ``first_step`` 实质相同的候选必须合并（按 case-insensitive 去重）
- 候选数量上限：超过 ``max_candidates`` 后截断
- 配置透传：``model_name`` 经 ``ChatParams`` 透传给 provider；None 时回落
  ``provider.default_model``；连默认也没有 → fail-open
- 失败容忍：provider 抛错 / 非 JSON / 非 dict 根 → mode=none 且不抛
- markdown 围栏容忍：能从 ```json 块里挖出对象
- 复用 ``_apply_rate_limits``：suggested 在 cap / cooldown 下被静默降级
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.learning_unit import LearningUnit, UnitObjective
from learning_agent.ai.models import ChatChunk, ChatParams
from learning_agent.learning_agent.alignment_classifier import AlignmentClassifier


def _unit(text: str = "理解 compaction") -> LearningUnit:
    return LearningUnit(
        session_id="sess-x",
        objective=UnitObjective(text=text),
    )


def _make_provider(
    raw_response: str,
    *,
    default_model: str | None = "qwen-turbo",
) -> MagicMock:
    provider = MagicMock()
    provider.default_model = default_model
    provider.chat = AsyncMock(return_value=ChatChunk(content=raw_response))
    return provider


# ---------------------------------------------------------------------------
# 档位映射
# ---------------------------------------------------------------------------


class TestTierMapping:
    @pytest.mark.asyncio
    async def test_single_candidate_maps_to_none(self):
        provider = _make_provider(
            '{"candidates":[{"objective":"学 X","first_step":"从 x1 开始"}],'
            '"divergence_cost":"high","reason":"clear_enough"}'
        )
        classifier = AlignmentClassifier(provider)

        decision = await classifier.classify(_unit(), "带我学 X")
        # 单候选无论 divergence_cost 是什么，都收口到 none，不打扰用户
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"
        assert len(decision.candidates) == 1

    @pytest.mark.asyncio
    async def test_zero_candidates_maps_to_none(self):
        provider = _make_provider(
            '{"candidates":[],"divergence_cost":"low","reason":"clear_enough"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "带我学")
        assert decision.mode == "none"
        assert decision.candidates == []

    @pytest.mark.asyncio
    async def test_multi_candidates_low_cost_maps_to_suggested(self):
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"从 a1 切入"},'
            '{"objective":"学 B","first_step":"从 b1 切入"}'
            '],"divergence_cost":"low","reason":"too_broad"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "整个项目")

        assert decision.mode == "suggested"
        assert decision.reason == "too_broad"
        assert decision.divergence_cost == "low"
        assert len(decision.candidates) == 2
        # suggested 必须给 suggested_objective + assumption_note 让 UI 渲染
        assert decision.suggested_objective == "学 A"
        assert decision.assumption_note

    @pytest.mark.asyncio
    async def test_multi_candidates_high_cost_maps_to_active(self):
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学产品侧 phase 机","first_step":"从 absorbing 切入"},'
            '{"objective":"学 AI 层 forge","first_step":"从 forge_stage 切入"}'
            '],"divergence_cost":"high","reason":"conflicting_scope"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "讲讲学习卷")

        assert decision.mode == "active"
        assert decision.reason == "conflicting_scope"
        assert decision.divergence_cost == "high"
        assert len(decision.candidates) == 2
        assert decision.assumption_note

    @pytest.mark.asyncio
    async def test_multi_candidates_no_divergence_falls_to_suggested(self):
        # divergence_cost 缺失或非法值 → 默认按 low 处理（保守，不弹 modal）
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"a1"},'
            '{"objective":"学 B","first_step":"b1"}'
            '],"reason":"too_broad"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "suggested"
        assert decision.divergence_cost == "low"


# ---------------------------------------------------------------------------
# 候选合并 / 截断
# ---------------------------------------------------------------------------


class TestCandidateDedup:
    @pytest.mark.asyncio
    async def test_first_step_dedup_case_insensitive(self):
        # 两个候选 first_step 实质相同（仅大小写差异）→ 合并
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"从 absorbing 切入"},'
            '{"objective":"学 A 又一说","first_step":"从 ABSORBING 切入"},'
            '{"objective":"学 B","first_step":"从 outputting 切入"}'
            '],"divergence_cost":"high","reason":"conflicting_scope"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        # 三选二（第二条因 first_step 撞第一条被丢弃）
        assert len(decision.candidates) == 2
        assert decision.candidates[0].objective == "学 A"
        assert decision.candidates[1].objective == "学 B"

    @pytest.mark.asyncio
    async def test_candidates_truncated_at_max(self):
        # 模型给了 5 个，分类器封顶 3 个
        items = ",".join(
            f'{{"objective":"学 {i}","first_step":"切口 {i}"}}'
            for i in range(5)
        )
        provider = _make_provider(
            f'{{"candidates":[{items}],"divergence_cost":"high","reason":"conflicting_scope"}}'
        )
        classifier = AlignmentClassifier(provider, max_candidates=3)
        decision = await classifier.classify(_unit(), "x")
        assert len(decision.candidates) == 3

    @pytest.mark.asyncio
    async def test_missing_fields_skipped(self):
        # objective 或 first_step 为空的条目直接丢弃
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"","first_step":"x"},'
            '{"objective":"学 A","first_step":""},'
            '{"objective":"学 B","first_step":"切口 b"}'
            '],"divergence_cost":"low","reason":"too_broad"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert len(decision.candidates) == 1
        # 单候选 → 降到 none
        assert decision.mode == "none"


# ---------------------------------------------------------------------------
# 配置透传
# ---------------------------------------------------------------------------


class TestModelRouting:
    @pytest.mark.asyncio
    async def test_explicit_model_name_passed_to_provider(self):
        provider = _make_provider(
            '{"candidates":[{"objective":"x","first_step":"y"}],'
            '"divergence_cost":"low","reason":"clear_enough"}'
        )
        classifier = AlignmentClassifier(provider, model_name="qwen-turbo")
        await classifier.classify(_unit(), "x")

        provider.chat.assert_awaited_once()
        (params,), _ = provider.chat.call_args
        assert isinstance(params, ChatParams)
        assert params.model == "qwen-turbo"
        assert params.stream is False

    @pytest.mark.asyncio
    async def test_model_name_none_falls_back_to_default(self):
        provider = _make_provider(
            '{"candidates":[],"divergence_cost":"low","reason":"clear_enough"}',
            default_model="qwen-plus",
        )
        classifier = AlignmentClassifier(provider, model_name=None)
        await classifier.classify(_unit(), "x")

        (params,), _ = provider.chat.call_args
        assert params.model == "qwen-plus"

    @pytest.mark.asyncio
    async def test_no_model_resolved_short_circuits_to_none(self):
        provider = _make_provider("{}", default_model=None)
        classifier = AlignmentClassifier(provider, model_name=None)

        decision = await classifier.classify(_unit(), "x")
        # 没模型可用 → fail-open，不打扰
        assert decision.mode == "none"
        provider.chat.assert_not_awaited()


# ---------------------------------------------------------------------------
# 失败容忍
# ---------------------------------------------------------------------------


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_provider_exception_returns_none(self):
        provider = MagicMock()
        provider.default_model = "qwen-turbo"
        provider.chat = AsyncMock(side_effect=RuntimeError("provider 挂了"))
        classifier = AlignmentClassifier(provider)

        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "none"
        assert decision.reason == "clear_enough"

    @pytest.mark.asyncio
    async def test_non_json_response_returns_none(self):
        provider = _make_provider("这不是 JSON，模型摆烂了")
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "none"
        assert decision.candidates == []

    @pytest.mark.asyncio
    async def test_non_dict_root_returns_none(self):
        # 模型回了 JSON 数组 / 字符串 / 数字 → 不是合法 schema → fail-open
        provider = _make_provider('["not", "an", "object"]')
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "none"

    @pytest.mark.asyncio
    async def test_markdown_fence_is_tolerated(self):
        provider = _make_provider(
            "好的，结果是：\n"
            "```json\n"
            '{"candidates":['
            '{"objective":"学 A","first_step":"a1"},'
            '{"objective":"学 B","first_step":"b1"}'
            '],"divergence_cost":"high","reason":"conflicting_scope"}\n'
            "```"
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "active"
        assert len(decision.candidates) == 2

    @pytest.mark.asyncio
    async def test_invalid_reason_falls_back_to_clear_enough(self):
        # reason 用了枚举外的值 → 替换为 clear_enough（但因为多候选，
        # 在 active/suggested 分支会被 _build_decision 改写为更具体的原因）
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"a1"},'
            '{"objective":"学 B","first_step":"b1"}'
            '],"divergence_cost":"high","reason":"made_up_reason"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "x")
        assert decision.mode == "active"
        # active 分支：reason==clear_enough 时 _build_decision 兜底为
        # missing_learnable_target
        assert decision.reason == "missing_learnable_target"


# ---------------------------------------------------------------------------
# 与 _apply_rate_limits 的集成
# ---------------------------------------------------------------------------


class TestRateLimitIntegration:
    @pytest.mark.asyncio
    async def test_suggested_downgraded_when_cap_hit(self):
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"a1"},'
            '{"objective":"学 B","first_step":"b1"}'
            '],"divergence_cost":"low","reason":"too_broad"}'
        )
        classifier = AlignmentClassifier(provider)

        unit = _unit()
        unit.suggestion_count = 2  # 已经弹过 2 条建议，到 cap
        decision = await classifier.classify(unit, "x")
        # 走过 _apply_rate_limits 后，suggested 被静默降级为 none
        assert decision.mode == "none"

    @pytest.mark.asyncio
    async def test_active_unaffected_by_suggestion_cap(self):
        provider = _make_provider(
            '{"candidates":['
            '{"objective":"学 A","first_step":"a1"},'
            '{"objective":"学 B","first_step":"b1"}'
            '],"divergence_cost":"high","reason":"conflicting_scope"}'
        )
        classifier = AlignmentClassifier(provider)

        unit = _unit()
        unit.suggestion_count = 99
        unit.nag_cooldown_remaining = 99
        decision = await classifier.classify(unit, "x")
        # active 不受 suggestion cap 影响
        assert decision.mode == "active"


# ---------------------------------------------------------------------------
# Prompt 构造（黑盒：观察 provider.chat 收到的 prompt）
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    @pytest.mark.asyncio
    async def test_recent_messages_included_in_prompt(self):
        provider = _make_provider(
            '{"candidates":[],"divergence_cost":"low","reason":"clear_enough"}'
        )
        classifier = AlignmentClassifier(provider)
        recent = [
            {"role": "user", "content": "讲讲 attention"},
            {"role": "assistant", "content": "attention 是..."},
        ]
        await classifier.classify(_unit(), "继续学", recent_messages=recent)

        (params,), _ = provider.chat.call_args
        prompt = params.messages[0].content
        # 上下文必须出现在 prompt 里，让 LLM 看到「继续学」的指代对象
        assert "讲讲 attention" in prompt
        assert "继续学" in prompt

    @pytest.mark.asyncio
    async def test_empty_user_input_still_calls_provider(self):
        # 空输入也走分类器（让 LLM 自己说「拼不出方向」），上层不做截胡
        provider = _make_provider(
            '{"candidates":[],"divergence_cost":"low",'
            '"reason":"missing_learnable_target"}'
        )
        classifier = AlignmentClassifier(provider)
        decision = await classifier.classify(_unit(), "   ")
        provider.chat.assert_awaited_once()
        assert decision.mode == "none"
