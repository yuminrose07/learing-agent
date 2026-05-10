"""
内置扩展：系统核心功能通过扩展系统实现，保持核心最小化。
当前版本包含占位实现，展示扩展系统的使用方式。
"""

from __future__ import annotations

import logging

from learning_agent.core.extension_manager import Extension, ExtensionContext
from learning_agent.core.hook_system import HookPoint
from learning_agent.models import Event, ToolDefinition

logger = logging.getLogger(__name__)


def create_builtin_extensions() -> list[Extension]:
    """创建所有内置扩展的列表。"""
    return [
        _create_observability_extension(),
        _create_output_prompting_extension(),
        _create_knowledge_extraction_extension(),
        _create_review_extension(),
        _create_material_text_extension(),
    ]


def _create_observability_extension() -> Extension:
    """
    core-observability：可观测性收集扩展。
    订阅所有事件，记录指标和日志。
    """
    ext = Extension(
        id="core-observability",
        name="Observability Collector",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.subscribe_event("*", _on_any_event)
        ctx.register_hook(
            HookPoint.BEFORE_LLM_CALL,
            _hook_before_llm,
            priority=100,
        )
        ctx.register_hook(
            HookPoint.AFTER_RESPONSE,
            _hook_after_response,
            priority=100,
        )

    ext.on_activate(activate)
    return ext


async def _on_any_event(event: Event) -> None:
    logger.debug(f"[core-observability] Event: {event.type} from {event.source}")


async def _hook_before_llm(data, context):
    logger.debug(f"[core-observability] Hook beforeLLMCall: {len(data)} messages")


async def _hook_after_response(data, context):
    logger.debug(f"[core-observability] Hook afterResponse: {len(data)} chars")


def _create_output_prompting_extension() -> Extension:
    """
    core-output-prompting：输出倒逼扩展。
    当检测到用户连续多轮被动接收时，追加输出要求。
    """
    ext = Extension(
        id="core-output-prompting",
        name="Output Prompting",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.register_hook(
            HookPoint.AFTER_RESPONSE,
            _hook_output_prompting,
            priority=50,
        )

    ext.on_activate(activate)
    return ext


async def _hook_output_prompting(response_content: str, context: dict):
    """
    简化版输出倒逼：每隔一定轮数提醒用户主动输出。
    实际实现应分析会话历史判断用户是否被动。
    """
    session = context.get("session")
    if session:
        history = [e for e in session.entries if e.role and e.role.value == "user"]
        if len(history) > 0 and len(history) % 5 == 0:
            # 每 5 轮用户消息提醒一次
            modified = response_content + (
                "\n\n💡 **输出倒逼**: 你已经接收了一段时间的内容。"
                "请尝试用自己的话总结一下刚才学到的要点，这将大大加深记忆。"
            )
            from learning_agent.models import HookResult
            return HookResult(modified=True, data=modified)
    from learning_agent.models import HookResult
    return HookResult(modified=False, data=response_content)


def _create_knowledge_extraction_extension() -> Extension:
    """
    core-knowledge-extraction：知识提取扩展。
    在响应后自动提取候选知识节点到 L1。
    """
    ext = Extension(
        id="core-knowledge-extraction",
        name="Knowledge Extraction",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.subscribe_event("agent.responseDone", _on_response_done)
        ctx.register_tool(
            ToolDefinition(
                id="create_knowledge_node",
                name="create_knowledge_node",
                description="Create a knowledge node from the current context",
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The knowledge content"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["content"],
                },
            ),
            _tool_create_knowledge_node,
        )

    ext.on_activate(activate)
    return ext


async def _on_response_done(event: Event) -> None:
    logger.debug(f"[core-knowledge-extraction] Response done in session {event.session_id}")


async def _tool_create_knowledge_node(content: str, tags: list[str] = None, **kwargs):
    from learning_agent.models import KnowledgeNode
    node = KnowledgeNode(content=content, tags=tags or [])
    return {"node_id": node.id, "content": node.content}


def _create_review_extension() -> Extension:
    """
    core-review：间隔重复调度扩展。
    在知识确认后安排复习，处理复习结果。
    """
    ext = Extension(
        id="core-review",
        name="Spaced Repetition Scheduler",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.subscribe_event("knowledge.confirmed", _on_knowledge_confirmed)
        ctx.register_tool(
            ToolDefinition(
                id="schedule_review",
                name="schedule_review",
                description="Schedule a review for a knowledge node",
                parameters={
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "interval_days": {"type": "integer"},
                    },
                    "required": ["node_id"],
                },
            ),
            _tool_schedule_review,
        )

    ext.on_activate(activate)
    return ext


async def _on_knowledge_confirmed(event: Event) -> None:
    logger.info(f"[core-review] Knowledge confirmed, scheduling review: {event.payload}")


async def _tool_schedule_review(node_id: str, interval_days: int = 1, **kwargs):
    return {"node_id": node_id, "scheduled": True, "interval_days": interval_days}


def _create_material_text_extension() -> Extension:
    """
    core-material-text：文本材料解析扩展。
    提供基础文本材料的读取工具。
    """
    ext = Extension(
        id="core-material-text",
        name="Text Material Parser",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.register_tool(
            ToolDefinition(
                id="read_material",
                name="read_material",
                description="Read a text material by path or ID",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            ),
            _tool_read_material,
        )

    ext.on_activate(activate)
    return ext


async def _tool_read_material(path: str, **kwargs):
    import os
    if not os.path.exists(path):
        return {"error": f"Material not found: {path}"}
    with open(path, "r", encoding="utf-8") as f:
        return {"content": f.read()}
