"""Phase 1A 铸造状态骨架策略。

Product/Application 层模块，负责根据学习卷当前 forge_stage 和 temperature_state
派生本轮的铸造 metadata（learning_action 等）。

Phase 1A 只实现 entry/collision 两个阶段的映射，不做启发式判断。
后续 Phase 1B-1E 会在此扩展碰撞检测、火候调整等逻辑。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from learning_agent.ai.learning_unit import ForgeStage, TemperatureState

if TYPE_CHECKING:
    from learning_agent.ai.learning_unit import LearningUnit


LearningAction = Literal["orient", "prepare_to_guess"]


class ForgeStagePlan(BaseModel):
    forge_stage: ForgeStage
    temperature_state: TemperatureState
    learning_action: LearningAction


_ACTION_MAP: dict[str, LearningAction] = {
    "entry": "orient",
    "collision": "prepare_to_guess",
}

_DEFAULT_ACTION: LearningAction = "orient"


def prepare_forge_stage_metadata(
    unit: LearningUnit,
    user_input: str,  # noqa: ARG001 — Phase 1A 不使用，为后续阶段预留
) -> ForgeStagePlan:
    return ForgeStagePlan(
        forge_stage=unit.forge_stage,
        temperature_state=unit.temperature_state,
        learning_action=_ACTION_MAP.get(unit.forge_stage, _DEFAULT_ACTION),
    )
