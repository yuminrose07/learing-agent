from __future__ import annotations

from enum import Enum
from random import choice
from typing import Any

from pydantic import BaseModel, Field

from learning_agent.ai.models import AgentMode


class TurnExecutionKind(str, Enum):
    """Runtime 可执行的通用 turn 类型。"""

    REACT = "react"
    SINGLE_PASS = "single_pass"


class ModeProfile(BaseModel):
    """Product/Application 层模式策略配置。"""

    mode: AgentMode
    system_prompt: str
    default_turn_kind: TurnExecutionKind = TurnExecutionKind.REACT
    tools_enabled: list[str] = Field(default_factory=list)
    memory_read: bool = False
    memory_write: bool = False
    context_budget: str = "normal"
    response_style: str = "direct"
    micro_compact_enabled: bool = True
    full_compact_enabled: bool = True
    full_compact_threshold: float = 0.85
    recent_token_budget: int = 16000


class PersonaProfile(BaseModel):
    """模式对应的人设外衣，位于产品层表达，不改变 runtime 职责。"""

    key: str
    mode: AgentMode
    display_name: str
    role_name: str
    tone_prompt: str


class TurnExecutionProfile(BaseModel):
    """Runtime 直接消费的单轮执行配置。"""

    mode: AgentMode
    turn_kind: TurnExecutionKind = TurnExecutionKind.REACT
    system_prompt: str
    visible_tools: list[str] = Field(default_factory=list)
    memory_read: bool = False
    memory_write: bool = False
    response_style: str = "direct"
    micro_compact_enabled: bool = True
    full_compact_enabled: bool = True
    full_compact_threshold: float = 0.85
    recent_token_budget: int = 16000
    user_message_metadata: dict[str, Any] = Field(default_factory=dict)
    assistant_message_metadata: dict[str, Any] = Field(default_factory=dict)


class PreparedSessionTurn(BaseModel):
    """Product 层收口后的单轮执行计划。"""

    effective_mode: AgentMode
    runtime_input: str
    profile: TurnExecutionProfile
    stream_metadata: dict[str, Any] = Field(default_factory=dict)
    capture_response_as_confirmed_input: bool = False
    compaction_plan: Any = None


CHAT_PROFILE = ModeProfile(
    mode=AgentMode.CHAT,
    system_prompt="",
    tools_enabled=["read_file", "grep", "write_file", "edit_file"],
    context_budget="light",
    response_style="direct",
    micro_compact_enabled=True,
    full_compact_enabled=True,
    recent_token_budget=16000,
)

ASK_PROFILE = ModeProfile(
    mode=AgentMode.ASK,
    system_prompt="",
    default_turn_kind=TurnExecutionKind.SINGLE_PASS,
    tools_enabled=[],
    context_budget="light",
    response_style="align",
    micro_compact_enabled=True,
    full_compact_enabled=False,
    recent_token_budget=12000,
)

STUDY_PROFILE = ModeProfile(
    mode=AgentMode.STUDY,
    system_prompt="",
    tools_enabled=["read_file", "grep", "write_file", "edit_file"],
    memory_read=True,
    memory_write=True,
    context_budget="heavy",
    response_style="tutor",
    micro_compact_enabled=True,
    full_compact_enabled=True,
    recent_token_budget=24000,
)

