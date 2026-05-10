"""
Learning-Agent 主入口。
初始化所有层，组装系统，提供 CLI 交互入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from learning_agent.agent.agent_loop import AgentLoop
from learning_agent.config import Config
from learning_agent.core.event_bus import EventBus
from learning_agent.core.extension_manager import ExtensionManager
from learning_agent.core.hook_system import HookSystem
from learning_agent.core.observability import ObservabilityCollector
from learning_agent.core.tool_registry import ToolRegistry
from learning_agent.extensions.built_in import create_builtin_extensions
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.models import ChatChunk, Event, LearningObjective, MessageRole
from learning_agent.persistence.file_store import FileStore
from learning_agent.provider.openai_provider import OpenAIProvider
from learning_agent.session.session_manager import SessionManager

logger = logging.getLogger(__name__)


class LearningAgentSystem:
    """
    系统组装器：按正确顺序初始化所有层，管理生命周期。
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._setup_logging()

        # 初始化各层
        self.file_store = FileStore(self.config.data_dir)
        self.event_bus = EventBus()
        self.hook_system = HookSystem()
        self.tool_registry = ToolRegistry()
        self.observability = ObservabilityCollector(self.config.observability_dir)
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
        """初始化系统：加载持久化数据、激活扩展。"""
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
        for ext in create_builtin_extensions():
            self.extension_manager.register(ext)

        # 激活所有扩展
        await self.extension_manager.activate_all()

        # 订阅可观测性事件
        self.event_bus.subscribe("*", self.observability.on_event)

        # 初始化 Agent Loop
        self.agent_loop = AgentLoop(
            provider=self.provider,
            memory_manager=self.memory_manager,
            session_manager=self.session_manager,
            hook_system=self.hook_system,
            event_bus=self.event_bus,
            tool_registry=self.tool_registry,
            observability=self.observability,
        )

        logger.info("[System] Initialization complete.")

    async def shutdown(self) -> None:
        """优雅关闭：保存状态、停用扩展。"""
        logger.info("[System] Shutting down...")
        await self._save_state()
        await self.extension_manager.deactivate_all()
        logger.info("[System] Shutdown complete.")

    async def _load_state(self) -> None:
        """从文件加载记忆和会话。"""
        kg_data = self.file_store.load_knowledge_graph()
        if kg_data:
            self.memory_manager.kg.from_dict(kg_data)
            logger.info(f"[System] Loaded {len(self.memory_manager.kg._nodes)} knowledge nodes")

        for sid in self.file_store.list_sessions():
            data = self.file_store.load_session(sid)
            if data:
                from learning_agent.models import LearningSession
                self.session_manager._sessions[sid] = LearningSession(**data)

    async def _save_state(self) -> None:
        """保存记忆和会话到文件。"""
        self.file_store.save_knowledge_graph(self.memory_manager.kg.to_dict())
        for sid, session in self.session_manager._sessions.items():
            self.file_store.save_session(sid, session.model_dump())
        logger.info("[System] State saved.")

    # ─── 用户交互接口 ───

    async def create_objective(self, title: str, description: Optional[str] = None) -> LearningObjective:
        obj = LearningObjective(title=title, description=description)
        self._current_objective = obj
        self.file_store.save_objective(obj.id, obj.model_dump())
        logger.info(f"[System] Created objective: {obj.id} - {title}")
        return obj

    async def start_session(self, objective_id: Optional[str] = None) -> str:
        session = self.session_manager.create_session(
            objective_id=objective_id or (self._current_objective.id if self._current_objective else None),
            title="Learning Session",
        )
        self._current_session = session
        await self.event_bus.publish(
            Event(
                type="session.created",
                payload={"session_id": session.id},
                source="system",
                session_id=session.id,
            )
        )
        return session.id

    async def chat(self, user_input: str) -> None:
        """
        执行一轮对话，流式输出到 stdout。
        """
        if not self._current_session:
            await self.start_session()

        session = self._current_session
        print(f"\n[You] {user_input}\n")
        print("[Assistant] ", end="", flush=True)

        try:
            async for chunk in self.agent_loop.run(session, user_input):
                print(chunk.content, end="", flush=True)
            print()  # 换行
        except Exception as e:
            logger.exception(f"[System] Chat error: {e}")
            print(f"\n[Error] {e}")

    async def fork_session(self, entry_id: Optional[str] = None) -> str:
        """在当前会话的指定节点分叉。"""
        if not self._current_session:
            raise ValueError("No active session")
        fork = self.session_manager.fork_at(
            self._current_session.id,
            entry_id or self._current_session.current_leaf_id,
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
        candidate = self.memory_manager._l1_working.pop(node_id, None)
        if candidate:
            self.memory_manager.promote_to_l2(candidate, auto_confirm=True)
            await self.event_bus.publish(
                Event(
                    type="knowledge.confirmed",
                    payload={"node_id": node_id},
                    source="user",
                )
            )
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
    """交互式 CLI 入口。"""
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
                await system._save_state()
                print("State saved.")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # 普通对话
        await system.chat(user_input)

    await system.shutdown()


if __name__ == "__main__":
    asyncio.run(interactive_cli(sys.argv[1:]))
