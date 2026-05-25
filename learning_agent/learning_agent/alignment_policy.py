"""自适应对齐策略（adaptive alignment §5.3 / §9.2）。

把"是否需要对齐 / 收窄"的判断收口到一个纯函数 ``should_run_alignment``。
Product 层 (``_prepare_learning_unit_turn``) 调用它拿 ``AlignmentDecision``，
据此决定本轮 effective_mode 是 CHAT / ASK，以及是否给 UI 暴露建议条。

一阶段启发式：只用关键词 + 长度 + 学习卷状态做判定，不引入 LLM 意图分类器。
后续 (B4 之后) 可以把 ``recent_messages`` 与 concept_list 用起来做"中途纠偏"。
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel

from learning_agent.ai.learning_unit import LearningUnit

AlignmentMode = Literal["none", "suggested", "active"]
AlignmentReason = Literal[
    "clear_enough",
    "too_broad",
    "goal_drift",
    "conflicting_scope",
    "missing_learnable_target",
    # 用户主动触发的对齐（POST /align）。policy 不会自己产出这个原因，
    # 仅作为 Product 层调用 _apply_alignment_decision 时的合法值。
    "user_request",
]


class AlignmentDecision(BaseModel):
    """``should_run_alignment`` 的判定结果。

    - ``mode``: 三档输出
        - ``none``: A 档，直接进 absorbing
        - ``suggested``: B 档，进 absorbing 但 UI 给非阻塞建议条
        - ``active``: C 档，本轮临时覆写为 ASK 做一次澄清
    - ``reason``: 触发档位的原因，进事件 payload 供观测
    - ``suggested_objective``: B 档时给 UI 显示的"建议收窄成 X"文本
    - ``assumption_note``: A 档"系统替你假设"或 B 档"按 X 切口"的简短说明
    """

    mode: AlignmentMode
    reason: AlignmentReason
    suggested_objective: str = ""
    assumption_note: str = ""


# ─── 启发式词表 ───
# 不打算做成可配置 —— 命中规则在 §5.3 已经明确，词表足够稳，
# 若误判可以从单测看清楚边界再补。

# 表达"过宽范围"的中英文标记 —— 命中即视为"目标太大"
_TOO_BROAD_PATTERNS: tuple[str, ...] = (
    "整个项目",
    "整个仓库",
    "整个代码库",
    "全部",
    "所有",
    "全套",
    "全栈",
    "一切",
    "everything",
    "all of",
    "whole project",
    "entire project",
    "entire codebase",
)

# 表达"没有可学对象"的标记 —— 命中即视为 missing_learnable_target
_VAGUE_OBJECT_PATTERNS: tuple[str, ...] = (
    "讲讲这个",
    "教我这个",
    "教教我",
    "随便讲",
    "随便聊",
    "什么都行",
    "都可以",
    "tell me about this",
    "teach me this",
)

# 学习意图动词 —— 用来区分"有学习意图但太宽" vs "完全没意图"
_STUDY_VERBS: tuple[str, ...] = (
    "学",
    "教",
    "讲",
    "解释",
    "理解",
    "了解",
    "学习",
    "学一下",
    "搞懂",
    "弄清楚",
    "learn",
    "teach",
    "explain",
    "understand",
    "study",
)

# 多目标分隔符 —— 强列举型（顿号 / 分号 / 半角逗号）。
# 故意不包含"和" / "with"，因为它们也用在"A 和 B 的关系"这种单一目标里。
_MULTI_OBJECT_SEPARATORS: tuple[str, ...] = ("、", "；", ";", ",")

# C 档触发的最小可学习 token 数（中文按字符计，英文按词计）
_MIN_LEARNABLE_TOKENS = 4

# §9.3 #2：每个 unit 启动期最多弹 ``_MAX_SUGGESTIONS_PER_UNIT`` 条非阻塞建议；
# 超过后即使 policy 仍判 B 档也会被静默降级为 A 档，避免反复唠叨。
_MAX_SUGGESTIONS_PER_UNIT = 2

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _token_count(text: str) -> int:
    """混合文本的 token 估计。

    中文按汉字数，英文按 ``\\w+`` 切分，取 ``max`` 作为"用户给的信息量"估计。
    阈值用得保守 —— 宁可漏 C 档（不打断），不要误打断。
    """
    cn_chars = sum(1 for ch in text if "一" <= ch <= "鿿")
    en_words = len(_WORD_RE.findall(text))
    return max(cn_chars, en_words)


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(p in text or p in lowered for p in patterns)


def _count_distinct_objects(text: str) -> int:
    """粗略估计输入中有几个学习对象。用分隔符切片后看每片是否包含名词性 token。"""
    pieces = [text]
    for sep in _MULTI_OBJECT_SEPARATORS:
        new_pieces: list[str] = []
        for piece in pieces:
            new_pieces.extend(p.strip() for p in piece.split(sep) if p.strip())
        pieces = new_pieces
    # 每段 ≥ 1 token 才算"有效对象"。强列举分隔符已经过滤掉连词副词残片，
    # 阈值放宽是为了让"A、B、C"三项列举能被识别为多目标。
    return sum(1 for p in pieces if _token_count(p) >= 1)


def _mentions_known_concept(unit: LearningUnit, text: str) -> bool:
    """``text`` 是否提到了 ``unit.concept_list`` 中的任一概念名。

    用于豁免短输入的 C 档：若用户输入虽短但已点名一个 unit 已抽到的概念，
    则视为 "在已知地图上深挖"，不应该被打断要求澄清。
    """
    if not unit.concept_list:
        return False
    lowered = text.lower()
    for concept in unit.concept_list:
        if concept.name and concept.name.lower() in lowered:
            return True
    return False


def should_run_alignment(
    unit: LearningUnit,
    user_input: str,
    recent_messages: Optional[list] = None,  # noqa: ARG001 — B4 之后接入"中途纠偏"
) -> AlignmentDecision:
    """三档自适应对齐判定（adaptive alignment §5.3）。

    判定顺序：先看"完全不能学"(C)，再看"明显过宽"(B)，剩下都视为"能直接学"(A)。
    最后再叠加 §9.3 限流：suggested 命中上限或冷静期内将被降级为 none。

    一阶段限制：只用单轮 user_input + unit.concept_list 做判定；
    ``recent_messages`` 在 B4 之后接入"中途纠偏"路径。
    """
    decision = _raw_judgment(unit, user_input)
    return _apply_rate_limits(unit, decision)


def _raw_judgment(unit: LearningUnit, user_input: str) -> AlignmentDecision:
    """启发式分档主体；不感知任何持久化计数器，保证可独立单测。"""
    text = user_input.strip()

    # ─── C 档：missing_learnable_target / conflicting_scope ───

    # 短输入豁免：若已点名某个已知 concept，不再视为模糊
    if _token_count(text) < _MIN_LEARNABLE_TOKENS and not _mentions_known_concept(
        unit, text
    ):
        return AlignmentDecision(
            mode="active",
            reason="missing_learnable_target",
            assumption_note="输入太短，无法判断你想学的具体对象。",
        )

    if _contains_any(text, _VAGUE_OBJECT_PATTERNS):
        return AlignmentDecision(
            mode="active",
            reason="missing_learnable_target",
            assumption_note="代词指代不明，无法定位具体学习对象。",
        )

    has_study_intent = _contains_any(text, _STUDY_VERBS)

    # 多目标冲突：≥3 个分隔的对象 + 有学习意图 → 让用户先挑一个
    if has_study_intent and _count_distinct_objects(text) >= 3:
        return AlignmentDecision(
            mode="active",
            reason="conflicting_scope",
            assumption_note="一次列了多个学习目标，需要先挑一个主线。",
        )

    # ─── B 档：too_broad ───

    if has_study_intent and _contains_any(text, _TOO_BROAD_PATTERNS):
        return AlignmentDecision(
            mode="suggested",
            reason="too_broad",
            assumption_note="目标范围较大，先按一个切口展开。",
        )

    # ─── A 档：clear_enough ───

    return AlignmentDecision(
        mode="none",
        reason="clear_enough",
    )


def _apply_rate_limits(
    unit: LearningUnit, decision: AlignmentDecision
) -> AlignmentDecision:
    """§9.3 #2 / #3 限流：suggested 命中上限或冷静期内静默降为 none。

    - #2 ``suggestion_count >= _MAX_SUGGESTIONS_PER_UNIT``：单卷已弹过 2 条建议，
      不再让 UI 出第三条；目标已经被"宽"了两次，再弹只是噪声。
    - #3 ``nag_cooldown_remaining > 0``：用户已经按下"先按这个学"，N 轮内豁免建议。
    其它情形（active、none）保持原状 —— C 档由 ``clarification_count`` 在调度器
    侧单独限流，A 档本就不打扰。
    """
    if decision.mode != "suggested":
        return decision
    if unit.suggestion_count >= _MAX_SUGGESTIONS_PER_UNIT:
        return AlignmentDecision(mode="none", reason="clear_enough")
    if unit.nag_cooldown_remaining > 0:
        return AlignmentDecision(mode="none", reason="clear_enough")
    return decision


__all__ = [
    "AlignmentDecision",
    "AlignmentMode",
    "AlignmentReason",
    "should_run_alignment",
]
