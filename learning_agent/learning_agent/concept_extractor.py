"""学习卷 absorbing 阶段的概念抽取。

落实设计文档 §3.5、C7、C8：每回合后异步调用一次（cheap-tier）LLM，
把 AI 的回答里出现的关键概念按 relevance 分流到 ``concept_list`` /
``tangent_notes``。

设计原则：
- 失败容忍：解析不出 JSON / schema 不合规 → 返回空列表 + 日志告警，
  绝不抛回 stream_session_chat 的尾任务。
- 模型可配：``model_name=None`` 时直接复用 provider.default_model；
  否则把字段值原样塞 ``ChatParams.model``，由 base_url 后面的路由库
  决定怎么转发（设计文档 P4 / 用户拍板的策略）。
- MVP 去重：name case-insensitive 完全匹配，命中则跳过；
  智能合并留给设计文档 §8.4。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from learning_agent.ai.learning_unit import ConceptItem, TangentNote
from learning_agent.ai.models import ChatMessage, ChatParams, MessageRole

logger = logging.getLogger(__name__)


_EXTRACTION_PROMPT_TEMPLATE = (
    "你是一个学习概念抽取器。下面给你这一卷学习的目标，以及刚刚 AI 助手"
    "给学习者的回答。请从回答里抽出"
    "**关键的可命名概念 / 术语 / 模型**，每条给一个一句话定义和"
    "可选的最多 2 个例子。\n"
    "\n"
    "对每条概念给一个 `relevance` 分（0-1 浮点），表示这条概念与"
    "学习目标的相关度——\n"
    "- ≥ 0.6 = 是目标本身的自然延伸 / 必经知识点；\n"
    "- < 0.6 = 跑题但有价值，作为「值得单独学一卷」备忘录留存。\n"
    "\n"
    "## 输出格式（严格 JSON 数组，不要其他文本，不要 markdown 围栏）\n"
    "```\n"
    '[{{"name": "...", "summary": "...", "examples": ["...", "..."],'
    ' "relevance": 0.85}}, ...]\n'
    "```\n"
    "如果回答里没有可抽取的概念，直接返回 `[]`。\n"
    "\n"
    "## 学习目标\n"
    "{objective}\n"
    "\n"
    "## AI 助手刚刚的回答\n"
    "{assistant_text}"
)


class ConceptExtractor:
    """概念抽取器：一次 LLM 调用 + 一次 JSON 解析 + 一次去重分流。"""

    def __init__(
        self,
        provider: Any,
        model_name: Optional[str] = None,
        *,
        threshold: float = 0.6,
        max_tokens: int = 1024,
    ):
        self.provider = provider
        self.model_name = model_name
        self.threshold = threshold
        self.max_tokens = max_tokens

    async def extract(
        self,
        *,
        objective_text: str,
        assistant_text: str,
        existing_concept_names: set[str],
        existing_tangent_names: set[str],
    ) -> tuple[list[ConceptItem], list[TangentNote]]:
        if not assistant_text.strip():
            return [], []

        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            objective=objective_text or "（未明确）",
            assistant_text=assistant_text,
        )
        model = self.model_name or getattr(self.provider, "default_model", None)
        if not model:
            logger.warning(
                "[ConceptExtractor] No model resolved (model_name=None and "
                "provider.default_model missing); skipping extraction"
            )
            return [], []

        try:
            chunk = await self.provider.chat(
                ChatParams(
                    model=model,
                    messages=[
                        ChatMessage(role=MessageRole.USER, content=prompt),
                    ],
                    temperature=0.1,
                    stream=False,
                    max_tokens=self.max_tokens,
                )
            )
        except Exception:
            logger.exception("[ConceptExtractor] Provider call failed")
            return [], []

        raw = (chunk.content or "").strip()
        parsed = _parse_json_array(raw)
        if parsed is None:
            logger.warning(
                "[ConceptExtractor] Failed to parse JSON array from model "
                "response (head=%r)",
                raw[:200],
            )
            return [], []

        return self._split(
            parsed,
            existing_concept_names={n.lower() for n in existing_concept_names},
            existing_tangent_names={n.lower() for n in existing_tangent_names},
        )

    def _split(
        self,
        items: list[dict[str, Any]],
        *,
        existing_concept_names: set[str],
        existing_tangent_names: set[str],
    ) -> tuple[list[ConceptItem], list[TangentNote]]:
        new_concepts: list[ConceptItem] = []
        new_tangents: list[TangentNote] = []
        # 同一批返回内也去重
        batch_concept_names = set(existing_concept_names)
        batch_tangent_names = set(existing_tangent_names)

        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = (raw.get("name") or "").strip()
            summary = (raw.get("summary") or "").strip()
            if not name or not summary:
                continue
            try:
                relevance = float(raw.get("relevance", 0.0))
            except (TypeError, ValueError):
                continue
            relevance = max(0.0, min(1.0, relevance))
            examples_raw = raw.get("examples") or []
            examples = [
                str(e).strip()
                for e in examples_raw
                if isinstance(e, (str, int, float)) and str(e).strip()
            ][:2]

            key = name.lower()
            if relevance >= self.threshold:
                if key in batch_concept_names:
                    continue
                batch_concept_names.add(key)
                new_concepts.append(
                    ConceptItem(
                        name=name,
                        summary=summary,
                        examples=examples,
                        relevance=relevance,
                    )
                )
            else:
                if key in batch_tangent_names:
                    continue
                batch_tangent_names.add(key)
                new_tangents.append(
                    TangentNote(
                        name=name,
                        summary=summary,
                        relevance=relevance,
                    )
                )

        return new_concepts, new_tangents


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_json_array(text: str) -> Optional[list[dict[str, Any]]]:
    """尽力从模型回应里提取 JSON 数组。

    - 先按整段 ``json.loads``。
    - 失败则正则提取第一个 ``[...]`` 块再试一次（兼容模型偷偷加了
      解释性前缀或 ``json`` 围栏的情况）。
    - 仍失败 → 返回 None。
    """
    text = text.strip()
    if not text:
        return None
    # 去掉常见 markdown 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, list):
        return None
    return parsed


__all__ = ["ConceptExtractor"]
