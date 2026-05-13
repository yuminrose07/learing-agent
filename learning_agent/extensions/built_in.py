"""内置扩展集合。"""

from __future__ import annotations

import logging

from learning_agent.core.extension_manager import Extension, ExtensionContext
from learning_agent.extensions.code_tools import create_code_tools_extension
from learning_agent.extensions.security_audit import create_security_audit_extension
from learning_agent.extensions.tool_guard import create_tool_guard_extension
from learning_agent.models import (
    AfterResponseInput,
    AfterResponseResult,
    AfterToolExecuteInput,
    AfterToolExecuteResult,
    BeforeAgentRunInput,
    BeforeAgentRunResult,
    Event,
    HookName,
    OnStreamChunkInput,
    OnStreamChunkResult,
)

logger = logging.getLogger(__name__)


def create_builtin_extensions(config: dict | None = None) -> list[Extension]:
    """创建默认启用的最小运行时扩展集合。"""
    return [
        _create_observability_extension(),
        _create_fulltrace_extension(),
        _create_output_prompting_extension(),
        create_code_tools_extension(),
        create_tool_guard_extension(config),
        create_security_audit_extension(config),
    ]


def _create_observability_extension() -> Extension:
    ext = Extension(
        id="core-observability",
        name="Observability Collector",
        version="0.2.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.subscribe_event("*", _on_any_event)
        ctx.register_hook(HookName.BEFORE_AGENT_RUN, _hook_before_agent_run, priority=100)
        ctx.register_hook(HookName.AFTER_RESPONSE, _hook_after_response, priority=100)

    ext.on_activate(activate)
    return ext


async def _on_any_event(event: Event) -> None:
    logger.debug("[core-observability] Event: %s from %s", event.type, event.source)


async def _hook_before_agent_run(hook_input: BeforeAgentRunInput) -> BeforeAgentRunResult:
    logger.debug(
        "[core-observability] Hook before_agent_run: %s tools=%s",
        hook_input.context.session_id,
        len(hook_input.tools_summary),
    )
    return BeforeAgentRunResult()


async def _hook_after_response(hook_input: AfterResponseInput) -> AfterResponseResult:
    logger.debug(
        "[core-observability] Hook after_response: %s chars",
        len(hook_input.response_text),
    )
    return AfterResponseResult()


def _create_output_prompting_extension() -> Extension:
    ext = Extension(
        id="core-output-prompting",
        name="Output Prompting",
        version="0.2.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.register_hook(HookName.AFTER_RESPONSE, _hook_output_prompting, priority=50)

    ext.on_activate(activate)
    return ext


async def _hook_output_prompting(hook_input: AfterResponseInput) -> AfterResponseResult:
    history_count = hook_input.response_metadata.get("user_message_count", 0)
    if history_count > 0 and history_count % 5 == 0:
        modified = hook_input.response_text + (
            "\n\n提示：你已经连续接收了一段内容，"
            "可以试着用自己的话总结刚才的关键点。"
        )
        return AfterResponseResult(response_override=modified)
    return AfterResponseResult()


_fulltrace_flows: dict[str, list[dict]] = {}
_fulltrace_stream_started: set[str] = set()


def _fulltrace_add_step(session_id: str, phase: str, detail: dict) -> None:
    from datetime import datetime, timezone

    _fulltrace_flows.setdefault(session_id, []).append(
        {
            "phase": phase,
            "time": datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        }
    )


def _fulltrace_persist(session_id: str) -> None:
    import json
    import os

    steps = _fulltrace_flows.get(session_id, [])
    if not steps:
        return
    path = os.path.join(".observability", f"flow_{session_id}.json")
    os.makedirs(".observability", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"session_id": session_id, "steps": steps},
            f,
            ensure_ascii=False,
            default=str,
            indent=2,
        )


def _fulltrace_load_existing() -> None:
    import json
    from pathlib import Path

    for f in Path(".observability").glob("flow_*.json"):
        try:
            sid = f.stem.replace("flow_", "")
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            _fulltrace_flows[sid] = data.get("steps", [])
        except Exception:
            continue


def _create_fulltrace_extension() -> Extension:
    ext = Extension(
        id="core-fulltrace",
        name="Full Trace Collector",
        version="0.2.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        _fulltrace_load_existing()
        ctx.register_hook(HookName.BEFORE_AGENT_RUN, _fulltrace_hook_before_agent_run, priority=90)
        ctx.register_hook(HookName.ON_STREAM_CHUNK, _fulltrace_hook_stream_chunk, priority=90)
        ctx.register_hook(HookName.AFTER_TOOL_EXECUTE, _fulltrace_hook_after_tool_execute, priority=90)
        ctx.register_hook(HookName.AFTER_RESPONSE, _fulltrace_hook_after_response, priority=90)

    ext.on_activate(activate)
    return ext


async def _fulltrace_hook_before_agent_run(
    hook_input: BeforeAgentRunInput,
) -> BeforeAgentRunResult:
    sid = hook_input.context.session_id
    _fulltrace_add_step(
        sid,
        "before_agent_run",
        {
            "user_input": hook_input.user_input[:1000],
            "tool_count": len(hook_input.tools_summary),
            "provider": hook_input.provider_summary,
        },
    )
    return BeforeAgentRunResult()


async def _fulltrace_hook_stream_chunk(
    hook_input: OnStreamChunkInput,
) -> OnStreamChunkResult:
    sid = hook_input.context.session_id
    if sid not in _fulltrace_stream_started:
        _fulltrace_stream_started.add(sid)
        _fulltrace_add_step(
            sid,
            "llm_stream_start",
            {"first_chunk": hook_input.content[:300]},
        )
    if hook_input.finish_reason:
        _fulltrace_stream_started.discard(sid)
        _fulltrace_add_step(
            sid,
            "llm_stream_end",
            {"finish_reason": hook_input.finish_reason},
        )
    return OnStreamChunkResult()


async def _fulltrace_hook_after_tool_execute(
    hook_input: AfterToolExecuteInput,
) -> AfterToolExecuteResult:
    sid = hook_input.context.session_id
    _fulltrace_add_step(
        sid,
        "tool_result",
        {
            "tool_name": hook_input.tool_name,
            "success": hook_input.success,
            "duration_ms": hook_input.duration_ms,
            "retry_count": hook_input.retry_count,
            "error": hook_input.error,
        },
    )
    return AfterToolExecuteResult()


async def _fulltrace_hook_after_response(
    hook_input: AfterResponseInput,
) -> AfterResponseResult:
    sid = hook_input.context.session_id
    _fulltrace_add_step(
        sid,
        "final_response",
        {"content": hook_input.response_text[:2000]},
    )
    _fulltrace_persist(sid)
    _fulltrace_stream_started.discard(sid)
    return AfterResponseResult()
