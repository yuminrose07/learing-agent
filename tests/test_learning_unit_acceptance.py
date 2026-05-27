"""V1–V3：学习卷生命周期端到端验收测试。

把 MVP §12.1 的"五条 P0 必过项"中可测的两条编译成事件序列断言：
- 用户走到一个明确的结束动作 → 卷必出现 ``TEACH_ENTERED`` 与 ``CONSOLIDATED``
  事件，且 ``teach_entry_rate`` 计算回写为 1.0（分子分母都为 1）。
- 反馈卡显示 mastered/gaps/next → ``record_reuse_feedback`` 能发出
  ``REUSE_FEEDBACK`` 事件并被 ``reuse_intent_rate`` 计入。

加上 V3 列出的失败信号反例：``reuse=no`` 走通后指标如实记录 0%，证明
分母被正确算入；这是 MVP "reuse_feedback.value=yes ≥ 70%" 阈值能被
监控到的前提。

刻意用真 ``LearningUnitStore`` + 真 ``SessionEventStore`` 串成：
- 验证 M1 写出的事件确实进得了磁盘（不只是 mock call 计数）
- 验证 M2 的 ``calculate_metrics_summary`` 从 JSONL 回读后计算正确

不依赖 LLM：``teach_generator=None`` 让 absorbing → outputting 走 skip
路径直接 consolidate，免去任何模型调用。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import (
    AgentMode,
    LearningSession,
    LearningUnit,
)
from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.learning_unit_metrics import (
    calculate_metrics_summary,
)
from learning_agent.learning_agent.learning_unit_store import LearningUnitStore
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    build_turn_profile,
)
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_events import (
    SessionEvent,
    SessionEventType,
)

from tests.observability_asserts import assert_event_present


class _SessionManagerStub:
    """SessionManager 替身：本测试只用 create_session + register_delete_callback。

    返回的 LearningSession 直接写到一个本地 dict，供 `create_learning_unit`
    回填 ``session.learning_unit_id``——它要求是 mutable LearningSession 对象。
    """

    def __init__(self):
        self._sessions: dict[str, LearningSession] = {}
        self._delete_callbacks: list = []

    def register_delete_callback(self, cb) -> None:
        self._delete_callbacks.append(cb)

    def create_session(self, *, title: str | None = None) -> LearningSession:
        session = LearningSession(
            mode=AgentMode.CHAT,
            metadata={"title": title or ""},
        )
        self._sessions[session.id] = session
        return session

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        for cb in list(self._delete_callbacks):
            cb(session_id)


@pytest.fixture
def real_system(tmp_path: Path) -> Iterator[LearningAgentSystem]:
    """组装一个用真 store、桩 session_manager、无 teach_generator 的最小系统。"""
    file_store = FileStore(base_dir=str(tmp_path))
    session_manager = _SessionManagerStub()
    learning_unit_store = LearningUnitStore(
        file_store, session_manager=session_manager
    )
    session_event_store = SessionEventStore(file_store)

    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.file_store = file_store
    system.session_manager = session_manager
    system.learning_unit_store = learning_unit_store
    system.session_event_store = session_event_store
    system.teach_generator = None  # 让 _start_teach_session 走 skip 路径
    system.teach_judge = None
    system.concept_extractor = None

    yield system


def _read_all_events(system: LearningAgentSystem) -> list[SessionEvent]:
    out: list[SessionEvent] = []
    for sid in system.file_store.list_sessions():
        out.extend(system.session_event_store.read_events(sid))
    return out


def _simulate_first_value(
    system: LearningAgentSystem,
    session: LearningSession,
    unit: LearningUnit,
) -> None:
    """模拟 absorbing 阶段一次成功的 CHAT 回答（不真走 SSE）。"""
    profile = build_turn_profile(AgentMode.CHAT)
    prepared = PreparedSessionTurn(
        effective_mode=AgentMode.CHAT,
        runtime_input="测试用户输入",
        profile=profile,
        stream_metadata={
            "learning_unit_id": unit.id,
            "learning_unit_phase": unit.phase,
        },
        compaction_plan=None,
    )
    system._maybe_emit_first_value(session, prepared, "学习卷第一段讲解内容。")


# ─── V1 P0 lifecycle ────────────────────────────────────────────────


class TestV1HappyPathLifecycle:
    """走完一卷：create → first_value → consolidated → reuse_feedback=yes。"""

    @pytest.mark.asyncio
    async def test_full_lifecycle_emits_expected_events(
        self, real_system: LearningAgentSystem
    ):
        session, unit = real_system.create_learning_unit(
            seed_text="理解 attention 机制",
        )
        _simulate_first_value(real_system, session, unit)
        await real_system.advance_learning_unit(unit.id, "outputting")
        real_system.record_reuse_feedback(unit.id, "yes")

        events = _read_all_events(real_system)
        # 必经事件全部就位
        assert_event_present(
            events,
            type=SessionEventType.LEARNING_UNIT_CREATED,
            payload_contains={"learning_unit_id": unit.id},
        )
        assert_event_present(
            events,
            type=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
            payload_contains={"learning_unit_id": unit.id},
        )
        assert_event_present(
            events,
            type=SessionEventType.LEARNING_UNIT_TEACH_ENTERED,
            payload_contains={"learning_unit_id": unit.id},
        )
        assert_event_present(
            events,
            type=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
            payload_contains={"learning_unit_id": unit.id},
        )
        assert_event_present(
            events,
            type=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
            payload_contains={"learning_unit_id": unit.id, "value": "yes"},
        )

    @pytest.mark.asyncio
    async def test_full_lifecycle_metrics_are_unit_ratios(
        self, real_system: LearningAgentSystem
    ):
        session, unit = real_system.create_learning_unit(
            seed_text="理解 attention 机制",
        )
        _simulate_first_value(real_system, session, unit)
        await real_system.advance_learning_unit(unit.id, "outputting")
        real_system.record_reuse_feedback(unit.id, "yes")

        summary = real_system.get_learning_unit_metrics(window_days=None)

        # 4 个 P0 指标都成立且都为 100%（一个卷，全跑通）
        assert summary.consolidation.numerator == 1
        assert summary.consolidation.denominator == 1
        assert summary.consolidation.ratio == 1.0

        assert summary.teach_entry.numerator == 1
        assert summary.teach_entry.denominator == 1
        assert summary.teach_entry.ratio == 1.0

        assert summary.reuse_intent.numerator == 1
        assert summary.reuse_intent.denominator == 1
        assert summary.reuse_intent.ratio == 1.0

        assert summary.ttfv.sample_size == 1
        assert summary.ttfv.p50_seconds is not None
        assert summary.ttfv.p50_seconds >= 0  # absorbing→first_value 间隔


# ─── V2 reuse=no 仍计分母（指标可监测的前提）────────────────────────


class TestV2ReuseNoStillCountsToDenominator:
    """复用反馈 ``value=no`` 时分母 +1 但分子不动；阈值监控才能起作用。"""

    @pytest.mark.asyncio
    async def test_reuse_no_records_zero_ratio(
        self, real_system: LearningAgentSystem
    ):
        _, unit = real_system.create_learning_unit(seed_text="x")
        await real_system.advance_learning_unit(unit.id, "outputting")
        real_system.record_reuse_feedback(unit.id, "no")

        summary = real_system.get_learning_unit_metrics(window_days=None)
        assert summary.reuse_intent.numerator == 0
        assert summary.reuse_intent.denominator == 1
        assert summary.reuse_intent.ratio == 0.0


# ─── V3 测试用例补丁：直接拿事件流喂 calculate_metrics_summary ─────


class TestV3OfflineBackfill:
    """``calculate_metrics_summary`` 接受外部事件流，便于离线 backfill / 回归。

    把磁盘读出的事件传给它，应与系统 in-process 聚合结果一致——这是
    M2 "纯函数 + 事件流入参"设计意图能落地的体现。
    """

    @pytest.mark.asyncio
    async def test_disk_read_back_matches_in_process_summary(
        self, real_system: LearningAgentSystem
    ):
        session, unit = real_system.create_learning_unit(seed_text="x")
        _simulate_first_value(real_system, session, unit)
        await real_system.advance_learning_unit(unit.id, "outputting")
        real_system.record_reuse_feedback(unit.id, "yes")

        in_proc = real_system.get_learning_unit_metrics(window_days=None)
        from_disk = calculate_metrics_summary(
            _read_all_events(real_system),
            window_days=None,
        )
        # window_start / generated_at 会差几微秒；指标字段必须一致
        assert in_proc.consolidation == from_disk.consolidation
        assert in_proc.teach_entry == from_disk.teach_entry
        assert in_proc.reuse_intent == from_disk.reuse_intent
        assert in_proc.ttfv.sample_size == from_disk.ttfv.sample_size


# ─── V 失败信号：窗口外的卷不影响窗口内指标 ─────────────────────


class TestV2WindowIsolation:
    """30 天外的旧卷不该污染 7 天窗口指标（避免误报达标）。"""

    @pytest.mark.asyncio
    async def test_old_unit_outside_window_excluded(
        self, real_system: LearningAgentSystem
    ):
        # 走一卷 → 把其 created/consolidated 事件 ts 后移到 30 天前
        # （我们没法直接改事件文件 ts，因此用 calculate_metrics_summary
        # 的 now 参数把"现在"挪到 30 天后，等价于把事件挪到 30 天前。）
        session, unit = real_system.create_learning_unit(seed_text="x")
        _simulate_first_value(real_system, session, unit)
        await real_system.advance_learning_unit(unit.id, "outputting")
        real_system.record_reuse_feedback(unit.id, "yes")

        events = _read_all_events(real_system)
        future_now = datetime.now(timezone.utc) + timedelta(days=30)
        summary = calculate_metrics_summary(
            events, window_days=7, now=future_now
        )
        # 30 天前的事件已经在 7 天窗口外
        assert summary.consolidation.denominator == 0
        assert summary.consolidation.ratio is None
        assert summary.reuse_intent.denominator == 0
