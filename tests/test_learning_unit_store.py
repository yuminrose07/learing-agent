"""LearningUnitStore 单元测试。

覆盖：
- create/get/save 在内存 + 磁盘上的回环
- load_all() 从磁盘恢复
- delete 直接调用 + session 删除回调的级联
- find_active() 在 consolidated / 非 consolidated 混合下的判定
- per-unit asyncio.Lock 的并发序列化最小验证
- P3 单卷不变量：active 存在时再 create 抛 ActiveUnitExistsError
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.file_store import FileStore
from learning_agent.learning_agent.learning_unit_store import (
    ActiveUnitExistsError,
    LearningUnitStore,
)


@pytest.fixture
def file_store(tmp_path: Path) -> FileStore:
    return FileStore(base_dir=str(tmp_path))


class _SessionManagerStub:
    """最小 SessionManager 替身，只暴露 register_delete_callback。"""

    def __init__(self):
        self.callbacks = []

    def register_delete_callback(self, cb):
        self.callbacks.append(cb)

    def trigger_delete(self, session_id: str):
        for cb in list(self.callbacks):
            cb(session_id)


class TestLearningUnitStoreCRUD:
    def test_create_persists_to_memory_and_disk(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(
            session_id="sess-1",
            objective_text="理解 attention 机制",
        )

        assert store.get(unit.id) is unit
        assert unit.objective.text == "理解 attention 机制"
        assert unit.objective.source == "ai_distilled"
        assert unit.phase == "aligning"

        # 磁盘上确实落了文件
        raw = file_store.load_learning_unit(unit.id)
        assert raw is not None
        assert raw["id"] == unit.id
        assert raw["session_id"] == "sess-1"

    def test_create_with_promoted_from_chat_source_ref(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(
            session_id="sess-promoted",
            objective_text="理解 Pydantic v2",
            source="promoted_from_chat",
            source_ref="sess-orig",
        )
        assert unit.objective.source == "promoted_from_chat"
        assert unit.objective.source_ref == "sess-orig"

    def test_save_overwrites_existing_unit(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-2", objective_text="t")

        unit.objective.confirmed = True
        unit.transition_to("absorbing")
        store.save(unit)

        reloaded_store = LearningUnitStore(file_store)
        reloaded = reloaded_store.get(unit.id)
        assert reloaded is not None
        assert reloaded.phase == "absorbing"
        assert reloaded.objective.confirmed is True

    def test_list_returns_all_units(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        u1 = store.create(session_id="s1", objective_text="t1")
        # 把 u1 推进到 consolidated，让 find_active 不报 P3
        u1.transition_to("absorbing")
        u1.transition_to("outputting")
        u1.transition_to("consolidated")
        store.save(u1)
        u2 = store.create(session_id="s2", objective_text="t2")

        ids = {u.id for u in store.list()}
        assert ids == {u1.id, u2.id}

    def test_delete_removes_from_memory_and_disk(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-3", objective_text="t")
        unit_id = unit.id

        assert store.delete(unit_id) is True
        assert store.get(unit_id) is None
        assert file_store.load_learning_unit(unit_id) is None
        # 第二次删返回 False
        assert store.delete(unit_id) is False


class TestLoadAll:
    def test_load_all_restores_units_from_disk(self, file_store: FileStore):
        store1 = LearningUnitStore(file_store)
        unit = store1.create(session_id="sess-load", objective_text="t-load")
        unit.transition_to("absorbing")
        store1.save(unit)

        store2 = LearningUnitStore(file_store)
        reloaded = store2.get(unit.id)
        assert reloaded is not None
        assert reloaded.session_id == "sess-load"
        assert reloaded.phase == "absorbing"

    def test_load_all_with_no_file_store_is_noop(self):
        store = LearningUnitStore(file_store=None)
        assert store.list() == []


class TestActiveUnitInvariant:
    def test_find_active_returns_non_terminal_unit(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-a", objective_text="t")
        assert store.find_active() is unit

    def test_find_active_skips_consolidated(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-a", objective_text="t")
        unit.transition_to("absorbing")
        unit.transition_to("outputting")
        unit.transition_to("consolidated")
        store.save(unit)

        assert store.find_active() is None

    def test_create_raises_when_active_unit_exists(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        first = store.create(session_id="sess-1", objective_text="t1")

        with pytest.raises(ActiveUnitExistsError) as exc_info:
            store.create(session_id="sess-2", objective_text="t2")
        assert exc_info.value.active_unit_id == first.id

    def test_create_allowed_after_consolidation(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        first = store.create(session_id="sess-1", objective_text="t1")
        first.transition_to("absorbing")
        first.transition_to("outputting")
        first.transition_to("consolidated")
        store.save(first)

        second = store.create(session_id="sess-2", objective_text="t2")
        assert second.id != first.id
        assert second.phase == "aligning"


class TestSessionDeleteCascade:
    def test_session_delete_callback_removes_associated_unit(self, file_store: FileStore):
        sm = _SessionManagerStub()
        store = LearningUnitStore(file_store, session_manager=sm)
        unit = store.create(session_id="sess-cascade", objective_text="t")

        assert len(sm.callbacks) == 1
        sm.trigger_delete("sess-cascade")

        assert store.get(unit.id) is None
        assert file_store.load_learning_unit(unit.id) is None

    def test_session_delete_callback_noop_when_no_associated_unit(
        self, file_store: FileStore
    ):
        sm = _SessionManagerStub()
        store = LearningUnitStore(file_store, session_manager=sm)
        unit = store.create(session_id="sess-keep", objective_text="t")

        # 删另一个 session id，当前 unit 不应被牵连
        sm.trigger_delete("sess-unrelated")
        assert store.get(unit.id) is unit


class TestPerUnitLock:
    @pytest.mark.asyncio
    async def test_lock_serializes_concurrent_writers(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-lock", objective_text="t")
        unit.transition_to("absorbing")
        store.save(unit)

        order: list[str] = []

        async def writer(tag: str, delay: float):
            async with store.lock(unit.id):
                order.append(f"{tag}-start")
                await asyncio.sleep(delay)
                order.append(f"{tag}-end")

        await asyncio.gather(writer("a", 0.02), writer("b", 0.0))

        # 两段必须各自完整串行——start/end 不能交错
        assert order in (
            ["a-start", "a-end", "b-start", "b-end"],
            ["b-start", "b-end", "a-start", "a-end"],
        )

    @pytest.mark.asyncio
    async def test_lock_is_per_unit_not_global(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        u1 = store.create(session_id="s1", objective_text="t1")
        u1.transition_to("absorbing")
        u1.transition_to("outputting")
        u1.transition_to("consolidated")
        store.save(u1)
        u2 = store.create(session_id="s2", objective_text="t2")

        lock1 = store.lock(u1.id)
        lock2 = store.lock(u2.id)
        assert lock1 is not lock2

        # 同一 unit 的两次取锁返回同一对象（不会每次新建）
        assert store.lock(u1.id) is lock1
