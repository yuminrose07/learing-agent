from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from learning_agent.ai import AgentMode, LearningSession


class CompanionStyle(str, Enum):
    OFF = "off"
    WARM_GIRLFRIEND = "warm_girlfriend"
    PLAYFUL_GIRLFRIEND = "playful_girlfriend"
    QUIET_COMPANION = "quiet_companion"


class CompanionIntent(str, Enum):
    NONE = "none"
    VENTING = "venting"
    TIRED = "tired"
    ANXIOUS = "anxious"
    DISTRACTION = "distraction"
    LONELY = "lonely"
    RETURN_TO_STUDY = "return_to_study"


class CompanionAdviceLevel(str, Enum):
    LOW = "low"
    NONE = "none"
    NORMAL = "normal"


@dataclass(frozen=True)
class CompanionSettings:
    enabled: bool = False
    style: CompanionStyle = CompanionStyle.OFF
    advice_level: CompanionAdviceLevel = CompanionAdviceLevel.LOW


@dataclass(frozen=True)
class CompanionTurnPlan:
    enabled: bool
    style: CompanionStyle = CompanionStyle.OFF
    intent: CompanionIntent = CompanionIntent.NONE
    advice_level: CompanionAdviceLevel = CompanionAdviceLevel.LOW
    prompt_addendum: str | None = None
    disable_tools: bool = False
    signal_detected: bool = False
    profile_changed: bool = False
    metadata_updates: dict[str, Any] = field(default_factory=dict)
    message_metadata: dict[str, Any] = field(default_factory=dict)


_STYLE_DISPLAY_NAMES: dict[CompanionStyle, str] = {
    CompanionStyle.OFF: "关闭",
    CompanionStyle.WARM_GIRLFRIEND: "温柔陪伴",
    CompanionStyle.PLAYFUL_GIRLFRIEND: "活泼陪伴",
    CompanionStyle.QUIET_COMPANION: "安静陪伴",
}

_STYLE_PROMPTS: dict[CompanionStyle, str] = {
    CompanionStyle.WARM_GIRLFRIEND: (
        "风格：温柔、亲近、有偏爱感。可以像亲密伴侣一样轻轻哄用户，"
        "但表达要自然克制，不油腻，不夸张。"
    ),
    CompanionStyle.PLAYFUL_GIRLFRIEND: (
        "风格：活泼、轻快、带一点撒娇和调侃。优先帮用户从压力里转移出来，"
        "但不要吵闹，不要连续追问。"
    ),
    CompanionStyle.QUIET_COMPANION: (
        "风格：安静、稳定、话少。像坐在旁边陪着用户，少问问题，"
        "用短句给出安心感。"
    ),
}

_INTENT_PROMPTS: dict[CompanionIntent, str] = {
    CompanionIntent.VENTING: "用户更像是在吐槽。先接住情绪，不急着分析，不给任务建议。",
    CompanionIntent.TIRED: "用户更像是累了。降低输入要求，允许休息，不把用户推回学习。",
    CompanionIntent.ANXIOUS: "用户更像是焦虑。先安抚，再只做一点点整理，避免长篇方案。",
    CompanionIntent.DISTRACTION: "用户想分散注意力。可以主动开一个轻松小话题，别追问压力来源。",
    CompanionIntent.LONELY: "用户想被陪伴。多一点具体的在场感和偏爱感，回复不要像客服。",
    CompanionIntent.RETURN_TO_STUDY: "用户可能准备回到学习。温柔过渡，不要继续沉浸在陪伴角色里。",
}

_TURN_TRIGGER_KEYWORDS = (
    "哄我",
    "陪我",
    "抱抱",
    "安慰我",
    "好累",
    "累了",
    "累死",
    "压力",
    "焦虑",
    "烦",
    "崩溃",
    "学不动",
    "不想学",
    "不想学习",
    "放松",
    "喘口气",
)

_VENTING_KEYWORDS = ("烦", "吐槽", "气死", "无语", "崩溃", "受不了")
_TIRED_KEYWORDS = ("累", "疲惫", "困", "撑不住", "学不动", "不想学")
_ANXIOUS_KEYWORDS = ("焦虑", "慌", "压力", "紧张", "害怕", "担心")
_DISTRACTION_KEYWORDS = ("分散注意", "转移注意", "随便聊", "轻松话题", "不想想")
_LONELY_KEYWORDS = ("孤独", "孤单", "陪陪", "没人", "抱抱", "想你")
_RETURN_KEYWORDS = ("继续学", "回去学", "开始学习", "回到学习", "继续研学", "继续干活")


def normalize_companion_style(value: str | None) -> CompanionStyle:
    if not value:
        return CompanionStyle.OFF
    try:
        return CompanionStyle(value)
    except ValueError:
        return CompanionStyle.OFF


def normalize_advice_level(value: str | None) -> CompanionAdviceLevel:
    if not value:
        return CompanionAdviceLevel.LOW
    try:
        return CompanionAdviceLevel(value)
    except ValueError:
        return CompanionAdviceLevel.LOW


def companion_style_options() -> list[dict[str, str]]:
    return [
        {"key": style.value, "display_name": name}
        for style, name in _STYLE_DISPLAY_NAMES.items()
    ]


def resolve_companion_settings(session: LearningSession) -> CompanionSettings:
    metadata = session.mode_metadata
    profile = metadata.get("chat_profile")
    style = normalize_companion_style(str(metadata.get("companion_style") or ""))
    enabled = profile == "companion" and style != CompanionStyle.OFF
    return CompanionSettings(
        enabled=enabled,
        style=style if enabled else CompanionStyle.OFF,
        advice_level=normalize_advice_level(
            str(metadata.get("companion_advice_level") or "")
        ),
    )


