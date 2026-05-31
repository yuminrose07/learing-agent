"""Phase 1B 入局情境生成策略。

Product/Application 层模块。orientation 通过独立通道写入
``LearningUnit.orientation_context``，不进入 TurnExecutionProfile 或主回合
system prompt 装配链。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Awaitable, Callable, Optional

from learning_agent.ai.learning_unit import (
    LearningUnit,
    OrientationContext,
    OrientationHookKind,
)
from learning_agent.learning_agent.session_events import SessionEventType

logger = logging.getLogger(__name__)

ORIENTATION_TIMEOUT_SECONDS = 6

OrientationProvider = Callable[[LearningUnit, object], str | None]
AsyncOrientationProvider = Callable[[LearningUnit, object], Awaitable[str | None]]


def maybe_generate_orientation(
    *,
    store: object,
    emit_unit_event: Callable[..., None],
    session: object,
    unit: LearningUnit,
    provider: Optional[OrientationProvider] = None,
    timeout_seconds: float = ORIENTATION_TIMEOUT_SECONDS,
    regenerated: bool = False,
) -> OrientationContext | None:
    """生成并持久化 OrientationContext。

    返回持久化后的结构体；守卫未命中时返回 None。持久化失败只回滚内存态并
    静默返回，主回复流不因 orientation 通道失败而中断。
    """
    if os.getenv("LA_LEARNING_ORIENTATION_ENABLED", "1").lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return None
    if unit.phase != "absorbing":
        return None
    if unit.forge_stage != "entry":
        return None
    if unit.orientation_context is not None:
        return None
    if unit.alignment_state not in {"idle", "skipped"}:
        return None

    raw_text: str | None = None
    provider_error = False
    if _force_fallback(unit):
        provider_error = True
    else:
        try:
            raw_text = _call_provider_with_timeout(
                provider or _builtin_orientation_provider,
                unit=unit,
                session=session,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            provider_error = True
            logger.exception(
                "[OrientationPolicy] orientation provider failed for unit %s",
                unit.id,
            )

    ctx = _context_from_raw(unit=unit, raw_text=raw_text, provider_error=provider_error)

    return _persist_and_emit_orientation(
        store=store,
        emit_unit_event=emit_unit_event,
        unit=unit,
        ctx=ctx,
        regenerated=regenerated,
    )


async def maybe_generate_orientation_async(
    *,
    store: object,
    emit_unit_event: Callable[..., None],
    session: object,
    unit: LearningUnit,
    provider: Optional[AsyncOrientationProvider] = None,
    timeout_seconds: float = ORIENTATION_TIMEOUT_SECONDS,
    regenerated: bool = False,
) -> OrientationContext | None:
    """异步版本：优先使用注入的独立 provider，超时/失败走 fallback。"""
    if os.getenv("LA_LEARNING_ORIENTATION_ENABLED", "1").lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return None
    if unit.phase != "absorbing":
        return None
    if unit.forge_stage != "entry":
        return None
    if unit.orientation_context is not None:
        return None
    if unit.alignment_state not in {"idle", "skipped"}:
        return None

    if provider is None:
        return await asyncio.to_thread(
            maybe_generate_orientation,
            store=store,
            emit_unit_event=emit_unit_event,
            session=session,
            unit=unit,
            timeout_seconds=timeout_seconds,
            regenerated=regenerated,
        )

    raw_text: str | None = None
    provider_error = False
    if _force_fallback(unit):
        provider_error = True
    else:
        try:
            raw_text = await asyncio.wait_for(
                provider(unit, session),
                timeout=timeout_seconds,
            )
        except Exception:
            provider_error = True
            logger.exception(
                "[OrientationPolicy] async orientation provider failed for unit %s",
                unit.id,
            )

    ctx = _context_from_raw(unit=unit, raw_text=raw_text, provider_error=provider_error)

    return _persist_and_emit_orientation(
        store=store,
        emit_unit_event=emit_unit_event,
        unit=unit,
        ctx=ctx,
        regenerated=regenerated,
    )


def _context_from_raw(
    *,
    unit: LearningUnit,
    raw_text: str | None,
    provider_error: bool,
) -> OrientationContext:
    validated = None if provider_error else _validate_orientation_text(raw_text or "")
    if validated is not None:
        return _make_orientation_context(
            prompt_text=validated,
            source="llm",
            unit=unit,
        )
    return _build_fallback_orientation(unit)


def _persist_and_emit_orientation(
    *,
    store: object,
    emit_unit_event: Callable[..., None],
    unit: LearningUnit,
    ctx: OrientationContext,
    regenerated: bool,
) -> OrientationContext | None:

    previous = unit.orientation_context
    unit.orientation_context = ctx
    try:
        store.save(unit)  # type: ignore[attr-defined]
    except Exception:
        unit.orientation_context = previous
        logger.exception(
            "[OrientationPolicy] Failed to persist orientation_context for unit %s",
            unit.id,
        )
        return None

    event_type = (
        SessionEventType.LEARNING_UNIT_ORIENTATION_GENERATED
        if ctx.source == "llm"
        else SessionEventType.LEARNING_UNIT_ORIENTATION_FALLBACK_USED
    )
    emit_unit_event(
        unit,
        event_type,
        extra={
            "learning_unit_id": unit.id,
            "forge_stage": unit.forge_stage,
            "temperature_state": unit.temperature_state,
            "source": ctx.source,
            "orientation_digest": ctx.orientation_digest,
            "hook_kind": ctx.hook_kind,
            "prompt_text": ctx.prompt_text,
            "source_seed_ref": ctx.source_seed_ref,
            "regenerated": regenerated,
        },
    )
    return ctx


def _call_provider_with_timeout(
    provider: OrientationProvider,
    *,
    unit: LearningUnit,
    session: object,
    timeout_seconds: float,
) -> str | None:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(provider, unit, session)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError("orientation provider timeout") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _builtin_orientation_provider(unit: LearningUnit, session: object) -> str:  # noqa: ARG001
    objective = _compact_objective(unit.objective.text)
    return f"先把「{objective}」放进一个真实选择里：如果换成你要立刻用它解决问题，你会先抓哪条线索？"


def _build_fallback_orientation(unit: LearningUnit) -> OrientationContext:
    objective = _compact_objective(unit.objective.text)
    text = f"先从「{objective}」里挑一个最容易误判的点：你觉得它为什么会让人想错？"
    validated = _validate_orientation_text(text) or "先用你的第一反应说一句：你觉得这里最关键的矛盾是什么？"
    return _make_orientation_context(
        prompt_text=validated,
        source="fallback",
        unit=unit,
    )


def _make_orientation_context(
    *,
    prompt_text: str,
    source: str,
    unit: LearningUnit,
) -> OrientationContext:
    hook_kind = _infer_hook_kind(prompt_text)
    source_seed_ref = _source_seed_ref(unit)
    digest = _compute_orientation_digest(
        prompt_text=prompt_text,
        hook_kind=hook_kind,
        source_seed_ref=source_seed_ref,
    )
    return OrientationContext(
        prompt_text=prompt_text,
        hook_kind=hook_kind,
        source=source,  # type: ignore[arg-type]
        source_seed_ref=source_seed_ref,
        orientation_digest=digest,
    )


def _validate_orientation_text(text: str) -> str | None:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned or len(cleaned) > 120:
        return None
    if "\n" in cleaned or "#" in cleaned or "```" in cleaned:
        return None
    forbidden_starts = ("请", "请读", "想想", "理解了吗")
    if cleaned.startswith(forbidden_starts):
        return None
    return cleaned


def _infer_hook_kind(text: str) -> OrientationHookKind:
    stripped = (text or "").strip()
    if "如果换成你" in stripped or "你会怎么" in stripped:
        return "scenario"
    if stripped.endswith(("?", "？")):
        return "question"
    return "counterintuitive"


def _compute_orientation_digest(
    *,
    prompt_text: str,
    hook_kind: OrientationHookKind,
    source_seed_ref: str | None,
) -> str:
    raw = f"{prompt_text}|{hook_kind}|{source_seed_ref or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _source_seed_ref(unit: LearningUnit) -> str:
    return f"objective:{_compact_objective(unit.objective.text, limit=30)}"


def _compact_objective(text: str, *, limit: int = 24) -> str:
    cleaned = " ".join((text or "").strip().split())
    return cleaned[:limit] if cleaned else "这个目标"


def _force_fallback(unit: LearningUnit) -> bool:
    source_ref = (unit.objective.source_ref or "").strip()
    e2e_ref_enabled = os.getenv(
        "LA_E2E_ENABLE_ORIENTATION_FORCE_FALLBACK", ""
    ).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if e2e_ref_enabled and source_ref == "e2e:force_orientation_fallback":
        return True
    return os.getenv("LA_ORIENTATION_FORCE_FALLBACK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    } or os.getenv("LA_LEARNING_ORIENTATION_PROVIDER_FAIL", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
