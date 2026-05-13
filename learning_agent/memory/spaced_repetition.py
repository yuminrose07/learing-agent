"""
Memory 子域中的复习调度引擎。

逻辑上属于 Product/Application 层的独立 Memory 子域，负责知识节点复习节奏，
而不是 Agent Runtime 的对话状态机。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from learning_agent.ai import KnowledgeNode, MasteryLevel, MemoryLevel, ReviewRecord

logger = logging.getLogger(__name__)


class SpacedRepetitionEngine:
    """
    简化 SM-2 间隔重复引擎。
    - ease_factor 初始 2.5
    - interval 基于 review result 调整
    - mastery level 随复习结果演化
    """

    def __init__(
        self,
        initial_interval_days: int = 1,
        ease_factor_default: float = 2.5,
        ease_factor_min: float = 1.3,
    ):
        self.initial_interval = initial_interval_days
        self.ease_factor_default = ease_factor_default
        self.ease_factor_min = ease_factor_min

    def schedule_first_review(self, node: KnowledgeNode) -> KnowledgeNode:
        """为新知识节点安排第一次复习。"""
        node.next_review_at = datetime.now(timezone.utc) + timedelta(days=self.initial_interval)
        node.last_reviewed_at = datetime.now(timezone.utc)
        node.mastery_level = MasteryLevel.ESTIMATED
        logger.debug(f"[SR] First review scheduled for {node.id} at {node.next_review_at}")
        return node

    def process_review(
        self,
        node: KnowledgeNode,
        result: str,  # pass | struggle | fail
    ) -> KnowledgeNode:
        """
        处理一次复习结果，更新 interval、ease_factor、mastery_level。
        """
        now = datetime.now(timezone.utc)
        last_record = node.review_history[-1] if node.review_history else None
        last_interval = last_record.interval_days if last_record else self.initial_interval
        last_ef = last_record.ease_factor if last_record else self.ease_factor_default

        if result == "pass":
            new_interval = int(last_interval * last_ef)
            new_ef = max(last_ef + 0.1, self.ease_factor_min)
            new_mastery = self._promote_mastery(node.mastery_level)
        elif result == "struggle":
            new_interval = last_interval
            new_ef = max(last_ef - 0.15, self.ease_factor_min)
            new_mastery = node.mastery_level
        else:  # fail
            new_interval = self.initial_interval
            new_ef = max(last_ef - 0.3, self.ease_factor_min)
            new_mastery = self._demote_mastery(node.mastery_level)

        record = ReviewRecord(
            date=now,
            result=result,
            interval_days=new_interval,
            ease_factor=new_ef,
        )
        node.review_history.append(record)
        node.last_reviewed_at = now
        node.next_review_at = now + timedelta(days=new_interval)
        node.mastery_level = new_mastery

        # 如果掌握度达到 mastered，考虑归档到 L3
        if node.mastery_level == MasteryLevel.MASTERED:
            node.memory_level = MemoryLevel.L3_ARCHIVE

        logger.info(f"[SR] Review processed for {node.id}: result={result}, next={new_interval}d, ef={new_ef:.2f}")
        return node

    def get_due_nodes(self, nodes: list[KnowledgeNode]) -> list[KnowledgeNode]:
        """返回所有到期的复习节点。"""
        now = datetime.now(timezone.utc)
        return [n for n in nodes if n.next_review_at and n.next_review_at <= now]

    def _promote_mastery(self, current: MasteryLevel) -> MasteryLevel:
        order = [
            MasteryLevel.ESTIMATED,
            MasteryLevel.FAMILIAR,
            MasteryLevel.UNDERSTOOD,
            MasteryLevel.MASTERED,
        ]
        idx = order.index(current)
        return order[min(idx + 1, len(order) - 1)]

    def _demote_mastery(self, current: MasteryLevel) -> MasteryLevel:
        order = [
            MasteryLevel.ESTIMATED,
            MasteryLevel.FAMILIAR,
            MasteryLevel.UNDERSTOOD,
            MasteryLevel.MASTERED,
        ]
        idx = order.index(current)
        return order[max(idx - 1, 0)]
