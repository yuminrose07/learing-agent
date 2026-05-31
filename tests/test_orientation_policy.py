from __future__ import annotations

import ast
import asyncio
from unittest.mock import MagicMock

from learning_agent.ai.learning_unit import LearningUnit, UnitObjective
from learning_agent.learning_agent import orientation_policy
from learning_agent.learning_agent.orientation_policy import (
    _compute_orientation_digest,
    _infer_hook_kind,
    _validate_orientation_text,
    maybe_generate_orientation,
    maybe_generate_orientation_async,
)


def _make_unit() -> LearningUnit:
    return LearningUnit(
        session_id="sess-orientation",
        objective=UnitObjective(text="理解缓存穿透"),
    )


def test_success_path_persists_context_and_emits_generated():
    unit = _make_unit()
    store = MagicMock()
    emit = MagicMock()

    ctx = maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
        provider=lambda _unit, _session: "如果换成你遇到缓存穿透，你会先检查哪条线索？",
    )

    assert ctx is not None
    assert unit.orientation_context is ctx
    assert ctx.source == "llm"
    assert len(ctx.prompt_text) <= 120
    assert ctx.hook_kind == "scenario"
    expected_digest = _compute_orientation_digest(
        prompt_text=ctx.prompt_text,
        hook_kind=ctx.hook_kind,
        source_seed_ref=ctx.source_seed_ref,
    )
    assert ctx.orientation_digest == expected_digest
    store.save.assert_called_once_with(unit)
    assert emit.call_args.args[1] == "learning_unit.orientation_generated"
    payload = emit.call_args.kwargs["extra"]
    assert payload["source"] == "llm"
    assert payload["prompt_text"] == ctx.prompt_text
    assert payload["orientation_digest"] == ctx.orientation_digest


def test_provider_failure_uses_fallback_event():
    unit = _make_unit()
    store = MagicMock()
    emit = MagicMock()

    def boom(_unit, _session):
        raise RuntimeError("provider down")

    ctx = maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
        provider=boom,
    )

    assert ctx is not None
    assert ctx.source == "fallback"
    assert unit.orientation_context is ctx
    assert emit.call_args.args[1] == "learning_unit.orientation_fallback_used"
    payload = emit.call_args.kwargs["extra"]
    assert payload["source"] == "fallback"
    assert payload["prompt_text"] == ctx.prompt_text
    assert payload["orientation_digest"] == ctx.orientation_digest


def test_e2e_source_ref_force_fallback_requires_env(monkeypatch):
    unit = _make_unit()
    unit.objective.source_ref = "e2e:force_orientation_fallback"
    store = MagicMock()
    emit = MagicMock()
    monkeypatch.setenv("LA_E2E_ENABLE_ORIENTATION_FORCE_FALLBACK", "1")

    ctx = maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
        provider=lambda _unit, _session: "如果换成你遇到缓存穿透，你会先检查哪条线索？",
    )

    assert ctx is not None
    assert ctx.source == "fallback"
    assert emit.call_args.args[1] == "learning_unit.orientation_fallback_used"


def test_async_provider_success_path():
    unit = _make_unit()
    store = MagicMock()
    emit = MagicMock()

    async def provider(_unit, _session):
        return "如果换成你遇到缓存穿透，你会先检查哪条线索？"

    ctx = asyncio.run(
        maybe_generate_orientation_async(
            store=store,
            emit_unit_event=emit,
            session=object(),
            unit=unit,
            provider=provider,
        )
    )

    assert ctx is not None
    assert ctx.source == "llm"
    assert emit.call_args.args[1] == "learning_unit.orientation_generated"


def test_async_provider_timeout_uses_fallback():
    unit = _make_unit()
    store = MagicMock()
    emit = MagicMock()

    async def provider(_unit, _session):
        await asyncio.sleep(0.05)
        return "如果换成你遇到缓存穿透，你会先检查哪条线索？"

    ctx = asyncio.run(
        maybe_generate_orientation_async(
            store=store,
            emit_unit_event=emit,
            session=object(),
            unit=unit,
            provider=provider,
            timeout_seconds=0.001,
        )
    )

    assert ctx is not None
    assert ctx.source == "fallback"
    assert emit.call_args.args[1] == "learning_unit.orientation_fallback_used"


def test_invalid_provider_text_uses_fallback():
    unit = _make_unit()
    store = MagicMock()
    emit = MagicMock()

    ctx = maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
        provider=lambda _unit, _session: "请阅读以下材料，然后理解了吗",
    )

    assert ctx is not None
    assert ctx.source == "fallback"
    assert emit.call_args.args[1] == "learning_unit.orientation_fallback_used"


def test_persist_failure_rolls_back_and_does_not_emit():
    unit = _make_unit()
    store = MagicMock()
    store.save.side_effect = RuntimeError("disk full")
    emit = MagicMock()

    ctx = maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
        provider=lambda _unit, _session: "如果换成你遇到缓存穿透，你会先检查哪条线索？",
    )

    assert ctx is None
    assert unit.orientation_context is None
    emit.assert_not_called()


def test_alignment_state_guards_generation():
    for state in ("active", "suggested"):
        unit = _make_unit()
        unit.alignment_state = state  # type: ignore[assignment]
        store = MagicMock()
        emit = MagicMock()

        assert maybe_generate_orientation(
            store=store,
            emit_unit_event=emit,
            session=object(),
            unit=unit,
        ) is None
        store.save.assert_not_called()
        emit.assert_not_called()


def test_existing_context_is_reused_without_event():
    unit = _make_unit()
    existing = orientation_policy._build_fallback_orientation(unit)
    unit.orientation_context = existing
    store = MagicMock()
    emit = MagicMock()

    assert maybe_generate_orientation(
        store=store,
        emit_unit_event=emit,
        session=object(),
        unit=unit,
    ) is None
    assert unit.orientation_context is existing
    store.save.assert_not_called()
    emit.assert_not_called()


def test_validate_orientation_text():
    assert _validate_orientation_text("如果换成你，你会怎么解释？") is not None
    assert _validate_orientation_text("") is None
    assert _validate_orientation_text("x" * 121) is None
    assert _validate_orientation_text("请阅读以下材料") is None


def test_infer_hook_kind():
    assert _infer_hook_kind("你觉得为什么？") == "question"
    assert _infer_hook_kind("如果换成你，你会怎么做") == "scenario"
    assert _infer_hook_kind("反直觉的是，缓存越多越慢。") == "counterintuitive"


def test_orientation_policy_does_not_import_main():
    tree = ast.parse(orientation_policy.__loader__.get_source(orientation_policy.__name__))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert "learning_agent.learning_agent.main" not in imports
