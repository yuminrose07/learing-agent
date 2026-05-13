"""
工具失败追踪器：维护每个工具的滑动窗口失败计数，判断是否触发临时禁用。

行为：
- record_success 时清空该工具的全部历史计数（彻底重置）
- is_banned 只统计 current_turn - turn_count <= window 且 reason == "execution_error" 的记录
- validation_failed、hook_abort 等不计入 ban 计数
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ToolFailureTracker:
    """基于滑动窗口的工具失败计数器。"""

    def __init__(self, window_turns: int = 5, threshold: int = 3):
        self._window = window_turns
        self._threshold = threshold
        # tool_id -> [(turn_count, reason), ...]
        self._counts: dict[str, list[tuple[int, str]]] = {}

    def record_failure(self, tool_id: str, turn_count: int, reason: str = "execution_error") -> None:
        """记录一次工具失败，附带失败原因。"""
        if tool_id not in self._counts:
            self._counts[tool_id] = []
        self._counts[tool_id].append((turn_count, reason))
        logger.debug(
            f"[ToolFailureTracker] Recorded failure for '{tool_id}' "
            f"at turn {turn_count} (reason={reason})."
        )

    def record_success(self, tool_id: str) -> None:
        """成功执行后清零该工具计数。"""
        if tool_id in self._counts:
            del self._counts[tool_id]
            logger.debug(f"[ToolFailureTracker] Cleared failures for '{tool_id}' after success.")

    def is_banned(self, tool_id: str, current_turn: int) -> bool:
        """判断工具是否在当前滑动窗口内被临时禁用。

        只对 'execution_error' 类型的失败进行 ban 计数。
        validation_failed、hook_abort 等不计入 ban。
        """
        entries = self._counts.get(tool_id, [])
        if not entries:
            return False

        failures_in_window = sum(
            1 for turn, reason in entries
            if current_turn - turn <= self._window and reason == "execution_error"
        )
        return failures_in_window >= self._threshold

    def get_ban_message(self, tool_id: str) -> str:
        """返回临时禁用的标准错误文本。"""
        return (
            f"[Tool Unavailable]\n"
            f"Tool '{tool_id}' has been temporarily disabled due to repeated failures.\n"
            f"Please try a different approach or wait before using this tool again."
        )

    def get_failures_in_window(self, tool_id: str, current_turn: int) -> int:
        """返回工具在当前窗口内的 execution_error 失败次数。"""
        entries = self._counts.get(tool_id, [])
        return sum(
            1 for turn, reason in entries
            if current_turn - turn <= self._window and reason == "execution_error"
        )

    def get_all_failures_in_window(self, tool_id: str, current_turn: int) -> dict[str, int]:
        """返回各类失败在窗口内的分布（用于观测）。"""
        entries = self._counts.get(tool_id, [])
        counts: dict[str, int] = {}
        for turn, reason in entries:
            if current_turn - turn <= self._window:
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    def reset(self) -> None:
        """重置所有计数（session 恢复等场景）。"""
        self._counts.clear()
