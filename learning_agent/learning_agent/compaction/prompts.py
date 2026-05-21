from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .full_compact import estimate_text_tokens
from .models import CompactMode, CompactSourceUnit, SessionMemoryState

TEMPLATE_VERSION = "compact-summary-v1"

SUMMARY_TITLES_BASE = [
    "1. Primary Learning Request and Intent",
    "2. Learning Context and Goals",
    "3. Key Concepts, Explanations, and Examples",
    "4. Materials, Files, and External Artifacts",
    "5. Errors, Misunderstandings, and Corrections",
    "6. All User Messages and Feedback",
    "7. Pending Learning Tasks",
]

CONTINUATION_TITLES = [
    "8. Work Completed in Summarized Portion",
    "9. Context for Continuing Recent Messages",
]

CURRENT_STATE_TITLES = [
    "8. Current Learning State",
    "9. Optional Next Step with Verbatim Anchor",
]

MODE_INSTRUCTIONS = {
    CompactMode.AUTO_PREFIX.value: (
        "You are summarizing the earlier portion of a learning-agent conversation.\n"
        "Newer messages and the current user message will be preserved verbatim after\n"
        "your summary. Do not invent those newer messages. Write historical context\n"
        "that lets the conversation continue naturally from the retained messages."
    ),
    CompactMode.SLACT_FULL.value: (
        "You are creating a canonical summary of the selected conversation history.\n"
        "The original selected history will be replaced by this summary. Preserve enough\n"
        "detail for the session to continue without rereading the original transcript."
    ),
    CompactMode.SLACT_FROM.value: (
        "You are summarizing the later portion of the conversation after a user-selected\n"
        "pivot. Earlier messages remain verbatim before your summary. Focus on what\n"
        "changed, what was decided, what the user corrected, and what remains pending."
    ),
    CompactMode.SLACT_UP_TO.value: (
        "You are summarizing the earlier portion of the conversation up to a user-selected\n"
        "pivot. Later messages remain verbatim after your summary. Write only the\n"
        "historical context needed to understand those later messages."
    ),
    "incremental": (
        "You are updating an existing compact summary with a new source delta. Produce\n"
        "a fresh canonical nine-section summary, not an append-only changelog. Preserve\n"
        "stable facts from the existing summary and integrate newer source facts."
    ),
    "rebase": (
        "You are rebasing an existing compact summary and new source delta into one\n"
        "canonical summary. Remove stale tasks, resolve contradictions in favor of newer\n"
        "explicit user feedback, and output a single coherent summary."
    ),
}


@dataclass
class CompactPromptSpec:
    session_id: str
    mode: str
    scope: str
    summary_position: str
    source_event_start_seq: int | None = None
    source_event_end_seq: int | None = None
    source_snapshot_seq: int | None = None
    current_user_event_id: str | None = None
    source_event_ids: list[str] = field(default_factory=list)
    source_entry_ids: list[str] = field(default_factory=list)
    retained_entry_ids: list[str] = field(default_factory=list)
    retained_policy: str = ""
    source_units: list[CompactSourceUnit] = field(default_factory=list)
    existing_summary: str | None = None
    session_memory_state: SessionMemoryState | None = None
    template_version: str = TEMPLATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_units"] = [unit.to_dict() for unit in self.source_units]
        data["session_memory_state"] = (
            self.session_memory_state.to_dict() if self.session_memory_state else None
        )
        return data


@dataclass
class CompactSummaryValidation:
    valid: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary_hash: str = ""
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def section_titles_for_mode(mode: str) -> list[str]:
    tail = (
        CONTINUATION_TITLES
        if mode in {CompactMode.AUTO_PREFIX.value, CompactMode.SLACT_UP_TO.value}
        else CURRENT_STATE_TITLES
    )
    return [*SUMMARY_TITLES_BASE, *tail]


def summary_position_for_mode(mode: str) -> str:
    if mode == CompactMode.SLACT_FROM.value:
        return "after_retained"
    if mode == CompactMode.SLACT_FULL.value:
        return "replace_history"
    return "before_retained"


def retained_policy_for_mode(
    mode: str,
    *,
    cut_point_entry_id: str | None,
    current_user_event_id: str | None,
    retained_entry_ids: list[str],
) -> str:
    retained_count = len(retained_entry_ids)
    if mode == CompactMode.SLACT_FULL.value:
        return "The selected history is replaced by this summary. No ordinary conversation messages are retained unless explicitly listed."
    if mode == CompactMode.SLACT_FROM.value:
        return (
            f"Earlier safe units before pivot/cut point {cut_point_entry_id or 'unknown'} "
            f"remain verbatim before this summary. Retained entries: {retained_count}."
        )
    return (
        f"Recent safe units from cut_point_entry_id={cut_point_entry_id or 'unknown'} "
        f"and current_user_event_id={current_user_event_id or 'none'} are preserved "
        f"verbatim after this summary. Retained entries: {retained_count}. Do not "
        "write a next step that competes with the retained latest user request."
    )


