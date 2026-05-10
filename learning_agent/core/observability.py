"""
可观测性基础设施：Trace、Metrics、Snapshot、Log 收集器。
观测即基础设施，通过订阅事件和注册 Hook 实现零侵入观测。
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional

from learning_agent.models import Event, Snapshot, Trace, TraceSpan

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
    """
    可观测性收集器（内置扩展 core-observability 的核心逻辑）。
    - 订阅所有事件，转换为观测日志
    - 注册关键 Hook，记录性能数据
    - 维护 Trace 和 Snapshot
    """

    def __init__(self, data_dir: str = ".observability"):
        self.data_dir = data_dir
        self.metrics = MetricsStore()
        self._traces: dict[str, Trace] = {}
        self._snapshots: list[Snapshot] = []
        self._logs: list[dict[str, Any]] = []
        self._active_trace: Optional[Trace] = None
        self._active_span_stack: list[TraceSpan] = []

        os.makedirs(data_dir, exist_ok=True)

    # ─── Trace 管理 ───

    def start_trace(self, session_id: Optional[str] = None, objective_id: Optional[str] = None) -> Trace:
        trace = Trace(session_id=session_id, objective_id=objective_id)
        self._traces[trace.trace_id] = trace
        self._active_trace = trace
        self._active_span_stack.clear()
        logger.info(f"[Observability] Trace started: {trace.trace_id}")
        return trace

    def end_trace(self) -> Optional[Trace]:
        if self._active_trace:
            self._active_trace.end()
            trace = self._active_trace
            self._active_trace = None
            self._active_span_stack.clear()
            self._persist_trace(trace)
            return trace
        return None

    def start_span(self, name: str, parent: Optional[TraceSpan] = None) -> TraceSpan:
        if not self._active_trace:
            self.start_trace()
        span = self._active_trace.start_span(name, parent)
        self._active_span_stack.append(span)
        return span

    def end_span(self, span: Optional[TraceSpan] = None) -> None:
        target = span or (self._active_span_stack[-1] if self._active_span_stack else None)
        if target:
            target.end()
            if target in self._active_span_stack:
                self._active_span_stack.remove(target)

    def current_span(self) -> Optional[TraceSpan]:
        return self._active_span_stack[-1] if self._active_span_stack else None

    def current_trace(self) -> Optional[Trace]:
        return self._active_trace

    # ─── Snapshot ───

    def take_snapshot(self, node: str, state: dict[str, Any]) -> Snapshot:
        snap = Snapshot(
            trace_id=self._active_trace.trace_id if self._active_trace else None,
            node=node,
            state=state,
        )
        self._snapshots.append(snap)
        self._persist_snapshot(snap)
        return snap

    # ─── 事件处理 ───

    async def on_event(self, event: Event) -> None:
        """作为 EventBus 的订阅者，接收所有事件。"""
        # 记录结构化日志
        log_entry = {
            "timestamp": event.timestamp.isoformat(),
            "level": "info",
            "trace_id": event.trace_id,
            "type": event.type,
            "source": event.source,
            "session_id": event.session_id,
            "payload_keys": list(event.payload.keys()),
        }
        self._logs.append(log_entry)
        self._append_jsonl("events.jsonl", log_entry)

        # 根据事件类型更新指标
        self._update_metrics_from_event(event)

    def _update_metrics_from_event(self, event: Event) -> None:
        et = event.type

        if et == "agent.responseChunk":
            self.metrics.counter_inc("llm.token.received", 1)
        elif et == "agent.responseDone":
            self.metrics.counter_inc("agent.request.completed", 1)
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

    # ─── 持久化 ───

    def _persist_trace(self, trace: Trace) -> None:
        path = os.path.join(self.data_dir, f"trace_{trace.trace_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(trace.model_dump_json(indent=2))

    def _persist_snapshot(self, snap: Snapshot) -> None:
        path = os.path.join(self.data_dir, f"snap_{snap.snapshot_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(snap.model_dump_json(indent=2))

    def _append_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        path = os.path.join(self.data_dir, filename)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # ─── 查询 ───

    def get_trace(self, trace_id: str) -> Optional[Trace]:
        return self._traces.get(trace_id)

    def get_metrics_summary(self) -> dict[str, Any]:
        return self.metrics.get_summary()

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
