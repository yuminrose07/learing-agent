"""M2：``learning_unit_metrics`` 纯函数 + LearningAgentSystem 聚合 API 单测。

事件流由测试代码直接构造 ``SessionEvent``，不经文件 IO，免去 fixture 复杂度。

覆盖：
- ``calculate_ttfv``：成对、乱序、缺一不可、跨卷不串、单点 p50=p90
- ``calculate_consolidation_rate`` / ``calculate_teach_entry_rate``：
  分子分母去重、窗口裁剪、分母为 0 时 ratio=None
- ``calculate_reuse_intent_rate``：取每卷首条、yes 比例
- ``calculate_metrics_summary``：window_days=None 走全量、负数抛 ValueError
- ``LearningAgentSystem.record_reuse_feedback``：phase/value 校验、事件发射
- ``LearningAgentSystem.get_learning_unit_metrics``：把多 session 事件合流
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import LearningUnit, UnitObjective
from learning_agent.learning_agent.learning_unit_metrics import (
    calculate_consolidation_rate,
    calculate_metrics_summary,
    calculate_reuse_intent_rate,
    calculate_teach_entry_rate,
    calculate_ttfv,
    summary_to_dict,
)
from learning_agent.learning_agent.main import LearningAgentSystem
from learning_agent.learning_agent.session_events import (
    SessionEvent,
    SessionEventType,
)


_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def _evt(
    *,
    seq: int,
    session_id: str,
    type_: str,
    unit_id: str,
    ts: datetime,
    extra: dict | None = None,
) -> SessionEvent:
    payload = {
        "learning_unit_id": unit_id,
        "phase": "absorbing",
        "alignment_state": "idle",
        "objective_status": "working",
        "alignment_reason": "",
        "clarification_count": 0,
    }
    if extra:
        payload.update(extra)
    return SessionEvent(
        seq=seq,
        session_id=session_id,
        type=type_,
        payload=payload,
        ts=ts,
    )


class TestCalculateTTFV:
    def test_single_pair_yields_same_p50_p90(self):
        created = _evt(
            seq=1, session_id="s1", unit_id="lu-1",
            type_=SessionEventType.LEARNING_UNIT_CREATED,
            ts=_NOW - timedelta(seconds=30),
        )
        first = _evt(
            seq=2, session_id="s1", unit_id="lu-1",
            type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
            ts=_NOW,
        )

        stats = calculate_ttfv([created, first])

        assert stats.sample_size == 1
        assert stats.p50_seconds == 30
        assert stats.p90_seconds == 30

    def test_unit_without_first_value_is_excluded(self):
        events = [
            _evt(seq=1, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            # lu-2 has both
            _evt(seq=2, session_id="s2", unit_id="lu-2",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(seconds=10)),
            _evt(seq=3, session_id="s2", unit_id="lu-2",
                 type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                 ts=_NOW),
        ]

        stats = calculate_ttfv(events)

        assert stats.sample_size == 1
        assert stats.p50_seconds == 10

    def test_negative_delta_is_skipped(self):
        """first_value 在 created 之前 → 乱序，弃用。"""
        events = [
            _evt(seq=1, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=2, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                 ts=_NOW - timedelta(seconds=10)),
        ]

        stats = calculate_ttfv(events)
        assert stats.sample_size == 0
        assert stats.p50_seconds is None

    def test_duplicate_first_value_takes_earliest(self):
        events = [
            _evt(seq=1, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(seconds=100)),
            _evt(seq=2, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                 ts=_NOW - timedelta(seconds=50)),
            _evt(seq=3, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                 ts=_NOW),
        ]

        stats = calculate_ttfv(events)
        assert stats.p50_seconds == 50  # 取最早一条

    def test_p50_p90_ordering(self):
        """5 个样本 [1s, 2s, 5s, 10s, 100s] → p50 ≈ 5, p90 ≈ 100。"""
        events = []
        deltas = [1, 2, 5, 10, 100]
        for i, d in enumerate(deltas):
            uid = f"lu-{i}"
            events.append(_evt(
                seq=2 * i + 1, session_id=f"s{i}", unit_id=uid,
                type_=SessionEventType.LEARNING_UNIT_CREATED,
                ts=_NOW - timedelta(seconds=d),
            ))
            events.append(_evt(
                seq=2 * i + 2, session_id=f"s{i}", unit_id=uid,
                type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                ts=_NOW,
            ))

        stats = calculate_ttfv(events)
        assert stats.sample_size == 5
        # nearest-rank：p50 索引 = round(0.5*5)=2 → 第3 个 = 5
        assert stats.p50_seconds == 5
        # p90 索引 = round(0.9*5)=4 → 第 5 个 = 100
        assert stats.p90_seconds == 100


class TestRatioMetrics:
    def test_consolidation_rate_within_window(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=1)),
            _evt(seq=2, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(hours=1)),
            _evt(seq=3, session_id="s", unit_id="lu-b",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=2)),
            # lu-b 未 consolidated → 拉低分子
        ]
        window_start = _NOW - timedelta(days=7)
        stats = calculate_consolidation_rate(events, window_start=window_start)
        assert stats.numerator == 1
        assert stats.denominator == 2
        assert stats.ratio == 0.5

    def test_consolidation_rate_zero_denominator_returns_none_ratio(self):
        stats = calculate_consolidation_rate([], window_start=None)
        assert stats.denominator == 0
        assert stats.ratio is None

    def test_consolidation_rate_window_excludes_old_events(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-old",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=30)),
            _evt(seq=2, session_id="s", unit_id="lu-old",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(days=29)),
        ]
        stats = calculate_consolidation_rate(
            events, window_start=_NOW - timedelta(days=7)
        )
        # 全部被裁出
        assert stats.numerator == 0
        assert stats.denominator == 0

    def test_consolidation_rate_ignores_old_units_completed_in_window(self):
        events = [
            _evt(seq=1, session_id="s-old-1", unit_id="lu-old-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=30)),
            _evt(seq=2, session_id="s-old-1", unit_id="lu-old-1",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(days=1)),
            _evt(seq=3, session_id="s-old-2", unit_id="lu-old-2",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=20)),
            _evt(seq=4, session_id="s-old-2", unit_id="lu-old-2",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(hours=1)),
            _evt(seq=5, session_id="s-new", unit_id="lu-new",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(hours=2)),
        ]
        stats = calculate_consolidation_rate(
            events, window_start=_NOW - timedelta(days=7)
        )
        assert stats.numerator == 0
        assert stats.denominator == 1
        assert stats.ratio == 0.0

    def test_teach_entry_rate(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=2, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED, ts=_NOW),
            _evt(seq=3, session_id="s", unit_id="lu-b",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=4, session_id="s", unit_id="lu-c",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=5, session_id="s", unit_id="lu-c",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED, ts=_NOW),
        ]
        stats = calculate_teach_entry_rate(events, window_start=None)
        assert stats.numerator == 2
        assert stats.denominator == 3
        assert stats.ratio == pytest.approx(2 / 3)

    def test_teach_entry_dedups_same_unit(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=2, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED, ts=_NOW),
            # 重复事件（理论上不应出现，但 replay 可能）
            _evt(seq=3, session_id="s", unit_id="lu-a",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED, ts=_NOW),
        ]
        stats = calculate_teach_entry_rate(events, window_start=None)
        assert stats.numerator == 1

    def test_teach_entry_rate_ignores_old_units_entered_in_window(self):
        events = [
            _evt(seq=1, session_id="s-old", unit_id="lu-old",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=30)),
            _evt(seq=2, session_id="s-old", unit_id="lu-old",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED,
                 ts=_NOW - timedelta(hours=1)),
            _evt(seq=3, session_id="s-new", unit_id="lu-new",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(hours=2)),
        ]
        stats = calculate_teach_entry_rate(
            events, window_start=_NOW - timedelta(days=7)
        )
        assert stats.numerator == 0
        assert stats.denominator == 1
        assert stats.ratio == 0.0


class TestReuseIntentMetric:
    def test_yes_no_split(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "yes"}),
            _evt(seq=2, session_id="s", unit_id="lu-2",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "yes"}),
            _evt(seq=3, session_id="s", unit_id="lu-3",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "no"}),
        ]
        stats = calculate_reuse_intent_rate(events, window_start=None)
        assert stats.numerator == 2
        assert stats.denominator == 3
        assert stats.ratio == pytest.approx(2 / 3)

    def test_takes_first_vote_per_unit(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "yes"}),
            _evt(seq=2, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW + timedelta(minutes=1), extra={"value": "no"}),
        ]
        stats = calculate_reuse_intent_rate(events, window_start=None)
        assert stats.numerator == 1  # 首条 yes 算
        assert stats.denominator == 1

    def test_invalid_value_skipped(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "maybe"}),
        ]
        stats = calculate_reuse_intent_rate(events, window_start=None)
        assert stats.denominator == 0


class TestMetricsSummary:
    def test_summary_aggregates_all_four(self):
        events = [
            # lu-1: created → first_value → teach_entered → consolidated → yes
            _evt(seq=1, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(seconds=20)),
            _evt(seq=2, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
                 ts=_NOW - timedelta(seconds=15)),
            _evt(seq=3, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED,
                 ts=_NOW - timedelta(seconds=10)),
            _evt(seq=4, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(seconds=5)),
            _evt(seq=5, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
                 ts=_NOW, extra={"value": "yes"}),
        ]

        summary = calculate_metrics_summary(events, window_days=7, now=_NOW)

        assert summary.window_days == 7
        assert summary.ttfv.sample_size == 1
        assert summary.ttfv.p50_seconds == 5
        assert summary.consolidation.ratio == 1.0
        assert summary.teach_entry.ratio == 1.0
        assert summary.reuse_intent.ratio == 1.0
        assert summary.diagnostics["learning_unit_events"] == 5

    def test_summary_window_days_none_skips_window(self):
        events = [
            _evt(seq=1, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED,
                 ts=_NOW - timedelta(days=30)),
            _evt(seq=2, session_id="s", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                 ts=_NOW - timedelta(days=29)),
        ]
        summary = calculate_metrics_summary(events, window_days=None, now=_NOW)
        assert summary.consolidation.denominator == 1
        assert summary.consolidation.ratio == 1.0

    def test_negative_window_days_raises(self):
        with pytest.raises(ValueError):
            calculate_metrics_summary([], window_days=-1, now=_NOW)

    def test_summary_to_dict_shape(self):
        summary = calculate_metrics_summary([], window_days=7, now=_NOW)
        payload = summary_to_dict(summary)
        assert set(payload.keys()) == {
            "window_days",
            "window_start",
            "generated_at",
            "ttfv",
            "consolidation_rate",
            "teach_entry_rate",
            "reuse_intent_rate",
            "diagnostics",
        }
        assert payload["ttfv"]["sample_size"] == 0
        assert payload["consolidation_rate"]["ratio"] is None


# ── LearningAgentSystem aggregation / reuse-feedback API ─────────────


def _stub_system_for_metrics() -> LearningAgentSystem:
    system = LearningAgentSystem.__new__(LearningAgentSystem)
    system.session_event_store = MagicMock()
    system.session_event_store.append_event = MagicMock()
    system.session_event_store.read_events = MagicMock(return_value=[])
    system.learning_unit_store = MagicMock()
    system.learning_unit_store.get = MagicMock(return_value=None)
    system.file_store = MagicMock()
    system.file_store.list_sessions = MagicMock(return_value=[])
    return system


def _make_unit(*, phase: str = "consolidated") -> LearningUnit:
    unit = LearningUnit(
        session_id="sess-fb",
        objective=UnitObjective(text="x"),
    )
    unit.phase = phase  # type: ignore[assignment]
    return unit


class TestRecordReuseFeedback:
    def test_happy_path_emits_event_with_value(self):
        system = _stub_system_for_metrics()
        unit = _make_unit(phase="consolidated")
        system.learning_unit_store.get = MagicMock(
            side_effect=lambda uid: unit if uid == unit.id else None
        )

        result = system.record_reuse_feedback(unit.id, "yes")

        assert result is unit
        call = system.session_event_store.append_event.call_args
        assert call.args[1] == "learning_unit.reuse_feedback"
        assert call.kwargs["payload"]["value"] == "yes"

    def test_invalid_value_raises(self):
        system = _stub_system_for_metrics()
        with pytest.raises(ValueError):
            system.record_reuse_feedback("lu-x", "maybe")

    def test_missing_unit_raises_keyerror(self):
        system = _stub_system_for_metrics()
        with pytest.raises(KeyError):
            system.record_reuse_feedback("lu-missing", "yes")

    def test_non_consolidated_phase_raises(self):
        system = _stub_system_for_metrics()
        unit = _make_unit(phase="absorbing")
        system.learning_unit_store.get = MagicMock(return_value=unit)
        with pytest.raises(ValueError, match="consolidated"):
            system.record_reuse_feedback(unit.id, "yes")


class TestGetLearningUnitMetricsAggregation:
    def test_aggregates_events_across_sessions(self):
        system = _stub_system_for_metrics()
        system.file_store.list_sessions = MagicMock(return_value=["s1", "s2"])

        s1_events = [
            _evt(seq=1, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
            _evt(seq=2, session_id="s1", unit_id="lu-1",
                 type_=SessionEventType.LEARNING_UNIT_TEACH_ENTERED, ts=_NOW),
        ]
        s2_events = [
            _evt(seq=1, session_id="s2", unit_id="lu-2",
                 type_=SessionEventType.LEARNING_UNIT_CREATED, ts=_NOW),
        ]

        def _read(sid, after_seq=None):
            return {"s1": s1_events, "s2": s2_events}.get(sid, [])

        system.session_event_store.read_events = MagicMock(side_effect=_read)

        summary = system.get_learning_unit_metrics(window_days=None)
        assert summary.teach_entry.numerator == 1
        assert summary.teach_entry.denominator == 2
        assert summary.teach_entry.ratio == 0.5

    def test_empty_event_store_returns_zero_summary(self):
        system = _stub_system_for_metrics()
        summary = system.get_learning_unit_metrics(window_days=7)
        assert summary.consolidation.denominator == 0
        assert summary.ttfv.sample_size == 0