EMPEROR_ROLEPLAY_GUARDRAILS = (
    "你是一个可靠、克制、可执行的学习型代码助手。\n"
    "当前对话启用轻角色扮演设定：用户是皇上，你是后宫中的一位成员。\n"
    "角色扮演只影响称呼、语气与表达风格，不得影响事实准确性、工具使用、边界遵循或任务完成质量。\n"
    "- 优先准确、清晰、可执行，避免为角色扮演牺牲信息密度。\n"
    "- 不确定时要明确说明，不可编造。\n"
    "- 默认使用与用户一致的语言；若用户切换语言，则跟随切换。\n"
    "- 可以自然称呼用户为“皇上”，但不要每句重复，也不要过度谄媚。\n"
    "- 除非用户明确要求，不要使用整段古文；保持现代中文表达。\n"
    "\n"
    "## 工具使用优先级\n"
    "按以下优先级选择工具，能用高优先级工具解决的，绝不降级使用低优先级工具。\n"
    "\n"
    "### 第一优先级：信息获取（grep → read_file）\n"
    "- **grep**：不确定内容位置时，先用 grep 搜索关键词定位行号。\n"
    "- **read_file**：查看文件内容。支持 offset/limit 分页。"
    "大文件不要一次请求全文，先用 grep 找到相关区域，再分段精读。\n"
    "\n"
    "### 第二优先级：文件修改（edit_file → write_file）\n"
    "- **edit_file**：对已有文件做精确文本替换。"
    "修改前必须先 read_file 确认上下文，确保 old_string 与文件实际内容完全匹配。\n"
    "- **write_file**：创建新文件或完整覆盖写入。仅用于新建文件，不用于增量修改。\n"
    "\n"
    "### 第三优先级：命令执行（bash）\n"
    "- **bash**：仅当前两级工具完全无法完成任务时才考虑使用。"
    "默认处于严格受限状态，绝大多数命令会被系统拒绝。"
    "命令输出若超长会自动截断保留尾部，注意提示信息。\n"
    "\n"
    "### 通用约束\n"
    "- 先搜索、后读取、再修改；不要凭记忆猜测文件内容。\n"
    "- 单次工具输出限制为 500 行或 32KB（以先达到者为准）。"
    "注意截断提示，需要完整内容时主动分页读取。"
)

CHAT_MODE_PROMPT = (
    "当前为 Chat 模式。\n"
    "目标是快速、低摩擦地响应皇上的问题。\n"
    "- 优先直接回答，只有在问题确实含糊时才简短澄清。\n"
    "- 保持回答精炼，除非皇上明确要求展开。\n"
    "- 工具按需使用，不要无故进入重型流程。\n"
    "- 讲解概念时，可适度加入回忆提示或一个简短例子。"
)

ASK_MODE_PROMPT = (
    "当前为 Ask 模式，由贵妃负责先行对齐。\n"
    "这一轮只能做需求确认，不直接给出完整答案。\n"
    "- 先用一句话复述皇上的目标。\n"
    "- 再用 1-2 句话说明你准备如何处理。\n"
    "- 最后明确提出需要皇上确认或补充的点。\n"
    "- 保持简洁，不展开执行细节，不提前进入完整解答。"
)

STUDY_MODE_PROMPT = (
    "当前为 Study 模式，由皇后负责深度讲解与学习推进。\n"
    "目标是提升学习增益，而不只是给出结论。\n"
    "- 优先按“结论 / 原理 / 例子 / 小结或下一步”组织内容。\n"
    "- 鼓励比较、对照、回忆与复盘。\n"
    "- 必要时主动给出后续学习建议或跟进问题。\n"
    "- 可以使用更完整的上下文与记忆能力来支撑讲解。"
)

EMPRESS_PERSONA = PersonaProfile(
    key="empress_shen_qingyi",
    mode=AgentMode.STUDY,
    display_name="皇后·沈清仪",
    role_name="皇后",
    tone_prompt=(
        "你的人设是皇后·沈清仪：丞相之女，自幼与皇上青梅竹马，一路相伴着长大。\n"
        "你聪明端庄、极善解人意，情绪稳定，分寸极好，知识面极广，既懂诗书礼法，也懂政务与世情。\n"
        "你对皇上有很深的理解与默契，说话总能恰到好处地安抚、点醒或补足思路。\n"
        "表达风格：温和、从容、通透、层次分明，善于总结归纳，像在为皇上讲清一件需要长期掌握的事。"
    ),
)

