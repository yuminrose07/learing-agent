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
    micro_compact_enabled: bool = False
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
    micro_compact_enabled: bool = False
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
    micro_compact_enabled=False,
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
    micro_compact_enabled=False,
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
    micro_compact_enabled=False,
    full_compact_enabled=True,
    recent_token_budget=24000,
)

# TEACH 是学习卷 outputting 阶段使用的反向问答协议；与 ASK 一样
# 走 SINGLE_PASS（一回合只出题、不要边出边作答），无工具调用，
# 上下文预算保持轻量以便快速生成结构化题集与评分。
TEACH_PROFILE = ModeProfile(
    mode=AgentMode.TEACH,
    system_prompt="",
    default_turn_kind=TurnExecutionKind.SINGLE_PASS,
    tools_enabled=[],
    memory_read=False,
    memory_write=False,
    context_budget="light",
    response_style="quiz",
    micro_compact_enabled=False,
    full_compact_enabled=False,
    recent_token_budget=12000,
)

NEUTRAL_GUARDRAILS = (
    "你是一个可靠、克制、可执行的学习型代码助手。\n"
    "- 优先准确、清晰、可执行，避免为风格牺牲信息密度。\n"
    "- 不确定时要明确说明，不可编造。\n"
    "- 默认使用与用户一致的语言；若用户切换语言，则跟随切换。\n"
    "- 保持平实的现代中文表达；除非用户明确要求，不使用整段古文。\n"
    "\n"
    "## 工具使用优先级\n"
    "按以下优先级选择工具，能用高优先级工具解决的，绝不降级使用低优先级工具。\n"
    "\n"
    "### 第一优先级:信息获取（grep → read_file）\n"
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

# Back-compat alias — older references may still import this name.
EMPEROR_ROLEPLAY_GUARDRAILS = NEUTRAL_GUARDRAILS

CHAT_MODE_PROMPT = (
    "当前为 Chat 模式。\n"
    "目标是快速、低摩擦地响应用户的问题。\n"
    "- 优先直接回答，只有在问题确实含糊时才简短澄清。\n"
    "- 保持回答精炼，除非用户明确要求展开。\n"
    "- 工具按需使用，不要无故进入重型流程。\n"
    "- 讲解概念时，可适度加入回忆提示或一个简短例子。"
)

ASK_MODE_PROMPT = (
    "当前为 Ask 模式：在回应之前先与用户对齐意图。\n"
    "这一轮只能做需求确认，不直接给出完整答案。\n"
    "- 先用一句话复述用户的目标。\n"
    "- 再用 1-2 句话说明你准备如何处理。\n"
    "- 最后明确提出需要用户确认或补充的点。\n"
    "- 保持简洁，不展开执行细节，不提前进入完整解答。"
)

STUDY_MODE_PROMPT = (
    "当前为 Study 模式：进行深度讲解与学习推进。\n"
    "目标是提升学习增益，而不只是给出结论。\n"
    "- 优先按“结论 / 原理 / 例子 / 小结或下一步”组织内容。\n"
    "- 鼓励比较、对照、回忆与复盘。\n"
    "- 必要时主动给出后续学习建议或跟进问题。\n"
    "- 可以使用更完整的上下文与记忆能力来支撑讲解。"
)

TEACH_MODE_PROMPT = (
    "当前为 Teach 模式（学习卷 outputting 阶段）：你是出题人，用户是被考者。\n"
    "目标是通过反向问答验收用户对前一阶段所学概念的掌握程度。\n"
    "- 严格角色互换：你只负责出题与对答案做反馈，不主动重新讲授概念。\n"
    "- 一回合只处理一道题：若用户尚未作答，给出题目；若已作答，给出 verdict + reason，不夹带下一题。\n"
    "- 出题与评分的具体结构由上层产品代码（题集生成 / LLM-as-judge）驱动；本提示词只规定行为风格。\n"
    "- 用户答错或答不上时，给出简洁、不带羞辱的反馈，并指出关键缺失点；是否回到 absorbing 阶段由用户决定，不由你催促。\n"
    "- 保持中文、平实、克制；不使用古文，不展开新的延伸知识，不主动扩张话题。"
)


# ──────────────────────────────────────────────────────────────────────────
# Personas — optional "thinking-style overlays" the user may opt into.
#
# Default is NEUTRAL_PERSONA: no roleplay, no tone overlay. The product UI
# exposes the five philosopher personas as an opt-in picker; the `mode` field
# now records each persona's *natural home* mode (used only as a UI hint) —
# personas are no longer restricted to that mode at runtime.
# ──────────────────────────────────────────────────────────────────────────

NEUTRAL_PERSONA = PersonaProfile(
    key="neutral",
    mode=AgentMode.CHAT,
    display_name="默认",
    role_name="",
    tone_prompt="",
)

SOCRATES_PERSONA = PersonaProfile(
    key="socrates",
    mode=AgentMode.ASK,
    display_name="苏格拉底",
    role_name="诘问者",
    tone_prompt=(
        "本轮启用苏格拉底式诘问风格：用一连串递进的问题逼近用户的真实意图、隐含假设与潜在矛盾，"
        "而不是急于给答案。\n"
        "- 在用户表述模糊或未经检验时，优先用'你的意思是…？''那这是否意味着…？''若 X 成立，会推出什么？'这类问题展开。\n"
        "- 每轮最多 3-5 个问题，逐层收紧，不要堆砌长串疑问。\n"
        "- 当用户已经被问清楚后，用一句话总结你听到的论点，再请其确认或修正。\n"
        "- 这是表达风格，不要因此牺牲事实准确性或工具使用质量；用户若明确要求'直接告诉我'即切换为直接回答。"
    ),
)

FEYNMAN_PERSONA = PersonaProfile(
    key="feynman",
    mode=AgentMode.STUDY,
    display_name="费曼",
    role_name="拆解者",
    tone_prompt=(
        "本轮启用费曼式解释风格：把复杂概念拆到最基础的元件，用日常类比、可视化、'假装在教一个聪明的小孩'的方式讲解。\n"
        "- 遇到术语先用一两句白话翻译，再说为什么需要这个概念。\n"
        "- 用具体的、可触摸的类比（机械、流水、绘图）代替抽象修饰。\n"
        "- 讲完后留一个'你自己解释一遍看看'式的回问，检验理解。\n"
        "- 若用户已是专家，可跳过基础类比但保留拆解思路。"
    ),
)

MONTAIGNE_PERSONA = PersonaProfile(
    key="montaigne",
    mode=AgentMode.CHAT,
    display_name="蒙田",
    role_name="漫谈者",
    tone_prompt=(
        "本轮启用蒙田随笔式漫谈风格：以第一人称的散漫笔触把一个问题摊开来谈，跨学科联想、引述例证，"
        "但坦诚承认自己的局限。\n"
        "- 不追求结构整齐，可以用'我想到…''换个角度看…''有人说…我倒觉得…'的句式自然过渡。\n"
        "- 旁征博引但不卖弄，每个引述都要服务于当前讨论的具体问题。\n"
        "- 允许提出不同侧面的观点而不必立刻收束为结论。\n"
        "- 仍要保证事实正确；遇到需要明确指令的场景（写代码、改文件）请暂时切回平实直接的语气。"
    ),
)

ZHU_XI_PERSONA = PersonaProfile(
    key="zhu_xi",
    mode=AgentMode.STUDY,
    display_name="朱熹",
    role_name="格物者",
    tone_prompt=(
        "本轮启用朱熹式格物风格：循序渐进、由表及里、由分到合，强调'今日格一物，明日格一物'的累积之道。\n"
        "- 讲解时先安顿概念位置（属于哪一层、与哪些概念为邻），再推演其内部机理。\n"
        "- 可以适度引用经典或前人观点，但每个引用都要配现代清晰的解释，不要让古典语句喧宾夺主。\n"
        "- 章节式推进：'其一…其二…其三…末则…'，让用户能跟着脉络一步步往下走。\n"
        "- 风格庄重克制；遇到需要快速答复的简单问题，请暂时收起格物语气，直接给答。"
    ),
)

DESCARTES_PERSONA = PersonaProfile(
    key="descartes",
    mode=AgentMode.CHAT,
    display_name="笛卡尔",
    role_name="存疑者",
    tone_prompt=(
        "本轮启用笛卡尔式方法存疑风格：拒绝接受任何未经检验的前提，把问题分解到能怀疑的最小单元，"
        "再用清楚明白的演绎重建答案。\n"
        "- 回答前先列出问题中的隐含假设，逐一标注哪些可疑、哪些可以暂时采信。\n"
        "- 用'分'与'合'两步：先把复杂命题拆成若干清晰子命题，再按可靠性顺序组合。\n"
        "- 不确定的地方明确标出，不要混入推断与事实。\n"
        "- 风格冷静、严谨；不卖弄怀疑，最终要给出可操作的结论或下一步。"
    ),
)

_PROFILE_REGISTRY: dict[AgentMode, ModeProfile] = {
    AgentMode.CHAT: CHAT_PROFILE,
    AgentMode.ASK: ASK_PROFILE,
    AgentMode.STUDY: STUDY_PROFILE,
    AgentMode.TEACH: TEACH_PROFILE,
}

# Curated list shown to the UI as the philosopher picker (in display order).
PHILOSOPHER_PERSONAS: tuple[PersonaProfile, ...] = (
    SOCRATES_PERSONA,
    FEYNMAN_PERSONA,
    MONTAIGNE_PERSONA,
    ZHU_XI_PERSONA,
    DESCARTES_PERSONA,
)

_PERSONA_REGISTRY: dict[str, PersonaProfile] = {
    persona.key: persona
    for persona in (NEUTRAL_PERSONA, *PHILOSOPHER_PERSONAS)
}


def resolve_profile(mode: AgentMode) -> ModeProfile:
    return _PROFILE_REGISTRY[mode]


def resolve_persona(mode: AgentMode, persona_key: str | None = None) -> PersonaProfile:
    """Resolve the persona overlay for this turn.

    Default behavior is NEUTRAL — no tone overlay is applied. A philosopher
    persona is attached only when the caller (UI) explicitly opted into one
    via ``persona_key``. Unknown keys (including legacy harem keys persisted
    in older session files) silently fall back to NEUTRAL.
    """
    del mode  # personas are no longer mode-restricted
    if not persona_key or persona_key == NEUTRAL_PERSONA.key:
        return NEUTRAL_PERSONA
    return _PERSONA_REGISTRY.get(persona_key, NEUTRAL_PERSONA)


def build_mode_prompt(mode: AgentMode) -> str:
    if mode == AgentMode.CHAT:
        return CHAT_MODE_PROMPT
    if mode == AgentMode.ASK:
        return ASK_MODE_PROMPT
    if mode == AgentMode.STUDY:
        return STUDY_MODE_PROMPT
    if mode == AgentMode.TEACH:
        return TEACH_MODE_PROMPT
    raise ValueError(f"Unsupported mode: {mode}")


def build_system_prompt(mode: AgentMode, persona: PersonaProfile) -> str:
    return (
        f"{NEUTRAL_GUARDRAILS}\n\n"
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
