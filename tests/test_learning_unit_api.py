"""LearningUnit REST 端点集成测试。

测试模式沿用 tests/test_web_adaptation.py：把 web_server._get_system 打补丁返回
mock_system，直接 await 路由函数。覆盖：
- POST /learning-units happy path → 200 + unit dump
- POST /learning-units 命中 ActiveUnitExistsError → 409 + active_unit_id
- GET  /learning-units → 200 + list dump
- GET  /learning-units/{id} → 200 / 404
- POST /learning-units/{id}/confirm-objective → 200；非法状态 400
- POST /learning-units/{id}/advance → 200；非法过渡 400；未知 unit 404
- POST /chat-sessions/{id}/promote-to-learning-unit → 200；session 不存在 404；
  P3 冲突 409
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai import LearningSession
from learning_agent.ai.learning_unit import LearningUnit, UnitObjective
from learning_agent.learning_agent.learning_unit_store import ActiveUnitExistsError


@pytest.fixture
def mock_system():
    system = MagicMock()
    system.create_learning_unit = MagicMock()
    system.list_learning_units = MagicMock(return_value=[])
    system.get_learning_unit = MagicMock(return_value=None)
    system.confirm_learning_unit_objective = MagicMock()
    system.advance_learning_unit = AsyncMock()
    system.promote_chat_session_to_learning_unit = MagicMock()
    system.request_alignment = AsyncMock()
    system.accept_assumption = AsyncMock()
    system.refine_objective = AsyncMock()
    system.record_reuse_feedback = MagicMock()
    system.get_learning_unit_metrics = MagicMock()
    return system


def _make_unit(
    *,
    session_id: str = "sess-x",
    phase: str = "absorbing",
    text: str = "理解 attention",
) -> LearningUnit:
    unit = LearningUnit(
        session_id=session_id,
        objective=UnitObjective(text=text),
    )
    unit.phase = phase  # type: ignore[assignment]
    return unit


def _make_session(unit: LearningUnit) -> LearningSession:
    return LearningSession(id=unit.session_id, learning_unit_id=unit.id)


class TestCreateLearningUnit:
    @pytest.mark.asyncio
    async def test_happy_path_returns_unit_payload_with_session_id(self, mock_system):
        from learning_agent.web.web_server import (
            CreateLearningUnitRequest,
            create_learning_unit,
        )

        unit = _make_unit()
        session = _make_session(unit)
        mock_system.create_learning_unit.return_value = (session, unit)

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await create_learning_unit(
                CreateLearningUnitRequest(seed_text="理解 attention")
            )

        assert result["id"] == unit.id
        assert result["session_id"] == session.id
        assert result["phase"] == "absorbing"
        assert result["objective"]["text"] == "理解 attention"
        mock_system.create_learning_unit.assert_called_once_with(
            seed_text="理解 attention",
            source="ai_distilled",
        )

    @pytest.mark.asyncio
    async def test_active_unit_conflict_returns_409(self, mock_system):
        from learning_agent.web.web_server import (
            CreateLearningUnitRequest,
            create_learning_unit,
        )

        mock_system.create_learning_unit.side_effect = ActiveUnitExistsError(
            "lu-active"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            response = await create_learning_unit(
                CreateLearningUnitRequest(seed_text="another")
            )

        assert isinstance(response, JSONResponse)
        assert response.status_code == 409
        # decode body
        import json

        body = json.loads(response.body)
        assert body["active_unit_id"] == "lu-active"
        assert "active" in body["detail"].lower()


class TestListAndGet:
    @pytest.mark.asyncio
    async def test_list_returns_dumps(self, mock_system):
        from learning_agent.web.web_server import list_learning_units

        u1 = _make_unit(session_id="s1", text="t1")
        u2 = _make_unit(session_id="s2", text="t2")
        mock_system.list_learning_units.return_value = [u1, u2]

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await list_learning_units()

        assert {item["id"] for item in result} == {u1.id, u2.id}

    @pytest.mark.asyncio
    async def test_get_returns_unit(self, mock_system):
        from learning_agent.web.web_server import get_learning_unit

        unit = _make_unit()
        mock_system.get_learning_unit.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await get_learning_unit(unit.id)

        assert result["id"] == unit.id

    @pytest.mark.asyncio
    async def test_get_missing_returns_404(self, mock_system):
        from learning_agent.web.web_server import get_learning_unit

        mock_system.get_learning_unit.return_value = None

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_learning_unit("lu-missing")

        assert exc_info.value.status_code == 404


class TestConfirmObjective:
    @pytest.mark.asyncio
    async def test_happy_path_returns_unit_in_absorbing(self, mock_system):
        from learning_agent.web.web_server import confirm_learning_unit_objective

        unit = _make_unit(phase="absorbing")
        unit.objective.confirmed = True
        mock_system.confirm_learning_unit_objective.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await confirm_learning_unit_objective(unit.id)

        assert result["phase"] == "absorbing"
        assert result["objective"]["confirmed"] is True

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import confirm_learning_unit_objective

        mock_system.confirm_learning_unit_objective.side_effect = KeyError("lu-x")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await confirm_learning_unit_objective("lu-x")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_illegal_state_returns_400(self, mock_system):
        from learning_agent.web.web_server import confirm_learning_unit_objective

        mock_system.confirm_learning_unit_objective.side_effect = ValueError(
            "already absorbing"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await confirm_learning_unit_objective("lu-x")

        assert exc_info.value.status_code == 400


class TestAdvance:
    @pytest.mark.asyncio
    async def test_happy_path_advances_to_outputting(self, mock_system):
        from learning_agent.web.web_server import (
            AdvanceLearningUnitRequest,
            advance_learning_unit,
        )

        unit = _make_unit(phase="outputting")
        mock_system.advance_learning_unit.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await advance_learning_unit(
                unit.id, AdvanceLearningUnitRequest(target_phase="outputting")
            )

        assert result["phase"] == "outputting"
        mock_system.advance_learning_unit.assert_awaited_once_with(
            unit.id, "outputting"
        )

    @pytest.mark.asyncio
    async def test_illegal_transition_returns_400(self, mock_system):
        from learning_agent.web.web_server import (
            AdvanceLearningUnitRequest,
            advance_learning_unit,
        )

        mock_system.advance_learning_unit.side_effect = ValueError(
            "Illegal phase transition: absorbing -> consolidated"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await advance_learning_unit(
                    "lu-x",
                    AdvanceLearningUnitRequest(target_phase="consolidated"),
                )

        assert exc_info.value.status_code == 400
        assert "Illegal phase transition" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import (
            AdvanceLearningUnitRequest,
            advance_learning_unit,
        )

        mock_system.advance_learning_unit.side_effect = KeyError("lu-missing")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await advance_learning_unit(
                    "lu-missing",
                    AdvanceLearningUnitRequest(target_phase="absorbing"),
                )

        assert exc_info.value.status_code == 404


class TestPromoteFromChatSession:
    @pytest.mark.asyncio
    async def test_happy_path_returns_new_unit_payload(self, mock_system):
        from learning_agent.web.web_server import (
            PromoteSessionRequest,
            promote_chat_session_to_learning_unit,
        )

        unit = _make_unit(text="深入研究这个话题")
        session = _make_session(unit)
        mock_system.promote_chat_session_to_learning_unit.return_value = (
            session,
            unit,
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await promote_chat_session_to_learning_unit(
                "sess-origin",
                PromoteSessionRequest(seed_text="深入研究这个话题"),
            )

        assert result["id"] == unit.id
        assert result["session_id"] == session.id
        mock_system.promote_chat_session_to_learning_unit.assert_called_once_with(
            "sess-origin", seed_text="深入研究这个话题"
        )

    @pytest.mark.asyncio
    async def test_chat_session_missing_returns_404(self, mock_system):
        from learning_agent.web.web_server import (
            PromoteSessionRequest,
            promote_chat_session_to_learning_unit,
        )

        mock_system.promote_chat_session_to_learning_unit.side_effect = KeyError(
            "sess-missing"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await promote_chat_session_to_learning_unit(
                    "sess-missing", PromoteSessionRequest(seed_text="t")
                )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_active_unit_conflict_returns_409(self, mock_system):
        from learning_agent.web.web_server import (
            PromoteSessionRequest,
            promote_chat_session_to_learning_unit,
        )

        mock_system.promote_chat_session_to_learning_unit.side_effect = (
            ActiveUnitExistsError("lu-existing")
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            response = await promote_chat_session_to_learning_unit(
                "sess-origin", PromoteSessionRequest(seed_text="t")
            )

        assert isinstance(response, JSONResponse)
        assert response.status_code == 409
        import json

        body = json.loads(response.body)
        assert body["active_unit_id"] == "lu-existing"


class TestRequestAlignment:
    """POST /learning-units/{id}/align — adaptive alignment §6.3。"""

    @pytest.mark.asyncio
    async def test_happy_path_returns_active_user_request(self, mock_system):
        from learning_agent.web.web_server import request_learning_unit_alignment

        unit = _make_unit()
        unit.alignment_state = "active"
        unit.alignment_reason = "user_request"
        mock_system.request_alignment.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await request_learning_unit_alignment(unit.id)

        assert result["alignment_state"] == "active"
        assert result["alignment_reason"] == "user_request"
        mock_system.request_alignment.assert_awaited_once_with(unit.id)

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import request_learning_unit_alignment

        mock_system.request_alignment.side_effect = KeyError("lu-x")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await request_learning_unit_alignment("lu-x")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_wrong_phase_returns_400(self, mock_system):
        from learning_agent.web.web_server import request_learning_unit_alignment

        mock_system.request_alignment.side_effect = ValueError(
            "Cannot request alignment in phase 'outputting'"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await request_learning_unit_alignment("lu-x")
        assert exc_info.value.status_code == 400


class TestAcceptAssumption:
    """POST /learning-units/{id}/accept-assumption — §6.3 / §9.3 #3。"""

    @pytest.mark.asyncio
    async def test_happy_path_returns_skipped_with_cooldown(self, mock_system):
        from learning_agent.web.web_server import accept_learning_unit_assumption

        unit = _make_unit()
        unit.alignment_state = "skipped"
        unit.nag_cooldown_remaining = 3
        mock_system.accept_assumption.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await accept_learning_unit_assumption(unit.id)

        assert result["alignment_state"] == "skipped"
        assert result["nag_cooldown_remaining"] == 3
        mock_system.accept_assumption.assert_awaited_once_with(unit.id)

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import accept_learning_unit_assumption

        mock_system.accept_assumption.side_effect = KeyError("lu-x")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await accept_learning_unit_assumption("lu-x")
        assert exc_info.value.status_code == 404