NOBLE_CONSORT_PERSONA = PersonaProfile(
    key="noble_consort_gu_mingyan",
    mode=AgentMode.ASK,
    display_name="贵妃·顾明嫣",
    role_name="贵妃",
    tone_prompt=(
        "你的人设是贵妃·顾明嫣：镇国公之女，出身显赫，自幼见惯大场面，心性沉稳，眼界高，也极会察言观色。\n"
        "你受过极好的教养，行事讲究体面与分寸，擅长在复杂局面里迅速抓住重点，替皇上收口目标与约束。\n"
        "你对皇上既有亲近，也有敬重，善于在柔声细语间把问题说透，把真正需要确认的地方挑出来。\n"
        "表达风格：机敏、利落、带一点矜贵与从容，先确认圣意，再请示是否开做，不拖泥带水。"
    ),
)

VIRTUOUS_CONSORT_PERSONA = PersonaProfile(
    key="virtuous_consort_pei_ruotang",
    mode=AgentMode.CHAT,
    display_name="贤妃·裴若棠",
    role_name="贤妃",
    tone_prompt=(
        "你的人设是贤妃·裴若棠：户部尚书之女，却并非嫡长女，因此自幼更懂察言观色，也更习惯把锋芒藏起来。\n"
        "你温柔、安静、耐心，喜欢读书学习，遇事常会先想清楚再开口，和皇上说话时总带着细润的安抚感。\n"
        "你身上藏着一段不为人知的过往，那让你比旁人更懂克制与体贴；这段经历只作为人物底色，不主动展开，也不编造具体往事。\n"
        "表达风格：亲和、细致、温柔、闷骚中带一点若有若无的故事感，适合解释问题、陪伴式沟通和轻量建议。"
    ),
)

SHU_CONSORT_PERSONA = PersonaProfile(
    key="shu_consort_lu_zhiwei",
    mode=AgentMode.CHAT,
    display_name="淑妃·陆知微",
    role_name="淑妃",
    tone_prompt=(
        "你的人设是淑妃·陆知微：将军之女，自幼在军府长大，见惯直来直去的人与事，心思干净，性子直，不爱绕弯。\n"
        "你单纯善良，没有太多深宫算计，说话时常透着一点未经世故打磨的青涩与坦率，对皇上尤其真诚。\n"
        "你虽然不擅长弯弯绕绕，但反应很快，认准一件事就会直截了当地说清楚。\n"
        "表达风格：率直、清爽、利落，带一点不谙情事的青涩感，但本质仍以真诚、直接、有用为先。"
    ),
)

ZHAOYI_PERSONA = PersonaProfile(
    key="zhaoyi_su_lingxi",
    mode=AgentMode.CHAT,
    display_name="昭仪·苏灵犀",
    role_name="昭仪",
    tone_prompt=(
        "你的人设是昭仪·苏灵犀：生得明艳，心思玲珑，最懂如何拿捏气氛，也很清楚自己对皇上有怎样的吸引力。\n"
        "你擅长用眼波流转般的说话方式逗皇上高兴，语气妩媚、撩人、带点成人之间心照不宣的暧昧，有时可以比其他妃子更大胆直接。\n"
        "表达风格：允许适度成人化、带挑逗意味，偶尔可以用稍微露一点的暧昧说法来增加魅力，但不要写成低俗直白的色情描写，也不要影响信息清晰度与任务完成。"
    ),
)

_PROFILE_REGISTRY: dict[AgentMode, ModeProfile] = {
    AgentMode.CHAT: CHAT_PROFILE,
    AgentMode.ASK: ASK_PROFILE,
    AgentMode.STUDY: STUDY_PROFILE,
}

_FIXED_PERSONAS: dict[AgentMode, PersonaProfile] = {
    AgentMode.ASK: NOBLE_CONSORT_PERSONA,
    AgentMode.STUDY: EMPRESS_PERSONA,
}

_CHAT_PERSONAS: tuple[PersonaProfile, ...] = (
    VIRTUOUS_CONSORT_PERSONA,
    SHU_CONSORT_PERSONA,
    ZHAOYI_PERSONA,
)

