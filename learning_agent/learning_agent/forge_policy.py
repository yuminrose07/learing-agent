"""Phase 1A 铸造状态骨架策略。

Product/Application 层模块，负责根据学习卷当前 forge_stage 和 temperature_state
派生本轮的铸造 metadata（learning_action 等）。

Phase 1A 只实现 entry/collision 两个阶段的映射，不做启发式判断。
后续 Phase 1B-1E 会在此扩展碰撞检测、火候调整等逻辑。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel

from learning_agent.ai import AgentMode
from learning_agent.ai.learning_unit import ForgeStage, TemperatureState
from learning_agent.learning_agent.session_events import SessionEventType

if TYPE_CHECKING:
    from learning_agent.ai import LearningSession
    from learning_agent.ai.learning_unit import LearningUnit
    from learning_agent.learning_agent.learning_unit_store import LearningUnitStore
    from learning_agent.learning_agent.mode_service import PreparedSessionTurn


logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# 注入式 advance（带副作用）
#
# 上面是「纯 plan 派生」逻辑：无 I/O、无事件、可单测。
# 下面是「副作用编排」：把 store/事件回调由调用方注入，本模块只负责
# 守卫判定与状态推进的纯逻辑组合，便于在 Phase 1B-1E 持续增长。
# ---------------------------------------------------------------------------


def maybe_advance_forge_stage(
    *,
    store: "LearningUnitStore",
    emit_unit_event: Callable[..., None],
    session: "LearningSession",
    prepared_turn: "PreparedSessionTurn",
    response_text: str,
) -> None:
    """Phase 1A：首轮 STUDY absorbing 成功后将 forge_stage 从 entry 推进为 collision。

    守卫顺序与历史实现保持一致（response 非空 → 学习卷绑定 → STUDY 模式 →
    phase=absorbing → forge_stage=entry），任一不满足直接 no-op。

    副作用（持久化 + 事件发射）由注入的 ``store`` 与 ``emit_unit_event`` 承担，
    持久化失败时记录异常并提前返回，避免发出与持久态不一致的事件。
    """
    if not response_text.strip():
        return
    unit_id = session.learning_unit_id
    if not unit_id:
        return
    if prepared_turn.effective_mode != AgentMode.STUDY:
        return
    unit = store.get(unit_id)
    if unit is None:
        return
    if unit.phase != "absorbing":
        return
    if unit.forge_stage != "entry":
        return
    unit.forge_stage = "collision"
    unit.updated_at = datetime.now(timezone.utc)
    try:
        store.save(unit)
    except Exception:
        logger.exception(
            "[System] Failed to persist forge_stage advance for unit %s", unit_id
        )
        return
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
