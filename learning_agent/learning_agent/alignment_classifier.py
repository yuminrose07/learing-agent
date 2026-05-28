"""LLM 驱动的对齐意图分类器（替换原 ``should_run_alignment`` 规则启发式）。

落实 plan 阶段 B / §9.2：

- 输入：当前学习卷 + 本轮用户输入 + 最近若干条对话上下文
- 输出：``AlignmentDecision``，其中
  - ``candidates`` 列出 LLM 识别出的"会导向不同 first_step"的可能解读，
    封顶 3 条；first_step 实质相同的解读必须合并；
  - ``divergence_cost`` 是 LLM 对"选错代价"的判断（``high`` = 把整卷带沟里，
    ``low`` = 选错也能轻松拉回）；
  - ``mode`` 由档位映射决定：
    | candidates.len | divergence_cost | mode      |
    |----------------|-----------------|-----------|
    | 0 / 1          | *               | none      |
    | ≥2             | low             | suggested |
    | ≥2             | high            | active    |

失败容忍（fail-open，与 ``concept_extractor`` / ``teach_generator`` 同套）：
provider 抛错 / JSON 解析失败 / schema 不合规 → 返回 mode=none，避免无谓打扰。

模式参考 ``concept_extractor.py``，复用 ``ChatParams`` + JSON 解析模板；
模型默认从 ``provider.default_model`` 回落，部署时在 config 里把
``alignment_classifier_model`` 指向 cheap-tier（如 ``qwen-turbo``）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from learning_agent.ai.learning_unit import LearningUnit
from learning_agent.ai.models import ChatMessage, ChatParams, MessageRole
from learning_agent.learning_agent.alignment_policy import (
    AlignmentDecision,
    AlignmentReason,
    Candidate,
    _apply_rate_limits,
)

logger = logging.getLogger(__name__)


# 一次最多列几个候选。封顶 3 是为了 modal 卡片不爆屏。
MAX_CANDIDATES = 3

# 进 prompt 的对话上下文条数。给 LLM 一点上下文判断"继续学"这类省略式输入。
RECENT_MESSAGE_LIMIT = 6


_CLASSIFIER_PROMPT_TEMPLATE = (
    "你是一个学习对齐裁判。学习者正在进入一卷学习；下面给出该卷的「学习目标」、"
    "「已沉淀概念」以及「最近几轮对话」与「本轮用户输入」。\n"
    "请基于这些上下文，判断用户本轮输入是否足够清晰，能让 AI 助手"
    "**只朝一个方向**展开教学。\n"
    "\n"
    "## 你要输出的字段\n"
    "- `candidates`: 列出「会导向不同 first_step」的可能解读，最多 {max_candidates} 个。\n"
    "    - 若两个解读的 `first_step` 实质相同，必须合并为一个。\n"
    "    - 每条 `objective` 是给用户挑选用的目标复述（一句话），\n"
    "      `first_step` 是配套的可操作切入点（一句话）。\n"
    "    - 若你能确定唯一解读，请只列 1 个；切勿为了凑数硬拆。\n"
    "- `divergence_cost`: 选错的代价。\n"
    "    - `high`：选错会把整卷带沟里，难以拉回（如不同学科领域、不同抽象层级）。\n"
    "    - `low`：选错也能在下一轮轻松拉回（只是切入点不同，知识半径接近）。\n"
    "- `reason`: 不清晰的根因，从以下枚举中选一个：\n"
    "    - `clear_enough`（候选只有 1 个时必须用这个）\n"
    "    - `too_broad`（候选 ≥2 且都属于「目标过大」的不同切口）\n"
    "    - `goal_drift`（候选 ≥2 且至少一个偏离了「学习目标」）\n"
    "    - `conflicting_scope`（候选 ≥2 且属于互斥的不同范围）\n"
    "    - `missing_learnable_target`（输入太模糊，连一个具体方向都拼不出）\n"
    "\n"
    "## 输出格式（严格 JSON 对象，不要其他文本，不要 markdown 围栏）\n"
    "```\n"
    '{{"candidates": [{{"objective": "...", "first_step": "..."}}, ...], '
    '"divergence_cost": "low" | "high", "reason": "clear_enough" | ...}}\n'
    "```\n"
    "\n"
    "## 学习目标\n"
    "{objective}\n"
    "\n"
    "## 已沉淀概念（按 relevance 降序，最多 8 个）\n"
    "{concepts_json}\n"
    "\n"
    "## 最近几轮对话（按时间升序，最后一条之前是历史）\n"
    "{recent_dialog}\n"
    "\n"
    "## 本轮用户输入\n"
    "{user_input}"
)


_VALID_REASONS: set[str] = {
    "clear_enough",
    "too_broad",
    "goal_drift",
    "conflicting_scope",
    "missing_learnable_target",
}


class AlignmentClassifier:
    """对齐分类器：一次 LLM 调用 + 一次 JSON 解析 + 一次 schema 映射。"""

    def __init__(
        self,
        provider: Any,
        model_name: Optional[str] = None,
        *,
        max_candidates: int = MAX_CANDIDATES,
        max_tokens: int = 1024,
    ):
        self.provider = provider
        self.model_name = model_name
        self.max_candidates = max(1, min(max_candidates, MAX_CANDIDATES))
        self.max_tokens = max_tokens

    async def classify(
        self,
        unit: LearningUnit,
        user_input: str,
        *,
        recent_messages: Optional[list[Any]] = None,
    ) -> AlignmentDecision:
        prompt = self._build_prompt(unit, user_input, recent_messages or [])
        model = self.model_name or getattr(self.provider, "default_model", None)
        if not model:
            logger.warning(
                "[AlignmentClassifier] No model resolved (model_name=None and "
                "provider.default_model missing); falling back to mode=none"
            )
            return _fail_open()

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
            logger.exception("[AlignmentClassifier] Provider call failed")
            return _fail_open()

        raw = (chunk.content or "").strip()
        parsed = _parse_json_object(raw)
        if parsed is None:
            logger.warning(
                "[AlignmentClassifier] Failed to parse JSON object from model "
                "response (head=%r)",
                raw[:200],
            )
            return _fail_open()

        decision = self._build_decision(parsed)
        # 复用 §9.3 #2/#3 限流：suggested 命中上限 / 冷静期内静默降级
        return _apply_rate_limits(unit, decision)

    def _build_prompt(
        self,
        unit: LearningUnit,
        user_input: str,
        recent_messages: list[Any],
    ) -> str:
        objective_text = (unit.objective.text or "").strip() or "（未明确）"

        concept_payload: list[dict[str, Any]] = []
        for concept in sorted(
            unit.concept_list, key=lambda c: c.relevance, reverse=True
        )[:8]:
            concept_payload.append(
                {
                    "name": concept.name,
                    "summary": concept.summary,
                    "relevance": round(concept.relevance, 3),
                }
            )

        dialog_lines: list[str] = []
        tail = recent_messages[-RECENT_MESSAGE_LIMIT:]
        for entry in tail:
            role = _coerce_role(entry)
            content = _coerce_content(entry)
            if not content:
                continue
            # 截断单条消息，避免对话历史压垮 prompt
            if len(content) > 400:
                content = content[:400] + "…"
            dialog_lines.append(f"[{role}] {content}")
        recent_dialog = "\n".join(dialog_lines) or "（无）"

        return _CLASSIFIER_PROMPT_TEMPLATE.format(
            max_candidates=self.max_candidates,
            objective=objective_text,
            concepts_json=json.dumps(concept_payload, ensure_ascii=False),
            recent_dialog=recent_dialog,
            user_input=user_input.strip() or "（空）",
        )

    def _build_decision(self, payload: dict[str, Any]) -> AlignmentDecision:
        raw_candidates = payload.get("candidates") or []
        candidates: list[Candidate] = []
        seen_first_steps: set[str] = set()
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if not isinstance(item, dict):
                    continue
                objective = (item.get("objective") or "").strip()
                first_step = (item.get("first_step") or "").strip()
                if not objective or not first_step:
                    continue
                key = first_step.lower()
                if key in seen_first_steps:
                    # first_step 实质相同 → 合并
                    continue
                seen_first_steps.add(key)
                candidates.append(
                    Candidate(objective=objective, first_step=first_step)
                )
                if len(candidates) >= self.max_candidates:
                    break

        divergence_raw = (payload.get("divergence_cost") or "").strip().lower()
        divergence_cost: Optional[str] = (
            divergence_raw if divergence_raw in ("low", "high") else None
        )

        reason_raw = (payload.get("reason") or "").strip()
        reason: AlignmentReason = (
            reason_raw if reason_raw in _VALID_REASONS else "clear_enough"
        )  # type: ignore[assignment]

        # 档位映射
        if len(candidates) <= 1:
            return AlignmentDecision(
                mode="none",
                reason="clear_enough",
                candidates=candidates,
                divergence_cost=divergence_cost,
            )

        if divergence_cost == "high":
            return AlignmentDecision(
                mode="active",
                reason=reason if reason != "clear_enough" else "missing_learnable_target",
                candidates=candidates,
                divergence_cost="high",
                assumption_note=_default_assumption_note(reason),
            )

        # 默认 low / None → suggested
        return AlignmentDecision(
            mode="suggested",
            reason=reason if reason != "clear_enough" else "too_broad",
            candidates=candidates,
            divergence_cost=divergence_cost or "low",
            suggested_objective=candidates[0].objective,
            assumption_note=_default_assumption_note(reason),
        )


def _fail_open() -> AlignmentDecision:
    """所有失败路径统一回落：不打扰用户、不切模式。"""
    return AlignmentDecision(mode="none", reason="clear_enough")


def _default_assumption_note(reason: str) -> str:
    if reason == "too_broad":
        return "目标范围较大，先按一个切口展开。"
    if reason == "conflicting_scope":
        return "一次列了多个学习目标，需要先挑一个主线。"
    if reason == "goal_drift":
        return "本轮输入偏离了原学习目标，先确认方向。"
    if reason == "missing_learnable_target":
        return "输入太短，无法判断你想学的具体对象。"
    return ""


def _coerce_role(entry: Any) -> str:
    role = getattr(entry, "role", None)
    if role is None and isinstance(entry, dict):
        role = entry.get("role")
    if hasattr(role, "value"):
        role = role.value
    return str(role) if role else "user"


def _coerce_content(entry: Any) -> str:
    content = getattr(entry, "content", None)
    if content is None and isinstance(entry, dict):
        content = entry.get("content")
    return str(content).strip() if content else ""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """尽力从模型回应里提取 JSON 对象（concept_extractor._parse_json_array 的对象版）。"""
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
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return None
    return parsed


__all__ = ["AlignmentClassifier", "MAX_CANDIDATES"]
