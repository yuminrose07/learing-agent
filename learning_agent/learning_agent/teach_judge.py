"""学习卷 outputting 阶段的答案评判器（B5 E3）。

落实任务拆解 B5 §3 与设计文档 §3.6：

- 接收一道 ``TeachQuestion`` 以及学习者的 ``user_answer``，调用 cheap-tier
  LLM 充当 judge，输出 verdict ∈ {"passed", "needs_review"} 与一段
  简短 ``judge_reason`` 解释。
- LLM-as-judge 仅校验"是否命中 rubric 里写明的关键点"，不要求逐字相同。
- 失败容忍：解析失败 / provider 抛错 → 默认 ``verdict="needs_review"`` +
  一段告警，把"判错"的成本压到最低（宁可让学习者再答一次，不要冒充通过）。

模式参考 ``concept_extractor.py`` / ``teach_generator.py``；不同点在于
输出是单个对象而非数组——见 ``_parse_json_object``。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from learning_agent.ai.learning_unit import QuestionVerdict, TeachQuestion
from learning_agent.ai.models import ChatMessage, ChatParams, MessageRole

logger = logging.getLogger(__name__)


_JUDGE_PROMPT_TEMPLATE = (
    "你是一名严格但公平的学习评估助教。学习者刚刚回答了一道简答题，"
    "你需要根据出题人写下的「判分标准（rubric）」判断这次作答是否过关。\n"
    "\n"
    "判分原则：\n"
    "- 只看是否命中 rubric 里写明的关键点；不要追求遣词造句完美。\n"
    "- 学习者用自己的话表达即可。allow paraphrasing。\n"
    "- 如果关键点全部命中 → `verdict=\"passed\"`；任何关键点缺失或答错 → "
    "`verdict=\"needs_review\"`。\n"
    "- `reason` 必须是一句话，告诉学习者「你哪个点对了 / 哪个点还差」，"
    "不要超过 120 字。\n"
    "\n"
    "## 输出格式（严格 JSON 对象，不要其他文本，不要 markdown 围栏）\n"
    "```\n"
    '{{"verdict": "passed" | "needs_review", "reason": "..."}}\n'
    "```\n"
    "\n"
    "## 题目\n"
    "{stem}\n"
    "\n"
    "## 判分标准（rubric）\n"
    "{rubric}\n"
    "\n"
    "## 学习者作答\n"
    "{user_answer}"
)


class TeachJudge:
    """单题答案评判器：一次 LLM 调用 + 一次 JSON 解析 + 兜底为 needs_review。"""

    def __init__(
        self,
        provider: Any,
        model_name: Optional[str] = None,
        *,
        max_tokens: int = 512,
    ):
        self.provider = provider
        self.model_name = model_name
        self.max_tokens = max_tokens

    async def judge(
        self,
        *,
        question: TeachQuestion,
        user_answer: str,
    ) -> tuple[QuestionVerdict, str]:
        if not user_answer.strip():
            # 空白作答直接判 needs_review，不打 LLM
            return "needs_review", "答案为空，请用自己的话再答一次。"

        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            stem=question.stem,
            rubric=question.rubric or "（出题人未提供 rubric，凭常识判断）",
            user_answer=user_answer.strip(),
        )
        model = self.model_name or getattr(self.provider, "default_model", None)
        if not model:
            logger.warning(
                "[TeachJudge] No model resolved (model_name=None and "
                "provider.default_model missing); falling back to needs_review"
            )
            return "needs_review", "评判服务暂不可用，请稍后再试。"

        try:
            chunk = await self.provider.chat(
                ChatParams(
                    model=model,
                    messages=[
                        ChatMessage(role=MessageRole.USER, content=prompt),
                    ],
                    temperature=0.0,
                    stream=False,
                    max_tokens=self.max_tokens,
                )
            )
        except Exception:
            logger.exception("[TeachJudge] Provider call failed")
            return "needs_review", "评判服务调用失败，请稍后再试。"

        raw = (chunk.content or "").strip()
        parsed = _parse_json_object(raw)
        if not parsed:
            logger.warning(
                "[TeachJudge] Failed to parse JSON object from model "
                "response (head=%r)",
                raw[:200],
            )
            return "needs_review", "判分结果格式异常，按未通过处理。"

        verdict_raw = (parsed.get("verdict") or "").strip().lower()
        reason = (parsed.get("reason") or "").strip()
        if verdict_raw not in ("passed", "needs_review"):
            logger.warning(
                "[TeachJudge] Unknown verdict %r; coercing to needs_review",
                verdict_raw,
            )
            return "needs_review", reason or "判分结果非法，按未通过处理。"

        if not reason:
            reason = (
                "已通过。" if verdict_raw == "passed" else "未通过，请再想想 rubric 里的关键点。"
            )
        # 截断超长 reason，避免 LLM 不听话糊一堆字
        if len(reason) > 240:
            reason = reason[:240] + "…"
        return verdict_raw, reason  # type: ignore[return-value]


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """尽力从模型回应里提取一个 JSON 对象（与 _parse_json_array 同套路）。"""
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


__all__ = ["TeachJudge"]