def render_compact_prompt(spec: CompactPromptSpec) -> str:
    instruction = MODE_INSTRUCTIONS.get(spec.scope) or MODE_INSTRUCTIONS.get(spec.mode) or MODE_INSTRUCTIONS[CompactMode.AUTO_PREFIX.value]
    section_titles = section_titles_for_mode(spec.mode)
    range_text = _range_text(spec.source_event_start_seq, spec.source_event_end_seq)
    return "\n".join(
        [
            f'<compact_prompt version="{spec.template_version}">',
            "<no_tools_preamble>",
            "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.",
            "- Do NOT use read_file, grep, write_file, edit_file, bash, web, memory, or any other tool.",
            "- You already have all the context you need in the structured blocks below.",
            "- Tool calls will be rejected and will waste your only summarization turn.",
            "- Output exactly one <analysis> block followed by exactly one <summary> block.",
            "</no_tools_preamble>",
            "",
            "<task>",
            f"mode: {spec.mode}",
            f"scope: {spec.scope}",
            f"summary_position: {spec.summary_position}",
            "source_kind: CompactionSourceView.safe_units",
            f"source_event_range: {range_text}",
            f"source_snapshot_seq: {spec.source_snapshot_seq}",
            f"current_user_event_id: {spec.current_user_event_id or ''}",
            f"template_version: {spec.template_version}",
            "</task>",
            "",
            "<task_instruction>",
            instruction,
            "</task_instruction>",
            "",
            "<hard_rules>",
            "- Summarize ONLY <source_transcript>.",
            "- Do not summarize, invent, or update retained messages that are described only in <retained_policy>.",
            "- Preserve the user's explicit goals, corrections, preferences, and latest relevant wording.",
            "- Keep tool calls and tool results as completed interactions; never imply a tool result exists if it is not in source.",
            "- Preserve artifact_ref values when they are needed to continue work, but do not expand or invent artifact content.",
            "- Sensitive content omitted from source must remain omitted.",
            "- Remove obsolete pending tasks when source proves they were completed.",
            "- Resolve contradictions in favor of newer explicit user feedback or current session state.",
            "- The compact summary is not long-term memory and must not state that memory has been updated.",
            "</hard_rules>",
            "",
            "<retained_policy>",
            spec.retained_policy,
            "</retained_policy>",
            "",
            "<existing_summary>",
            (spec.existing_summary or "").strip(),
            "</existing_summary>",
            "",
            "<session_memory_state>",
            _render_session_memory_state(spec.session_memory_state),
            "</session_memory_state>",
            "",
            "<source_units>",
            render_source_units(spec.source_units),
            "</source_units>",
            "",
            "<source_transcript>",
            render_source_transcript(spec.source_units),
            "</source_transcript>",
            "",
            "<example>",
            _few_shot_example(spec.mode),
            "</example>",
            "",
            "<required_output>",
            "Use the exact output contract below. Persist only <summary>, never <analysis>.",
            _required_output_contract(section_titles),
            "</required_output>",
            "</compact_prompt>",
        ]
    ).strip()


def render_source_units(units: list[CompactSourceUnit]) -> str:
    chunks = []
    for unit in units:
        chunks.append(
            f'<unit id="{unit.unit_id}" type="{unit.unit_type}" '
            f'entry_ids="{",".join(unit.entry_ids)}" event_ids="{",".join(unit.event_ids)}" '
            f'event_range="{_range_text(unit.source_event_start_seq, unit.source_event_end_seq)}">'
        )
        chunks.append(unit.transcript.strip())
        chunks.append("</unit>")
    return "\n".join(chunks).strip()


def render_source_transcript(units: list[CompactSourceUnit]) -> str:
    return "\n\n".join(unit.transcript.strip() for unit in units if unit.transcript.strip()).strip()


