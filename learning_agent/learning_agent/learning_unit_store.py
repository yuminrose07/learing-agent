"""学习卷持久化 store。

对应设计文档 [design-modes-refactor-chat-vs-learning-unit.md](../../docs/design/design-modes-refactor-chat-vs-learning-unit.md)
§3.8 / §四 后端契约。

- 每卷一个 JSON 文件 `learning_units/{id}.json`；phase 跃迁 / concept 写入
  / TEACH 提交后整棵 dump 覆写。
- 不复用 session event log——学习卷状态独立于 session 消息流，简单覆写就够。
- per-unit ``asyncio.Lock`` 给 tail-task 用，避免抽取与 phase 推进 RMW 竞态。
- 通过 ``SessionManager.register_delete_callback`` 实现 session 删除时的级联清理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from learning_agent.ai.file_store import FileStore
from learning_agent.ai.learning_unit import LearningUnit, UnitObjective

logger = logging.getLogger(__name__)


class ActiveUnitExistsError(Exception):
    """已有一个非 consolidated 学习卷在跑（P3 单卷不变量）。"""

    def __init__(self, active_unit_id: str):
        super().__init__(
            f"Active learning unit already exists: {active_unit_id}"
        )
        self.active_unit_id = active_unit_id


class LearningUnitStore:
    """内存缓存 + JSON 文件持久化的学习卷 store。"""

    def __init__(
        self,
        file_store: Optional[FileStore],
        *,
        session_manager: object | None = None,
    ):
        self._file_store = file_store
        self._units: dict[str, LearningUnit] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        if session_manager is not None and hasattr(
            session_manager, "register_delete_callback"
        ):
            session_manager.register_delete_callback(self._on_session_deleted)
        self.load_all()

    # ─── 加载 ───

    def load_all(self) -> None:
        if self._file_store is None:
            return
        for unit_id in self._file_store.list_learning_units():
            try:
                raw = self._file_store.load_learning_unit(unit_id)
                if raw is None:
                    continue
                raw = _project_legacy_unit(raw)
                # Phase 1B 兼容性：旧 1A unit 可能已在 collision/forge/fixed/cooling
                # 且没有 orientation_context。加载时直接放行，不补生成、不阻塞主流程；
                # 只有新建 entry unit 会在 Product 编排阶段进入完整 1B 路径。
                unit = LearningUnit.model_validate(raw)
                self._units[unit.id] = unit
            except Exception:
                logger.exception(
                    f"[LearningUnitStore] Failed to load unit {unit_id}; skipped"
                )

    # ─── CRUD ───

    def create(
        self,
        *,
        session_id: str,
        objective_text: str,
        source: str = "ai_distilled",
        source_ref: Optional[str] = None,
    ) -> LearningUnit:
        active = self.find_active()
        if active is not None:
            raise ActiveUnitExistsError(active.id)

        unit = LearningUnit(
            session_id=session_id,
            objective=UnitObjective(
                text=objective_text,
                source=source,
                source_ref=source_ref,
            ),
        )
        self._units[unit.id] = unit
        self._persist(unit)
        return unit

    def get(self, unit_id: str) -> Optional[LearningUnit]:
        return self._units.get(unit_id)

    def list(self) -> list[LearningUnit]:
        return list(self._units.values())

    def save(self, unit: LearningUnit) -> None:
        self._units[unit.id] = unit
        self._persist(unit)

    def delete(self, unit_id: str) -> bool:
        existed = self._units.pop(unit_id, None) is not None
        self._locks.pop(unit_id, None)
        if self._file_store is not None:
            self._file_store.delete_learning_unit(unit_id)
        return existed

    # ─── 不变量查询 ───

    def find_active(self) -> Optional[LearningUnit]:
        """返回任意一个非 consolidated 的学习卷；用于 P3 单卷强制。"""
        for unit in self._units.values():
            if not unit.is_terminal():
                return unit
        return None

    def find_by_session(self, session_id: str) -> Optional[LearningUnit]:
        for unit in self._units.values():
            if unit.session_id == session_id:
                return unit
        return None

    # ─── 并发协调 ───

    def lock(self, unit_id: str) -> asyncio.Lock:
        """取 / 建该 unit 的 RMW 锁。"""
        lock = self._locks.get(unit_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[unit_id] = lock
        return lock

    # ─── 内部 ───

    def _persist(self, unit: LearningUnit) -> None:
        if self._file_store is None:
            return
        try:
            self._file_store.save_learning_unit(
                unit.id, unit.model_dump(mode="json")
            )
        except Exception:
            logger.exception(
                f"[LearningUnitStore] Failed to persist unit {unit.id}"
            )

    def _on_session_deleted(self, session_id: str) -> None:
        """SessionManager 删除回调：连带删除该 session 关联的学习卷。"""
        unit = self.find_by_session(session_id)
        if unit is None:
            return
        logger.info(
            f"[LearningUnitStore] Cascade-deleting unit {unit.id} "
            f"with session {session_id}"
        )
        self.delete(unit.id)


__all__ = ["LearningUnitStore", "ActiveUnitExistsError"]


def _project_legacy_unit(raw: dict) -> dict:
    """旧 JSON 数据加载时的投影迁移（adaptive alignment §8.4）。

    旧 schema 里 ``phase=aligning`` 表示"学习卷起步阶段，必须先走 ASK"。
    新 schema 里这条主链已被移除，对齐降级为 ``alignment_state`` 旁路。

    迁移规则：
    - ``phase == "aligning"`` → ``phase = "absorbing"``,
      ``alignment_state = "active"``, ``objective_status = "working"``
    - ``aligning_round`` 字段被丢弃（已无意义）。
    - 其他 phase 不动；缺失的新字段交给 Pydantic 默认值兜底。

    这是纯读侧投影：不回写历史 events，原地翻译后由 ``save`` 用新 schema 覆写。
    """
    if not isinstance(raw, dict):
        return raw
    phase = raw.get("phase")
    if phase == "aligning":
        raw = dict(raw)  # 不污染调用方传入对象
        raw["phase"] = "absorbing"
        raw.setdefault("alignment_state", "active")
        raw.setdefault("objective_status", "working")
    # 旧字段无意义，直接丢弃（Pydantic 默认会忽略，但显式更稳）
    raw.pop("aligning_round", None)
    return raw
