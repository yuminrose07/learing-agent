"""
可观测性基础设施：Trace、Metrics、Snapshot、Log 收集器（**纯内存**版本）。

【2026-05-24 重构】事件流持久化已迁移到 L1（`<base_dir>/sessions/<id>.events.jsonl`，
唯一写入入口为 ``SessionEventStore.append_event``），本模块只保留：

1. ``MetricsStore`` —— 内存计数 / 直方图 / Gauge
2. ``ObservabilityCollector`` —— 内存 Trace/Span/Snapshot 栈，给 ToolExecutor、
   SessionRuntime 等组件在运行时打点用（``if self.obs:`` 守护）
3. ``on_event`` —— EventBus 兜底订阅入口，目前仅做内存指标聚合，不再写入文件

**不再产出任何文件**。之前的 ``events.jsonl`` / ``errors.jsonl`` /
``trace_*.json`` / ``snap_*.json`` 写入逻辑已全部下线，详见
``docs/design/design-observability-l1-l4-architecture.md`` 第九节迁移策略。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from typing import Any, Optional

from learning_agent.ai import Event, Snapshot, Trace, TraceSpan

logger = logging.getLogger(__name__)


class MetricsStore:
    """
    内存中的指标存储，支持 Counter、Histogram、Gauge。
    后续可扩展为持久化到文件或时序数据库。
    """

    def __init__(self, max_samples: int = 10000):
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max_samples))

    def counter_inc(self, name: str, value: float = 1.0, labels: Optional[dict[str, str]] = None) -> None:
        key = self._key(name, labels)
        self._counters[key] += value

    def gauge_set(self, name: str, value: float, labels: Optional[dict[str, str]] = None) -> None:
        key = self._key(name, labels)
        self._gauges[key] = value

    def histogram_record(self, name: str, value: float, labels: Optional[dict[str, str]] = None) -> None:
        key = self._key(name, labels)
        self._histograms[key].append(value)

    def get_summary(self) -> dict[str, Any]:
        summary = {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {},
        }
        for key, samples in self._histograms.items():
            if not samples:
                continue
            arr = list(samples)
            arr.sort()
            summary["histograms"][key] = {
                "count": len(arr),
                "min": arr[0],
                "max": arr[-1],
                "p50": arr[int(len(arr) * 0.5)],
                "p95": arr[int(len(arr) * 0.95)] if len(arr) > 1 else arr[0],
                "p99": arr[int(len(arr) * 0.99)] if len(arr) > 1 else arr[0],
            }
        return summary

    def _key(self, name: str, labels: Optional[dict[str, str]]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"


class ObservabilityCollector:
    """纯内存的可观测性收集器（多 Session 安全）。

    span 栈按 trace_id 隔离，session→trace 映射用于按 session 查询。
    本类已不再产出任何文件——历史的 ``events.jsonl`` / ``trace_*.json`` 等都
    迁到 L1 事件流，由 ``SessionEventStore`` 写入。
    """

    def __init__(self) -> None:
        self.metrics = MetricsStore()
        self._traces: dict[str, Trace] = {}
        self._snapshots: list[Snapshot] = []

        # 按 trace_id 隔离的 span 栈
        self._span_stacks: dict[str, list[TraceSpan]] = {}

        # session → trace 映射
        self._session_trace_map: dict[str, str] = {}

        # 最后激活的 trace（兼容旧调用方）
        self._active_trace: Optional[Trace] = None

    # ─── Trace 管理 ───

    def start_trace(
        self,
        session_id: Optional[str] = None,
        objective_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Trace:
        trace = Trace(session_id=session_id, objective_id=objective_id, mode=mode)
        self._traces[trace.trace_id] = trace
        self._active_trace = trace

        self._span_stacks[trace.trace_id] = []

        if session_id:
            old_trace_id = self._session_trace_map.get(session_id)
            if old_trace_id and old_trace_id in self._traces:
                old_trace = self._traces[old_trace_id]
                if not old_trace.end_time:
                    old_trace.end()
                    self._span_stacks.pop(old_trace_id, None)

            self._session_trace_map[session_id] = trace.trace_id

        logger.debug("[Observability] Trace started: %s (session=%s)", trace.trace_id, session_id)
        return trace

    def end_trace(self, trace_id: Optional[str] = None) -> Optional[Trace]:
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            return None

        trace = self._traces.get(target_trace_id)
        if not trace:
            return None

        trace.end()
        self._span_stacks.pop(target_trace_id, None)

        sessions_to_remove = [
            sid for sid, tid in self._session_trace_map.items()
            if tid == target_trace_id
        ]
        for sid in sessions_to_remove:
            del self._session_trace_map[sid]

        if self._active_trace and self._active_trace.trace_id == target_trace_id:
            self._active_trace = None

        return trace

    # ─── Span 管理 ───

    def start_span(
        self,
        name: str,
        parent: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> TraceSpan:
        target_trace_id = trace_id
        if not target_trace_id and self._active_trace:
            target_trace_id = self._active_trace.trace_id

        if not target_trace_id:
            trace = self.start_trace()
            target_trace_id = trace.trace_id

        trace = self._traces.get(target_trace_id)
        if not trace:
            trace = self.start_trace()
            target_trace_id = trace.trace_id

        span = trace.start_span(name, parent)

        stack = self._span_stacks.setdefault(target_trace_id, [])
        stack.append(span)

        return span

    def end_span(
        self,
        span: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        target_trace_id = trace_id
        target_span = span

        if not target_trace_id and not target_span:
            target_trace_id = self._active_trace.trace_id if self._active_trace else None

        if not target_trace_id and target_span:
            for tid, trace in self._traces.items():
                if target_span in trace.spans:
                    target_trace_id = tid
                    break

        if not target_trace_id:
            logger.warning("[Observability] end_span called without trace context")
            return

        stack = self._span_stacks.get(target_trace_id, [])

        if target_span:
            target_span.end()
            if target_span in stack:
                stack.remove(target_span)
        elif stack:
            top = stack.pop()
            top.end()

    def current_span(self, trace_id: Optional[str] = None) -> Optional[TraceSpan]:
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            return None
        stack = self._span_stacks.get(target_trace_id, [])
        return stack[-1] if stack else None

    def current_trace(self) -> Optional[Trace]:
        return self._active_trace

    # ─── 按 Session 查询 ───

    def get_trace_by_session(self, session_id: str) -> Optional[Trace]:
        """获取指定 session 的当前活跃 trace。"""
        trace_id = self._session_trace_map.get(session_id)
        if trace_id:
            return self._traces.get(trace_id)
        return None

    def get_spans_by_session(self, session_id: str) -> list[TraceSpan]:
        """获取指定 session 的当前 span 栈。"""
        trace_id = self._session_trace_map.get(session_id)
        if trace_id:
            return list(self._span_stacks.get(trace_id, []))
        return []

    def clear_session_traces(self, session_id: str) -> None:
        """清理指定 session 的所有 trace 和 span 数据。"""
        trace_id = self._session_trace_map.pop(session_id, None)
        if trace_id:
            self._span_stacks.pop(trace_id, None)
            trace = self._traces.pop(trace_id, None)
            if trace and self._active_trace and self._active_trace.trace_id == trace_id:
                self._active_trace = None

    # ─── Snapshot ───

    def take_snapshot(self, node: str, state: dict[str, Any]) -> Snapshot:
        snap = Snapshot(
            trace_id=self._active_trace.trace_id if self._active_trace else None,
            node=node,
            state=state,
        )
        self._snapshots.append(snap)
        return snap

    # ─── 事件聚合（仅指标，不落盘）───

    async def on_event(self, event: Event) -> None:
        """作为 EventBus 的订阅者，把事件转换为内存指标。

        注意：这条路径**不再写入文件**。日志只保留 metrics 更新，事件流
        持久化由 L1 的 ``SessionEventStore`` 负责。
        """
        self._update_metrics_from_event(event)

    def _update_metrics_from_event(self, event: Event) -> None:
        et = event.type

        if et == "agent.responseChunk":
            self.metrics.counter_inc("llm.chunk.received", 1)
        elif et == "agent.responseDone":
            self.metrics.counter_inc("agent.request.completed", 1)
        elif et == "agent.turnUsage":
            usage = event.payload.get("usage", {})
            phase = event.payload.get("phase", "response")
            is_estimated = str(bool(usage.get("is_estimated", True))).lower()
            self.metrics.counter_inc(
                "llm.usage.turns",
                1,
                {"phase": phase, "is_estimated": is_estimated},
            )
            for field, metric in (
                ("estimated_prompt_tokens", "llm.usage.estimated_prompt_tokens"),
                ("actual_prompt_tokens", "llm.usage.actual_prompt_tokens"),
                ("actual_completion_tokens", "llm.usage.actual_completion_tokens"),
                ("actual_total_tokens", "llm.usage.actual_total_tokens"),
                ("context_limit", "llm.usage.context_limit"),
                ("utilization_ratio", "llm.usage.context_utilization_ratio"),
            ):
                value = usage.get(field)
                if value is not None:
                    self.metrics.histogram_record(metric, value)
        elif et == "agent.toolCalled":
            self.metrics.counter_inc("tool.call.total", 1)
            tool_id = event.payload.get("tool_id", "unknown")
            self.metrics.counter_inc("tool.call.total", 1, {"tool": tool_id})
        elif et == "agent.toolResult":
            success = event.payload.get("success", True)
            self.metrics.counter_inc("tool.call.success" if success else "tool.call.failure", 1)
        elif et == "memory.recallDone":
            nodes = event.payload.get("nodes", [])
            self.metrics.histogram_record("memory.recall.count", len(nodes))
        elif et == "knowledge.confirmed":
            self.metrics.counter_inc("knowledge.confirmed", 1)
        elif et == "extension.hookExecuted":
            duration = event.payload.get("duration_ms", 0)
            ext_id = event.payload.get("extension_id", "unknown")
            self.metrics.histogram_record("extension.hook.duration", duration, {"ext": ext_id})
        elif et == "session.forked":
            self.metrics.counter_inc("session.fork.count", 1)
        elif et == "agent.stateChanged":
            session_id = event.session_id or "unknown"
            self.metrics.gauge_set(
                "agent.state",
                1,
                labels={"session": session_id, "state": event.payload.get("new_state", "unknown")},
            )

    # ─── 查询 ───

    def get_trace(self, trace_id: str) -> Optional[Trace]:
        return self._traces.get(trace_id)

    def get_metrics_summary(self) -> dict[str, Any]:
        return self.metrics.get_summary()

    def record_runtime_created(self, session_id: str) -> None:
        """记录运行时创建指标。"""
        self.metrics.counter_inc("agent.runtime.created", 1, {"session": session_id})

    def record_runtime_cleared(self, session_id: str) -> None:
        """记录运行时清理指标。"""
        self.metrics.counter_inc("agent.runtime.cleared", 1, {"session": session_id})

    def write_narrative_summary(self, trace_id: str, output_path: Optional[str] = None) -> str:
        """生成叙事式 Trace 总结（供人类阅读）。"""
        trace = self._traces.get(trace_id)
        if not trace:
            return f"Trace '{trace_id}' not found."

        lines = [
            f"# Trace 叙事总结: {trace_id}",
            "",
            "## 概要",
            f"- **Trace ID**: {trace.trace_id}",
            f"- **会话 ID**: {trace.session_id or 'N/A'}",
            f"- **总耗时**: {trace.duration_ms or 'N/A'}ms",
            f"- **时间**: {trace.timestamp.isoformat()}",
            "",
            "## Span 详情",
        ]

        for span in trace.spans:
            lines.append(f"### {span.name}")
            lines.append(f"- **耗时**: {span.duration_ms or 'N/A'}ms")
            lines.append(f"- **Tags**: {json.dumps(span.tags, ensure_ascii=False)}")
            if span.error:
                lines.append(f"- **错误**: {span.error}")
            lines.append("")

        text = "\n".join(lines)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(text)
        return text
