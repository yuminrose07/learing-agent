"""闲聊 / 研学 分离 —— CHAT 侧的隔离锁（接口契约 §7）。

研学方已在 ``test_mode_layering.py`` 锁住了"路由"那一半：
- 无学习卷会话 → ``effective_mode == CHAT``；
- 学习卷 absorbing → ``effective_mode == STUDY``。

本文件锁住互补的另一半 —— **闲聊侧的隔离保证**：任何学习卷路径都不得
取用 ``CHAT_PROFILE`` / ``CHAT_MODE_PROMPT``。这正是契约 §2 的硬指标
（"CHAT 只服务闲聊"）在测试层的体现，也是闲聊方将来能放心调 ``CHAT_PROFILE``
/ ``CHAT_MODE_PROMPT`` 而不泄漏到研学 absorbing 的前提。

为什么单独成文件：``test_mode_layering`` 验的是"这一轮该走哪个 mode"，
本文件验的是"两张 profile 的指纹互不重叠 + CHAT 的提示词文本绝不出现在
absorbing 轮里"。前者会随 alignment 策略演进，后者是分离的不变式，放一起会
混淆关注点。
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import (
    AgentMode,
    ConceptItem,
    LearningSession,
    LearningUnit,
    UnitObjective,
)
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import (
    CHAT_MODE_PROMPT,
    STUDY_MODE_PROMPT,
    build_mode_prompt,
    build_turn_profile,
)


# ──────────────────────────────────────────────────────────────────────────
# 复用 test_mode_layering 的最小 stub 构造（保持独立，不跨文件 import 私有 helper）
# ──────────────────────────────────────────────────────────────────────────


def _build_system_stub() -> LearningAgentSystem:
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.agent_loop = MagicMock()
    system.save_session = MagicMock(return_value=True)
    system.get_session = MagicMock()
    learning_unit_store = MagicMock()
    learning_unit_store.get = MagicMock(return_value=None)
    system.learning_unit_store = learning_unit_store
    # alignment_classifier 默认返回 mode=none（clear_enough），让 absorbing 路径
    # 直接拿到 STUDY 指纹，不触发对齐 popup。
    from learning_agent.learning_agent.alignment_policy import AlignmentDecision

    classifier = MagicMock()
    classifier.classify = AsyncMock(
        return_value=AlignmentDecision(mode="none", reason="clear_enough")
    )
    system.alignment_classifier = classifier
    return system


def _make_absorbing_unit() -> LearningUnit:
    unit = LearningUnit(
        session_id="sess-sep",
        objective=UnitObjective(text="理解 Pydantic v2 的 BaseModel"),
        concept_list=[
            ConceptItem(name="BaseModel", summary="数据模型基类", relevance=0.95),
        ],
    )
    unit.phase = "absorbing"  # type: ignore[assignment]
    return unit


def _session_with_unit(unit: LearningUnit) -> LearningSession:
    return LearningSession(
        id=unit.session_id,
        mode=AgentMode.CHAT,
        learning_unit_id=unit.id,
    )


# ──────────────────────────────────────────────────────────────────────────
# Section 1 — 两张 profile 的指纹互不重叠（纯函数，不碰路由）
# ──────────────────────────────────────────────────────────────────────────


class TestProfileFingerprintsAreDisjoint:
    """CHAT 与 STUDY 的可观测指纹必须不同，否则"分离"只是名义上的。"""

    def test_chat_profile_is_lightweight_no_memory(self):
        profile = build_turn_profile(AgentMode.CHAT)

        assert profile.mode == AgentMode.CHAT
        assert profile.memory_read is False
        assert profile.memory_write is False
        assert profile.response_style == "direct"
        assert profile.recent_token_budget == 16000

    def test_study_profile_is_heavy_with_memory(self):
        profile = build_turn_profile(AgentMode.STUDY)

        assert profile.mode == AgentMode.STUDY
        assert profile.memory_read is True
        assert profile.memory_write is True
        assert profile.response_style == "tutor"
        assert profile.recent_token_budget == 24000

    def test_chat_and_study_fingerprints_do_not_collide(self):
        chat = build_turn_profile(AgentMode.CHAT)
        study = build_turn_profile(AgentMode.STUDY)

        # 至少在 memory / 风格 / 预算三个维度上必须可区分。
        assert (chat.memory_read, chat.memory_write) != (study.memory_read, study.memory_write)
        assert chat.response_style != study.response_style
        assert chat.recent_token_budget != study.recent_token_budget

    def test_mode_prompts_are_distinct_text(self):
        # build_mode_prompt 必须把两个模式映射到不同的提示词常量，
        # 且两段文本互不为子串 —— 这样"CHAT_MODE_PROMPT not in study_prompt"
        # 这类断言才有意义。
        assert build_mode_prompt(AgentMode.CHAT) == CHAT_MODE_PROMPT
        assert build_mode_prompt(AgentMode.STUDY) == STUDY_MODE_PROMPT
        assert CHAT_MODE_PROMPT != STUDY_MODE_PROMPT
        assert CHAT_MODE_PROMPT not in STUDY_MODE_PROMPT
        assert STUDY_MODE_PROMPT not in CHAT_MODE_PROMPT


# ──────────────────────────────────────────────────────────────────────────
# Section 2 — 学习卷路径绝不取用 CHAT（端到端走 _prepare_session_turn）
# ──────────────────────────────────────────────────────────────────────────


class TestLearningUnitPathNeverUsesChat:
    """absorbing 轮必须拿到 STUDY 指纹，且 system_prompt 不含 CHAT 文本。"""

    @pytest.mark.asyncio
    async def test_absorbing_turn_carries_study_not_chat_fingerprint(self):
        system = _build_system_stub()
        unit = _make_absorbing_unit()
        system.get_learning_unit = lambda uid: unit if uid == unit.id else None
        session = _session_with_unit(unit)

        # 清晰输入命中 concept_list（BaseModel）→ A 档 → STUDY
        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.STUDY
        # 拿到的是 STUDY 指纹，不是 CHAT 的
        assert turn.profile.memory_read is True
        assert turn.profile.response_style == "tutor"
        assert turn.profile.recent_token_budget == 24000

    @pytest.mark.asyncio
    async def test_absorbing_prompt_excludes_chat_mode_prompt(self):
        """闲聊方将来改 CHAT_MODE_PROMPT 不会泄漏到 absorbing —— 这是分离的核心保证。"""
        system = _build_system_stub()
        unit = _make_absorbing_unit()
        system.get_learning_unit = lambda uid: unit if uid == unit.id else None
        session = _session_with_unit(unit)

        _, turn = await system._prepare_session_turn(
            session, "讲讲 BaseModel", AgentMode.CHAT
        )

        prompt = turn.profile.system_prompt
        # absorbing 轮带的是 STUDY 提示词（可能再叠开场模板），绝不含 CHAT 文本
        assert CHAT_MODE_PROMPT not in prompt
        assert STUDY_MODE_PROMPT in prompt

    @pytest.mark.asyncio
    async def test_plain_chat_turn_carries_chat_not_study(self):
        """互补方向：无学习卷会话拿到 CHAT 指纹，且 prompt 不含 STUDY 文本。"""
        system = _build_system_stub()
        session = LearningSession(id="sess-plain", mode=AgentMode.CHAT)

        _, turn = await system._prepare_session_turn(
            session, "随便聊聊", AgentMode.CHAT
        )

        assert turn.effective_mode == AgentMode.CHAT
        assert turn.profile.memory_read is False
        assert turn.profile.response_style == "direct"
        assert turn.profile.recent_token_budget == 16000

        prompt = turn.profile.system_prompt
        assert CHAT_MODE_PROMPT in prompt
        assert STUDY_MODE_PROMPT not in prompt
