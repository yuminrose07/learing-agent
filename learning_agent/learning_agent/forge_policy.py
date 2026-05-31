"""Phase 1A/1B 铸造状态策略。

Product/Application 层模块，负责根据学习卷当前 forge_stage 和 temperature_state
派生本轮的铸造 metadata（learning_action 等）。

Phase 1B 继续保持 forge_stage 推进的唯一入口在本模块；主流程只委托，
不私自改写 stage。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional

from pydantic import BaseModel

from learning_agent.ai.learning_unit import (
    ForgeStage,
    LearningUnit,
    OrientationHookKind,
    TemperatureState,
)
from learning_agent.learning_agent.session_events import SessionEventType

if TYPE_CHECKING:
    from learning_agent.learning_agent.learning_unit_store import LearningUnitStore


logger = logging.getLogger(__name__)


LearningAction = Literal["orient", "prepare_to_guess", "await_orientation_response"]
EmitUnitEvent = Callable[[LearningUnit, str], None]


class ForgeStagePlan(BaseModel):
    forge_stage: ForgeStage
    temperature_state: TemperatureState
    learning_action: LearningAction
    hook_kind: Optional[OrientationHookKind] = None


_ACTION_MAP: dict[str, LearningAction] = {
    "entry": "orient",
    "collision": "prepare_to_guess",
}

_DEFAULT_ACTION: LearningAction = "orient"


def prepare_forge_stage_metadata(
    unit: LearningUnit,
    user_input: str,  # noqa: ARG001 — Phase 1A 不使用，为后续阶段预留
) -> ForgeStagePlan:
    learning_action = _ACTION_MAP.get(unit.forge_stage, _DEFAULT_ACTION)
    hook_kind = None
    if unit.orientation_context is not None and unit.forge_stage in {
        "entry",
        "collision",
    }:
        learning_action = "await_orientation_response"
        hook_kind = unit.orientation_context.hook_kind
    return ForgeStagePlan(
        forge_stage=unit.forge_stage,
        temperature_state=unit.temperature_state,
        learning_action=learning_action,
        hook_kind=hook_kind,
    )


def maybe_advance_forge_stage(
    *,
    unit: LearningUnit | None,
    store: "LearningUnitStore",
    emit_unit_event: Callable[..., None],
    response_text: str,
    effective_mode: object,
    user_input: str = "",
) -> LearningUnit | None:
    """推进 forge_stage 的唯一入口。

    1B 只在 entry→collision 前增加 orientation_context gate；collision→forge
    留给 Phase 1C，不在本阶段推进。
    """
    if unit is None:
        return None
    if not response_text.strip():
        return None
    if _mode_value(effective_mode) != "study":
        return None
    if unit.phase != "absorbing":
        return None
    if unit.forge_stage == "entry":
        if unit.orientation_context is None:
            return None
        return _advance_entry_to_collision(unit, store, emit_unit_event)
    return None


def _advance_entry_to_collision(
    unit: LearningUnit,
    store: "LearningUnitStore",
    emit_unit_event: Callable[..., None],
) -> LearningUnit | None:
    unit.forge_stage = "collision"
    unit.updated_at = datetime.now(timezone.utc)
    try:
        store.save(unit)
    except Exception:
        logger.exception(
            "[ForgePolicy] Failed to persist forge_stage advance for unit %s",
            unit.id,
        )
        return None
    emit_unit_event(
        unit,
        SessionEventType.LEARNING_UNIT_FORGE_STAGE_CHANGED,
        extra={
            "from": "entry",
            "to": "collision",
            "temperature_state": unit.temperature_state,
            "reason": "first_value_delivered",
        },
    )
    return unit


def _mode_value(effective_mode: object) -> str:
    value = getattr(effective_mode, "value", effective_mode)
    return str(value).lower()
