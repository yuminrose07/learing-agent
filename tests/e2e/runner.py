"""
Scenario runner —— 把声明式 YAML 翻译成实际的 agent 端到端执行。

注意：
- 本模块**不直接 import** 项目业务代码，全部 import 都放在 `run_scenario` 内部，
  避免 conftest/import 阶段就拉起重依赖。
- 业务接口可能仍在演进，runner 中标注了所有需要对齐的点（# ALIGN-POINT），
  方便后续按真实接口微调。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Assertion:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Assertion":
        kind = raw.get("kind")
        if not kind:
            raise ValueError(f"assertion 缺少 kind: {raw!r}")
        params = {k: v for k, v in raw.items() if k != "kind"}
        return cls(kind=kind, params=params)


@dataclass
class Scenario:
    name: str
    description: str
    markers: list[str]
    user_input: str
    llm_script: list[str]
    assertions: list[Assertion]
    raw_path: Path

    @classmethod
    def load(cls, path: Path) -> "Scenario":
        import yaml  # 延迟 import，未装 pyyaml 时只影响 e2e

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            markers=list(data.get("markers", [])),
            user_input=data["user_input"],
            llm_script=list(data.get("llm_script", [])),
            assertions=[Assertion.from_dict(a) for a in data.get("assertions", [])],
            raw_path=path,
        )


# ─── 断言执行器 ─────────────────────────────────────────────────────────────
# 每个 kind 对应一个 checker，返回 (ok: bool, detail: str)
def _check_event_count_min(
    events: list[dict[str, Any]], params: dict[str, Any]
) -> tuple[bool, str]:
    event_type = params["event_type"]
    minimum = int(params.get("count", 1))
    actual = sum(1 for e in events if e.get("type") == event_type)
    return (
        actual >= minimum,
        f"event_count_min[{event_type}]: 期望≥{minimum}, 实际={actual}",
    )


def _check_event_contains(
    events: list[dict[str, Any]], params: dict[str, Any]
) -> tuple[bool, str]:
    event_type = params["event_type"]
    substring = params["substring"]
    matches = [
        e
        for e in events
        if e.get("type") == event_type and substring in json.dumps(e, ensure_ascii=False)
    ]
    return (
        bool(matches),
        f"event_contains[{event_type} ~ {substring!r}]: matches={len(matches)}",
    )


def _check_no_error(
    events: list[dict[str, Any]], params: dict[str, Any]
) -> tuple[bool, str]:
    del params
    errors = [e for e in events if e.get("type", "").endswith("error")]
    return (
        not errors,
        f"no_error: found {len(errors)} error events",
    )


CHECKERS = {
    "event_count_min": _check_event_count_min,
    "event_contains": _check_event_contains,
    "no_error": _check_no_error,
}


# ─── 主入口 ─────────────────────────────────────────────────────────────────
@dataclass
class ScenarioResult:
    scenario: Scenario
    events: list[dict[str, Any]]
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def _build_agent_loop(provider: Any, session_manager: Any) -> Any:
    """
    复用 tests/test_compaction_dataset_runner.py 中 _make_agent_loop 的装配姿势。
    所有项目内 import 都在这里完成，避免污染 conftest 加载阶段。
    """
    from learning_agent.agent.agent_loop import AgentLoop
    from learning_agent.agent.event_bus import EventBus
    from learning_agent.agent.hook_system import HookSystem
    from learning_agent.ai import ResilienceConfig
    from learning_agent.learning_agent.tool_execution_service import (
        ToolExecutionServiceImpl,
    )
    from learning_agent.learning_agent.tool_registry import ToolRegistry
    from learning_agent.memory.memory_manager import MemoryManager

    tool_registry = ToolRegistry()
    return AgentLoop(
        provider=provider,
        memory_service=MemoryManager(),
        session_store=session_manager,
        hook_system=HookSystem(),
        event_bus=EventBus(),
        tool_execution_service=ToolExecutionServiceImpl(tool_registry),
        observability=None,
        max_react_turns=5,
        resilience_config=ResilienceConfig(),
    )


async def run_scenario(
    scenario: Scenario,
    *,
    scripted_provider_factory: Any,
    tmp_session_root: Path,
    artifact_dir: Path,
) -> ScenarioResult:
    """
    执行单个 scenario，跑真实的 AgentLoop + SessionManager。

    所有项目内部 import 放在函数内 / _build_agent_loop 内，避免 conftest 加载阶段触发重依赖。
    """
    from learning_agent.ai.file_store import FileStore
    from learning_agent.learning_agent.session_manager import SessionManager

    # 1. 真实存储 + session manager（每个 scenario 独立 tmp 目录）
    data_dir = tmp_session_root / scenario.name
    data_dir.mkdir(parents=True, exist_ok=True)
    file_store = FileStore(str(data_dir))
    session_manager = SessionManager(file_store=file_store)

    # 2. 脚本化 provider + 真实 AgentLoop
    provider = scripted_provider_factory(scenario.llm_script)
    agent_loop = _build_agent_loop(provider, session_manager)

    # 3. 跑一轮（profile / compaction_plan 都用默认值）
    session = session_manager.create_session(title=scenario.name)
    response_parts: list[str] = []
    run_error: str | None = None
    try:
        async for chunk in agent_loop.run(session, scenario.user_input):
            if getattr(chunk, "content", None):
                response_parts.append(chunk.content)
    except Exception as exc:  # noqa: BLE001 - e2e runner 捕获后转成可断言事件
        run_error = f"{type(exc).__name__}: {exc}"

    response_text = "".join(response_parts)

    # 4. 从真实事件存储读事件流（这是生产路径的同一份事实源）
    raw_events: list[dict[str, Any]] = []
    try:
        raw_events = list(file_store.read_session_events(session.id))
    except Exception as exc:  # noqa: BLE001
        run_error = run_error or f"read_session_events failed: {exc}"

    # 5. 归一化事件类型，方便 yaml 里写断言（"user_message" / "assistant_message"）
    #    现有 SessionEventType 命名格式如 USER_MESSAGE_ADDED，这里做轻量映射。
    events: list[dict[str, Any]] = []
    for ev in raw_events:
        raw_type = str(ev.get("type", ""))
        normalized_type = raw_type.lower()
        # 把 *_added 后缀去掉，让 scenario yaml 写法更自然
        if normalized_type.endswith("_added"):
            normalized_type = normalized_type[: -len("_added")]
        events.append({**ev, "type": normalized_type, "_raw_type": raw_type})

    # 综合事件：把 assistant 累计文本也作为一条 normalized 事件加入，
    # 因为 SessionEventType 里的 assistant 消息可能分多条 chunk，方便 yaml 断言。
    if response_text:
        events.append(
            {
                "type": "assistant_response_aggregate",
                "content": response_text,
            }
        )
    if run_error:
        events.append({"type": "runner_error", "detail": run_error})

    # 6. 证据落盘
    (artifact_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "response.txt").write_text(response_text, encoding="utf-8")
    if run_error:
        (artifact_dir / "error.txt").write_text(run_error, encoding="utf-8")

    # 7. 执行断言
    failures: list[str] = []
    if run_error:
        failures.append(f"agent_loop.run 抛异常: {run_error}")
    for assertion in scenario.assertions:
        checker = CHECKERS.get(assertion.kind)
        if checker is None:
            failures.append(f"未知 assertion kind: {assertion.kind}")
            continue
        ok, detail = checker(events, assertion.params)
        if not ok:
            failures.append(detail)

    return ScenarioResult(scenario=scenario, events=events, failures=failures)
