"""
Learning-Agent 主入口。

承载 Product/Application 层的 ``LearningAgentSystem``，负责系统装配与产品级编排。
Interface 层的交互式 CLI 已下沉到 ``cli.py``；本文件末尾仅保留 ``__main__``
启动器，按 ``--web`` 在 web server 与 ``cli.interactive_cli`` 之间路由。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.learning_agent.config import Config
from learning_agent.agent.event_bus import EventBus
from learning_agent.learning_agent.extension_manager import ExtensionManager
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.unresolved_failure_logger import UnresolvedFailureLogger
from learning_agent.learning_agent.extensions.built_in import create_builtin_extensions
from learning_agent.learning_agent.tool_registry import ToolRegistry
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.ai import (
    AgentMode,
    ChatChunk,
    ChatMessage,
    ChatParams,
    Event,
    KnowledgeNode,
    LearningObjective,
    LearningSession,
    LearningUnit,
    LearningUnitPhase,
    MessageRole,
)
from learning_agent.ai.file_store import FileStore
from learning_agent.ai.learning_unit import (
    TeachFeedbackCard,
    TeachQuestion,
    TeachSession,
)
from learning_agent.ai.openai_provider import OpenAIProvider
from learning_agent.learning_agent.alignment_policy import (
    AlignmentDecision,
    COOLDOWN_AFTER_ACCEPT_ASSUMPTION,
    apply_alignment_decision,
    build_absorbing_opening_addendum,
    should_run_alignment,
)
from learning_agent.learning_agent.answer_quality import (
    PARTIAL_AFTER_ERROR_MAX_LEN as _PARTIAL_AFTER_ERROR_MAX_LEN,
    SAFE_FALLBACK_ANSWER as _SAFE_FALLBACK_ANSWER,
    build_rescue_answer,
    classify_incomplete_answer as _classify_incomplete_answer,
    final_answer_verdict as _final_answer_verdict,
)
from learning_agent.learning_agent.compaction import CompactionCoordinator, CompactionPlan
from learning_agent.learning_agent.companion_policy import (
    CompanionTurnPlan,
    prepare_companion_turn,
)
from learning_agent.learning_agent.concept_extractor import ConceptExtractor
from learning_agent.learning_agent.forge_policy import (
    maybe_advance_forge_stage,
    prepare_forge_stage_metadata,
)
from learning_agent.learning_agent.learning_unit_store import (
    ActiveUnitExistsError,
    LearningUnitStore,
)
from learning_agent.learning_agent.learning_unit_metrics import (
    LearningUnitMetricsSummary,
    calculate_metrics_summary,
)
from learning_agent.learning_agent.teach_generator import TeachQuestionGenerator
from learning_agent.learning_agent.teach_judge import TeachJudge
from learning_agent.learning_agent.teach_flow import (
    TeachFlow,
    finalize_consolidation as _finalize_consolidation_impl,
)
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    TurnExecutionProfile,
    build_turn_profile,
)
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_events import SessionEventType
from learning_agent.learning_agent.session_migration import migrate_all_sessions
from learning_agent.learning_agent.session_manager import SessionManager

logger = logging.getLogger(__name__)


class SessionNotFoundError(LookupError):
    """指定 session_id 不存在。与其他业务 ValueError 区分，避免
    web 层一刀切误把"学习卷已 consolidated"映射成"session 不存在"。"""


class LearningAgentSystem:
    """
    Product/Application 层主入口。

    负责系统装配、生命周期管理以及对外暴露稳定的产品级 API。
    它编排 Session、Memory、Extension 与 Agent Runtime，但不持有
    单 session 的运行时私有状态。
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._setup_logging()

        # Layer 4: Infrastructure
        self.file_store = FileStore(self.config.data_dir)
        self.session_event_store = SessionEventStore(self.file_store)
        # 工具调用成功导向架构：不可恢复失败的内部账本（仅用于内部诊断和后续优化）
        from pathlib import Path
        self.unresolved_failure_logger = UnresolvedFailureLogger(
            Path(self.config.data_dir) / "unresolved_failures.jsonl"
        )
        # Layer 3: Agent Runtime cross-cutting support
        self.event_bus = EventBus()
        self.hook_system = HookSystem()
        self.tool_registry = ToolRegistry()
        self.observability = ObservabilityCollector()
        # Layer 2: Product/Application subdomains
        self.extension_manager = ExtensionManager(
            self.hook_system,
            self.event_bus,
            self.tool_registry,
        )
        self.memory_manager = MemoryManager()
        self.session_manager = SessionManager(
            event_bus=self.event_bus,
            file_store=self.file_store,
            event_store=self.session_event_store,
        )
        self.learning_unit_store = LearningUnitStore(
            self.file_store,
            session_manager=self.session_manager,
        )
        self.provider: Optional[OpenAIProvider] = None
        self.agent_loop: Optional[AgentLoop] = None
        self.compaction_coordinator: Optional[CompactionCoordinator] = None
        self.concept_extractor: Optional[ConceptExtractor] = None
        self.teach_generator: Optional[TeachQuestionGenerator] = None
        self.teach_judge: Optional[TeachJudge] = None

        self._current_session = None
        self._current_objective = None
        self._compaction_turn_counts: dict[str, int] = {}
        self._extraction_tasks: set[asyncio.Task] = set()

    def _setup_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    async def initialize(self) -> None:
        """初始化系统并接通四层主链。"""
        logger.info("[System] Initializing Learning-Agent...")

        # 验证配置
        errors = self.config.validate()
        if errors:
            for e in errors:
                logger.error(f"[System] Config error: {e}")
            raise RuntimeError("Configuration validation failed")

        # 初始化 Provider
        self.provider = OpenAIProvider(self.config.provider_config)

        # 加载持久化数据
        await self._load_state()

        # 注册内置扩展
        for ext in create_builtin_extensions(config=self.config.to_dict()):
            self.extension_manager.register(ext)

        # 激活所有扩展
        await self.extension_manager.activate_all()

        # Layer 3: Agent Runtime
        # 创建工具执行服务（Product 层实现）
        from learning_agent.learning_agent.tool_execution_service import ToolExecutionServiceImpl
        tool_execution_service = ToolExecutionServiceImpl(self.tool_registry)
        
        self.agent_loop = AgentLoop(
            provider=self.provider,
            memory_service=self.memory_manager,
            session_store=self.session_manager,
            hook_system=self.hook_system,
            event_bus=self.event_bus,
            tool_execution_service=tool_execution_service,
            observability=self.observability,
            max_react_turns=10,
            event_writer=self.session_event_store,
            unresolved_failure_logger=self.unresolved_failure_logger,
        )
        self.compaction_coordinator = CompactionCoordinator(
            self.session_manager,
            max_context_tokens=self.provider.get_max_context_length(),
            summary_executor=self._execute_compact_summary,
        )
        self.concept_extractor = ConceptExtractor(
            provider=self.provider,
            model_name=self.config.concept_extractor_model,
            threshold=self.config.concept_extraction_threshold,
        )
        self.teach_generator = TeachQuestionGenerator(
            provider=self.provider,
            model_name=self.config.teach_generator_model,
        )
        self.teach_judge = TeachJudge(
            provider=self.provider,
            model_name=self.config.teach_judge_model,
        )
        # 状态快照只用于观测，不再触发持久化层 snapshot/delta compaction
        self.event_bus.subscribe("agent.stateSnapshot", self._on_state_snapshot)

        logger.info("[System] Initialization complete.")

    async def shutdown(self) -> None:
        """优雅关闭：保存状态、停用扩展。"""
        logger.info("[System] Shutting down...")
        await self._save_state()
        await self.extension_manager.deactivate_all()
        logger.info("[System] Shutdown complete.")

    async def _load_state(self) -> None:
        """加载产品层持久化状态，从 session event log replay 会话。"""
        kg_data = self.file_store.load_knowledge_graph()
        if kg_data:
            self.memory_manager.kg.from_dict(kg_data)
            logger.info(f"[System] Loaded {len(self.memory_manager.kg._nodes)} knowledge nodes")

        migration_report = migrate_all_sessions(self.file_store)
        if migration_report.migrated:
            logger.info("[System] Migrated legacy sessions: %s", ", ".join(migration_report.migrated))
        if migration_report.failed:
            logger.warning("[System] Legacy session migration failures: %s", migration_report.failed)

        for sid in self.file_store.list_sessions():
            events = self.session_event_store.read_events(sid)
            if not events:
                continue
            snapshot = self.session_manager.get_agent_snapshot(sid)
            if snapshot.corrupt_events:
                logger.warning("[System] Session event log has corrupt events: %s", snapshot.corrupt_events)
            self.session_manager.add_snapshot(snapshot)

        self._backfill_learning_unit_session_links()

    async def _save_state(self) -> None:
        """保存非 session-event-log 的产品状态。Session 事实源已实时 append。"""
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        logger.info("[System] Durable state saved. Session event logs are append-only.")

    def _backfill_learning_unit_session_links(self) -> None:
        """为旧数据追加 session <-> learning_unit 绑定事件，不回写既有事实。"""
        for unit in self.learning_unit_store.list():
            session = self.session_manager.get_session(unit.session_id)
            if session is None:
                continue
            if session.learning_unit_id == unit.id:
                continue
            logger.info(
                "[System] Backfilling learning unit link: session=%s unit=%s",
                session.id,
                unit.id,
            )
            self.session_manager.bind_learning_unit(session.id, unit.id)

    async def _on_state_snapshot(self, event: Event) -> None:
        """运行时快照进入 observability，不再触发持久化层 compact。"""
        snapshot = event.payload
        session_id = snapshot.get("session_id")
        turn_count = snapshot.get("turn_count", 0)
        if session_id:
            self._compaction_turn_counts[session_id] = max(
                self._compaction_turn_counts.get(session_id, 0),
                turn_count,
            )

    # ─── Product/Application facade ───

    async def create_objective(self, title: str, description: Optional[str] = None) -> LearningObjective:
        obj = LearningObjective(title=title, description=description)
        self._current_objective = obj
        self.file_store.save_objective(obj.id, obj.model_dump())
        logger.info(f"[System] Created objective: {obj.id} - {title}")
        return obj

    def list_objectives(self) -> list[LearningObjective]:
        objectives = []
        for objective_id in self.file_store.list_objectives():
            data = self.file_store.load_objective(objective_id)
            if data:
                objectives.append(LearningObjective(**data))
        return objectives

    def get_objective(self, objective_id: str) -> Optional[LearningObjective]:
        data = self.file_store.load_objective(objective_id)
        if not data:
            return None
        return LearningObjective(**data)

    def create_session(
        self,
        objective_id: Optional[str] = None,
        title: Optional[str] = None,
        set_current: bool = False,
    ) -> LearningSession:
        session = self.session_manager.create_session(
            objective_id=objective_id,
            title=title,
        )
        if set_current:
            self._current_session = session
        return session

    def list_sessions(self) -> list[LearningSession]:
        return self.session_manager.list_sessions()

    def get_ui_messages(self, session_id: str) -> list[dict[str, Any]]:
        if self.get_session(session_id) is None:
            return []
        return [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "status": message.status,
                "metadata": message.metadata,
            }
            for message in self.session_manager.build_ui_messages(session_id)
        ]

    def get_session(
        self,
        session_id: Optional[str],
        *,
        load_if_missing: bool = True,
    ) -> Optional[LearningSession]:
        if not session_id:
            return None

        session = self.session_manager.get_session(session_id)
        if session is not None or not load_if_missing:
            return session

        events = self.session_event_store.read_events(session_id)
        if not events:
            return None

        snapshot = self.session_manager.get_agent_snapshot(session_id)
        return self.session_manager.add_snapshot(snapshot)

    def has_session(self, session_id: str) -> bool:
        return self.get_session(session_id) is not None

    def update_session_title(
        self,
        session_id: str,
        title: Optional[str],
    ) -> Optional[LearningSession]:
        return self.session_manager.update_session_title(session_id, title)

    def save_session(self, session_id: str) -> bool:
        """兼容入口。Session 主事实源实时写入 event log，不再保存 snapshot。"""
        return self.get_session(session_id) is not None

    def update_session_mode(
        self,
        session_id: str,
        mode: AgentMode,
    ) -> LearningSession:
        return self.session_manager.switch_session_mode(session_id, mode)

    async def save_state(self) -> None:
        await self._save_state()

    async def delete_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False

        self.reset_session_runtime(session_id)
        deleted = self.session_manager.delete_session(session_id)
        if not deleted:
            return False

        self.file_store.delete(f"sessions/{session_id}.json")
        self.file_store.delete(f"sessions/{session_id}.jsonl")
        self.file_store.delete(f"sessions/{session_id}.events.jsonl")
        self.file_store.delete(f"memory/session_state/{session_id}.json")
        self.file_store.delete(f"memory/compact/{session_id}.meta.json")
        self.file_store.delete(f"memory/compact/{session_id}.summary.txt")
        if self._current_session and self._current_session.id == session_id:
            self._current_session = None
        return True

    def get_session_runtime_summary(self, session_id: str) -> Optional[dict[str, Any]]:
        if self.agent_loop is None:
            return None
        return self.agent_loop.get_runtime_summary(session_id)

    def reset_session_runtime(self, session_id: str) -> Optional[dict[str, Any]]:
        if self.agent_loop is None or self.get_session(session_id) is None:
            return None
        return self.agent_loop.clear_runtime(session_id)

    def clear_all_runtimes(self) -> None:
        if self.agent_loop is not None:
            self.agent_loop.clear_all_runtimes()

    def get_runtime_overview(self) -> dict[str, Any]:
        runtimes = []
        if self.agent_loop is not None:
            runtimes = self.agent_loop.list_runtime_summaries()
        for runtime in runtimes:
            session = self.get_session(runtime["session_id"])
            runtime["mode"] = session.mode.value if session else AgentMode.CHAT.value
        return {
            "active_runtime_count": len(runtimes),
            "total_session_count": len(self.list_sessions()),
            "runtimes": runtimes,
        }

    async def confirm_knowledge_candidate(
        self,
        node_id: str,
        source: str = "user",
    ) -> Optional[KnowledgeNode]:
        promoted = self.memory_manager.confirm_l1_candidate(node_id, auto_confirm=True)
        if promoted is None:
            return None

        await self.event_bus.publish(
            Event(
                type="knowledge.confirmed",
                payload={"node_id": node_id},
                source=source,
            )
        )
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        return promoted

    async def _execute_compact_summary(self, prompt_text: str) -> str:
        if self.provider is None:
            raise RuntimeError("Provider is not initialized")
        chunk = await self.provider.chat(
            ChatParams(
                model=self.provider.default_model,
                messages=[ChatMessage(role=MessageRole.USER, content=prompt_text)],
                temperature=0.1,
                stream=False,
                max_tokens=4096,
            )
        )
        return chunk.content

    def get_learning_unit(self, unit_id: str) -> Optional[LearningUnit]:
        """按 id 取出学习卷；委托给 LearningUnitStore。"""
        return self.learning_unit_store.get(unit_id)

    def list_learning_units(self) -> list[LearningUnit]:
        return self.learning_unit_store.list()

    def _emit_unit_event(
        self,
        unit: LearningUnit,
        event_type: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """M1：把学习卷产品事件写进 ``sessions/<id>.events.jsonl``。

        Payload 最小集（adaptive alignment §12.2）：
        ``{learning_unit_id, phase, alignment_state, objective_status,
        alignment_reason, clarification_count}``。

        失败仅记日志：事件用于观测/指标，不应阻塞产品主流。``extra`` 用于
        给特定事件补字段（如 PHASE_CHANGED 的 ``from``/``to``，CONSOLIDATED
        的 ``verification_status``）。
        """
        store = getattr(self, "session_event_store", None)
        if store is None:
            return
        payload: dict[str, Any] = {
            "learning_unit_id": unit.id,
            "phase": unit.phase,
            "alignment_state": unit.alignment_state,
            "objective_status": unit.objective_status,
            "alignment_reason": unit.alignment_reason or "",
            "clarification_count": unit.clarification_count,
        }
        if extra:
            payload.update(extra)
        try:
            store.append_event(unit.session_id, event_type, payload=payload)
            if event_type in {
                SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                SessionEventType.LEARNING_UNIT_STOPPED,
            }:
                self._emit_companion_recovery_suggestion(unit, source_event=event_type)
        except Exception:
            logger.exception(
                f"[System] Failed to emit {event_type} for unit {unit.id}"
            )

    def _emit_companion_recovery_suggestion(
        self,
        unit: LearningUnit,
        *,
        source_event: str,
    ) -> None:
        store = getattr(self, "session_event_store", None)
        if store is None:
            return
        try:
            store.append_event(
                unit.session_id,
                SessionEventType.COMPANION_RECOVERY_SUGGESTED,
                payload={
                    "learning_unit_id": unit.id,
                    "source_event": source_event,
                    "suggested_style": "warm_girlfriend",
                    "reason": "study_recovery",
                },
            )
        except Exception:
            logger.exception(
                "[System] Failed to emit companion recovery suggestion for unit %s",
                unit.id,
            )

    def _emit_companion_event(
        self,
        session: LearningSession,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        store = getattr(self, "session_event_store", None)
        if store is None:
            return
        try:
            store.append_event(session.id, event_type, payload=payload)
        except Exception:
            logger.exception(
                "[System] Failed to emit %s for session %s",
                event_type,
                session.id,
            )

    def create_learning_unit(
        self,
        *,
        seed_text: str,
        source: str = "ai_distilled",
        source_ref: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[LearningSession, LearningUnit]:
        """创建一个新的学习卷及其专属会话。

        命中 P3 单卷不变量（已有非 consolidated 卷）时抛 ActiveUnitExistsError，
        由路由层翻译成 409。
        """
        session = self.session_manager.create_session(title=title or seed_text[:48])
        try:
            unit = self.learning_unit_store.create(
                session_id=session.id,
                objective_text=seed_text,
                source=source,
                source_ref=source_ref,
            )
        except ActiveUnitExistsError:
            # 回滚刚创建的空 session，避免泄漏
            self.session_manager.delete_session(session.id)
            raise

        bind_learning_unit = getattr(self.session_manager, "bind_learning_unit", None)
        if callable(bind_learning_unit):
            bind_learning_unit(session.id, unit.id)
        session.learning_unit_id = unit.id
        session.mode = AgentMode.CHAT
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_CREATED,
            extra={"source": source, "seed_text": seed_text[:200]},
        )
        return session, unit

    def confirm_learning_unit_objective(self, unit_id: str) -> LearningUnit:
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        unit.objective.confirmed = True
        unit.objective_status = "confirmed"
        self.learning_unit_store.save(unit)
        return unit

    def stop_learning_unit(
        self,
        unit_id: str,
        *,
        reason: str = "user_stopped",
    ) -> LearningUnit:
        """用户显式结束当前研习卷：进入 stopped 终态并释放单卷互斥。

        ``stopped`` 与 ``consolidated`` 都是 terminal，但语义不同：
        - consolidated：完成验收，计入完成率。
        - stopped：用户选择"先学到这里"，保留历史，不计入完成率。
        """
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if unit.phase in {"stopped", "consolidated"}:
            return unit

        prev = unit.phase
        safe_reason = (reason or "user_stopped").strip() or "user_stopped"
        unit.transition_to("stopped")
        unit.stop_reason = safe_reason
        unit.stopped_at = datetime.now(timezone.utc)
        unit.alignment_state = "skipped"
        unit.assumption_note = ""
        self.learning_unit_store.save(unit)
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
            extra={"from": prev, "to": "stopped", "reason": safe_reason},
        )
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_STOPPED,
            extra={
                "from": prev,
                "stop_reason": safe_reason,
                "stopped_at": unit.stopped_at.isoformat(),
            },
        )
        return unit

    async def advance_learning_unit(
        self,
        unit_id: str,
        target_phase: LearningUnitPhase,
    ) -> LearningUnit:
        """推进卷到目标阶段；进入 ``outputting`` 时同步生成 ``TeachSession``。

        B5 E4：在 ``transition_to`` 之前 eager 调用 ``TeachQuestionGenerator``，
        让前端拿到的卷一上手就带题目。生成失败（0 题）则视为"无可评估概念"，
        直接 ``verification_status="skipped"`` 并跳过 outputting 直接 consolidate。

        M1：每次实际 phase 变更触发 ``LEARNING_UNIT_PHASE_CHANGED``；
        进入 outputting/consolidated 同时分别触发 ``TEACH_ENTERED`` /
        ``CONSOLIDATED`` 标记事件（便于指标查询不必扫 phase 变更对）。
        """
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)

        if target_phase == "outputting" and unit.phase == "absorbing":
            await self._start_teach_session(unit)
            if unit.verification_status == "skipped":
                # 没有可评估的概念 → 跨过 outputting 收束（不能直接跳，
                # 状态机要求 absorbing→outputting→consolidated）。
                prev = unit.phase
                unit.transition_to("outputting")
                self._emit_unit_event(
                    unit,
                    SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
                    extra={"from": prev, "to": "outputting", "skipped_teach": True},
                )
                self._emit_unit_event(
                    unit,
                    SessionEventType.LEARNING_UNIT_TEACH_ENTERED,
                    extra={"skipped": True, "question_total": 0},
                )
                self._finalize_consolidation(unit)
                unit.transition_to("consolidated")
                self.learning_unit_store.save(unit)
                self._emit_unit_event(
                    unit,
                    SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
                    extra={"from": "outputting", "to": "consolidated"},
                )
                self._emit_unit_event(
                    unit,
                    SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                    extra={
                        "verification_status": unit.verification_status,
                        "mastered_count": (
                            len(unit.feedback_card.mastered)
                            if unit.feedback_card else 0
                        ),
                        "gaps_count": (
                            len(unit.feedback_card.gaps)
                            if unit.feedback_card else 0
                        ),
                    },
                )
                return unit

        prev = unit.phase
        unit.transition_to(target_phase)  # 非法过渡抛 ValueError
        self.learning_unit_store.save(unit)
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_PHASE_CHANGED,
            extra={"from": prev, "to": target_phase},
        )
        if target_phase == "outputting":
            self._emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_TEACH_ENTERED,
                extra={
                    "question_total": (
                        len(unit.teach_session.questions)
                        if unit.teach_session is not None else 0
                    ),
                },
            )
        elif target_phase == "consolidated":
            self._emit_unit_event(
                unit,
                SessionEventType.LEARNING_UNIT_CONSOLIDATED,
                extra={
                    "verification_status": unit.verification_status,
                    "mastered_count": (
                        len(unit.feedback_card.mastered)
                        if unit.feedback_card else 0
                    ),
                    "gaps_count": (
                        len(unit.feedback_card.gaps)
                        if unit.feedback_card else 0
                    ),
                },
            )
        return unit

    async def _start_teach_session(self, unit: LearningUnit) -> None:
        """eager 生成本卷的 TeachSession；调用方负责后续 transition + save。

        - generator 返回空列表 → 标 ``verification_status="skipped"``，调用方
          应直接 consolidate。
        - 非空 → 挂上 ``unit.teach_session``，state=``prompted``，
          ``current_index=0``。
        """
        if self.teach_generator is None:
            # 服务未初始化：保守降级为 skipped
            unit.verification_status = "skipped"
            return
        try:
            questions = await self.teach_generator.generate(
                objective_text=unit.objective.text,
                concepts=unit.concept_list,
            )
        except Exception:
            logger.exception(
                f"[System] Teach generation failed for unit {unit.id}; "
                "falling back to skipped"
            )
            questions = []
        if not questions:
            unit.verification_status = "skipped"
            return
        unit.teach_session = TeachSession(
            questions=questions,
            current_index=0,
            state="prompted",
        )

    def _finalize_consolidation(self, unit: LearningUnit) -> None:
        """委托给 teach_flow.finalize_consolidation（纯函数）。

        保留方法名是为了 advance_learning_unit 内部仍通过 self.xxx 调用，
        同时让 test_learning_unit_acceptance 等测试通过同样的入口生效。
        """
        _finalize_consolidation_impl(unit)

    async def request_alignment(self, unit_id: str) -> LearningUnit:
        """用户主动点"帮我收窄"：把卷拉成 (active + user_request)，下一轮走 ASK。

        adaptive alignment §6.3。绕过 §9.3 #1 限流（用户显式请求不计入系统主动澄清额度）。
        必须处于 ``absorbing``；outputting/consolidated 阶段没有"对齐"语义。
        """
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if unit.phase != "absorbing":
            raise ValueError(
                f"Cannot request alignment in phase {unit.phase!r}; "
                "alignment only applies during absorbing."
            )
        async with self.learning_unit_store.lock(unit_id):
            latest = self.learning_unit_store.get(unit_id) or unit
            latest.alignment_state = "active"
            latest.alignment_reason = "user_request"
            latest.assumption_note = ""
            latest.last_alignment_at = datetime.now(timezone.utc)
            self.learning_unit_store.save(latest)
            self._emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_ALIGNMENT_STARTED,
                extra={"trigger": "user_request"},
            )
            return latest

    async def accept_assumption(self, unit_id: str) -> LearningUnit:
        """用户点"先按这个学"：消除建议条，进入 N 轮冷静期。

        adaptive alignment §6.3 / §9.3 #3。状态置 ``skipped``，
        ``nag_cooldown_remaining`` 重置为 ``COOLDOWN_AFTER_ACCEPT_ASSUMPTION``。
        """
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if unit.phase != "absorbing":
            raise ValueError(
                f"Cannot accept assumption in phase {unit.phase!r}; "
                "only meaningful during absorbing."
            )
        async with self.learning_unit_store.lock(unit_id):
            latest = self.learning_unit_store.get(unit_id) or unit
            latest.alignment_state = "skipped"
            latest.nag_cooldown_remaining = COOLDOWN_AFTER_ACCEPT_ASSUMPTION
            latest.last_alignment_at = datetime.now(timezone.utc)
            self.learning_unit_store.save(latest)
            self._emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_ASSUMPTION_ACCEPTED,
                extra={"cooldown": latest.nag_cooldown_remaining},
            )
            self._emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_ALIGNMENT_SKIPPED,
                extra={"trigger": "user_accept_assumption"},
            )
            return latest

    async def refine_objective(
        self,
        unit_id: str,
        new_text: str,
    ) -> LearningUnit:
        """用户主动改写工作目标：替换 objective.text，状态推到 (resolved + refined)。

        adaptive alignment §6.3 / §11.2。``new_text`` 必须非空。
        """
        cleaned = new_text.strip()
        if not cleaned:
            raise ValueError("Objective text cannot be empty.")
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if unit.phase != "absorbing":
            raise ValueError(
                f"Cannot refine objective in phase {unit.phase!r}; "
                "only meaningful during absorbing."
            )
        async with self.learning_unit_store.lock(unit_id):
            latest = self.learning_unit_store.get(unit_id) or unit
            old_text = latest.objective.text
            latest.objective.text = cleaned
            latest.objective_status = "refined"
            latest.alignment_state = "resolved"
            latest.alignment_reason = "user_request"
            latest.assumption_note = ""
            latest.last_alignment_at = datetime.now(timezone.utc)
            self.learning_unit_store.save(latest)
            self._emit_unit_event(
                latest,
                SessionEventType.LEARNING_UNIT_OBJECTIVE_REFINED,
                extra={
                    "old_text": old_text[:200],
                    "new_text": cleaned[:200],
                },
            )
            return latest

    def record_reuse_feedback(self, unit_id: str, value: str) -> LearningUnit:
        """M2：用户在反馈卡上点"赞/否"后落事件。

        - ``value`` 只接受 ``"yes"``/``"no"``，其他抛 ``ValueError``。
        - 仅对 consolidated 的卷有意义；其他阶段抛 ``ValueError`` 让前端反馈
          状态错误（避免用户误点）。
        - 不修改卷本身字段——意愿数据走事件流，便于指标按窗口聚合且不需要
          额外 schema 迁移。
        """
        if value not in ("yes", "no"):
            raise ValueError("reuse feedback value must be 'yes' or 'no'")
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if not unit.is_terminal():
            raise ValueError(
                f"Cannot record reuse feedback in phase {unit.phase!r}; "
                "only valid once the unit is consolidated."
            )
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_REUSE_FEEDBACK,
            extra={"value": value},
        )
        return unit

    def get_learning_unit_metrics(
        self,
        *,
        window_days: Optional[int] = 7,
    ) -> LearningUnitMetricsSummary:
        """M2：扫所有 session 的事件流，聚合 4 个 P0 指标。

        ``window_days=None`` 用于全量回看；正数限定到最近 N 天。算子在
        ``learning_unit_metrics`` 模块内部按指标各自决定是否裁窗（TTFV 不裁，
        其余 3 个裁）。
        """
        all_events: list = []
        for sid in self.file_store.list_sessions():
            all_events.extend(self.session_event_store.read_events(sid))
        return calculate_metrics_summary(all_events, window_days=window_days)

    async def _run_concept_extraction_tail(
        self,
        unit_id: str,
        assistant_text: str,
    ) -> None:
        """absorbing 尾任务：抽概念 → RMW 合并到 unit → 落库。任何失败仅记日志。"""
        if self.concept_extractor is None:
            return
        try:
            unit_before = self.learning_unit_store.get(unit_id)
            if unit_before is None or unit_before.phase != "absorbing":
                return
            new_concepts, new_tangents = await self.concept_extractor.extract(
                objective_text=unit_before.objective.text,
                assistant_text=assistant_text,
                existing_concept_names={c.name for c in unit_before.concept_list},
                existing_tangent_names={t.name for t in unit_before.tangent_notes},
            )
            if not new_concepts and not new_tangents:
                return
            async with self.learning_unit_store.lock(unit_id):
                unit = self.learning_unit_store.get(unit_id)
                if unit is None or unit.phase != "absorbing":
                    return
                # 持锁后再核一次名字集，避免和并发抽取重复落
                concept_names = {c.name.lower() for c in unit.concept_list}
                tangent_names = {t.name.lower() for t in unit.tangent_notes}
                for c in new_concepts:
                    if c.name.lower() in concept_names:
                        continue
                    unit.concept_list.append(c)
                    concept_names.add(c.name.lower())
                for t in new_tangents:
                    if t.name.lower() in tangent_names:
                        continue
                    unit.tangent_notes.append(t)
                    tangent_names.add(t.name.lower())
                self.learning_unit_store.save(unit)
        except Exception:
            logger.exception(
                f"[System] Concept extraction tail failed for unit {unit_id}"
            )

    async def _prepare_learning_unit_turn(
        self,
        session: LearningSession,
        unit: LearningUnit,
        user_input: str,
    ) -> tuple[LearningSession, PreparedSessionTurn]:
        """学习卷会话的 turn 调度：phase 派生主协议，alignment_policy 决定是否覆写为 ASK。

        - consolidated → 拒绝继续对话（只读）
        - outputting → TEACH (single_pass)
        - absorbing → 默认 STUDY；若策略判 active 且未澄清过则覆写为 ASK；
          策略判 suggested 时保留 STUDY 但通过 ``alignment_state`` 让 UI 出建议条。

        adaptive alignment §9.1 / §9.3：限流由 3 道护栏分摊：
        - #1 启动期阻塞澄清不超过 1 次（``clarification_count``，本函数内 override）
        - #2 单卷最多 2 条非阻塞建议（``suggestion_count``，在 ``alignment_policy``
          内降级）
        - #3 "先按这个学"后 N 轮冷静期（``nag_cooldown_remaining``，在
          ``_apply_alignment_decision`` 内递减）
        """
        if unit.is_terminal():
            raise ValueError(
                f"Learning unit {unit.id} is terminal ({unit.phase}); "
                "no further turns can be prepared."
            )

        if unit.phase == "absorbing":
            # /align 端点会把 unit 提前置为 (active + user_request)。这种"用户主动
            # 触发"的对齐绕过 §9.3 #1 限流，但只生效本轮（在 _apply 中消费为 resolved）。
            user_initiated_alignment = (
                unit.alignment_state == "active"
                and unit.alignment_reason == "user_request"
            )
            legacy_pending_alignment = (
                unit.alignment_state == "active"
                and not unit.alignment_reason
                and unit.clarification_count == 0
            )
            if user_initiated_alignment:
                decision = AlignmentDecision(
                    mode="active",
                    reason="user_request",
                    assumption_note=unit.assumption_note or "用户主动请求对齐。",
                )
            elif legacy_pending_alignment:
                decision = AlignmentDecision(
                    mode="active",
                    reason="missing_learnable_target",
                    assumption_note=unit.assumption_note
                    or "旧学习卷需要先确认学习目标。",
                )
            else:
                decision = should_run_alignment(unit, user_input)
                # §9.3 第 1 条护栏：启动期阻塞澄清不超过 1 次
                if decision.mode == "active" and unit.clarification_count >= 1:
                    decision = AlignmentDecision(
                        mode="none",
                        reason="clear_enough",
                    )
            await self._apply_alignment_decision(unit, decision)
            effective_mode = (
                AgentMode.ASK if decision.mode == "active" else AgentMode.STUDY
            )
        else:
            decision = None
            effective_mode = AgentMode.TEACH

        unit_metadata: dict[str, Any] = {
            "mode": effective_mode.value,
            "learning_unit_id": unit.id,
            "learning_unit_phase": unit.phase,
            "alignment_state": unit.alignment_state,
            "objective_status": unit.objective_status,
            "forge_stage": unit.forge_stage,
            "temperature_state": unit.temperature_state,
        }
        if decision is not None and decision.mode != "none":
            unit_metadata["alignment_reason"] = decision.reason
            if decision.assumption_note:
                unit_metadata["assumption_note"] = decision.assumption_note
            if decision.suggested_objective:
                unit_metadata["suggested_objective"] = decision.suggested_objective
        if effective_mode == AgentMode.ASK:
            unit_metadata["alignment"] = True
        if effective_mode == AgentMode.STUDY and unit.phase == "absorbing":
            forge_plan = prepare_forge_stage_metadata(unit, user_input)
            unit_metadata["learning_action"] = forge_plan.learning_action
        if effective_mode == AgentMode.TEACH and unit.teach_session is not None:
            unit_metadata["teach_session_id"] = unit.teach_session.id
            unit_metadata["teach_state"] = unit.teach_session.state

        opening_addendum = self._build_absorbing_opening_addendum(
            session, unit, decision, effective_mode
        )

        profile = build_turn_profile(
            effective_mode,
            persona_key=self._resolve_session_persona_key(session, effective_mode),
            system_prompt_addendum=opening_addendum,
            user_message_metadata=dict(unit_metadata),
            assistant_message_metadata=dict(unit_metadata),
        )
        compaction_plan = await self._build_compaction_plan(
            session,
            user_input,
            profile,
            allow_full_compact=(effective_mode == AgentMode.STUDY),
        )
        return session, PreparedSessionTurn(
            effective_mode=effective_mode,
            runtime_input=user_input,
            profile=profile,
            stream_metadata=dict(profile.assistant_message_metadata),
            compaction_plan=compaction_plan,
        )

    def _build_absorbing_opening_addendum(
        self,
        session: LearningSession,
        unit: LearningUnit,
        decision: Optional[AlignmentDecision],
        effective_mode: AgentMode,
    ) -> Optional[str]:
        return build_absorbing_opening_addendum(
            session, unit, decision, effective_mode
        )

    async def _apply_alignment_decision(
        self,
        unit: LearningUnit,
        decision: AlignmentDecision,
    ) -> None:
        await apply_alignment_decision(
            store=self.learning_unit_store,
            emit_unit_event=self._emit_unit_event,
            unit=unit,
            decision=decision,
        )

    async def _prepare_session_turn(
        self,
        session: LearningSession,
        user_input: str,
        requested_mode: AgentMode,
    ) -> tuple[LearningSession, PreparedSessionTurn]:
        """
        在 Product/Application 层收口模式语义，生成 Runtime 可执行的 turn 计划。
        Runtime 不直接判断模式切换。
        """
        if session.learning_unit_id:
            unit = self.get_learning_unit(session.learning_unit_id)
            if unit is not None:
                return await self._prepare_learning_unit_turn(
                    session, unit, user_input
                )

        # ASK 不再作为独立可选模式（学习卷对齐门在 _prepare_learning_unit_turn
        # 内部消费 AgentMode.ASK，不经过这里）。非学习卷会话归一到 CHAT。
        if requested_mode == AgentMode.ASK:
            requested_mode = AgentMode.CHAT

        if session.mode != requested_mode:
            session = self.update_session_mode(session.id, requested_mode)

        companion_plan = prepare_companion_turn(
            session=session,
            user_input=user_input,
            requested_mode=requested_mode,
        )
        self._apply_companion_turn_plan(session, companion_plan)

        profile = build_turn_profile(
            requested_mode,
            persona_key=self._resolve_session_persona_key(session, requested_mode),
            system_prompt_addendum=companion_plan.prompt_addendum,
            override_tools=[] if companion_plan.disable_tools else None,
            user_message_metadata=companion_plan.message_metadata,
            assistant_message_metadata=companion_plan.message_metadata,
        )
        compaction_plan = await self._build_compaction_plan(
            session,
            user_input,
            profile,
            allow_full_compact=True,
        )
        return session, PreparedSessionTurn(
            effective_mode=requested_mode,
            runtime_input=user_input,
            profile=profile,
            stream_metadata=dict(profile.assistant_message_metadata),
            compaction_plan=compaction_plan,
        )

    def _apply_companion_turn_plan(
        self,
        session: LearningSession,
        plan: CompanionTurnPlan,
    ) -> None:
        changed: dict[str, Any] = {}
        for key, value in plan.metadata_updates.items():
            if value is None:
                if key in session.mode_metadata:
                    session.mode_metadata.pop(key, None)
                    changed[key] = None
            elif session.mode_metadata.get(key) != value:
                session.mode_metadata[key] = value
                changed[key] = value

        if changed:
            manager = getattr(self, "session_manager", None)
            persist = getattr(manager, "persist_mode_metadata", None)
            if callable(persist):
                try:
                    persist(session.id)
                except Exception:
                    logger.exception(
                        "[System] Failed to persist companion metadata for %s",
                        session.id,
                    )

        if plan.profile_changed or changed.get("chat_profile") is not None:
            self._emit_companion_event(
                session,
                SessionEventType.COMPANION_PROFILE_CHANGED,
                {
                    "enabled": plan.enabled,
                    "style": plan.style.value,
                    "intent": plan.intent.value,
                    "advice_level": plan.advice_level.value,
                    "metadata_changed": changed,
                },
            )
        if plan.signal_detected:
            self._emit_companion_event(
                session,
                SessionEventType.COMPANION_SIGNAL_DETECTED,
                {
                    "enabled": plan.enabled,
                    "style": plan.style.value,
                    "intent": plan.intent.value,
                    "advice_level": plan.advice_level.value,
                    "profile_changed": plan.profile_changed,
                },
            )

    async def _build_compaction_plan(
        self,
        session: LearningSession,
        user_input: str,
        profile: TurnExecutionProfile,
        *,
        allow_full_compact: bool,
    ) -> CompactionPlan:
        coordinator = getattr(self, "compaction_coordinator", None)
        if coordinator is None:
            return CompactionPlan(
                use_micro_compact=profile.micro_compact_enabled,
                recent_token_budget=profile.recent_token_budget,
            )
        return await coordinator.evaluate_turn(
            session,
            user_input,
            profile,
            allow_full_compact=allow_full_compact,
        )

    def _resolve_session_persona_key(
        self,
        session: LearningSession,
        mode: AgentMode,
    ) -> str | None:
        """Return the persona key for this session, if the user opted into one.

        Personas are an optional *thinking-style overlay* — the default is
        NEUTRAL (no overlay). We do not auto-pick a persona; we only read what
        the UI has explicitly stored on the session under ``chat_persona_key``.
        The key name is kept for back-compat; semantically it is the session's
        persona regardless of which mode this turn runs in.
        """
        del mode  # personas are no longer mode-restricted
        persona_key = session.mode_metadata.get("chat_persona_key")
        if isinstance(persona_key, str) and persona_key:
            return persona_key
        return None

    def _maybe_fire_concept_extraction(
        self,
        session: LearningSession,
        prepared_turn: PreparedSessionTurn,
        response_text: str,
    ) -> None:
        """absorbing 阶段每回合后异步抽取概念。失败只记日志，不影响主流。"""
        if not response_text.strip():
            return
        unit_id = session.learning_unit_id
        if not unit_id:
            return
        if prepared_turn.effective_mode != AgentMode.STUDY:
            return
        unit = self.learning_unit_store.get(unit_id)
        if unit is None or unit.phase != "absorbing":
            return
        try:
            task = asyncio.create_task(
                self._run_concept_extraction_tail(unit_id, response_text)
            )
        except RuntimeError:
            logger.warning(
                "[System] event loop closed; skip concept extraction "
                f"for unit {unit_id}"
            )
            return
        self._extraction_tasks.add(task)
        task.add_done_callback(self._extraction_tasks.discard)

    def _maybe_emit_first_value(
        self,
        session: LearningSession,
        prepared_turn: PreparedSessionTurn,
        response_text: str,
    ) -> None:
        """M1：absorbing 阶段首次成功产生非空 assistant 回答时发 FIRST_VALUE_DELIVERED。

        守卫顺序（任一失败则跳过）：
        - 响应非空
        - 会话挂着 learning_unit
        - 本轮 effective_mode 是 STUDY（teach/ask 不算"学习价值"）
        - 卷处于 absorbing
        - 卷的 ``first_value_delivered_at`` 仍为 None（once-only）
        """
        if not response_text.strip():
            return
        unit_id = session.learning_unit_id
        if not unit_id:
            return
        if prepared_turn.effective_mode != AgentMode.STUDY:
            return
        unit = self.learning_unit_store.get(unit_id)
        if unit is None:
            return
        if unit.phase != "absorbing":
            return
        if unit.first_value_delivered_at is not None:
            return
        unit.first_value_delivered_at = datetime.now(timezone.utc)
        try:
            self.learning_unit_store.save(unit)
        except Exception:
            logger.exception(
                f"[System] Failed to persist first_value_delivered_at for unit {unit_id}"
            )
            return
        self._emit_unit_event(
            unit,
            SessionEventType.LEARNING_UNIT_FIRST_VALUE_DELIVERED,
            extra={
                "delivered_at": unit.first_value_delivered_at.isoformat(),
                "response_chars": len(response_text),
            },
        )

    def _maybe_advance_forge_stage(
        self,
        session: LearningSession,
        prepared_turn: PreparedSessionTurn,
        response_text: str,
    ) -> None:
        """委托给 forge_policy.maybe_advance_forge_stage，注入 store 与事件回调。

        保留方法名是为了让 test_learning_unit_events 等测试可继续 monkeypatch / 直接调用。
        """
        maybe_advance_forge_stage(
            store=self.learning_unit_store,
            emit_unit_event=self._emit_unit_event,
            session=session,
            prepared_turn=prepared_turn,
            response_text=response_text,
        )

    async def _stream_teach_answer_flow(
        self,
        session: LearningSession,
        unit: LearningUnit,
        user_input: str,
    ) -> AsyncGenerator[ChatChunk, None]:
        """委托给 TeachFlow.stream_teach_answer_flow。

        现场构造 TeachFlow（构造代价仅 4 个字段赋值），让测试通过
        ``system.teach_judge=...`` / ``system.session_manager=...`` 等
        monkeypatch 的最新依赖能够立即被读取，避免协作者持有过期引用。
        """
        flow = TeachFlow(
            learning_unit_store=self.learning_unit_store,
            session_manager=self.session_manager,
            teach_judge=self.teach_judge,
            emit_unit_event=self._emit_unit_event,
        )
        async for chunk in flow.stream_teach_answer_flow(
            session, unit, user_input
        ):
            yield chunk


    async def stream_session_chat(
        self,
        session_id: str,
        user_input: str,
        mode: AgentMode = AgentMode.CHAT,
    ) -> AsyncGenerator[ChatChunk, None]:
        """将产品级聊天请求路由到指定 session runtime。

        契约（工具调用成功导向架构 / Final Answer Guarantee）：
        本生成器面向 web/CLI 上游，任何分支（chat / outputting / agent_loop）
        在内部失败时都不应让上游拿到"空内容"。生成器自身吞掉所有非
        BaseException 的失败，未输出可见 content 时由 rescue answer 兜底。
        """
        if self.agent_loop is None:
            raise RuntimeError("Agent loop is not initialized")

        session = self.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(f"Session {session_id} not found")

        visible_chars = 0
        rescue_reason: Optional[str] = None
        rescue_detail: Optional[str] = None

        async def _inner() -> AsyncGenerator[ChatChunk, None]:
            nonlocal rescue_reason, rescue_detail
            try:
                # B5 E4：outputting 阶段绕开 agent_loop，由 _stream_teach_answer_flow
                # 直接消费 user_input 为答题 + judge + 渲染下一题或反馈卡。
                if session.learning_unit_id:
                    unit = self.learning_unit_store.get(session.learning_unit_id)
                    if unit is not None and unit.phase == "outputting":
                        async for chunk in self._stream_teach_answer_flow(
                            session, unit, user_input
                        ):
                            yield chunk
                        return

                prepared_session, prepared_turn = await self._prepare_session_turn(
                    session, user_input, mode
                )
                response_parts: list[str] = []
                completed = False

                try:
                    async for chunk in self.agent_loop.run(
                        prepared_session,
                        prepared_turn.runtime_input,
                        profile=prepared_turn.profile,
                        compaction_plan=prepared_turn.compaction_plan,
                    ):
                        if chunk.content:
                            response_parts.append(chunk.content)
                        merged_metadata = dict(prepared_turn.stream_metadata)
                        merged_metadata.update(dict(chunk.metadata))
                        if "turn_usage" in merged_metadata and "usage" not in merged_metadata:
                            merged_metadata["usage"] = merged_metadata["turn_usage"]
                        yield chunk.model_copy(update={"metadata": merged_metadata})
                    completed = True
                finally:
                    try:
                        if completed:
                            self._maybe_emit_first_value(
                                prepared_session, prepared_turn, "".join(response_parts)
                            )
                            self._maybe_advance_forge_stage(
                                prepared_session, prepared_turn, "".join(response_parts)
                            )
                            self._maybe_fire_concept_extraction(
                                prepared_session, prepared_turn, "".join(response_parts)
                            )
                    except Exception:
                        logger.exception(
                            f"[System] Failed to finalize turn for session {session_id}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 任何模式下，pre-turn 准备 / teach flow / agent_loop 的未捕获异常
                # 都不能让 SSE 上游拿到空响应。统一打 rescue 通道。
                logger.exception(
                    "[System] stream_session_chat inner failure for session %s",
                    session_id,
                )
                rescue_reason = "inner_exception"
                rescue_detail = f"{type(exc).__name__}: {exc}"

        visible_parts: list[str] = []
        stream_error_reason: Optional[str] = None
        stream_error_detail: Optional[str] = None

        async for chunk in _inner():
            md = chunk.metadata or {}
            if md.get("stream_error"):
                # 系统自己产生的错误 chunk（provider 4xx、finalize / single-pass 失败、
                # context limit 等）。绝不转发给上游——否则用户会看到 "[Error] ..." 原始串。
                # 记录原因，交由下方 verdict 统一用 rescue 收口。
                if stream_error_reason is None:
                    stream_error_reason = str(md.get("stream_error_reason") or "stream_error")
                    stream_error_detail = (chunk.content or "").strip() or None
                continue
            if chunk.content:
                visible_chars += len(chunk.content)
                visible_parts.append(chunk.content)
            yield chunk

        visible_text = "".join(visible_parts)
        verdict_reason = _final_answer_verdict(
            visible_text=visible_text,
            stream_error_reason=stream_error_reason,
            inner_reason=rescue_reason,
        )

        if verdict_reason is not None:
            # Final Answer Guarantee：本轮没有给用户一个"可用答案"（空 / 残句 /
            # 被抑制的系统错误 / 流错误后的短片段）—— 必须由系统兜底。
            reason = verdict_reason
            detail = (
                stream_error_detail
                or rescue_detail
                or "final answer guard: visible answer not usable"
            )
            try:
                rescue_text, rescue_via = await self._build_rescue_answer(
                    session_id=session_id,
                    user_input=user_input,
                    reason=reason,
                    detail=detail,
                )
            except Exception:
                logger.exception(
                    "[System] rescue answer build raised for session %s", session_id
                )
                rescue_text = _SAFE_FALLBACK_ANSWER
                rescue_via = "static_fallback"
            try:
                await self.unresolved_failure_logger.record(
                    session_id=session_id,
                    layer="stream_session_chat",
                    reason_code=reason,
                    message=detail,
                    user_input=user_input,
                    rescue_used=True,
                    extra={
                        "rescue_via": rescue_via,
                        "mode": mode.value,
                        "visible_chars": visible_chars,
                    },
                )
            except Exception:
                logger.exception(
                    "[System] unresolved_failure_logger.record failed (non-fatal)"
                )
            yield ChatChunk(
                content=rescue_text,
                metadata={
                    "mode": mode.value,
                    "rescue": True,
                    "rescue_reason": reason,
                    "rescue_via": rescue_via,
                },
            )
        elif stream_error_reason is not None:
            # 答案可用（前面已有足够内容），但本轮确实发生过一次系统级流错误。
            # 不打扰用户，但要落账本，供后续优化定位。
            try:
                await self.unresolved_failure_logger.record(
                    session_id=session_id,
                    layer="stream_session_chat",
                    reason_code=stream_error_reason,
                    message=stream_error_detail or "stream error occurred but answer was usable",
                    user_input=user_input,
                    rescue_used=False,
                    extra={"mode": mode.value, "visible_chars": visible_chars},
                )
            except Exception:
                logger.exception(
                    "[System] unresolved_failure_logger.record failed (non-fatal)"
                )

    async def _build_rescue_answer(
        self,
        *,
        session_id: str,
        user_input: str,
        reason: str,
        detail: str,
    ) -> tuple[str, str]:
        """生成最终兜底回答。委托给 answer_quality.build_rescue_answer，注入 provider。"""
        return await build_rescue_answer(
            self.provider,
            session_id=session_id,
            user_input=user_input,
            reason=reason,
            detail=detail,
        )

    async def collect_session_chat(
        self,
        session_id: str,
        user_input: str,
        mode: AgentMode = AgentMode.CHAT,
    ) -> str:
        content_parts = []
        async for chunk in self.stream_session_chat(session_id, user_input, mode=mode):
            content_parts.append(chunk.content)
        return "".join(content_parts)

    async def start_session(self, objective_id: Optional[str] = None) -> str:
        session = self.create_session(
            objective_id=objective_id or (self._current_objective.id if self._current_objective else None),
            title="Learning Session",
            set_current=True,
        )
        await self.event_bus.publish(
            Event(
                type="session.created",
                payload={"session_id": session.id},
                source="system",
                session_id=session.id,
            )
        )
        return session.id


if __name__ == "__main__":
    # 提前解析参数，web 模式需要在 asyncio.run 之外启动，避免嵌套事件循环
    _parser = argparse.ArgumentParser(description="Learning-Agent CLI")
    _parser.add_argument("--config", "-c", type=str, default=None)
    _parser.add_argument("--show-config", action="store_true")
    _parser.add_argument("--web", action="store_true")
    _parser.add_argument("--host", type=str, default="127.0.0.1")
    _parser.add_argument("--port", type=int, default=8000)
    _args = _parser.parse_args()

    if _args.web:
        import os
        import uvicorn
        if _args.config:
            os.environ["LA_CONFIG_PATH"] = _args.config
        uvicorn.run(
            "learning_agent.web.web_server:app",
            host=_args.host,
            port=_args.port,
            reload=False,
        )
    else:
        from learning_agent.learning_agent.cli import interactive_cli
        asyncio.run(interactive_cli(sys.argv[1:]))
