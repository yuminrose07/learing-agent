"""
E2E 测试入口 —— 自动发现 ./scenarios/*.yaml 并参数化执行。

新增一个 e2e 用例 = 在 scenarios/ 下加一个 yaml，不用改这个文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.runner import Scenario, run_scenario

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def _discover_scenarios() -> list[Scenario]:
    if not SCENARIO_DIR.exists():
        return []
    yaml_files = sorted(SCENARIO_DIR.glob("*.yaml")) + sorted(SCENARIO_DIR.glob("*.yml"))
    return [Scenario.load(p) for p in yaml_files]


# 在 collection 阶段就把 scenarios 展开成 pytest 参数
_SCENARIOS = _discover_scenarios()


@pytest.mark.e2e
@pytest.mark.parametrize(
    "scenario",
    _SCENARIOS,
    ids=[s.name for s in _SCENARIOS] if _SCENARIOS else None,
)
async def test_scenario(
    scenario: Scenario,
    scripted_provider,
    tmp_session_root,
    tmp_artifact_dir,
) -> None:
    """跑一个 scenario yaml，失败时报告含 .test_artifacts 路径。"""
    result = await run_scenario(
        scenario,
        scripted_provider_factory=scripted_provider,
        tmp_session_root=tmp_session_root,
        artifact_dir=tmp_artifact_dir,
    )
    if not result.ok:
        msg_lines = [
            f"Scenario `{scenario.name}` 失败：",
            f"  来源: {scenario.raw_path}",
            f"  证据: {tmp_artifact_dir}",
            "  断言失败:",
        ]
        msg_lines.extend(f"    - {f}" for f in result.failures)
        pytest.fail("\n".join(msg_lines))


def test_scenarios_discovered() -> None:
    """元测试：保证 scenarios/ 至少能扫到 1 个用例，防止脚手架被静默禁用。"""
    assert _SCENARIOS, (
        f"未在 {SCENARIO_DIR} 下发现任何 *.yaml scenario；"
        "至少保留 example_chat_smoke.yaml 作为冒烟用例。"
    )
