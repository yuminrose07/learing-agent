"""学习卷 outputting 阶段的题目生成器（B5 E2）。

落实任务拆解 B5 §2 与设计文档 §3.6：

- 调用 cheap-tier LLM，根据 ``objective_text`` 与已沉淀的 ``concept_list``
  生成 1~3 道简答题（``kind="sa"``），用于 outputting 阶段的"自我讲解"
  环节。
- 每道题必须挂在某个 ``ConceptItem.id`` 上（``concept_id``），方便后续按
  概念聚合掌握度。
- 失败容忍：解析不出 JSON / schema 不合规 / provider 抛错 → 返回空列表
  + 日志告警，不向上游 raise。orchestration（B5 E4）会根据空列表给出
  退化路径。

模式参考 ``concept_extractor.py``，复用 ``ChatParams`` + ``_parse_json_array``
风格；保持 LLM 接口一致以便统一替换。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from learning_agent.ai.learning_unit import ConceptItem, TeachQuestion
from learning_agent.ai.models import ChatMessage, ChatParams, MessageRole

logger = logging.getLogger(__name__)


# 一次最多出几道题。MVP 阶段做硬上限，避免 LLM 飙到 10+ 题让用户疲劳。
MAX_QUESTIONS_PER_SESSION = 3


_TEACH_PROMPT_TEMPLATE = (
    "你是一个学习评估出题人。学习者刚刚学完一卷内容，正在进入"
    "「向你讲解」环节。请基于下面给出的「学习目标」与「已覆盖概念」，"
    "出 1~{max_questions} 道**简答题（short-answer）**，每题挂在一个"
    "具体概念上。\n"
    "\n"
    "出题原则：\n"
    "- 每道题必须从「已覆盖概念」中精确选一个 `concept_id`；不要凭空捏造。\n"
    "- `stem` 是给学习者看的问题陈述，要求其用自己的话解释或举例，"
    "不要可以被一句术语糊弄过去。\n"
    "- `rubric` 是给评判者（另一个 LLM）的判分标准，写明「答对必须命中的关键点」。\n"
    "- 优先挑 `relevance` 高的概念出题；若概念不足 {max_questions} 个，"
    "宁可少出几道，不要重复挂同一个 concept_id。\n"
    "\n"
    "## 输出格式（严格 JSON 数组，不要其他文本，不要 markdown 围栏）\n"
    "```\n"
    '[{{"concept_id": "...", "stem": "...", "rubric": "..."}}, ...]\n'
    "```\n"
    "如果「已覆盖概念」为空或都不适合出题，直接返回 `[]`。\n"
    "\n"
    "## 学习目标\n"
    "{objective}\n"
    "\n"
    "## 已覆盖概念（JSON 数组，按 relevance 降序）\n"
    "{concepts_json}"
)


class TeachQuestionGenerator:
    """题目生成器：一次 LLM 调用 + 一次 JSON 解析 + 一次 schema 校验。"""

    def __init__(
        self,
        provider: Any,
        model_name: Optional[str] = None,
        *,
        max_questions: int = MAX_QUESTIONS_PER_SESSION,
        max_tokens: int = 1024,
    ):
        self.provider = provider
        self.model_name = model_name
        self.max_questions = max(1, min(max_questions, MAX_QUESTIONS_PER_SESSION))
        self.max_tokens = max_tokens

    async def generate(
        self,
        *,
        objective_text: str,
        concepts: list[ConceptItem],
    ) -> list[TeachQuestion]:
        if not concepts:
            return []

        # 按 relevance 降序排序，取前 N 个进 prompt，避免 prompt 太长
        ranked = sorted(
            concepts, key=lambda c: c.relevance, reverse=True
        )[: self.max_questions * 2]
        concepts_payload = [
            {
                "concept_id": c.id,
                "name": c.name,
                "summary": c.summary,
                "relevance": round(c.relevance, 3),
            }
            for c in ranked
        ]
        prompt = _TEACH_PROMPT_TEMPLATE.format(
            max_questions=self.max_questions,
            objective=objective_text or "（未明确）",
            concepts_json=json.dumps(concepts_payload, ensure_ascii=False),
        )

        model = self.model_name or getattr(self.provider, "default_model", None)
        if not model:
            logger.warning(
                "[TeachQuestionGenerator] No model resolved (model_name=None "
                "and provider.default_model missing); skipping generation"
            )
            return []

        try:
            chunk = await self.provider.chat(
                ChatParams(
                    model=model,
                    messages=[
                        ChatMessage(role=MessageRole.USER, content=prompt),
                    ],
                    temperature=0.2,
                    stream=False,
                    max_tokens=self.max_tokens,
                )
            )
        except Exception:
            logger.exception("[TeachQuestionGenerator] Provider call failed")
            return []

        raw = (chunk.content or "").strip()
        parsed = _parse_json_array(raw)
        if parsed is None:
            logger.warning(
                "[TeachQuestionGenerator] Failed to parse JSON array from "
                "model response (head=%r)",
                raw[:200],
            )
            return []

        return self._build_questions(parsed, valid_concept_ids={c.id for c in concepts})

    def _build_questions(
        self,
        items: list[dict[str, Any]],
        *,
        valid_concept_ids: set[str],
    ) -> list[TeachQuestion]:
        questions: list[TeachQuestion] = []
        seen_concept_ids: set[str] = set()

        for raw in items:
            if not isinstance(raw, dict):
                continue
            concept_id = (raw.get("concept_id") or "").strip()
            stem = (raw.get("stem") or "").strip()
            rubric = (raw.get("rubric") or "").strip()
            if not concept_id or not stem or not rubric:
                continue
            if concept_id not in valid_concept_ids:
                # 模型乱编了 id，丢弃
                continue
            if concept_id in seen_concept_ids:
                # 同一概念不重复出
                continue
            seen_concept_ids.add(concept_id)
            questions.append(
                TeachQuestion(
                    concept_id=concept_id,
                    kind="sa",
                    stem=stem,
                    rubric=rubric,
                )
            )
            if len(questions) >= self.max_questions:
                break

        return questions


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _parse_json_array(text: str) -> Optional[list[dict[str, Any]]]:
    """尽力从模型回应里提取 JSON 数组（同 concept_extractor 复制）。"""
    text = text.strip()
    if not text:
        return None
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


__all__ = ["TeachQuestionGenerator", "MAX_QUESTIONS_PER_SESSION"]