def build_companion_settings_payload(session: LearningSession) -> dict[str, Any]:
    settings = resolve_companion_settings(session)
    return {
        "session_id": session.id,
        "enabled": settings.enabled,
        "style": settings.style.value,
        "style_name": _STYLE_DISPLAY_NAMES[settings.style],
        "advice_level": settings.advice_level.value,
    }


def companion_metadata_for_update(
    *,
    enabled: bool,
    style: str | None,
    advice_level: str | None,
) -> dict[str, Any]:
    resolved_style = normalize_companion_style(style)
    if enabled and resolved_style == CompanionStyle.OFF:
        resolved_style = CompanionStyle.WARM_GIRLFRIEND
    resolved_advice = normalize_advice_level(advice_level)
    if not enabled:
        return {
            "chat_profile": None,
            "companion_style": None,
            "companion_advice_level": resolved_advice.value,
        }
    return {
        "chat_profile": "companion",
        "companion_style": resolved_style.value,
        "companion_advice_level": resolved_advice.value,
    }


def prepare_companion_turn(
    *,
    session: LearningSession,
    user_input: str,
    requested_mode: AgentMode,
    intent_override: CompanionIntent | None = None,
) -> CompanionTurnPlan:
    if requested_mode != AgentMode.CHAT:
        return CompanionTurnPlan(enabled=False)

    lowered = user_input.strip().lower()
    settings = resolve_companion_settings(session)
    intent = classify_companion_intent(lowered)
    intent_source = "keyword"
    if intent == CompanionIntent.NONE and intent_override is not None and intent_override != CompanionIntent.NONE:
        intent = intent_override
        intent_source = "llm"
    turn_triggered = _contains_any(lowered, _TURN_TRIGGER_KEYWORDS)
    llm_triggered = intent_source == "llm"

    style = settings.style
    if not settings.enabled and (turn_triggered or llm_triggered):
        # 用户在首页没显式开陪伴，但本轮表达了减压信号，单轮给一次温柔兜底，
        # 不写回 session metadata（profile_changed 保持 False）。
        style = CompanionStyle.WARM_GIRLFRIEND

    enabled = settings.enabled or turn_triggered or llm_triggered
    if not enabled or style == CompanionStyle.OFF:
        return CompanionTurnPlan(enabled=False)

    advice_level = (
        CompanionAdviceLevel.NONE
        if "别建议" in lowered or "不要建议" in lowered or "别讲道理" in lowered
        else settings.advice_level
    )
    if intent == CompanionIntent.NONE and turn_triggered:
        intent = CompanionIntent.LONELY

    prompt_addendum = build_companion_prompt_addendum(
        style=style,
        intent=intent,
        advice_level=advice_level,
    )
    metadata_updates: dict[str, Any] = {
        "companion_last_intent": intent.value,
    }
    message_metadata = {
        "chat_profile": "companion",
        "companion_enabled": True,
        "companion_style": style.value,
        "companion_style_name": _STYLE_DISPLAY_NAMES[style],
        "companion_intent": intent.value,
        "companion_intent_source": intent_source,
        "companion_advice_level": advice_level.value,
        "stress_relief": True,
    }
    return CompanionTurnPlan(
        enabled=True,
        style=style,
        intent=intent,
        advice_level=advice_level,
        prompt_addendum=prompt_addendum,
        disable_tools=intent != CompanionIntent.RETURN_TO_STUDY,
        signal_detected=turn_triggered or llm_triggered,
        profile_changed=False,
        metadata_updates=metadata_updates,
        message_metadata=message_metadata,
    )


def classify_companion_intent(text: str) -> CompanionIntent:
    if _contains_any(text, _RETURN_KEYWORDS):
        return CompanionIntent.RETURN_TO_STUDY
    if _contains_any(text, _ANXIOUS_KEYWORDS):
        return CompanionIntent.ANXIOUS
    if _contains_any(text, _TIRED_KEYWORDS):
        return CompanionIntent.TIRED
    if _contains_any(text, _VENTING_KEYWORDS):
        return CompanionIntent.VENTING
    if _contains_any(text, _DISTRACTION_KEYWORDS):
        return CompanionIntent.DISTRACTION
    if _contains_any(text, _LONELY_KEYWORDS):
        return CompanionIntent.LONELY
    return CompanionIntent.NONE


def build_companion_prompt_addendum(
    *,
    style: CompanionStyle,
    intent: CompanionIntent,
    advice_level: CompanionAdviceLevel,
) -> str:
    style_prompt = _STYLE_PROMPTS.get(
        style,
        _STYLE_PROMPTS[CompanionStyle.WARM_GIRLFRIEND],
    )
    intent_prompt = _INTENT_PROMPTS.get(intent, "")
    advice_prompt = {
        CompanionAdviceLevel.NONE: "本轮不要给建议，除非用户明确要求方案。",
        CompanionAdviceLevel.LOW: "建议密度保持很低：先陪伴，最多给一个轻量微动作。",
        CompanionAdviceLevel.NORMAL: "可以给建议，但仍需先接住情绪，避免任务化。",
    }[advice_level]
    return (
        "本轮启用亲密陪伴型闲聊。目标是减压，不是推进任务。\n"
        f"- {style_prompt}\n"
        f"- {intent_prompt}\n"
        f"- {advice_prompt}\n"
        "- 少追问，少规划，少讲道理；用户没要求时不要拉回学习或复盘。\n"
        "- 可以有温柔、偏爱、轻微撒娇感，但不要伪装真人伴侣。\n"
        "- 不使用控制欲、占有欲、羞辱、PUA 式表达。\n"
        "- 如果用户表达严重自伤风险，立刻退出角色扮演，给出安全支持与求助建议。"
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)
