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
        assert unit.phase == "absorbing"

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
        unit.transition_to("outputting")
        store.save(unit)

        reloaded_store = LearningUnitStore(file_store)
        reloaded = reloaded_store.get(unit.id)
        assert reloaded is not None
        assert reloaded.phase == "outputting"
        assert reloaded.objective.confirmed is True

    def test_list_returns_all_units(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        u1 = store.create(session_id="s1", objective_text="t1")
        # 把 u1 推进到 consolidated，让 find_active 不报 P3
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
        unit.transition_to("outputting")
        store1.save(unit)

        store2 = LearningUnitStore(file_store)
        reloaded = store2.get(unit.id)
        assert reloaded is not None
        assert reloaded.session_id == "sess-load"
        assert reloaded.phase == "outputting"

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
        unit.transition_to("outputting")
        unit.transition_to("consolidated")
        store.save(unit)

        assert store.find_active() is None

    def test_find_active_skips_stopped(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-a", objective_text="t")
        unit.transition_to("stopped")
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
        first.transition_to("outputting")
        first.transition_to("consolidated")
        store.save(first)

        second = store.create(session_id="sess-2", objective_text="t2")
        assert second.id != first.id
        assert second.phase == "absorbing"

    def test_create_allowed_after_stop(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        first = store.create(session_id="sess-1", objective_text="t1")
        first.transition_to("stopped")
        store.save(first)

        second = store.create(session_id="sess-2", objective_text="t2")
        assert second.id != first.id
        assert second.phase == "absorbing"


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


class TestLegacyMigration:
    """B1 投影迁移：旧 ``phase=aligning`` JSON 加载时转为新 schema。

    迁移规则来自 adaptive alignment §8.4：
    - phase=aligning → phase=absorbing, alignment_state=active, objective_status=working
    - aligning_round 字段被丢弃（旧字段已无意义）
    """

    def _write_legacy_unit_json(self, file_store: FileStore, raw: dict) -> str:
        """直接落一份模拟旧 schema 的 JSON 到磁盘，绕过 LearningUnitStore.create。"""
        file_store.save_learning_unit(raw["id"], raw)
        return raw["id"]

    def test_legacy_aligning_phase_projects_to_absorbing(
        self, file_store: FileStore
    ):
        legacy = {
            "id": "lu-legacy-1",
            "session_id": "sess-legacy",
            "objective": {
                "text": "理解旧 schema 的 aligning",
                "source": "ai_distilled",
                "source_ref": None,
                "confirmed": False,
            },
            "phase": "aligning",
            "concept_list": [],
            "tangent_notes": [],
            "aligning_round": 3,  # 已废弃字段
            "teach_session": None,
            "verification_status": None,
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        self._write_legacy_unit_json(file_store, legacy)

        store = LearningUnitStore(file_store)
        reloaded = store.get("lu-legacy-1")

        assert reloaded is not None
        assert reloaded.phase == "absorbing"
        assert reloaded.alignment_state == "active"
        assert reloaded.objective_status == "working"
        # aligning_round 字段已从模型中删除；不应出现在 dump 中
        dumped = reloaded.model_dump()
        assert "aligning_round" not in dumped

    def test_legacy_absorbing_phase_unchanged(self, file_store: FileStore):
        legacy = {
            "id": "lu-legacy-2",
            "session_id": "sess-legacy-2",
            "objective": {
                "text": "已经 absorbing 的旧卷",
                "source": "ai_distilled",
                "source_ref": None,
                "confirmed": True,
            },
            "phase": "absorbing",
            "concept_list": [],
            "tangent_notes": [],
            "aligning_round": 1,
            "teach_session": None,
            "verification_status": None,
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        self._write_legacy_unit_json(file_store, legacy)

        store = LearningUnitStore(file_store)
        reloaded = store.get("lu-legacy-2")

        assert reloaded is not None
        assert reloaded.phase == "absorbing"
        # 新字段默认值兜底
        assert reloaded.alignment_state == "idle"
        assert reloaded.objective_status == "working"
        # §9.3 #2/#3 限流计数器也要有默认 0 —— 旧 JSON 完全没有这些键
        assert reloaded.clarification_count == 0
        assert reloaded.suggestion_count == 0
        assert reloaded.nag_cooldown_remaining == 0

    def test_legacy_consolidated_phase_unchanged(self, file_store: FileStore):
        legacy = {
            "id": "lu-legacy-3",
            "session_id": "sess-legacy-3",
            "objective": {
                "text": "终态旧卷",
                "source": "ai_distilled",
                "source_ref": None,
                "confirmed": True,
            },
            "phase": "consolidated",
            "concept_list": [],
            "tangent_notes": [],
            "aligning_round": 2,
            "teach_session": None,
            "verification_status": "passed",
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        self._write_legacy_unit_json(file_store, legacy)

        store = LearningUnitStore(file_store)
        reloaded = store.get("lu-legacy-3")

        assert reloaded is not None
        assert reloaded.phase == "consolidated"
        assert reloaded.is_terminal()


class TestFeedbackCardRoundTrip:
    """B5 E1：consolidated 反馈卡字段 round-trip 与默认行为。"""

    def test_feedback_card_defaults_to_none(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-fb", objective_text="t")
        assert unit.feedback_card is None
        # 旧 JSON 缺该键也应兜底
        raw = file_store.load_learning_unit(unit.id)
        assert raw.get("feedback_card") is None

    def test_feedback_card_round_trips_after_write(self, file_store: FileStore):
        from learning_agent.ai.learning_unit import TeachFeedbackCard

        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-fb2", objective_text="t")
        unit.feedback_card = TeachFeedbackCard(
            mastered=["BaseModel", "field validator"],
            gaps=["ConfigDict 高阶用法"],
            next_topic_suggestion="可以接着学 model_validator 的多字段联动",
        )
        store.save(unit)

        fresh = LearningUnitStore(file_store)
        reloaded = fresh.get(unit.id)
        assert reloaded is not None
        assert reloaded.feedback_card is not None
        assert reloaded.feedback_card.mastered == ["BaseModel", "field validator"]
        assert reloaded.feedback_card.gaps == ["ConfigDict 高阶用法"]
        assert "model_validator" in reloaded.feedback_card.next_topic_suggestion


class TestAlignmentCountersRoundTrip:
    """B4 D1：§9.3 #2/#3 新增字段必须能落盘并 reload。"""

    def test_suggestion_count_and_cooldown_round_trip(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-counters", objective_text="t")
        unit.suggestion_count = 2
        unit.nag_cooldown_remaining = 3
        store.save(unit)

        # 清缓存重新读盘
        fresh = LearningUnitStore(file_store)
        reloaded = fresh.get(unit.id)
        assert reloaded is not None
        assert reloaded.suggestion_count == 2
        assert reloaded.nag_cooldown_remaining == 3


class TestForgeStateRoundTrip:
    """Phase 1A：forge_stage / temperature_state 默认值、落盘 round-trip、旧 JSON 兼容。"""

    def test_new_unit_defaults(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-forge", objective_text="t")
        assert unit.forge_stage == "entry"
        assert unit.temperature_state == "steady"
        # 默认值也应写进磁盘
        raw = file_store.load_learning_unit(unit.id)
        assert raw["forge_stage"] == "entry"
        assert raw["temperature_state"] == "steady"

    def test_forge_stage_round_trips_after_write(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-forge2", objective_text="t")
        unit.forge_stage = "collision"
        store.save(unit)

        fresh = LearningUnitStore(file_store)
        reloaded = fresh.get(unit.id)
        assert reloaded is not None
        assert reloaded.forge_stage == "collision"
        assert reloaded.temperature_state == "steady"

    def test_legacy_json_without_forge_fields_defaults(self, file_store: FileStore):
        # 旧卷完全没有 forge_stage / temperature_state 键 → Pydantic 兜底默认值
        legacy = {
            "id": "lu-legacy-forge",
            "session_id": "sess-legacy-forge",
            "objective": {
                "text": "旧卷无铸造字段",
                "source": "ai_distilled",
                "source_ref": None,
                "confirmed": False,
            },
            "phase": "absorbing",
            "concept_list": [],
            "tangent_notes": [],
            "teach_session": None,
            "verification_status": None,
            "created_at": "2026-05-01T00:00:00+00:00",
            "updated_at": "2026-05-01T00:00:00+00:00",
        }
        file_store.save_learning_unit(legacy["id"], legacy)

        store = LearningUnitStore(file_store)
        reloaded = store.get("lu-legacy-forge")
        assert reloaded is not None
        assert reloaded.forge_stage == "entry"
        assert reloaded.temperature_state == "steady"


class TestPerUnitLock:
    @pytest.mark.asyncio
    async def test_lock_serializes_concurrent_writers(self, file_store: FileStore):
        store = LearningUnitStore(file_store)
        unit = store.create(session_id="sess-lock", objective_text="t")
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
        u1.transition_to("outputting")
        u1.transition_to("consolidated")
        store.save(u1)
        u2 = store.create(session_id="s2", objective_text="t2")

        lock1 = store.lock(u1.id)
        lock2 = store.lock(u2.id)
        assert lock1 is not lock2

        # 同一 unit 的两次取锁返回同一对象（不会每次新建）
        assert store.lock(u1.id) is lock1
