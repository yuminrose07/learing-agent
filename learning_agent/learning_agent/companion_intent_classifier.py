"""闲聊陪伴意图的 LLM 兜底分类器。

关键字版本（``companion_policy.classify_companion_intent``）对显式信号工作得很好，
但对语义级表达（"今天 PR 又被驳回了"、"脑子糊住了"）完全漏掉。本模块在关键字
未命中时调用一次 cheap-tier 模型做意图分类，给减压陪伴一个语义级兜底。

设计原则同 ``concept_extractor``：
- 失败容忍：超时/异常/解析失败 → 返回 ``None``，调用方退回关键字结果，不打断闲聊。
- 模型可配：``model_name=None`` 时复用 ``provider.default_model``。
- 输出受限：仅接受 5 个意图（venting/tired/anxious/distraction/lonely）+ none。
  ``return_to_study`` 故意不让模型决定，保留给关键字精准捕获。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from learning_agent.ai.models import ChatMessage, ChatParams, MessageRole
from learning_agent.learning_agent.companion_policy import CompanionIntent

logger = logging.getLogger(__name__)


_ACCEPTED_INTENTS: dict[str, CompanionIntent] = {
    CompanionIntent.VENTING.value: CompanionIntent.VENTING,
    CompanionIntent.TIRED.value: CompanionIntent.TIRED,
    CompanionIntent.ANXIOUS.value: CompanionIntent.ANXIOUS,
    CompanionIntent.DISTRACTION.value: CompanionIntent.DISTRACTION,
    CompanionIntent.LONELY.value: CompanionIntent.LONELY,
}


_CLASSIFY_PROMPT_TEMPLATE = (
    "你是闲聊陪伴的意图标签器。读用户这一句话，从下面 6 个标签里选一个，"
    "**只输出标签词本身**，全小写，不加引号、不加解释、不加标点。\n"
    "\n"
    "- venting：用户在吐槽 / 抱怨 / 发泄情绪。\n"
    "- tired：用户表达疲惫 / 学不动 / 撑不住。\n"
    "- anxious：用户表达焦虑 / 担心 / 压力。\n"
    "- distraction：用户想换个轻松话题、转移注意力。\n"
    "- lonely：用户想被陪伴 / 想要人在场感。\n"
    "- none：以上都不像，是普通闲聊或事务性问题。\n"
    "\n"
    "用户这句话：\n"
    "{text}"
)


class CompanionIntentClassifier:
    """关键字未命中时的语义级 intent 分类器。"""

    def __init__(
        self,
        provider: Any,
        model_name: Optional[str] = None,
        *,
        timeout_seconds: float = 2.5,
        max_tokens: int = 16,
    ) -> None:
        self.provider = provider
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    async def classify(self, text: str) -> Optional[CompanionIntent]:
        cleaned = (text or "").strip()
        if not cleaned:
            return None

        model = self.model_name or getattr(self.provider, "default_model", None)
        if not model:
            logger.warning(
                "[CompanionIntentClassifier] No model resolved; skipping LLM classify"
            )
            return None

        prompt = _CLASSIFY_PROMPT_TEMPLATE.format(text=cleaned)
        params = ChatParams(
            model=model,
            messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            temperature=0.0,
            stream=False,
            max_tokens=self.max_tokens,
        )

        try:
            chunk = await asyncio.wait_for(
                self.provider.chat(params),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[CompanionIntentClassifier] LLM classify timed out after %.1fs",
                self.timeout_seconds,
            )
            return None
        except Exception:
            logger.exception("[CompanionIntentClassifier] Provider call failed")
            return None

        raw = (getattr(chunk, "content", "") or "").strip().lower()
        if not raw:
            return None
        token = raw.split()[0].strip(" \t\r\n.,;:'\"`")
        intent = _ACCEPTED_INTENTS.get(token)
        if intent is None:
            logger.debug(
                "[CompanionIntentClassifier] Unrecognized intent token: %r", raw[:60]
            )
            return None
        return intent
