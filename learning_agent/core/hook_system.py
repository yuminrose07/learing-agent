"""Typed Hook dispatcher used by the minimal runtime."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

from learning_agent.models import (
    AfterResponseInput,
    AfterResponseResult,
    AfterToolExecuteInput,
    AfterToolExecuteResult,
    BeforeAgentRunInput,
    BeforeAgentRunResult,
    BeforeToolExecuteInput,
    BeforeToolExecuteResult,
    HookDecision,
    HookName,
    OnStreamChunkInput,
    OnStreamChunkResult,
    TraceSpan,
)

logger = logging.getLogger(__name__)

HookHandler = Callable[[Any], Awaitable[Any]]
TInput = TypeVar("TInput")
TResult = TypeVar("TResult")


class _HookRegistration:
    def __init__(
        self,
        handler: HookHandler,
        priority: int = 0,
        extension_id: Optional[str] = None,
    ):
        self.handler = handler
        self.priority = priority
        self.extension_id = extension_id


class HookSystem:
    """管理 Hook 注册、排序与 typed 合并。"""

    def __init__(self):
        self._hooks: dict[HookName, list[_HookRegistration]] = {}

    def register(
        self,
        hook_name: HookName | str,
        handler: HookHandler,
        priority: int = 0,
        extension_id: Optional[str] = None,
    ) -> None:
        hook = self._normalize_name(hook_name)
        self._hooks.setdefault(hook, []).append(
            _HookRegistration(handler, priority, extension_id)
        )
        self._hooks[hook].sort(key=lambda r: r.priority, reverse=True)
        logger.debug(
            "[HookSystem] Registered '%s' (ext=%s, prio=%s)",
            hook.value,
            extension_id,
            priority,
        )

    def unregister(
        self,
        hook_name: HookName | str,
        handler: HookHandler,
    ) -> None:
        hook = self._normalize_name(hook_name)
        if hook in self._hooks:
            self._hooks[hook] = [r for r in self._hooks[hook] if r.handler != handler]

    async def run_before_agent_run(
        self,
        hook_input: BeforeAgentRunInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> BeforeAgentRunResult:
        merged = BeforeAgentRunResult()
        first_ask_message: Optional[str] = None
        first_deny_reason: Optional[str] = None

        for result in await self._run_handlers(
            HookName.BEFORE_AGENT_RUN,
            hook_input,
            trace_span,
            BeforeAgentRunResult,
        ):
            if result.decision == HookDecision.DENY and first_deny_reason is None:
                first_deny_reason = result.deny_reason
            if result.decision == HookDecision.ASK and first_ask_message is None:
                first_ask_message = result.ask_message
            merged.runtime_patch.update(result.runtime_patch)
            merged.warnings.extend(result.warnings)
            merged.audit_records.extend(result.audit_records)
            merged.decision = self._merge_decision(merged.decision, result.decision)

        if merged.decision == HookDecision.ASK:
            merged.ask_message = first_ask_message
        elif merged.decision == HookDecision.DENY:
            merged.deny_reason = first_deny_reason
        return merged

    async def run_before_tool_execute(
        self,
        hook_input: BeforeToolExecuteInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> BeforeToolExecuteResult:
        merged = BeforeToolExecuteResult()
        first_ask_message: Optional[str] = None
        first_deny_reason: Optional[str] = None

        for result in await self._run_handlers(
            HookName.BEFORE_TOOL_EXECUTE,
            hook_input,
            trace_span,
            BeforeToolExecuteResult,
        ):
            if result.decision == HookDecision.DENY and first_deny_reason is None:
                first_deny_reason = result.deny_reason
            if result.decision == HookDecision.ASK and first_ask_message is None:
                first_ask_message = result.ask_message
            merged.patched_arguments.update(result.patched_arguments)
            merged.annotations.update(result.annotations)
            merged.warnings.extend(result.warnings)
            merged.audit_records.extend(result.audit_records)
            merged.decision = self._merge_decision(merged.decision, result.decision)

        if merged.decision == HookDecision.ASK:
            merged.ask_message = first_ask_message
        elif merged.decision == HookDecision.DENY:
            merged.deny_reason = first_deny_reason
        return merged

    async def run_after_tool_execute(
        self,
        hook_input: AfterToolExecuteInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> AfterToolExecuteResult:
        merged = AfterToolExecuteResult()
        for result in await self._run_handlers(
            HookName.AFTER_TOOL_EXECUTE,
            hook_input,
            trace_span,
            AfterToolExecuteResult,
        ):
            if result.display_result_override:
                merged.display_result_override = result.display_result_override
            merged.extra_metadata.update(result.extra_metadata)
            merged.warnings.extend(result.warnings)
            merged.audit_records.extend(result.audit_records)
        return merged

    async def run_on_stream_chunk(
        self,
        hook_input: OnStreamChunkInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> OnStreamChunkResult:
        merged = OnStreamChunkResult()
        current_input = hook_input
        for result in await self._run_handlers(
            HookName.ON_STREAM_CHUNK,
            hook_input,
            trace_span,
            OnStreamChunkResult,
            chainable=True,
        ):
            if result.content_override is not None:
                merged.content_override = result.content_override
                current_input = current_input.model_copy(
                    update={"content": result.content_override}
                )
            if result.reasoning_content_override is not None:
                merged.reasoning_content_override = result.reasoning_content_override
                current_input = current_input.model_copy(
                    update={
                        "reasoning_content": result.reasoning_content_override,
                    }
                )
            merged.stream_tags.update(result.stream_tags)
            merged.warnings.extend(result.warnings)
            hook_input = current_input
        return merged

    async def run_after_response(
        self,
        hook_input: AfterResponseInput,
        trace_span: Optional[TraceSpan] = None,
    ) -> AfterResponseResult:
        merged = AfterResponseResult()
        signals_seen: set[str] = set()
        for result in await self._run_handlers(
            HookName.AFTER_RESPONSE,
            hook_input,
            trace_span,
            AfterResponseResult,
        ):
            if result.response_override:
                merged.response_override = result.response_override
            merged.extra_metadata.update(result.extra_metadata)
            for signal in result.followup_signals:
                if signal not in signals_seen:
                    merged.followup_signals.append(signal)
                    signals_seen.add(signal)
            merged.warnings.extend(result.warnings)
            merged.audit_records.extend(result.audit_records)
        return merged

    async def _run_handlers(
        self,
        hook_name: HookName,
        hook_input: TInput,
        trace_span: Optional[TraceSpan],
        expected_type: type[TResult],
        chainable: bool = False,
    ) -> list[TResult]:
        registrations = self._hooks.get(hook_name, [])
        if not registrations:
            return []

        results: list[TResult] = []
        current_input = hook_input
        for reg in registrations:
            ext_id = reg.extension_id or "unknown"
            start_ts = time.time()
            try:
                result = await reg.handler(current_input)
                if result is None:
                    continue
                if not isinstance(result, expected_type):
                    logger.warning(
                        "[HookSystem] Hook '%s' returned invalid result from %s: %s",
                        hook_name.value,
                        ext_id,
                        type(result).__name__,
                    )
                    continue
                results.append(result)
                if chainable and isinstance(current_input, OnStreamChunkInput):
                    update: dict[str, Any] = {}
                    if result.content_override is not None:
                        update["content"] = result.content_override
                    if result.reasoning_content_override is not None:
                        update["reasoning_content"] = result.reasoning_content_override
                    if update:
                        current_input = current_input.model_copy(update=update)
            except Exception as exc:
                logger.exception(
                    "[HookSystem] Hook '%s' error in %s: %s",
                    hook_name.value,
                    ext_id,
                    exc,
                )
                if trace_span:
                    trace_span.tags[f"hook.{hook_name.value}.{ext_id}.error"] = str(exc)
            finally:
                duration_ms = int((time.time() - start_ts) * 1000)
                if trace_span:
                    trace_span.tags[
                        f"hook.{hook_name.value}.{ext_id}.duration_ms"
                    ] = duration_ms
        return results

    def list_registered(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for hook_name, regs in self._hooks.items():
            result[hook_name.value] = [r.extension_id or "unknown" for r in regs]
        return result

    @staticmethod
    def _normalize_name(hook_name: HookName | str) -> HookName:
        return hook_name if isinstance(hook_name, HookName) else HookName(hook_name)

    @staticmethod
    def _merge_decision(
        current: HookDecision,
        candidate: HookDecision,
    ) -> HookDecision:
        priority = {
            HookDecision.CONTINUE: 0,
            HookDecision.ASK: 1,
            HookDecision.DENY: 2,
        }
        return candidate if priority[candidate] > priority[current] else current
