"""
项目级测试共享 fixture & 配置。

设计原则（对应 docs/TESTING.md 中的 FIRE 方法论）：
- F (Fixture-first): 所有 LLM / 事件总线 / 临时存储一律走这里的 fixture，禁止测试内部裸 mock。
- I (Isolation): 每个测试拿到的是独立 tmp 目录与独立事件存储，不污染共享数据目录。
- R (Replayable): ScriptedProvider 让 LLM 行为完全确定，测试可复现。
- E (Evidence): 失败时把关键事件 dump 到 .test_artifacts/<test_name>/。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterable, Iterator

import pytest

# ─── sys.path 收口 ──────────────────────────────────────────────────────────
# 历史遗留：旧测试在文件顶部各自 sys.path.insert，这里统一一次。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ─── 命令行开关 ─────────────────────────────────────────────────────────────
def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-llm",
        action="store_true",
        default=False,
        help="启用真实 LLM 调用（默认所有 live_llm marker 测试都被跳过）",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--live-llm"):
        return
    skip_live = pytest.mark.skip(reason="需要 --live-llm 才会跑")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip_live)


# ─── 临时目录隔离 ───────────────────────────────────────────────────────────
@pytest.fixture
def tmp_session_root(tmp_path: Path) -> Path:
    """每个测试独立的 session 持久化目录，自动清理。"""
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def tmp_artifact_dir(request: pytest.FixtureRequest) -> Path:
    """
    失败 evidence 落盘位置。测试中可以写中间状态进去，方便回放。

    路径形如 .test_artifacts/<module>/<test_name>/
    """
    artifact_root = PROJECT_ROOT / ".test_artifacts"
    rel = Path(request.node.nodeid.replace("::", "/").replace("/", "_"))
    target = artifact_root / rel
    target.mkdir(parents=True, exist_ok=True)
    return target


# ─── 通用 ScriptedProvider（从 test_compaction_dataset_runner.py 抽取） ───
# 把脚本化 LLM 抽到 conftest，避免每个测试重复定义。
# 真实导入放在 fixture 内部做延迟导入，避免 conftest 加载阶段触发重依赖。
class _ScriptedProviderState:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.cursor = 0


@pytest.fixture
def scripted_provider() -> Any:
    """
    返回一个工厂：传入 ["响应1", "响应2", ...]，得到一个可注入的 LLM provider。

    用法：
        async def test_xxx(scripted_provider):
            provider = scripted_provider(["{...json...}", "STOP"])
            # 把 provider 注入到 agent / coordinator
    """
    from learning_agent.ai import ChatChunk, ChatParams
    from learning_agent.ai.base_provider import BaseProvider

    class ScriptedProvider(BaseProvider):  # type: ignore[misc,valid-type]
        _default_model = "gpt-4o"

        def __init__(self, responses: list[str]) -> None:
            self._state = _ScriptedProviderState(responses)

        @property
        def default_model(self) -> str:
            return self._default_model

        async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
            del params
            if self._state.cursor >= len(self._state.responses):
                raise AssertionError(
                    f"ScriptedProvider 响应耗尽：第 {self._state.cursor + 1} 次请求无脚本"
                )
            response = self._state.responses[self._state.cursor]
            self._state.cursor += 1
            yield ChatChunk(content=response, finish_reason="stop")

        async def chat(self, params: ChatParams) -> ChatChunk:
            del params
            return ChatChunk(content="")

        def supports_tool_calling(self) -> bool:
            return True

        def supports_vision(self) -> bool:
            return False

        def get_max_context_length(self) -> int:
            return 128000

    def _factory(responses: list[str]) -> Any:
        return ScriptedProvider(responses)

    return _factory


# ─── 失败时自动 dump 证据 ────────────────────────────────────────────────────
@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    """
    任何失败都尝试把 tmp_artifact_dir 的路径打到报告里，方便定位。
    """
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        artifact_dir = getattr(item, "_artifact_dir", None)
        if artifact_dir is not None:
            rep.sections.append(("evidence", f"artifacts: {artifact_dir}"))
