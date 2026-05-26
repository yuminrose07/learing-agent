"""
Unresolved Failure Logger — 工具调用主链路的失败"内部账本"。

契约：工具调用必须对用户成功。任何最终对用户隐藏的失败
（结构化错误、不可恢复异常、被 Final Stream Guard 用 rescue
回答兜掉的空流），都必须在这里留下一条记录，供后续优化定位。

写入 JSONL，单行一条，永不抛异常到主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class UnresolvedFailureRecord:
    ts: float
    session_id: str
    layer: str
    reason_code: str
    message: str
    tool_id: Optional[str] = None
    arguments: Optional[dict[str, Any]] = None
    user_input: Optional[str] = None
    rescue_used: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class UnresolvedFailureLogger:
    """工具调用最终失败的"内部账本"。所有写入都不会向主链路抛异常。"""

    def __init__(self, log_path: str | os.PathLike[str]) -> None:
        self._path = Path(log_path)
        self._lock = asyncio.Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception(
                "[UnresolvedFailureLogger] Failed to ensure log dir: %s",
                self._path.parent,
            )

    async def record(
        self,
        *,
        session_id: str,
        layer: str,
        reason_code: str,
        message: str,
        tool_id: Optional[str] = None,
        arguments: Optional[dict[str, Any]] = None,
        user_input: Optional[str] = None,
        rescue_used: bool = False,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        record = UnresolvedFailureRecord(
            ts=time.time(),
            session_id=session_id,
            layer=layer,
            reason_code=reason_code,
            message=message,
            tool_id=tool_id,
            arguments=_safe_truncate_args(arguments),
            user_input=_safe_truncate_text(user_input, 512),
            rescue_used=rescue_used,
            extra=extra or {},
        )
        try:
            async with self._lock:
                await asyncio.to_thread(self._append_line, record)
        except Exception:
            logger.exception(
                "[UnresolvedFailureLogger] Failed to record (%s/%s)",
                layer, reason_code,
            )

    def _append_line(self, record: UnresolvedFailureRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False, default=str)
        with self._path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")


def _safe_truncate_text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


def _safe_truncate_args(args: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if args is None:
        return None
    out: dict[str, Any] = {}
    for key, value in args.items():
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = "<unrepresentable>"
        out[key] = _safe_truncate_text(text, 512)
    return out


__all__ = ["UnresolvedFailureLogger", "UnresolvedFailureRecord"]