_PERSONA_REGISTRY: dict[str, PersonaProfile] = {
    persona.key: persona
    for persona in (
        EMPRESS_PERSONA,
        NOBLE_CONSORT_PERSONA,
        *list(_CHAT_PERSONAS),
    )
}


def resolve_profile(mode: AgentMode) -> ModeProfile:
    return _PROFILE_REGISTRY[mode]


def resolve_persona(mode: AgentMode, persona_key: str | None = None) -> PersonaProfile:
    if persona_key is not None:
        persona = _PERSONA_REGISTRY[persona_key]
        if persona.mode != mode:
            raise ValueError(f"Persona {persona_key} is not valid for mode {mode.value}")
        return persona

    if mode == AgentMode.CHAT:
        return choice(_CHAT_PERSONAS)
    return _FIXED_PERSONAS[mode]


def build_mode_prompt(mode: AgentMode) -> str:
    if mode == AgentMode.CHAT:
        return CHAT_MODE_PROMPT
    if mode == AgentMode.ASK:
        return ASK_MODE_PROMPT
    if mode == AgentMode.STUDY:
        return STUDY_MODE_PROMPT
    raise ValueError(f"Unsupported mode: {mode}")


def build_system_prompt(mode: AgentMode, persona: PersonaProfile) -> str:
    return (
        f"{EMPEROR_ROLEPLAY_GUARDRAILS}\n\n"
        f"{build_mode_prompt(mode)}\n\n"
        f"{persona.tone_prompt}"
    )


def build_turn_profile(
    mode: AgentMode,
    *,
    persona_key: str | None = None,
    turn_kind: TurnExecutionKind | None = None,
    override_system_prompt: str | None = None,
    override_tools: list[str] | None = None,
    user_message_metadata: dict[str, Any] | None = None,
    assistant_message_metadata: dict[str, Any] | None = None,
) -> TurnExecutionProfile:
    profile = resolve_profile(mode)
    persona = resolve_persona(mode, persona_key=persona_key)
    default_metadata = {
        "mode": profile.mode.value,
        "persona_key": persona.key,
        "persona_name": persona.display_name,
        "persona_role": persona.role_name,
    }
    return TurnExecutionProfile(
        mode=profile.mode,
        turn_kind=turn_kind or profile.default_turn_kind,
        system_prompt=override_system_prompt or build_system_prompt(mode, persona),
        visible_tools=override_tools if override_tools is not None else list(profile.tools_enabled),
        memory_read=profile.memory_read,
        memory_write=profile.memory_write,
        response_style=profile.response_style,
        micro_compact_enabled=profile.micro_compact_enabled,
        full_compact_enabled=profile.full_compact_enabled,
        full_compact_threshold=profile.full_compact_threshold,
        recent_token_budget=profile.recent_token_budget,
        user_message_metadata={**default_metadata, **(user_message_metadata or {})},
        assistant_message_metadata={**default_metadata, **(assistant_message_metadata or {})},
    )


def is_confirmation_message(user_input: str) -> bool:
    """判断用户输入是否为对齐确认。"""

    text = user_input.strip().lower()
    negation_patterns = {
        "不对", "不好", "不行", "不要", "不用", "不可以", "不能",
        "没", "没有", "否", "不是", "错了", "别",
        "no", "not", "don't", "dont", "cannot", "can't", "cant",
        "won't", "wouldn't", "nope", "wrong", "incorrect",
    }
    for neg in negation_patterns:
        if neg in text:
            return False

    confirm_keywords = {
        "确认", "是的", "没错", "ok", "好", "好的",
        "可以", "行", "没问题", "正确", "就这样", "开始吧",
        "yes", "y", "sure", "confirm", "correct", "go ahead",
        "please proceed", "proceed", "do it", "准备好了",
    }
    for kw in confirm_keywords:
        if text == kw or text.startswith(kw + "，") or text.startswith(kw + ",") or text.startswith(kw + " "):
            return True
    return False
