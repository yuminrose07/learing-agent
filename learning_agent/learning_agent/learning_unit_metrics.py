"""学习卷核心指标计算（adaptive alignment §12.1 / MVP §11.1）。

四个 P0 指标，全部基于 ``sessions/<id>.events.jsonl`` 中的 ``learning_unit.*``
事件直接聚合，不读 unit 状态文件：

- ``首个学习价值时间 (TTFV)``：``ts(first_value_delivered) - ts(created)``，
  按卷成对、聚合 p50 / p90（秒）
- ``学习卷完成率``：``count(consolidated) / count(created)``，时间窗口默认 7d
- ``验收进入率``：``count(teach_entered) / count(created)``，同窗口
- ``复用意愿``：``count(reuse_feedback where value=yes) / count(reuse_feedback)``

设计取舍：
- 纯函数 + 事件流入参，便于离线 backfill 与回归测试。系统层在
  ``LearningAgentSystem.get_learning_unit_metrics`` 中负责拼装事件。
- ``created`` 用作分母时按时间窗筛选；``consolidated`` / ``teach_entered``
  只在同一批窗口内创建的卷里计数，避免旧卷在窗口内完成时把比率推到 100%
  以上。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from learning_agent.learning_agent.session_events import (
    SessionEvent,
    SessionEventType,
)


@dataclass(frozen=True)
class TTFVStats:
    """首个学习价值时间统计（秒）。``sample_size==0`` 时 p50/p90 为 None。"""

    p50_seconds: Optional[float]
    p90_seconds: Optional[float]
    sample_size: int


@dataclass(frozen=True)
class RatioStats:
    """通用比率指标。``denominator==0`` 时 ratio 为 None（避免假阳）。"""

    numerator: int
    denominator: int
    ratio: Optional[float]


@dataclass(frozen=True)
class LearningUnitMetricsSummary:
    """4 个 P0 指标的聚合视图，供 ``GET /learning-units/metrics`` 直接序列化。

    ``window_days``=None 表示全量（不裁窗口）；``window_start_iso`` 在全量
    情况下为 None，便于前端展示。
    """

    window_days: Optional[int]
    window_start_iso: Optional[str]
    generated_at_iso: str
    ttfv: TTFVStats
    consolidation: RatioStats
    teach_entry: RatioStats
    reuse_intent: RatioStats
    diagnostics: dict[str, int] = field(default_factory=dict)


def _ensure_aware(ts: datetime) -> datetime:
    """SessionEvent.ts 通常是 timezone-aware UTC，但磁盘回读偶尔会 naive。"""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _filter_window(
    events: Iterable[SessionEvent],
    *,
    window_start: Optional[datetime],
) -> list[SessionEvent]:
    if window_start is None:
        return list(events)
    out: list[SessionEvent] = []
    for e in events:
        if _ensure_aware(e.ts) >= window_start:
            out.append(e)
    return out


def calculate_ttfv(events: Iterable[SessionEvent]) -> TTFVStats:
    """配对 (created, first_value_delivered) 求 delta 秒，再算 p50 / p90。

    没有 first_value_delivered 的卷不计入（保留率口径放到完成率指标）。
    同一卷出现多条 first_value_delivered 是不应发生的（once-only 守卫），
    若真出现取最早一条以保守估计 TTFV。
    """
    created_at: dict[str, datetime] = {}
    first_value_at: dict[str, datetime] = {}
    for e in events:
        unit_id = e.payload.get("learning_unit_id")
        if not unit_id:
            continue
        ts = _ensure_aware(e.ts)
        if e.type == SessionEventType.LEARNING_UNIT_CREATED:
            # 最早的 created 才是真创建（防止 replay/重放双发）
            if unit_id not in created_at or ts < created_at[unit_id]:
                created_at[unit_id] = ts
        elif e.type == SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED:
            if unit_id not in first_value_at or ts < first_value_at[unit_id]:
                first_value_at[unit_id] = ts

    deltas: list[float] = []
    for unit_id, fv_ts in first_value_at.items():
        c_ts = created_at.get(unit_id)
        if c_ts is None:
            continue
        delta = (fv_ts - c_ts).total_seconds()
        if delta < 0:
            continue  # 事件乱序：弃用
        deltas.append(delta)

    if not deltas:
        return TTFVStats(p50_seconds=None, p90_seconds=None, sample_size=0)

    deltas.sort()
    return TTFVStats(
        p50_seconds=_percentile(deltas, 0.50),
        p90_seconds=_percentile(deltas, 0.90),
        sample_size=len(deltas),
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """nearest-rank 百分位（``rank = ceil(p * n)``，小数据集稳定，无插值伪精度）。"""
    if not sorted_values:
        raise ValueError("sorted_values must be non-empty")
    if pct <= 0:
        return sorted_values[0]
    if pct >= 1:
        return sorted_values[-1]
    rank = max(1, math.ceil(pct * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _count_unique_units(events: Iterable[SessionEvent], event_type: str) -> int:
    """同一卷的同一事件可能因 replay 出现多次，按 learning_unit_id 去重。"""
    return len(_unit_ids_for_event(events, event_type))


def _unit_ids_for_event(
    events: Iterable[SessionEvent],
    event_type: str,
) -> set[str]:
    """取出某类事件涉及的不重复 learning_unit_id。"""
    seen: set[str] = set()
    for e in events:
        if e.type != event_type:
            continue
        unit_id = e.payload.get("learning_unit_id")
        if not unit_id:
            continue
        seen.add(unit_id)
    return seen


def calculate_consolidation_rate(
    events: Iterable[SessionEvent],
    *,
    window_start: Optional[datetime] = None,
) -> RatioStats:
    """窗口内创建的卷里，已经 consolidated 的占比。"""
    scoped = _filter_window(events, window_start=window_start)
    created_units = _unit_ids_for_event(
        scoped, SessionEventType.LEARNING_UNIT_CREATED
    )
    consolidated_units = _unit_ids_for_event(
        scoped, SessionEventType.LEARNING_UNIT_CONSOLIDATED
    )
    return _make_ratio(
        len(created_units & consolidated_units),
        len(created_units),
    )


def calculate_teach_entry_rate(
    events: Iterable[SessionEvent],
    *,
    window_start: Optional[datetime] = None,
) -> RatioStats:
    """窗口内创建的卷里，进入 teach 的占比。"""
    scoped = _filter_window(events, window_start=window_start)
    created_units = _unit_ids_for_event(
        scoped, SessionEventType.LEARNING_UNIT_CREATED
    )
    teach_entered_units = _unit_ids_for_event(
        scoped, SessionEventType.LEARNING_UNIT_TEACH_ENTERED
    )
    return _make_ratio(
        len(created_units & teach_entered_units),
        len(created_units),
    )


def calculate_reuse_intent_rate(
    events: Iterable[SessionEvent],
    *,
    window_start: Optional[datetime] = None,
) -> RatioStats:
    """复用意愿：reuse_feedback 中 ``value=="yes"`` 的占比。

    每个卷只取 *第一条* reuse_feedback（防止用户反复点切换的票数膨胀）；
    分母为有过 reuse_feedback 的不重复卷数。
    """
    scoped = _filter_window(events, window_start=window_start)
    first_value_by_unit: dict[str, str] = {}
    for e in scoped:
        if e.type != SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK:
            continue
        unit_id = e.payload.get("learning_unit_id")
        if not unit_id or unit_id in first_value_by_unit:
            continue
        value = e.payload.get("value")
        if value in ("yes", "no"):
            first_value_by_unit[unit_id] = value
    total = len(first_value_by_unit)
    yes_count = sum(1 for v in first_value_by_unit.values() if v == "yes")
    return _make_ratio(yes_count, total)


def _make_ratio(numerator: int, denominator: int) -> RatioStats:
    if denominator <= 0:
        return RatioStats(numerator=numerator, denominator=denominator, ratio=None)
    return RatioStats(
        numerator=numerator,
        denominator=denominator,
        ratio=numerator / denominator,
    )


def calculate_metrics_summary(
    events: Iterable[SessionEvent],
    *,
    window_days: Optional[int] = 7,
    now: Optional[datetime] = None,
) -> LearningUnitMetricsSummary:
    """聚合所有 4 个 P0 指标。

    - ``window_days=None`` → 不裁窗口，全量
    - ``now`` 仅用于测试注入；默认取 ``datetime.now(tz=utc)``
    - TTFV 不裁窗口（卷的生命周期可能跨窗，裁了会人为偏低），其余 3 个走窗口
    """
    events_list = list(events)
    now = now or datetime.now(timezone.utc)
    window_start: Optional[datetime] = None
    if window_days is not None:
        if window_days < 0:
            raise ValueError("window_days must be non-negative or None")
        window_start = now - timedelta(days=window_days)

    diagnostics = {
        "total_events": len(events_list),
        "learning_unit_events": sum(
            1 for e in events_list if e.type.startswith("learning_unit.")
        ),
    }
    return LearningUnitMetricsSummary(
        window_days=window_days,
        window_start_iso=window_start.isoformat() if window_start else None,
        generated_at_iso=now.isoformat(),
        ttfv=calculate_ttfv(events_list),
        consolidation=calculate_consolidation_rate(events_list, window_start=window_start),
        teach_entry=calculate_teach_entry_rate(events_list, window_start=window_start),
        reuse_intent=calculate_reuse_intent_rate(events_list, window_start=window_start),
        diagnostics=diagnostics,
    )


def summary_to_dict(summary: LearningUnitMetricsSummary) -> dict:
    """把 dataclass 摊平成普通 dict，供 JSON 响应直接序列化。"""
    return {
        "window_days": summary.window_days,
        "window_start": summary.window_start_iso,
        "generated_at": summary.generated_at_iso,
        "ttfv": {
            "p50_seconds": summary.ttfv.p50_seconds,
            "p90_seconds": summary.ttfv.p90_seconds,
            "sample_size": summary.ttfv.sample_size,
        },
        "consolidation_rate": {
            "numerator": summary.consolidation.numerator,
            "denominator": summary.consolidation.denominator,
            "ratio": summary.consolidation.ratio,
        },
        "teach_entry_rate": {
            "numerator": summary.teach_entry.numerator,
            "denominator": summary.teach_entry.denominator,
            "ratio": summary.teach_entry.ratio,
        },
        "reuse_intent_rate": {
            "numerator": summary.reuse_intent.numerator,
            "denominator": summary.reuse_intent.denominator,
            "ratio": summary.reuse_intent.ratio,
        },
        "diagnostics": dict(summary.diagnostics),
    }


__all__ = [
    "TTFVStats",
    "RatioStats",
    "LearningUnitMetricsSummary",
    "calculate_ttfv",
    "calculate_consolidation_rate",
    "calculate_teach_entry_rate",
    "calculate_reuse_intent_rate",
    "calculate_metrics_summary",
    "summary_to_dict",
]
