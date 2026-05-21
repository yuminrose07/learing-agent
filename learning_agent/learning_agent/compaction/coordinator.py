from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from learning_agent.ai import LearningSession, SessionEntry
from learning_agent.learning_agent.mode_service import TurnExecutionProfile

from .full_compact import (
    build_full_compact_input,
    build_full_compact_result,
    estimate_text_tokens,
    list_entries_from,
    merge_incremental_summary,
    render_role_transcript,
    render_summary_block,
    render_summary_text_block,
)
from .micro_compact import build_micro_compacted_history
from .models import CompactMetadata, CompactMode, CompactionPlan, FullCompactInput, FullCompactResult
from .prompts import (
    CompactPromptSpec,
    format_compact_summary,
    retained_policy_for_mode,
    summary_position_for_mode,
    validate_compact_summary,
)

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_COMPACT_FAILURES = 3


class CompactionCoordinator:
    def __init__(
        self,
        session_manager,
        *,
        max_context_tokens: int = 128000,
        keep_recent_tool_groups: int = 2,
    ):
        self.session_manager = session_manager
        self.max_context_tokens = max_context_tokens
        self.keep_recent_tool_groups = keep_recent_tool_groups

    def evaluate_turn(
        self,
        session: LearningSession,
        user_input: str,
        profile: TurnExecutionProfile,
        *,
        allow_full_compact: bool = True,
    ) -> CompactionPlan:
        history = self.session_manager.get_message_history(session.id)
        plan = CompactionPlan(
            use_micro_compact=profile.micro_compact_enabled,
            recent_token_budget=profile.recent_token_budget,
        )
        metadata = self.session_manager.get_compact_metadata(session.id)
        if metadata is None:
            metadata = CompactMetadata(session_id=session.id)
            self.session_manager.set_compact_metadata(session.id, metadata)
        self._attach_latest_summary_to_plan(session.id, plan, metadata)

        estimated_history = self._filter_history_for_plan(history, plan)
        estimated_entries = (
            build_micro_compacted_history(estimated_history, keep_recent_groups=self.keep_recent_tool_groups)
            if plan.use_micro_compact
            else list(estimated_history)
        )
        estimated_tokens = self._estimate_turn_tokens(estimated_entries, profile.system_prompt, user_input)
        threshold_tokens = int(self.max_context_tokens * profile.full_compact_threshold)

        if (
            not allow_full_compact
            or not profile.full_compact_enabled
            or estimated_tokens < threshold_tokens
            or metadata.consecutive_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES
        ):
            return plan

        result = self.maybe_run_full_compact(
            session,
            profile=profile,
            entries=history,
            metadata=metadata,
        )
        if result is None:
            return plan

        plan.use_full_compact = True
        plan.compact_mode = result.compact_mode
        plan.compact_scope = result.scope
        plan.full_compact_scope = result.scope
        plan.compact_event_id = result.compact_event_id
        plan.compact_event_seq = result.compact_event_seq
        plan.previous_compact_event_id = result.previous_compact_event_id
        plan.current_user_event_id = result.current_user_event_id
        plan.source_snapshot_seq = result.source_snapshot_seq
        plan.compact_anchor_entry_id = result.compact_anchor_entry_id
        plan.cut_point_entry_id = result.cut_point_entry_id
        if result.source_event_start_seq is not None and result.source_event_end_seq is not None:
            plan.source_event_range = (result.source_event_start_seq, result.source_event_end_seq)
        plan.source_entry_ids = list(result.source_entry_ids)
        plan.retained_entry_ids = list(result.retained_entry_ids)
        plan.retained_position = "after_summary"
        plan.source_jsonl_cursor = (
            str(result.source_event_start_seq)
            if result.source_event_start_seq is not None
            else None
        )
        plan.next_jsonl_cursor = (
            result.source_event_end_seq
            if result.source_event_end_seq is not None
            else None
        )
        plan.summary_block = render_summary_block(result)
        return plan

    def maybe_run_full_compact(
        self,
        session: LearningSession,
        *,
        profile: TurnExecutionProfile,
        entries: Optional[list[SessionEntry]] = None,
        metadata: CompactMetadata | None = None,
    ) -> FullCompactResult | None:
        entries = entries if entries is not None else self.session_manager.get_message_history(session.id)
        metadata = metadata or self.session_manager.get_compact_metadata(session.id) or CompactMetadata(session_id=session.id)
        existing_summary = self.session_manager.load_compact_summary(session.id)
        sm_state = self.session_manager.get_session_memory_state(session.id)
        snapshot = self.session_manager.get_agent_snapshot(session.id)
        source_snapshot_seq = snapshot.source_event_range[1] if snapshot.source_event_range else None

        compact_input = build_full_compact_input(
            session.id,
            entries,
            metadata=metadata,
            recent_token_budget=profile.recent_token_budget,
            compact_mode=CompactMode.AUTO_PREFIX.value,
            source_snapshot_seq=source_snapshot_seq,
            existing_summary=existing_summary,
            sm_state=sm_state,
        )
        if compact_input is None:
            return None

        try:
            prompt_spec = self._build_prompt_spec(compact_input)
            raw_summary = self._summarize_transcript(compact_input, prompt_spec)
            delta_summary = format_compact_summary(raw_summary)
            validation = validate_compact_summary(
                delta_summary,
                mode=compact_input.compact_mode,
                source_units=compact_input.source_units,
            )
            if not validation.valid:
                raise ValueError(f"compact summary validation failed: {validation.errors}")
            summary_text = (
                merge_incremental_summary(existing_summary, delta_summary)
                if compact_input.scope == "incremental"
                else delta_summary
            )
            preserved_entries = [
                entry
                for entry in list_entries_from(entries, compact_input.cut_point_entry_id)
                if entry.id in compact_input.retained_entry_ids
            ]
            estimated_tokens_after = estimate_text_tokens(summary_text) + sum(
                estimate_text_tokens(entry.content or "") for entry in preserved_entries
            )
            result = build_full_compact_result(
                compact_input,
                summary_text=summary_text,
                preserved_entry_ids=[entry.id for entry in preserved_entries],
                estimated_tokens_after=estimated_tokens_after,
            )
            result.validation_status = validation.to_dict()
            result.summary_hash = validation.summary_hash
            result.retained_event_ids = [
                str(entry.metadata.get("source_event_id"))
                for entry in preserved_entries
                if entry.metadata.get("source_event_id")
            ]
            self.persist_compact_success(session.id, result, recent_token_budget=profile.recent_token_budget)
            return result
        except Exception:
            logger.exception("[CompactionCoordinator] Full compact failed (session=%s)", session.id)
            metadata.consecutive_failures += 1
            self.session_manager.set_compact_metadata(session.id, metadata)
            return None

    def persist_compact_success(
        self,
        session_id: str,
        result: FullCompactResult,
        *,
        recent_token_budget: int,
    ) -> None:
        summary_path = self.session_manager.save_compact_summary(session_id, result.summary_text)
        metadata = self.session_manager.get_compact_metadata(session_id) or CompactMetadata(session_id=session_id)
        previous_compact_event_id = metadata.last_compact_event_id
        result.previous_compact_event_id = result.previous_compact_event_id or previous_compact_event_id
        result.summary_hash = result.summary_hash or hashlib.sha256(result.summary_text.encode("utf-8")).hexdigest()
        metadata.last_summary_file = summary_path
        metadata.last_summary_hash = result.summary_hash
        applied = self.session_manager.apply_full_compact_result(session_id, result)
        if not applied:
            raise RuntimeError(f"failed to append compaction summary event for session {session_id}")
        metadata.compact_anchor_entry_id = result.next_anchor_entry_id
        metadata.compact_anchor_event_seq = result.compact_event_seq
        metadata.last_cut_point_entry_id = result.cut_point_entry_id
        metadata.last_compact_event_id = result.compact_event_id
        metadata.previous_compact_event_id = previous_compact_event_id
        metadata.last_compact_mode = result.compact_mode
        metadata.last_compact_scope = result.scope
        metadata.incremental_count_since_rebase = (
            0 if result.scope in {"full", "rebase"} else metadata.incremental_count_since_rebase + 1
        )
        metadata.consecutive_failures = 0
        metadata.source_event_start_seq = result.source_event_start_seq
        metadata.source_event_end_seq = result.source_event_end_seq
        metadata.last_source_snapshot_seq = result.source_snapshot_seq
        metadata.source_event_ids = list(result.source_event_ids)
        metadata.last_source_entry_ids = list(result.source_entry_ids)
        metadata.retained_event_ids = list(result.retained_event_ids)
        metadata.last_retained_entry_ids = list(result.retained_entry_ids)
        metadata.last_source_jsonl_cursor = (
            str(result.source_event_start_seq)
            if result.source_event_start_seq is not None
            else None
        )
        metadata.next_jsonl_cursor = (
            result.source_event_end_seq
            if result.source_event_end_seq is not None
            else None
        )
        metadata.template_version = result.template_version
        metadata.last_prompt_template = result.template_version
        metadata.last_recent_token_budget = recent_token_budget
        metadata.last_compacted_at = time.time()
        self.session_manager.set_compact_metadata(session_id, metadata)

    def _estimate_turn_tokens(
        self,
        entries: list[SessionEntry],
        system_prompt: str,
        user_input: str,
    ) -> int:
        tokens = estimate_text_tokens(system_prompt) + estimate_text_tokens(user_input)
        for entry in entries:
            tokens += estimate_text_tokens(entry.content or "")
        return tokens

    def _attach_latest_summary_to_plan(
        self,
        session_id: str,
        plan: CompactionPlan,
        metadata: CompactMetadata,
    ) -> None:
        latest = self.session_manager.get_latest_compact_summary_block(session_id)
        if latest is None:
            return
        summary_text, latest_metadata = latest
        compact_mode = latest_metadata.last_compact_mode or CompactMode.AUTO_PREFIX.value
        scope = latest_metadata.last_compact_scope or "full"
        plan.compact_mode = compact_mode
        plan.compact_scope = scope
        plan.full_compact_scope = scope
        plan.compact_event_id = latest_metadata.last_compact_event_id
        plan.compact_event_seq = latest_metadata.compact_anchor_event_seq
        plan.previous_compact_event_id = latest_metadata.previous_compact_event_id
        plan.source_snapshot_seq = latest_metadata.last_source_snapshot_seq
        plan.compact_anchor_entry_id = latest_metadata.compact_anchor_entry_id
        plan.cut_point_entry_id = latest_metadata.last_cut_point_entry_id
        if latest_metadata.source_event_start_seq is not None and latest_metadata.source_event_end_seq is not None:
            plan.source_event_range = (
                latest_metadata.source_event_start_seq,
                latest_metadata.source_event_end_seq,
            )
        plan.source_entry_ids = list(latest_metadata.last_source_entry_ids)
        plan.retained_entry_ids = list(latest_metadata.last_retained_entry_ids)
        plan.retained_position = "after_summary"
        plan.source_jsonl_cursor = latest_metadata.last_source_jsonl_cursor
        plan.next_jsonl_cursor = latest_metadata.next_jsonl_cursor
        plan.summary_block = render_summary_text_block(
            summary_text,
            compact_mode=compact_mode,
            scope=scope,
        )
        self.session_manager.set_compact_metadata(session_id, latest_metadata)

    def _filter_history_for_plan(
        self,
        history: list[SessionEntry],
        plan: CompactionPlan,
    ) -> list[SessionEntry]:
        if not plan.summary_block:
            return history
        retained_ids = set(plan.retained_entry_ids)
        compact_event_seq = plan.compact_event_seq
        if not retained_ids and compact_event_seq is None:
            return history

        filtered: list[SessionEntry] = []
        for entry in history:
            if entry.id in retained_ids:
                filtered.append(entry)
                continue
            event_seq = entry.metadata.get("source_event_seq")
            if compact_event_seq is not None and event_seq is not None:
                try:
                    if int(event_seq) > compact_event_seq:
                        filtered.append(entry)
                except (TypeError, ValueError):
                    continue
        return filtered

    def _build_prompt_spec(self, compact_input: FullCompactInput) -> CompactPromptSpec:
        summary_position = summary_position_for_mode(compact_input.compact_mode)
        retained_policy = retained_policy_for_mode(
            compact_input.compact_mode,
            cut_point_entry_id=compact_input.cut_point_entry_id,
            current_user_event_id=compact_input.current_user_event_id,
            retained_entry_ids=compact_input.retained_entry_ids,
        )
        return CompactPromptSpec(
            session_id=compact_input.session_id,
            mode=compact_input.compact_mode,
            scope=compact_input.scope,
            summary_position=summary_position,
            source_event_start_seq=compact_input.source_event_start_seq,
            source_event_end_seq=compact_input.source_event_end_seq,
            source_snapshot_seq=compact_input.source_snapshot_seq,
            current_user_event_id=compact_input.current_user_event_id,
            source_event_ids=list(compact_input.source_event_ids),
            source_entry_ids=list(compact_input.source_entry_ids),
            retained_entry_ids=list(compact_input.retained_entry_ids),
            retained_policy=retained_policy,
            source_units=list(compact_input.source_units),
            existing_summary=compact_input.existing_summary,
            session_memory_state=compact_input.sm_state,
        )

    def _summarize_transcript(
        self,
        compact_input: FullCompactInput,
        prompt_spec: CompactPromptSpec,
    ) -> str:
        del prompt_spec
        transcript = "\n\n".join(unit.transcript for unit in compact_input.source_units).strip()
        user_messages = [
            line.strip().removeprefix("USER:").strip()
            for line in transcript.splitlines()
            if line.strip().startswith("USER:")
        ]
        assistant_points = [
            line.strip().removeprefix("ASSISTANT:").strip()
            for line in transcript.splitlines()
            if line.strip().startswith("ASSISTANT:")
        ]
        tool_lines = [
            line.strip()
            for line in transcript.splitlines()
            if line.strip().startswith("TOOL") or "artifact_ref" in line
        ]
        user_anchor = user_messages[-1] if user_messages else ""
        user_bullets = "\n".join(f"- “{message[:180]}”" for message in user_messages[:12]) or "- 无用户消息。"
        concept_bullets = "\n".join(f"- {point[:180]}" for point in assistant_points[:8]) or "- 压缩范围内没有可保留的 assistant 解释。"
        material_bullets = "\n".join(f"- {line[:180]}" for line in tool_lines[:8]) or "- 无外部材料或工具结果。"
        section_8 = (
            "8. Work Completed in Summarized Portion:\n"
            "已将压缩范围内的旧对话折叠为摘要；近期 retained messages 会继续保留原文。\n\n"
            "9. Context for Continuing Recent Messages:\n"
            f"继续时应以后续 retained messages 和最新用户请求为准。关键原文锚点：“{user_anchor}”"
            if compact_input.compact_mode in {CompactMode.AUTO_PREFIX.value, CompactMode.SLACT_UP_TO.value}
            else
            "8. Current Learning State:\n"
            "当前会话状态由本摘要和后续用户请求共同决定。\n\n"
            "9. Optional Next Step with Verbatim Anchor:\n"
            f"Next Step: 继续回应最近明确请求。\nVerbatim Anchor: 用户说：“{user_anchor}”"
        )
        return (
            "<analysis>\n"
            "Coverage:\n"
            f"- Summarized {len(compact_input.source_units)} safe units in the selected source range.\n\n"
            "Anchors:\n"
            f"- {user_anchor or 'No user anchor in source.'}\n\n"
            "Contradictions:\n"
            "- No explicit contradiction detected by fallback summarizer.\n\n"
            "Omitted:\n"
            "- Retained messages, sensitive omissions, and UI-only events were not summarized.\n\n"
            "Validation Notes:\n"
            "- Deterministic fallback summary; provider SummaryExecutor can replace this port later.\n"
            "</analysis>\n\n"
            "<summary>\n"
            "1. Primary Learning Request and Intent:\n"
            f"{user_anchor or '压缩范围内没有明确的新用户请求。'}\n\n"
            "2. Learning Context and Goals:\n"
            f"当前 session_id={compact_input.session_id}，压缩模式为 {compact_input.compact_mode}，scope={compact_input.scope}。\n\n"
            "3. Key Concepts, Explanations, and Examples:\n"
            f"{concept_bullets}\n\n"
            "4. Materials, Files, and External Artifacts:\n"
            f"{material_bullets}\n\n"
            "5. Errors, Misunderstandings, and Corrections:\n"
            "压缩范围内未检测到明确纠错；后续若 retained messages 中有用户纠正，以用户原文为准。\n\n"
            "6. All User Messages and Feedback:\n"
            f"{user_bullets}\n\n"
            "7. Pending Learning Tasks:\n"
            "继续处理 retained context 和最新用户请求中仍未完成的事项。\n\n"
            f"{section_8}\n"
            "</summary>"
        )
