"""
安全审计日志扩展：记录所有权限决策（allow / ask / deny）到审计日志。

借鉴 Claude Code 的审计设计，所有工具权限决策都被持久化，
便于事后审查、安全分析和合规要求。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from learning_agent.core.extension_manager import Extension, ExtensionContext
from learning_agent.models import Event

logger = logging.getLogger(__name__)

_AUDIT_LOG_FILENAME = "audit.jsonl"


def _get_audit_log_path(config: dict[str, Any]) -> str:
    """获取审计日志文件路径。"""
    obs_dir = config.get("observability_dir", ".observability")
    return os.path.join(obs_dir, _AUDIT_LOG_FILENAME)


def _append_audit_record(record: dict[str, Any], config: dict[str, Any]) -> None:
    """追加一条审计记录到 JSONL 文件。"""
    path = _get_audit_log_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # 添加时间戳
    record["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"[SecurityAudit] Failed to write audit log: {e}")


async def _on_tool_called(event: Event) -> None:
    """处理工具调用事件，记录审计日志。"""
    payload = event.payload
    tool_id = payload.get("tool_id", "unknown")
    arguments = payload.get("arguments", {})
    session_id = event.session_id

    # 提取关键参数用于审计
    audit_args = {}
    if "command" in arguments:
        audit_args["command"] = arguments["command"]
    if "path" in arguments:
        audit_args["path"] = arguments["path"]

    record = {
        "event_type": "tool.called",
        "tool_id": tool_id,
        "arguments": audit_args,
        "session_id": session_id,
        "trace_id": event.trace_id,
        "source": event.source,
    }

    _append_audit_record(record, {})


async def _on_tool_result(event: Event) -> None:
    """处理工具结果事件，记录权限决策审计。"""
    payload = event.payload
    tool_id = payload.get("tool_id", "unknown")
    success = payload.get("success", True)
    error = payload.get("error", "")
    ask = payload.get("ask", False)
    aborted = payload.get("aborted", False)
    reason = payload.get("reason", "")
    session_id = event.session_id

    # 确定决策类型
    if aborted:
        decision = "deny"
    elif ask:
        decision = "ask"
    elif success:
        decision = "allow"
    else:
        decision = "error"

    record = {
        "event_type": "tool.result",
        "tool_id": tool_id,
        "decision": decision,
        "success": success,
        "reason": reason or error,
        "session_id": session_id,
        "trace_id": event.trace_id,
        "source": event.source,
    }

    _append_audit_record(record, {})

    # 对于 ask/deny 决策，额外记录警告日志
    if decision == "deny":
        logger.warning(
            f"[SecurityAudit] Tool '{tool_id}' DENIED in session {session_id}: {reason}"
        )
    elif decision == "ask":
        logger.info(
            f"[SecurityAudit] Tool '{tool_id}' ASK in session {session_id}: {reason}"
        )


def create_security_audit_extension(config: dict[str, Any] | None = None) -> Extension:
    """
    core-security-audit：安全审计日志扩展。

    记录所有工具调用的权限决策到 `.observability/audit.jsonl`，
    支持事后审查和安全分析。
    """
    ext = Extension(
        id="core-security-audit",
        name="Security Audit Logger",
        version="0.1.0",
        type="builtin",
        config=config or {},
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.subscribe_event("agent.toolCalled", _on_tool_called)
        ctx.subscribe_event("agent.toolResult", _on_tool_result)

    ext.on_activate(activate)
    return ext
