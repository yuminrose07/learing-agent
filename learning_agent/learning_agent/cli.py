"""Learning-Agent Interface 层 CLI 入口。

从 ``main.py`` 抽出的命令行适配层：参数解析、终端交互循环，以及把
``LearningAgentSystem`` 的产品级 API 包装成面向 stdout 的命令处理函数。

与 Product 层（``main.LearningAgentSystem``）的边界：本模块只做 I/O 适配，
不持有任何业务状态。每个 ``cli_*`` 函数以 system 实例为首参，调用其公共 API
并把结果打印到终端。``cli.py`` 运行时 import ``main``，``main`` 仅在
``__main__`` 块内 lazy import ``cli``，因此不构成循环。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from learning_agent.ai import AgentMode
from learning_agent.learning_agent.config import Config
from learning_agent.learning_agent.main import LearningAgentSystem

logger = logging.getLogger(__name__)


async def cli_chat(
    system: LearningAgentSystem,
    user_input: str,
    mode: AgentMode = AgentMode.CHAT,
) -> None:
    """执行一轮对话，流式输出到 stdout。"""
    if not system._current_session:
        await system.start_session()

    session = system._current_session
    if mode == AgentMode.ASK:
        print(f"\n[You (Ask)] {user_input}\n")
    else:
        print(f"\n[You] {user_input}\n")
    print("[Assistant] ", end="", flush=True)

    try:
        async for chunk in system.stream_session_chat(session.id, user_input, mode=mode):
            print(chunk.content, end="", flush=True)
        print()  # 换行
    except Exception as e:
        logger.exception(f"[System] Chat error: {e}")
        print(f"\n[Error] {e}")


async def cli_show_memory(system: LearningAgentSystem) -> None:
    """展示当前记忆状态。"""
    print("\n=== Memory Status ===")
    print(f"L1 Working candidates: {len(system.memory_manager.get_l1_candidates())}")
    print(f"L2 Long-term nodes: {len(system.memory_manager.get_l2_nodes())}")
    print(f"L3 Archive nodes: {len(system.memory_manager.get_l3_nodes())}")
    due = system.memory_manager.get_due_reviews()
    print(f"Due reviews: {len(due)}")
    for node in due[:5]:
        print(f"  - [{node.mastery_level.value}] {node.content[:60]}...")
    print("====================\n")


async def cli_confirm_knowledge(system: LearningAgentSystem, node_id: str) -> None:
    """手动确认 L1 候选知识晋升到 L2。"""
    promoted = await system.confirm_knowledge_candidate(node_id, source="user")
    if promoted:
        print(f"Knowledge node {node_id} confirmed and promoted to L2.")
    else:
        print(f"Candidate {node_id} not found in working memory.")


async def cli_show_metrics(system: LearningAgentSystem) -> None:
    """展示可观测性指标。"""
    summary = system.observability.get_metrics_summary()
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

    print("\n🧠 Learning-Agent v0.4.0-alpha.1")
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
                await cli_show_memory(system)
            elif cmd == "/metrics":
                await cli_show_metrics(system)
            elif cmd == "/confirm":
                if len(parts) < 2:
                    print("Usage: /confirm <node_id>")
                else:
                    await cli_confirm_knowledge(system, parts[1])
            elif cmd == "/save":
                await system.save_state()
                print("State saved.")
            elif cmd == "/ask":
                ask_input = user_input[len("/ask "):].strip()
                if not ask_input:
                    print("Usage: /ask <your question>")
                else:
                    await cli_chat(system, ask_input, mode=AgentMode.ASK)
            else:
                print(f"Unknown command: {cmd}")
            continue

        # 普通对话
        await cli_chat(system, user_input)

    await system.shutdown()
