"""
Learning-Agent 主入口。

本文件同时承载两类代码：
- Product/Application 层的 `LearningAgentSystem`，负责系统装配与产品级编排
- Interface 层的 CLI 入口，负责命令解析与终端交互适配
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import sys
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
    Event,
    KnowledgeNode,
    LearningObjective,
    LearningSession,
    SessionEntry,
)
from learning_agent.ai.file_store import FileStore
from learning_agent.ai.openai_provider import OpenAIProvider
from learning_agent.learning_agent.mode_service import (
    PreparedSessionTurn,
    build_turn_profile,
    is_confirmation_message,
    resolve_persona,
)
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
        self.session_manager = SessionManager(event_bus=self.event_bus)
        self.provider: Optional[OpenAIProvider] = None
        self.agent_loop: Optional[AgentLoop] = None

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

        # 订阅 session delta 事件，实现追加持久化
        self.event_bus.subscribe("session.entryAppended", self._on_entry_appended)
        self.event_bus.subscribe("session.entryPatched", self._on_entry_patched)
        self.event_bus.subscribe("session.scalarChanged", self._on_scalar_changed)

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
        # 订阅状态快照事件，用于定期 compaction
        self.event_bus.subscribe("agent.stateSnapshot", self._on_state_snapshot)

        logger.info("[System] Initialization complete.")

    async def shutdown(self) -> None:
        """优雅关闭：保存状态、停用扩展。"""
        logger.info("[System] Shutting down...")
        await self._save_state()
        await self.extension_manager.deactivate_all()
        logger.info("[System] Shutdown complete.")

    async def _load_state(self) -> None:
        """加载产品层持久化状态，replay delta 恢复完整会话。"""
        kg_data = self.file_store.load_knowledge_graph()
        if kg_data:
            self.memory_manager.kg.from_dict(kg_data)
            logger.info(f"[System] Loaded {len(self.memory_manager.kg._nodes)} knowledge nodes")

        for sid in self.file_store.list_sessions():
            session = self._load_session_with_deltas(sid)
            if session:
                self.session_manager.add_session(session)

    def _load_session_with_deltas(self, session_id: str) -> Optional[LearningSession]:
        """加载 snapshot 并按序 replay JSONL delta。"""
        snapshot = self.file_store.load_session(session_id)
        if not snapshot:
            return None

        session = LearningSession(**snapshot)

        for delta in self.file_store.read_session_deltas(session_id):
            op = delta.get("op")
            if op == "append":
                from learning_agent.ai import SessionEntry
                session.entries.append(SessionEntry(**delta["entry"]))
            elif op == "patch":
                entry_id = delta["entry_id"]
                entry = next((e for e in session.entries if e.id == entry_id), None)
                if entry is not None:
                    path = delta["path"]
                    value = delta["value"]
                    parts = path.split(".")
                    current: Any = entry
                    for part in parts[:-1]:
                        if isinstance(current, dict):
                            current = current.get(part)
                        elif isinstance(current, list):
                            current = current[int(part)]
                        else:
                            current = getattr(current, part, None)
                        if current is None:
                            break
                    if current is not None:
                        last = parts[-1]
                        if isinstance(current, dict):
                            current[last] = value
                        elif isinstance(current, list):
                            current[int(last)] = value
                        else:
                            setattr(current, last, value)
            elif op == "scalar":
                path = delta["path"]
                value = delta["value"]
                if hasattr(session, path):
                    setattr(session, path, value)
                elif "." in path:
                    obj_name, attr_name = path.split(".", 1)
                    obj = getattr(session, obj_name, None)
                    if obj is not None and isinstance(obj, dict):
                        obj[attr_name] = value

        # 重建 current_leaf_id
        message_entries = [e for e in session.entries if e.type == "message"]
        if message_entries:
            session.current_leaf_id = message_entries[-1].id

        return session

    async def _save_state(self) -> None:
        """全量 compaction：保存所有 session 的 snapshot 并清空 delta。"""
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        for session in self.session_manager.list_sessions():
            self.file_store.compact_session(session.id, session.model_dump())
        logger.info("[System] State compacted and saved.")

    async def _on_state_snapshot(self, event: Event) -> None:
        """消费运行时快照事件，定期触发 compaction。"""
        snapshot = event.payload
        session_id = snapshot.get("session_id")
        turn_count = snapshot.get("turn_count", 0)
        if not session_id:
            return

        # 每 20 轮或进程启动后首次到达时触发 compaction
        last = self._compaction_turn_counts.get(session_id, 0)
        if turn_count > 0 and (turn_count - last) >= 20:
            session = self.get_session(session_id)
            if session is not None:
                try:
                    self.file_store.compact_session(session_id, session.model_dump())
                    self._compaction_turn_counts[session_id] = turn_count
                    logger.info(f"[System] Compacted session {session_id} at turn {turn_count}")
                except Exception as e:
                    logger.exception(f"[System] Failed to compact session: {e}")

    async def _on_entry_appended(self, event: Event) -> None:
        """追加写入 entry delta。"""
        payload = event.payload
        session_id = payload["session_id"]
        delta = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": "append",
            "entry": payload["entry"],
        }
        try:
            self.file_store.append_session_delta(session_id, delta)
        except Exception as e:
            logger.exception(f"[System] Failed to append entry delta: {e}")

    async def _on_entry_patched(self, event: Event) -> None:
        """追加写入 entry patch delta。"""
        payload = event.payload
        delta = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": "patch",
            "entry_id": payload["entry_id"],
            "path": payload["path"],
            "value": payload["value"],
        }
        try:
            self.file_store.append_session_delta(payload["session_id"], delta)
        except Exception as e:
            logger.exception(f"[System] Failed to append patch delta: {e}")

    async def _on_scalar_changed(self, event: Event) -> None:
        """追加写入标量变更 delta。"""
        payload = event.payload
        delta = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": "scalar",
            "path": payload["path"],
            "value": payload["value"],
        }
        try:
            self.file_store.append_session_delta(payload["session_id"], delta)
        except Exception as e:
            logger.exception(f"[System] Failed to append scalar delta: {e}")

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
        self.save_session(session.id)
        return session

    def list_sessions(self) -> list[LearningSession]:
        return self.session_manager.list_sessions()

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

        session = self._load_session_with_deltas(session_id)
        if session is None:
            return None

        self.session_manager.add_session(session)
        return session

    def has_session(self, session_id: str) -> bool:
        return self.get_session(session_id) is not None

    def update_session_title(
        self,
        session_id: str,
        title: Optional[str],
    ) -> Optional[LearningSession]:
        return self.session_manager.update_session_title(session_id, title)

    def save_session(self, session_id: str) -> bool:
        """写全量 snapshot（用于初始创建，事件驱动不经过此处）。"""
        session = self.get_session(session_id)
        if session is None:
            return False
        self.file_store.save_session(session_id, session.model_dump())
        return True

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

    def _prepare_session_turn(
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
            return session, PreparedSessionTurn(
                effective_mode=target_mode,
                runtime_input=confirmed_input,
                profile=profile,
                stream_metadata=dict(profile.assistant_message_metadata),
            )

        if session.mode != requested_mode:
            session = self.update_session_mode(session.id, requested_mode)

        if requested_mode == AgentMode.ASK:
            session.ask_state.status = "aligning"
            profile = build_turn_profile(
                AgentMode.ASK,
                user_message_metadata={"mode": AgentMode.ASK.value, "alignment": True},
                assistant_message_metadata={"mode": AgentMode.ASK.value, "alignment": True},
            )
            return session, PreparedSessionTurn(
                effective_mode=AgentMode.ASK,
                runtime_input=user_input,
                profile=profile,
                stream_metadata=dict(profile.assistant_message_metadata),
                capture_response_as_confirmed_input=True,
            )

        profile = build_turn_profile(
            requested_mode,
            persona_key=self._resolve_session_persona_key(session, requested_mode),
        )
        return session, PreparedSessionTurn(
            effective_mode=requested_mode,
            runtime_input=user_input,
            profile=profile,
            stream_metadata=dict(profile.assistant_message_metadata),
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
        return persona.key

    def _finalize_prepared_turn(
        self,
        session: LearningSession,
        prepared_turn: PreparedSessionTurn,
        response_text: str,
    ) -> None:
        if prepared_turn.capture_response_as_confirmed_input:
            session.ask_state.confirmed_input = response_text

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

        session, prepared_turn = self._prepare_session_turn(session, user_input, mode)
        response_parts: list[str] = []
        completed = False

        try:
            async for chunk in self.agent_loop.run(
                session,
                prepared_turn.runtime_input,
                profile=prepared_turn.profile,
            ):
                if chunk.content:
                    response_parts.append(chunk.content)
                yield chunk.model_copy(update={"metadata": dict(prepared_turn.stream_metadata)})
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
