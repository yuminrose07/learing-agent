"""内置扩展集合。"""

from __future__ import annotations

import logging

from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.learning_agent.extensions.code_tools import create_code_tools_extension
from learning_agent.learning_agent.extensions.grep_tools import create_grep_tools_extension
from learning_agent.learning_agent.extensions.tool_guard import create_tool_guard_extension
from learning_agent.ai import (
    AfterResponseInput,
    AfterResponseResult,
    BeforeAgentRunInput,
    BeforeAgentRunResult,
    Event,
    HookName,
)

logger = logging.getLogger(__name__)


def create_builtin_extensions(config: dict | None = None) -> list[Extension]:
    """创建默认启用的最小运行时扩展集合。

    2026-05-24 重构：移除 ``core-fulltrace`` 与 ``core-security-audit`` 扩展，
    它们的产出（``flow_*.json`` / ``audit.jsonl``）已由 L1 事件流取代。
    """
    return [
        _create_observability_extension(),
        create_code_tools_extension(),
        create_grep_tools_extension(),
        create_tool_guard_extension(config),
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