def format_compact_summary(raw_text: str) -> str:
    summary_match = re.search(r"<summary>\s*(.*?)\s*</summary>", raw_text, flags=re.IGNORECASE | re.DOTALL)
    if summary_match:
        return summary_match.group(1).strip()

    without_analysis = re.sub(
        r"<analysis>\s*.*?\s*</analysis>",
        "",
        raw_text,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    without_summary_tags = re.sub(r"</?summary>", "", without_analysis, flags=re.IGNORECASE).strip()
    return without_summary_tags


def validate_compact_summary(
    summary_text: str,
    *,
    mode: str,
    source_units: list[CompactSourceUnit],
    summary_token_budget: int = 4096,
) -> CompactSummaryValidation:
    warnings: list[str] = []
    errors: list[str] = []
    clean = summary_text.strip()

    if not clean:
        errors.append("summary_empty")
    if re.search(r"</?analysis>", clean, flags=re.IGNORECASE):
        errors.append("analysis_tag_persisted")
    if "<compact_prompt" in clean or "<example>" in clean:
        errors.append("prompt_or_example_persisted")
    if "[Incremental Update]" in clean:
        warnings.append("incremental_append_marker_present")

    for title in section_titles_for_mode(mode):
        if title not in clean:
            errors.append(f"missing_section:{title}")

    source_text = render_source_transcript(source_units)
    user_anchors = _extract_user_anchor_candidates(source_text)
    if user_anchors and not any(anchor in clean for anchor in user_anchors):
        warnings.append("no_verbatim_user_anchor")
    if not user_anchors:
        warnings.append("source_has_no_user_message")

    source_artifact_values = set(re.findall(r"artifact://[^\s)]+", source_text))
    source_artifact_values.update(re.findall(r"artifact_ref=([^\s)]+)", source_text))
    summary_artifacts = set(re.findall(r"artifact://[^\s)]+", clean))
    unknown_artifacts = summary_artifacts - source_artifact_values
    if unknown_artifacts:
        warnings.append("unknown_artifact_ref")

    estimated_tokens = estimate_text_tokens(clean)
    if estimated_tokens > summary_token_budget:
        errors.append("summary_token_budget_exceeded")

    summary_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return CompactSummaryValidation(
        valid=not errors,
        warnings=warnings,
        errors=errors,
        summary_hash=summary_hash,
        estimated_tokens=estimated_tokens,
    )


def _required_output_contract(section_titles: list[str]) -> str:
    sections = "\n\n".join(f"{title}:\n..." for title in section_titles)
    return (
        "<analysis>\n"
        "Coverage:\n- Which source range and safe units were summarized.\n\n"
        "Anchors:\n- Verbatim user wording selected as anti-drift anchors.\n\n"
        "Contradictions:\n- Newer corrections or decisions that override older statements.\n\n"
        "Omitted:\n- Sensitive, tool-only, UI-only, or retained content intentionally not summarized.\n\n"
        "Validation Notes:\n- Missing section, weak anchor, truncated unit, or artifact reference risks.\n"
        "</analysis>\n\n"
        "<summary>\n"
        f"{sections}\n"
        "</summary>"
    )


def _render_session_memory_state(state: SessionMemoryState | None) -> str:
    if state is None:
        return ""
    lines = [
        f"mode: {state.mode}",
        f"objective: {state.objective}",
        f"current_subtask: {state.current_subtask}",
    ]
    for key in ("confirmed_facts", "key_decisions", "constraints", "key_files", "open_questions", "next_actions"):
        values = getattr(state, key)
        if values:
            lines.append(f"{key}:")
            lines.extend(f"- {value}" for value in values)
    return "\n".join(lines).strip()


def _few_shot_example(mode: str) -> str:
    tail = (
        "8. Work Completed in Summarized Portion:\n"
        "已建立装饰器、wrapper、闭包的基础直觉，并确认用户更适合调用顺序式讲解。\n\n"
        "9. Context for Continuing Recent Messages:\n"
        "后续近期消息会继续围绕调用顺序展开。关键原文锚点：“能不能按调用顺序讲？”"
        if mode in {CompactMode.AUTO_PREFIX.value, CompactMode.SLACT_UP_TO.value}
        else
        "8. Current Learning State:\n"
        "正准备从 @decorator 的语法糖展开式开始解释调用顺序。\n\n"
        "9. Optional Next Step with Verbatim Anchor:\n"
        "Next Step: 继续按调用顺序解释装饰器执行流程。\n"
        "Verbatim Anchor: 用户说：“能不能按调用顺序讲？”"
    )
    return (
        "<analysis>\n"
        "Coverage:\n- Summarize only the provided earlier transcript.\n"
        "Anchors:\n- 用户说：“能不能按调用顺序讲？”\n"
        "</analysis>\n\n"
        "<summary>\n"
        "1. Primary Learning Request and Intent:\n"
        "用户希望理解 Python 装饰器，不只是记住语法。\n\n"
        "2. Learning Context and Goals:\n"
        "用户偏好先给直观类比，再给小段代码验证理解。\n\n"
        "3. Key Concepts, Explanations, and Examples:\n"
        "已讲过 wrapper(*args, **kwargs) 保留原函数参数形态。\n\n"
        "4. Materials, Files, and External Artifacts:\n"
        "无外部文件。\n\n"
        "5. Errors, Misunderstandings, and Corrections:\n"
        "用户反馈抽象解释像背概念，后续改用调用顺序拆解。\n\n"
        "6. All User Messages and Feedback:\n"
        "- “我想真的理解装饰器，不想只是会写 @xxx。”\n"
        "- “能不能按调用顺序讲？”\n\n"
        "7. Pending Learning Tasks:\n"
        "继续解释 @decorator 展开后的执行流程。\n\n"
        f"{tail}\n"
        "</summary>"
    )


def _extract_user_anchor_candidates(source_text: str) -> list[str]:
    anchors: list[str] = []
    for line in source_text.splitlines():
        line = line.strip()
        if not line.startswith("USER:"):
            continue
        content = line.removeprefix("USER:").strip()
        if not content:
            continue
        anchors.append(content[:120])
    return anchors


def _range_text(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return "unknown"
    return f"{start or ''}..{end or ''}"
