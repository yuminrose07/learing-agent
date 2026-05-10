"""
知识图谱：知识节点的 CRUD、边的维护、基础查询。
当前版本使用内存存储 + 文件序列化。
后续可扩展为 SQLite 邻接表 + 节点内容存文件。
"""

from __future__ import annotations

import logging
from typing import Optional

from learning_agent.models import (
    EdgeType,
    KnowledgeEdge,
    KnowledgeNode,
    MasteryLevel,
    MemoryLevel,
)

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """
    知识图谱管理器。
    - 节点存储在内存 dict 中
    - 边存储在独立的 dict 中
    - 支持按标签、类型、掌握度查询
    - 支持路径发现（简单 BFS）
    """

    def __init__(self):
        self._nodes: dict[str, KnowledgeNode] = {}
        self._edges: dict[str, KnowledgeEdge] = {}
        # 反向索引
        self._tag_index: dict[str, list[str]] = {}  # tag -> node_ids
        self._source_index: dict[str, list[str]] = {}  # material_id -> node_ids

    # ─── 节点 CRUD ───

    def add_node(self, node: KnowledgeNode) -> KnowledgeNode:
        self._nodes[node.id] = node
        for tag in node.tags:
            self._tag_index.setdefault(tag, []).append(node.id)
        if node.source_material_id:
            self._source_index.setdefault(node.source_material_id, []).append(node.id)
        logger.debug(f"[KnowledgeGraph] Added node {node.id}")
        return node

    def get_node(self, node_id: str) -> Optional[KnowledgeNode]:
        return self._nodes.get(node_id)

    def update_node(self, node: KnowledgeNode) -> KnowledgeNode:
        old = self._nodes.get(node.id)
        if old:
            # 清理旧标签索引
            for tag in old.tags:
                if tag in self._tag_index and node.id in self._tag_index[tag]:
                    self._tag_index[tag].remove(node.id)
        self._nodes[node.id] = node
        for tag in node.tags:
            self._tag_index.setdefault(tag, []).append(node.id)
        return node

    def remove_node(self, node_id: str) -> bool:
        node = self._nodes.pop(node_id, None)
        if not node:
            return False
        for tag in node.tags:
            if tag in self._tag_index and node_id in self._tag_index[tag]:
                self._tag_index[tag].remove(node_id)
        # 清理相关边
        edges_to_remove = [e.id for e in self._edges.values() if e.source_id == node_id or e.target_id == node_id]
        for eid in edges_to_remove:
            self._edges.pop(eid, None)
        return True

    def list_nodes(
        self,
        tag: Optional[str] = None,
        node_type: Optional[str] = None,
        mastery: Optional[MasteryLevel] = None,
        memory_level: Optional[MemoryLevel] = None,
        limit: int = 100,
    ) -> list[KnowledgeNode]:
        results = list(self._nodes.values())
        if tag:
            ids = set(self._tag_index.get(tag, []))
            results = [n for n in results if n.id in ids]
        if node_type:
            results = [n for n in results if n.type.value == node_type]
        if mastery:
            results = [n for n in results if n.mastery_level == mastery]
        if memory_level:
            results = [n for n in results if n.memory_level == memory_level]
        return results[:limit]

    # ─── 边 CRUD ───

    def add_edge(self, edge: KnowledgeEdge) -> KnowledgeEdge:
        self._edges[edge.id] = edge
        # 同步更新节点的 related_node_ids（简化处理）
        src = self._nodes.get(edge.source_id)
        if src and edge.target_id not in src.related_node_ids:
            src.related_node_ids.append(edge.target_id)
        tgt = self._nodes.get(edge.target_id)
        if tgt and edge.source_id not in tgt.related_node_ids:
            tgt.related_node_ids.append(edge.source_id)
        return edge

    def get_edges(self, node_id: str) -> list[KnowledgeEdge]:
        return [e for e in self._edges.values() if e.source_id == node_id or e.target_id == node_id]

    def remove_edge(self, edge_id: str) -> bool:
        return self._edges.pop(edge_id, None) is not None

    # ─── 关联查询 ───

    def find_related(self, node_id: str, depth: int = 1) -> list[KnowledgeNode]:
        """BFS 查找关联节点。"""
        if node_id not in self._nodes:
            return []
        visited = {node_id}
        queue = [(node_id, 0)]
        results = []
        while queue:
            current, d = queue.pop(0)
            if d >= depth:
                continue
            for edge in self.get_edges(current):
                neighbor = edge.target_id if edge.source_id == current else edge.source_id
                if neighbor not in visited:
                    visited.add(neighbor)
                    node = self._nodes.get(neighbor)
                    if node:
                        results.append(node)
                    queue.append((neighbor, d + 1))
        return results

    def find_by_text_similarity(self, query: str, limit: int = 5) -> list[tuple[KnowledgeNode, float]]:
        """
        极度简化的"相似度"：基于关键词包含匹配。
        后续通过扩展替换为向量召回。
        """
        query_lower = query.lower()
        query_terms = set(query_lower.split())
        scored = []
        for node in self._nodes.values():
            content_lower = node.content.lower()
            tag_text = " ".join(node.tags).lower()
            combined = content_lower + " " + tag_text
            score = sum(1 for term in query_terms if term in combined) / max(len(query_terms), 1)
            if score > 0:
                scored.append((node, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # ─── 序列化 ───

    def to_dict(self) -> dict:
        return {
            "nodes": [n.model_dump() for n in self._nodes.values()],
            "edges": [e.model_dump() for e in self._edges.values()],
        }

    def from_dict(self, data: dict) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._tag_index.clear()
        self._source_index.clear()
        for n_data in data.get("nodes", []):
            node = KnowledgeNode(**n_data)
            self.add_node(node)
        for e_data in data.get("edges", []):
            edge = KnowledgeEdge(**e_data)
            self.add_edge(edge)
