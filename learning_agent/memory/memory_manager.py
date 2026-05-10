"""
记忆管理器：四层记忆的分层读写、晋升、降级、淘汰。
L0: Transient Memory（单轮对话）
L1: Working Memory（当前会话）
L2: Long-term Memory（跨会话，知识图谱载体）
L3: Archive Memory（长期归档，仍参与复习）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from learning_agent.memory.knowledge_graph import KnowledgeGraph
from learning_agent.memory.spaced_repetition import SpacedRepetitionEngine
from learning_agent.models import (
    ContextComponent,
    Event,
    KnowledgeNode,
    MasteryLevel,
    MemoryLevel,
)

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    记忆管理器核心。
    - 维护四层记忆的接口
    - 管理知识图谱
    - 集成间隔重复引擎
    - 支持 Relevant Recall（基于当前话题召回相关记忆）
    """

    def __init__(
        self,
        knowledge_graph: Optional[KnowledgeGraph] = None,
        sr_engine: Optional[SpacedRepetitionEngine] = None,
    ):
        self.kg = knowledge_graph or KnowledgeGraph()
        self.sr = sr_engine or SpacedRepetitionEngine()
        # L0: 单轮瞬态记忆
        self._l0_transient: list[ContextComponent] = []
        # L1: 当前会话的工作记忆（候选知识节点）
        self._l1_working: dict[str, KnowledgeNode] = {}

    # ─── L0: Transient Memory ───

    def set_l0(self, components: list[ContextComponent]) -> None:
        self._l0_transient = components

    def get_l0(self) -> list[ContextComponent]:
        return self._l0_transient.copy()

    def clear_l0(self) -> None:
        self._l0_transient.clear()

    # ─── L1: Working Memory ───

    def add_l1_candidate(self, node: KnowledgeNode) -> None:
        """在会话中识别到候选知识，暂存 L1。"""
        node.memory_level = MemoryLevel.L1_WORKING
        self._l1_working[node.id] = node
        logger.debug(f"[MemoryManager] L1 candidate added: {node.id}")

    def get_l1_candidates(self) -> list[KnowledgeNode]:
        return list(self._l1_working.values())

    def clear_l1(self) -> None:
        self._l1_working.clear()

    # ─── L2: Long-term Memory ───

    def promote_to_l2(self, node: KnowledgeNode, auto_confirm: bool = False) -> KnowledgeNode:
        """
        将 L1 候选知识晋升到 L2（长期记忆）。
        需要用户确认（除非配置为自动确认）。
        """
        node.memory_level = MemoryLevel.L2_LONGTERM
        node.mastery_level = MasteryLevel.ESTIMATED
        node = self.kg.add_node(node)
        self.sr.schedule_first_review(node)
        self._l1_working.pop(node.id, None)
        logger.info(f"[MemoryManager] Node {node.id} promoted to L2")
        return node

    def get_l2_nodes(
        self,
        tag: Optional[str] = None,
        limit: int = 100,
    ) -> list[KnowledgeNode]:
        nodes = self.kg.list_nodes(memory_level=MemoryLevel.L2_LONGTERM, limit=limit)
        if tag:
            nodes = [n for n in nodes if tag in n.tags]
        return nodes

    # ─── L3: Archive Memory ───

    def archive_to_l3(self, node_id: str) -> Optional[KnowledgeNode]:
        node = self.kg.get_node(node_id)
        if node:
            node.memory_level = MemoryLevel.L3_ARCHIVE
            self.kg.update_node(node)
            logger.info(f"[MemoryManager] Node {node_id} archived to L3")
        return node

    def get_l3_nodes(self, limit: int = 100) -> list[KnowledgeNode]:
        return self.kg.list_nodes(memory_level=MemoryLevel.L3_ARCHIVE, limit=limit)

    # ─── 复习相关 ───

    def get_due_reviews(self) -> list[KnowledgeNode]:
        """获取所有到期的复习节点（L2 + L3）。"""
        all_nodes = self.kg.list_nodes(limit=10000)
        return self.sr.get_due_nodes(all_nodes)

    def process_review(self, node_id: str, result: str) -> Optional[KnowledgeNode]:
        node = self.kg.get_node(node_id)
        if not node:
            return None
        self.sr.process_review(node, result)
        self.kg.update_node(node)
        return node

    # ─── Relevant Recall ───

    def relevant_recall(
        self,
        query: str,
        limit: int = 5,
        include_due_reviews: bool = True,
    ) -> list[KnowledgeNode]:
        """
        基于当前话题召回相关记忆。
        当前实现：标签匹配 + 关键词搜索（简化版）。
        """
        results = []
        seen = set()

        # 1. 到期复习节点优先插入
        if include_due_reviews:
            due = self.get_due_reviews()
            for node in due[:2]:
                if node.id not in seen:
                    results.append(node)
                    seen.add(node.id)

        # 2. 文本相似召回
        similar = self.kg.find_by_text_similarity(query, limit=limit)
        for node, score in similar:
            if node.id not in seen:
                results.append(node)
                seen.add(node.id)

        # 3. 关联节点联想
        for node in list(results):
            related = self.kg.find_related(node.id, depth=1)[:2]
            for r in related:
                if r.id not in seen:
                    results.append(r)
                    seen.add(r.id)

        return results[:limit + 2]

    # ─── 序列化 ───

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_graph": self.kg.to_dict(),
            "l1_working": {k: v.model_dump() for k, v in self._l1_working.items()},
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        self.kg.from_dict(data.get("knowledge_graph", {}))
        self._l1_working.clear()
        for nid, ndata in data.get("l1_working", {}).items():
            self._l1_working[nid] = KnowledgeNode(**ndata)