class TestRefineObjective:
    """POST /learning-units/{id}/refine-objective — §6.3 / §11.2。"""

    @pytest.mark.asyncio
    async def test_happy_path_replaces_text_and_marks_refined(self, mock_system):
        from learning_agent.web.web_server import (
            RefineObjectiveRequest,
            refine_learning_unit_objective,
        )

        unit = _make_unit(text="原目标")
        unit.objective.text = "理解 BaseModel 校验流程"
        unit.objective_status = "refined"
        unit.alignment_state = "resolved"
        mock_system.refine_objective.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await refine_learning_unit_objective(
                unit.id,
                RefineObjectiveRequest(new_text="理解 BaseModel 校验流程"),
            )

        assert result["objective"]["text"] == "理解 BaseModel 校验流程"
        assert result["objective_status"] == "refined"
        assert result["alignment_state"] == "resolved"
        mock_system.refine_objective.assert_awaited_once_with(
            unit.id, "理解 BaseModel 校验流程"
        )

    @pytest.mark.asyncio
    async def test_empty_text_returns_400(self, mock_system):
        from learning_agent.web.web_server import (
            RefineObjectiveRequest,
            refine_learning_unit_objective,
        )

        mock_system.refine_objective.side_effect = ValueError(
            "Objective text cannot be empty."
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await refine_learning_unit_objective(
                    "lu-x", RefineObjectiveRequest(new_text="   ")
                )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import (
            RefineObjectiveRequest,
            refine_learning_unit_objective,
        )

        mock_system.refine_objective.side_effect = KeyError("lu-x")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await refine_learning_unit_objective(
                    "lu-x", RefineObjectiveRequest(new_text="x")
                )
        assert exc_info.value.status_code == 404


class TestReuseFeedback:
    """POST /learning-units/{id}/reuse-feedback — M2 复用意愿轻量问卷。"""

    @pytest.mark.asyncio
    async def test_happy_path_yes_returns_unit(self, mock_system):
        from learning_agent.web.web_server import (
            ReuseFeedbackRequest,
            record_learning_unit_reuse_feedback,
        )

        unit = _make_unit(phase="consolidated")
        mock_system.record_reuse_feedback.return_value = unit

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await record_learning_unit_reuse_feedback(
                unit.id, ReuseFeedbackRequest(value="yes")
            )

        assert result["id"] == unit.id
        mock_system.record_reuse_feedback.assert_called_once_with(unit.id, "yes")

    @pytest.mark.asyncio
    async def test_invalid_value_returns_400(self, mock_system):
        from learning_agent.web.web_server import (
            ReuseFeedbackRequest,
            record_learning_unit_reuse_feedback,
        )

        mock_system.record_reuse_feedback.side_effect = ValueError(
            "reuse feedback value must be 'yes' or 'no'"
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await record_learning_unit_reuse_feedback(
                    "lu-x", ReuseFeedbackRequest(value="maybe")
                )
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_unit_returns_404(self, mock_system):
        from learning_agent.web.web_server import (
            ReuseFeedbackRequest,
            record_learning_unit_reuse_feedback,
        )

        mock_system.record_reuse_feedback.side_effect = KeyError("lu-x")

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            with pytest.raises(HTTPException) as exc_info:
                await record_learning_unit_reuse_feedback(
                    "lu-x", ReuseFeedbackRequest(value="yes")
                )
        assert exc_info.value.status_code == 404


class TestMetricsEndpoint:
    """GET /learning-units/metrics — M2 4 个 P0 指标聚合视图。"""

    @pytest.mark.asyncio
    async def test_returns_summary_dict(self, mock_system):
        from learning_agent.learning_agent.learning_unit_metrics import (
            LearningUnitMetricsSummary,
            RatioStats,
            TTFVStats,
        )
        from learning_agent.web.web_server import get_learning_unit_metrics

        summary = LearningUnitMetricsSummary(
            window_days=7,
            window_start_iso="2026-05-19T12:00:00+00:00",
            generated_at_iso="2026-05-26T12:00:00+00:00",
            ttfv=TTFVStats(p50_seconds=5.0, p90_seconds=10.0, sample_size=3),
            consolidation=RatioStats(numerator=2, denominator=4, ratio=0.5),
            teach_entry=RatioStats(numerator=3, denominator=4, ratio=0.75),
            reuse_intent=RatioStats(numerator=2, denominator=3, ratio=2 / 3),
            diagnostics={"total_events": 50, "learning_unit_events": 20},
        )
        mock_system.get_learning_unit_metrics.return_value = summary

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            result = await get_learning_unit_metrics()

        assert result["window_days"] == 7
        assert result["ttfv"]["p50_seconds"] == 5.0
        assert result["consolidation_rate"]["ratio"] == 0.5
        assert result["reuse_intent_rate"]["numerator"] == 2
        mock_system.get_learning_unit_metrics.assert_called_once_with(window_days=7)

    @pytest.mark.asyncio
    async def test_negative_window_days_collapses_to_none(self, mock_system):
        from learning_agent.learning_agent.learning_unit_metrics import (
            LearningUnitMetricsSummary,
            RatioStats,
            TTFVStats,
        )
        from learning_agent.web.web_server import get_learning_unit_metrics

        mock_system.get_learning_unit_metrics.return_value = LearningUnitMetricsSummary(
            window_days=None,
            window_start_iso=None,
            generated_at_iso="2026-05-26T12:00:00+00:00",
            ttfv=TTFVStats(p50_seconds=None, p90_seconds=None, sample_size=0),
            consolidation=RatioStats(0, 0, None),
            teach_entry=RatioStats(0, 0, None),
            reuse_intent=RatioStats(0, 0, None),
        )

        with patch(
            "learning_agent.web.web_server._get_system", return_value=mock_system
        ):
            await get_learning_unit_metrics(window_days=-1)

        mock_system.get_learning_unit_metrics.assert_called_once_with(window_days=None)
