"""
可观测性基础设施：Trace、Metrics、Snapshot、Log 收集器。
观测即基础设施，通过订阅事件和注册 Hook 实现零侵入观测。

【多 Session 安全版本】
核心变更：
1. _active_span_stack → _span_stacks: dict[str, list[TraceSpan]]
2. 增加 _session_trace_map 用于 session → trace 路由
3. start_trace/end_trace 增加 session_id 参数
4. 所有 span 操作默认使用当前 trace 的栈，支持显式指定 trace_id
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
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
    """
    可观测性收集器（多 Session 安全版本）。

    核心变更：
    1. _active_span_stack → _span_stacks: dict[str, list[TraceSpan]]
    2. 增加 _session_trace_map 用于 session → trace 路由
    3. start_trace/end_trace 增加 session_id 参数
    4. 所有 span 操作默认使用当前 trace 的栈，支持显式指定 trace_id
    """

    def __init__(self, data_dir: str = ".observability"):
        self.data_dir = data_dir
        self.metrics = MetricsStore()
        self._traces: dict[str, Trace] = {}
        self._snapshots: list[Snapshot] = []
        self._logs: list[dict[str, Any]] = []

        # 【改造】按 trace_id 隔离的 span 栈
        self._span_stacks: dict[str, list[TraceSpan]] = {}

        # 【改造】session → trace 映射
        self._session_trace_map: dict[str, str] = {}

        # 【保留兼容】最后激活的 trace
        self._active_trace: Optional[Trace] = None

        os.makedirs(data_dir, exist_ok=True)

    # ─── Trace 管理 ───

    def start_trace(
        self,
        session_id: Optional[str] = None,
        objective_id: Optional[str] = None,
    ) -> Trace:
        trace = Trace(session_id=session_id, objective_id=objective_id)
        self._traces[trace.trace_id] = trace
        self._active_trace = trace

        # 【改造】为该 trace 创建独立的 span 栈
        self._span_stacks[trace.trace_id] = []

        # 【改造】建立 session → trace 映射
        if session_id:
            # 如果该 session 已有活跃 trace，先结束它
            old_trace_id = self._session_trace_map.get(session_id)
            if old_trace_id and old_trace_id in self._traces:
                old_trace = self._traces[old_trace_id]
                if not old_trace.end_time:
                    old_trace.end()
                    self._persist_trace(old_trace)
                    # 清理旧栈
                    self._span_stacks.pop(old_trace_id, None)

            self._session_trace_map[session_id] = trace.trace_id

        logger.info(f"[Observability] Trace started: {trace.trace_id} (session={session_id})")
        return trace

    def end_trace(self, trace_id: Optional[str] = None) -> Optional[Trace]:
        target_trace_id = trace_id or (self._active_trace.trace_id if self._active_trace else None)
        if not target_trace_id:
            return None

        trace = self._traces.get(target_trace_id)
        if not trace:
            return None

        trace.end()

        # 【改造】清理该 trace 的 span 栈
        self._span_stacks.pop(target_trace_id, None)

        # 【改造】清理 session → trace 映射（反向查找）
        sessions_to_remove = [
            sid for sid, tid in self._session_trace_map.items()
            if tid == target_trace_id
        ]
        for sid in sessions_to_remove:
            del self._session_trace_map[sid]

        if self._active_trace and self._active_trace.trace_id == target_trace_id:
            self._active_trace = None

        self._persist_trace(trace)
        return trace

    # ─── Span 管理 ───

    def start_span(
        self,
        name: str,
        parent: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> TraceSpan:
        # 确定使用哪个 trace
        target_trace_id = trace_id
        if not target_trace_id and self._active_trace:
            target_trace_id = self._active_trace.trace_id

        if not target_trace_id:
            # 兜底：创建一个新的 trace
            trace = self.start_trace()
            target_trace_id = trace.trace_id

        trace = self._traces.get(target_trace_id)
        if not trace:
            # 如果 trace 不存在，创建一个新的
            trace = self.start_trace()
            target_trace_id = trace.trace_id

        span = trace.start_span(name, parent)

        # 【改造】推入该 trace 的独立栈
        stack = self._span_stacks.setdefault(target_trace_id, [])
        stack.append(span)

        return span

    def end_span(
        self,
        span: Optional[TraceSpan] = None,
        trace_id: Optional[str] = None,
    ) -> None:
        # 确定使用哪个 trace
        target_trace_id = trace_id
        target_span = span

        if not target_trace_id and not target_span:
            # 默认使用 active_trace
            target_trace_id = self._active_trace.trace_id if self._active_trace else None

        if not target_trace_id and target_span:
            # 从 span 反查 trace（遍历所有 trace 查找）
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
            # 默认结束栈顶 span
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
        self._persist_snapshot(snap)
        return snap

    # ─── 事件处理 ───

    async def on_event(self, event: Event) -> None:
        """作为 EventBus 的订阅者，接收所有事件。"""
        # 判断是否为错误/警告事件
        level = self._detect_event_level(event)

        # 记录结构化日志
        log_entry = {
            "timestamp": event.timestamp.isoformat(),
            "level": level,
            "trace_id": event.trace_id,
            "type": event.type,
            "source": event.source,
            "session_id": event.session_id,
            "payload_keys": list(event.payload.keys()),
        }
        self._logs.append(log_entry)
        self._append_jsonl("events.jsonl", log_entry)

        # 错误事件写入专门的错误日志
        if level in ("error", "warn"):
            error_entry = self._build_error_entry(event, level)
            self._append_jsonl("errors.jsonl", error_entry)

        # 根据事件类型更新指标
        self._update_metrics_from_event(event)

    def _detect_event_level(self, event: Event) -> str:
        """根据事件类型和载荷判断日志级别。"""
        et = event.type

        # 明确的错误事件
        if et in (
            "agent.toolBanned",
            "agent.toolValidationFailed",
            "agent.toolPermissionDenied",
            "agent.traceError",
        ):
            return "error"

        # 需要关注但非致命
        if et in (
            "agent.toolPermissionAsk",
            "agent.turnRetry",
            "agent.contextCompressed",
        ):
            return "warn"

        # 状态变化到 error
        if et == "agent.stateChanged" and event.payload.get("new_state") == "error":
            return "error"

        # toolResult 中的失败
        if et == "agent.toolResult" and not event.payload.get("success", True):
            return "error"

        return "info"

    def _build_error_entry(self, event: Event, level: str) -> dict[str, Any]:
        """构建结构化的错误记录。"""
        et = event.type
        payload = event.payload
        category = "system"
        message = ""
        details: dict[str, Any] = {}

        if et == "agent.toolBanned":
            category = "tool"
            message = f"Tool '{payload.get('tool_id')}' temporarily banned"
            details = {
                "tool_id": payload.get("tool_id"),
                "call_id": payload.get("call_id"),
                "turn": payload.get("turn"),
                "failures_in_window": payload.get("failures_in_window"),
            }
        elif et == "agent.toolValidationFailed":
            category = "validation"
            message = f"Tool '{payload.get('tool_id')}' input validation failed"
            details = {
                "tool_id": payload.get("tool_id"),
                "call_id": payload.get("call_id"),
                "errors": payload.get("errors"),
                "turn": payload.get("turn"),
            }
        elif et == "agent.toolPermissionDenied":
            category = "permission"
            message = f"Tool '{payload.get('tool_id')}' permission denied"
            details = {
                "tool_id": payload.get("tool_id"),
                "call_id": payload.get("call_id"),
                "reason": payload.get("reason"),
            }
        elif et == "agent.toolPermissionAsk":
            category = "permission"
            message = f"Tool '{payload.get('tool_id')}' requires approval"
            details = {
                "tool_id": payload.get("tool_id"),
                "call_id": payload.get("call_id"),
                "message": payload.get("message"),
            }
        elif et == "agent.toolResult":
            category = "tool"
            message = f"Tool '{payload.get('tool_id')}' execution failed"
            details = {
                "tool_id": payload.get("tool_id"),
                "success": payload.get("success"),
                "result": payload.get("result"),
            }
        elif et == "agent.turnRetry":
            category = "llm"
            message = f"Turn retry (attempt {payload.get('attempt')})"
            details = {
                "attempt": payload.get("attempt"),
                "reason": payload.get("reason"),
            }
        elif et == "agent.contextCompressed":
            category = "system"
            message = "Context compressed due to overflow"
            details = {
                "original_messages": payload.get("original_messages"),
                "remaining_messages": payload.get("remaining_messages"),
                "reason": payload.get("reason"),
            }
        elif et == "agent.stateChanged":
            category = "system"
            message = f"Agent entered error state"
            details = {
                "old_state": payload.get("old_state"),
                "new_state": payload.get("new_state"),
            }
        else:
            # 兜底：保留原始 payload
            message = et
            details = payload

        return {
            "timestamp": event.timestamp.isoformat(),
            "level": level,
            "category": category,
            "type": et,
            "message": message,
            "trace_id": event.trace_id,
            "session_id": event.session_id,
            "source": event.source,
            "details": details,
        }

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
        elif et == "agent.stateChanged":
            # 【新增】per-session 状态指标
            session_id = event.session_id or "unknown"
            self.metrics.gauge_set(
                "agent.state",
                1,
                labels={"session": session_id, "state": event.payload.get("new_state", "unknown")},
            )

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
