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
from typing import Any, AsyncGenerator, Optional

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.learning_agent.config import Config
from learning_agent.agent.event_bus import EventBus
from learning_agent.learning_agent.extension_manager import ExtensionManager
from learning_agent.agent.hook_system import HookSystem
from learning_agent.agent.observability import ObservabilityCollector
from learning_agent.agent.tool_registry import ToolRegistry
from learning_agent.learning_agent.extensions.built_in import create_builtin_extensions
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.ai import (
    ChatChunk,
    Event,
    KnowledgeNode,
    LearningObjective,
    LearningSession,
    SessionEntry,
)
from learning_agent.ai.file_store import FileStore
from learning_agent.ai.openai_provider import OpenAIProvider
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
        self.session_manager = SessionManager()
        self.provider: Optional[OpenAIProvider] = None
        self.agent_loop: Optional[AgentLoop] = None

        self._current_session = None
        self._current_objective = None

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
        self.agent_loop = AgentLoop(
            provider=self.provider,
            memory_manager=self.memory_manager,
            session_manager=self.session_manager,
            hook_system=self.hook_system,
            event_bus=self.event_bus,
            tool_registry=self.tool_registry,
            observability=self.observability,
            max_react_turns=10,
        )
        # 订阅状态快照事件，实现运行时持久化
        self.event_bus.subscribe("agent.stateSnapshot", self._on_state_snapshot)

        logger.info("[System] Initialization complete.")

    async def shutdown(self) -> None:
        """优雅关闭：保存状态、停用扩展。"""
        logger.info("[System] Shutting down...")
        await self._save_state()
        await self.extension_manager.deactivate_all()
        logger.info("[System] Shutdown complete.")

    async def _load_state(self) -> None:
        """加载产品层持久化状态，不恢复运行时私有状态。"""
        kg_data = self.file_store.load_knowledge_graph()
        if kg_data:
            self.memory_manager.kg.from_dict(kg_data)
            logger.info(f"[System] Loaded {len(self.memory_manager.kg._nodes)} knowledge nodes")

        for sid in self.file_store.list_sessions():
            data = self.file_store.load_session(sid)
            if data:
                self.session_manager.add_session(LearningSession(**data))

    async def _save_state(self) -> None:
        """保存产品层状态，由基础设施层负责具体文件落盘。"""
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        for session in self.session_manager.list_sessions():
            self.file_store.save_session(session.id, session.model_dump())
        logger.info("[System] State saved.")

    async def _on_state_snapshot(self, event: Event) -> None:
        """消费运行时快照事件，并编排会话持久化。"""
        snapshot = event.payload
        session_id = snapshot.get("session_id")
        session = self.get_session(session_id) if session_id else None
        if session is not None:
            try:
                self.file_store.save_session(session_id, session.model_dump())
                logger.debug(f"[System] Snapshot saved for session {session_id}")
            except Exception as e:
                logger.exception(f"[System] Failed to save snapshot: {e}")

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

        data = self.file_store.load_session(session_id)
        if not data:
            return None

        session = LearningSession(**data)
        self.session_manager.add_session(session)
        return session

    def has_session(self, session_id: str) -> bool:
        return self.get_session(session_id) is not None

    def update_session_title(
        self,
        session_id: str,
        title: Optional[str],
    ) -> Optional[LearningSession]:
        session = self.session_manager.update_session_title(session_id, title)
        if session is None:
            return None
        self.save_session(session_id)
        return session

    def save_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None:
            return False
        self.file_store.save_session(session_id, session.model_dump())
        return True

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
        if self._current_session and self._current_session.id == session_id:
            self._current_session = None
        await self.save_state()
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
        if self.agent_loop is None:
            return {
                "active_runtime_count": 0,
                "total_session_count": len(self.list_sessions()),
                "runtimes": [],
            }
        return self.agent_loop.get_runtime_overview()

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

    async def stream_session_chat(
        self,
        session_id: str,
        user_input: str,
        ask_mode: bool = False,
    ) -> AsyncGenerator[ChatChunk, None]:
        """将产品级聊天请求路由到指定 session runtime。"""
        if self.agent_loop is None:
            raise RuntimeError("Agent loop is not initialized")

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        try:
            async for chunk in self.agent_loop.run(session, user_input, ask_mode=ask_mode):
                yield chunk
        finally:
            try:
                self.save_session(session_id)
            except Exception:
                logger.exception(f"[System] Failed to save session {session_id}")

    async def collect_session_chat(
        self,
        session_id: str,
        user_input: str,
        ask_mode: bool = False,
    ) -> str:
        content_parts = []
        async for chunk in self.stream_session_chat(session_id, user_input, ask_mode=ask_mode):
            content_parts.append(chunk.content)
        return "".join(content_parts)

    def fork_session_entry(
        self,
        session_id: str,
        entry_id: Optional[str] = None,
        *,
        fork_content: Optional[str] = None,
    ) -> Optional[SessionEntry]:
        session = self.get_session(session_id)
        if session is None:
            return None

        fork = self.session_manager.fork_at(
            session_id,
            entry_id or session.current_leaf_id,
            fork_content=fork_content or "Forked branch",
        )
        if fork is None:
            return None

        self.save_session(session_id)
        return fork

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

    async def chat(self, user_input: str, ask_mode: bool = False) -> None:
        """
        执行一轮对话，流式输出到 stdout。
        """
        if not self._current_session:
            await self.start_session()

        session = self._current_session
        if ask_mode:
            print(f"\n[You (Ask)] {user_input}\n")
        else:
            print(f"\n[You] {user_input}\n")
        print("[Assistant] ", end="", flush=True)

        try:
            async for chunk in self.stream_session_chat(session.id, user_input, ask_mode=ask_mode):
                print(chunk.content, end="", flush=True)
            print()  # 换行
        except Exception as e:
            logger.exception(f"[System] Chat error: {e}")
            print(f"\n[Error] {e}")

    async def fork_session(self, entry_id: Optional[str] = None) -> str:
        """在当前会话的指定节点分叉。"""
        if not self._current_session:
            raise ValueError("No active session")
        fork = self.fork_session_entry(
            self._current_session.id,
            entry_id,
            fork_content="User initiated fork",
        )
        return fork.id if fork else ""

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
  /fork [entry_id]      Fork session at current or specified entry
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
            elif cmd == "/fork":
                entry_id = parts[1] if len(parts) > 1 else None
                new_id = await system.fork_session(entry_id)
                print(f"Forked at: {new_id}")
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
                    await system.chat(ask_input, ask_mode=True)
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
