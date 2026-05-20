"""
Learning-Agent 主入口。

本文件同时承载两类代码：
- Product/Application 层的 `LearningAgentSystem`，负责系统装配与产品级编排
- Interface 层的 CLI 入口，负责命令解析与终端交互适配
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.learning_agent.config import Config
from learning_agent.agent.event_bus import EventBus
from learning_agent.learning_agent.extension_manager import ExtensionManager
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
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
    MessageRole,
)
from learning_agent.ai.file_store import FileStore
from learning_agent.ai.openai_provider import OpenAIProvider
from learning_agent.learning_agent.compaction import CompactionCoordinator, CompactionPlan
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    TurnExecutionProfile,
    build_turn_profile,
    is_confirmation_message,
    resolve_persona,
)
from learning_agent.learning_agent.session_event_store import SessionEventStore
from learning_agent.learning_agent.session_migration import migrate_all_sessions
from learning_agent.learning_agent.session_manager import SessionManager

logger = logging.getLogger(__name__)


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
        # Layer 3: Agent Runtime cross-cutting support
        self.event_bus = EventBus()
        self.hook_system = HookSystem()
        self.tool_registry = ToolRegistry()
        self.observability = ObservabilityCollector(self.config.observability_dir)
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
        self.provider: Optional[OpenAIProvider] = None
        self.agent_loop: Optional[AgentLoop] = None
        self.compaction_coordinator: Optional[CompactionCoordinator] = None

        self._current_session = None
        self._current_objective = None
        self._compaction_turn_counts: dict[str, int] = {}

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

        # 订阅可观测性事件
        self.event_bus.subscribe("*", self.observability.on_event)

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
        )
        self.compaction_coordinator = CompactionCoordinator(
            self.session_manager,
            max_context_tokens=self.provider.get_max_context_length(),
            summary_executor=self._execute_compact_summary,
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

    async def _save_state(self) -> None:
        """保存非 session-event-log 的产品状态。Session 事实源已实时 append。"""
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        logger.info("[System] Durable state saved. Session event logs are append-only.")

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

    async def _on_entry_appended(self, event: Event) -> None:
        """Legacy no-op: session persistence now happens in SessionManager event log writes."""
        del event

    async def _on_entry_patched(self, event: Event) -> None:
        """Legacy no-op: session persistence now happens in SessionManager event log writes."""
        del event

    async def _on_scalar_changed(self, event: Event) -> None:
        """Legacy no-op: scalar persistence now happens in SessionManager event log writes."""
        del event

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
        *,
        clear_ask_state: bool = True,
    ) -> LearningSession:
        return self.session_manager.switch_session_mode(
            session_id,
            mode,
            clear_ask_state=clear_ask_state,
        )

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
        self._delete_session_observability_files(session_id)
        if self._current_session and self._current_session.id == session_id:
            self._current_session = None
        return True

    def _delete_session_observability_files(self, session_id: str) -> None:
        obs_dir = Path(self.observability.data_dir)
        for pattern in (f"flow_{session_id}.json", f"*{session_id}*.tmp", f"*{session_id}*.cache"):
            for path in obs_dir.glob(pattern):
                if path.is_file():
                    path.unlink()
        for trace_file in list(obs_dir.glob("trace_*.json")) + list(obs_dir.glob("snap_*.json")):
            try:
                data = json.loads(trace_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("session_id") == session_id or data.get("state", {}).get("session_id") == session_id:
                trace_file.unlink()

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

    async def _prepare_session_turn(
        self,
        session: LearningSession,
        user_input: str,
        requested_mode: AgentMode,
    ) -> tuple[LearningSession, PreparedSessionTurn]:
        """
        在 Product/Application 层收口模式语义，生成 Runtime 可执行的 turn 计划。
        Runtime 不直接判断 Ask 确认、模式切换或 ask_state 推进。
        """
        if (
            session.mode == AgentMode.ASK
            and session.ask_state.status == "aligning"
            and requested_mode == AgentMode.ASK
            and is_confirmation_message(user_input)
        ):
            target_mode = AgentMode(session.mode_metadata.get("post_ask_target", AgentMode.CHAT.value))
            confirmed_input = session.ask_state.confirmed_input or user_input
            session.ask_state.status = "idle"
            session.ask_state.confirmed_input = ""
            if hasattr(self, "session_manager"):
                self.session_manager.persist_ask_state(session.id)
            if target_mode != session.mode:
                session = self.update_session_mode(
                    session.id,
                    target_mode,
                    clear_ask_state=False,
                )
            profile = build_turn_profile(
                target_mode,
                persona_key=self._resolve_session_persona_key(session, target_mode),
            )
            compaction_plan = await self._build_compaction_plan(
                session,
                confirmed_input,
                profile,
                allow_full_compact=False,
            )
            return session, PreparedSessionTurn(
                effective_mode=target_mode,
                runtime_input=confirmed_input,
                profile=profile,
                stream_metadata=dict(profile.assistant_message_metadata),
                compaction_plan=compaction_plan,
            )

        if session.mode != requested_mode:
            session = self.update_session_mode(session.id, requested_mode)

        if requested_mode == AgentMode.ASK:
            session.ask_state.status = "aligning"
            if hasattr(self, "session_manager"):
                self.session_manager.persist_ask_state(session.id)
            profile = build_turn_profile(
                AgentMode.ASK,
                user_message_metadata={"mode": AgentMode.ASK.value, "alignment": True},
                assistant_message_metadata={"mode": AgentMode.ASK.value, "alignment": True},
            )
            compaction_plan = await self._build_compaction_plan(
                session,
                user_input,
                profile,
                allow_full_compact=False,
            )
            return session, PreparedSessionTurn(
                effective_mode=AgentMode.ASK,
                runtime_input=user_input,
                profile=profile,
                stream_metadata=dict(profile.assistant_message_metadata),
                capture_response_as_confirmed_input=True,
                compaction_plan=compaction_plan,
            )

        profile = build_turn_profile(
            requested_mode,
            persona_key=self._resolve_session_persona_key(session, requested_mode),
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
        if mode != AgentMode.CHAT:
            return None

        persona_key = session.mode_metadata.get("chat_persona_key")
        if isinstance(persona_key, str) and persona_key:
            return persona_key

        persona = resolve_persona(AgentMode.CHAT)
        session.mode_metadata["chat_persona_key"] = persona.key
        if hasattr(self, "session_manager"):
            self.session_manager.persist_mode_metadata(session.id)
        return persona.key

    def _finalize_prepared_turn(
        self,
        session: LearningSession,
        prepared_turn: PreparedSessionTurn,
        response_text: str,
    ) -> None:
        if prepared_turn.capture_response_as_confirmed_input:
            session.ask_state.confirmed_input = response_text
            if hasattr(self, "session_manager"):
                self.session_manager.persist_ask_state(session.id)

    async def stream_session_chat(
        self,
        session_id: str,
        user_input: str,
        mode: AgentMode = AgentMode.CHAT,
    ) -> AsyncGenerator[ChatChunk, None]:
        """将产品级聊天请求路由到指定 session runtime。"""
        if self.agent_loop is None:
            raise RuntimeError("Agent loop is not initialized")

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        session, prepared_turn = await self._prepare_session_turn(session, user_input, mode)
        response_parts: list[str] = []
        completed = False

        try:
            async for chunk in self.agent_loop.run(
                session,
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
                    self._finalize_prepared_turn(
                        session,
                        prepared_turn,
                        "".join(response_parts),
                    )
            except Exception:
                logger.exception(f"[System] Failed to finalize turn for session {session_id}")

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

    async def chat(self, user_input: str, mode: AgentMode = AgentMode.CHAT) -> None:
        """
        执行一轮对话，流式输出到 stdout。
        """
        if not self._current_session:
            await self.start_session()

        session = self._current_session
        if mode == AgentMode.ASK:
            print(f"\n[You (Ask)] {user_input}\n")
        else:
            print(f"\n[You] {user_input}\n")
        print("[Assistant] ", end="", flush=True)

        try:
            async for chunk in self.stream_session_chat(session.id, user_input, mode=mode):
                print(chunk.content, end="", flush=True)
            print()  # 换行
        except Exception as e:
            logger.exception(f"[System] Chat error: {e}")
            print(f"\n[Error] {e}")

    async def show_memory(self) -> None:
        """展示当前记忆状态。"""
        print("\n=== Memory Status ===")
        print(f"L1 Working candidates: {len(self.memory_manager.get_l1_candidates())}")
        print(f"L2 Long-term nodes: {len(self.memory_manager.get_l2_nodes())}")
        print(f"L3 Archive nodes: {len(self.memory_manager.get_l3_nodes())}")
        due = self.memory_manager.get_due_reviews()
        print(f"Due reviews: {len(due)}")
        for node in due[:5]:
            print(f"  - [{node.mastery_level.value}] {node.content[:60]}...")
        print("====================\n")

    async def confirm_knowledge(self, node_id: str) -> None:
        """手动确认 L1 候选知识晋升到 L2。"""
        promoted = await self.confirm_knowledge_candidate(node_id, source="user")
        if promoted:
            print(f"Knowledge node {node_id} confirmed and promoted to L2.")
        else:
            print(f"Candidate {node_id} not found in working memory.")

    async def show_metrics(self) -> None:
        """展示可观测性指标。"""
        summary = self.observability.get_metrics_summary()
        print("\n=== Metrics Summary ===")
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        print("=======================\n")


async def interactive_cli(argv: Optional[list[str]] = None) -> None:
    """Interface 层 CLI 入口。"""
    parser = argparse.ArgumentParser(description="Learning-Agent CLI")
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config file (YAML/JSON/TOML). "
             "Defaults to config.yaml / config.json in current directory.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print loaded configuration and exit.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Start the web API server instead of interactive CLI.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the web server (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the web server (default: 8000).",
    )
    args = parser.parse_args(argv)

    config = Config(config_path=args.config)

    if args.show_config:
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
        sys.exit(0)

    system = LearningAgentSystem(config)

    try:
        await system.initialize()
    except RuntimeError as e:
        print(f"Initialization failed: {e}")
        print("Please set OPENAI_API_KEY environment variable.")
        sys.exit(1)

    print("\n🧠 Learning-Agent v0.1.0")
    print("Type /help for available commands.\n")

    # 自动创建默认目标与会话
    obj = await system.create_objective("General Learning", "Default learning objective")
    session_id = await system.start_session(obj.id)
    print(f"Created default objective: {obj.title}")
    print(f"Started session: {session_id}\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            parts = user_input.split()
            cmd = parts[0].lower()

            if cmd == "/quit" or cmd == "/exit":
                break
            elif cmd == "/help":
                print(
                    """
Commands:
  /quit, /exit          Exit the application
  /memory               Show memory status
  /metrics              Show observability metrics
  /confirm <node_id>    Confirm a knowledge candidate to L2
  /save                 Save state manually
  /ask <message>        Send message in Ask mode (alignment first)
  /help                 Show this help message
"""
                )
            elif cmd == "/memory":
                await system.show_memory()
            elif cmd == "/metrics":
                await system.show_metrics()
            elif cmd == "/confirm":
                if len(parts) < 2:
                    print("Usage: /confirm <node_id>")
                else:
                    await system.confirm_knowledge(parts[1])
            elif cmd == "/save":
                await system.save_state()
                print("State saved.")
            elif cmd == "/ask":
                ask_input = user_input[len("/ask "):].strip()
                if not ask_input:
                    print("Usage: /ask <your question>")
                else:
                    await system.chat(ask_input, mode=AgentMode.ASK)
            else:
                print(f"Unknown command: {cmd}")
            continue

        # 普通对话
        await system.chat(user_input)

    await system.shutdown()


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
        asyncio.run(interactive_cli(sys.argv[1:]))
