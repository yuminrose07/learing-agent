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
    merge_incremental_summary,
    render_role_transcript,
    render_summary_block,
)
from .micro_compact import build_micro_compacted_history
from .models import CompactMetadata, CompactionPlan, FullCompactResult

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

        estimated_entries = (
            build_micro_compacted_history(history, keep_recent_groups=self.keep_recent_tool_groups)
            if plan.use_micro_compact
            else list(history)
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
        plan.full_compact_scope = result.scope
        plan.compact_anchor_entry_id = result.compact_anchor_entry_id
        plan.cut_point_entry_id = result.cut_point_entry_id
        if result.source_event_start_seq is not None and result.source_event_end_seq is not None:
            plan.source_event_range = (result.source_event_start_seq, result.source_event_end_seq)
        plan.next_jsonl_cursor = result.source_event_end_seq
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

        compact_input = build_full_compact_input(
            session.id,
            entries,
            metadata=metadata,
            recent_token_budget=profile.recent_token_budget,
            existing_summary=existing_summary,
            sm_state=sm_state,
        )
        if compact_input is None:
            return None

        try:
            delta_transcript = "\n\n".join(unit.transcript for unit in compact_input.source_units).strip()
            delta_summary = self._summarize_transcript(delta_transcript)
            summary_text = (
                merge_incremental_summary(existing_summary, delta_summary)
                if compact_input.scope == "incremental"
                else delta_summary
            )
            preserved_entries = self.session_manager.list_entries_after(session.id, compact_input.cut_point_entry_id)
            estimated_tokens_after = estimate_text_tokens(summary_text) + sum(
                estimate_text_tokens(entry.content or "") for entry in preserved_entries
            )
            result = build_full_compact_result(
                compact_input,
                summary_text=summary_text,
                preserved_entry_ids=[entry.id for entry in preserved_entries],
                estimated_tokens_after=estimated_tokens_after,
            )
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
        metadata.compact_anchor_entry_id = result.next_anchor_entry_id
        metadata.last_cut_point_entry_id = result.cut_point_entry_id
        metadata.last_compact_scope = result.scope
        metadata.incremental_count_since_rebase = (
            0 if result.scope in {"full", "rebase"} else metadata.incremental_count_since_rebase + 1
        )
        metadata.consecutive_failures = 0
        metadata.last_summary_file = summary_path
        metadata.last_summary_hash = hashlib.sha256(result.summary_text.encode("utf-8")).hexdigest()
        metadata.source_event_start_seq = result.source_event_start_seq
        metadata.source_event_end_seq = result.source_event_end_seq
        metadata.source_event_ids = list(result.source_event_ids)
        metadata.retained_event_ids = list(result.retained_event_ids)
        metadata.next_jsonl_cursor = result.source_event_end_seq
        metadata.template_version = result.template_version
        metadata.last_recent_token_budget = recent_token_budget
        metadata.last_compacted_at = time.time()
        self.session_manager.set_compact_metadata(session_id, metadata)
        self.session_manager.apply_full_compact_result(session_id, result)

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

    def _summarize_transcript(self, transcript: str) -> str:
        lines = [line.strip() for line in transcript.splitlines() if line.strip()]
        selected = lines[:12]
        summary = "\n".join(f"- {line[:200]}" for line in selected)
        if not summary:
            return "- 无可压缩历史。"
        return f"已压缩历史摘要：\n{summary}"
