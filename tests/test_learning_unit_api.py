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
    system.advance_learning_unit = MagicMock()
    system.promote_chat_session_to_learning_unit = MagicMock()
    return system


def _make_unit(
    *,
    session_id: str = "sess-x",
    phase: str = "aligning",
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
        assert result["phase"] == "aligning"
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
        mock_system.advance_learning_unit.assert_called_once_with(
            unit.id, "outputting"
        )

    @pytest.mark.asyncio
    async def test_illegal_transition_returns_400(self, mock_system):
        from learning_agent.web.web_server import (
            AdvanceLearningUnitRequest,
            advance_learning_unit,
        )

        mock_system.advance_learning_unit.side_effect = ValueError(
            "Illegal phase transition: aligning -> consolidated"
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
