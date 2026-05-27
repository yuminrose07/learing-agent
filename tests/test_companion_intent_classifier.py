from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.models import ChatChunk
from learning_agent.learning_agent.companion_intent_classifier import (
    CompanionIntentClassifier,
)
from learning_agent.learning_agent.companion_policy import CompanionIntent


def _build_provider(content: str | None = None, exc: BaseException | None = None) -> MagicMock:
    provider = MagicMock()
    provider.default_model = "claude-haiku-4-5-20251001"
    if exc is not None:
        provider.chat = AsyncMock(side_effect=exc)
    else:
        provider.chat = AsyncMock(return_value=ChatChunk(content=content or ""))
    return provider


@pytest.mark.asyncio
async def test_classify_returns_intent_for_known_label():
    provider = _build_provider(content="venting")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("今天 PR 又被驳回了，真没意思")

    assert result == CompanionIntent.VENTING
    provider.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_tolerates_whitespace_and_case():
    provider = _build_provider(content="  TIRED\n")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("脑子糊住了打不开 IDE")

    assert result == CompanionIntent.TIRED


@pytest.mark.asyncio
async def test_classify_strips_trailing_punctuation():
    provider = _build_provider(content="anxious.")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("不知道怎么跟同事开口")

    assert result == CompanionIntent.ANXIOUS


@pytest.mark.asyncio
async def test_classify_rejects_return_to_study_label():
    """RETURN_TO_STUDY 不让 LLM 决定，故意从可接受集排除。"""
    provider = _build_provider(content="return_to_study")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("好了我准备继续学吧")

    assert result is None


@pytest.mark.asyncio
async def test_classify_rejects_unknown_label():
    provider = _build_provider(content="banana")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("有点想喝奶茶")

    assert result is None


@pytest.mark.asyncio
async def test_classify_rejects_empty_response():
    provider = _build_provider(content="")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("emoji full of nothing")

    assert result is None


@pytest.mark.asyncio
async def test_classify_returns_none_on_timeout():
    provider = _build_provider(exc=asyncio.TimeoutError())
    classifier = CompanionIntentClassifier(provider=provider, timeout_seconds=0.1)

    result = await classifier.classify("等好久没动静")

    assert result is None


@pytest.mark.asyncio
async def test_classify_returns_none_on_exception():
    provider = _build_provider(exc=RuntimeError("provider exploded"))
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("provider 挂了应该不影响闲聊")

    assert result is None


@pytest.mark.asyncio
async def test_classify_skips_provider_for_empty_input():
    provider = _build_provider(content="venting")
    classifier = CompanionIntentClassifier(provider=provider)

    result = await classifier.classify("   ")

    assert result is None
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_skips_when_no_model_resolved():
    provider = MagicMock()
    provider.default_model = None
    provider.chat = AsyncMock()
    classifier = CompanionIntentClassifier(provider=provider, model_name=None)

    result = await classifier.classify("没有可用模型也别炸")

    assert result is None
    provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_uses_overridden_model_name():
    provider = _build_provider(content="lonely")
    classifier = CompanionIntentClassifier(
        provider=provider,
        model_name="some-cheap-model",
    )

    await classifier.classify("有点想被陪着")

    call = provider.chat.await_args
    params = call.args[0]
    assert params.model == "some-cheap-model"
    assert params.stream is False
    assert params.max_tokens == 16
