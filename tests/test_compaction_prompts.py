from __future__ import annotations

import sys

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.learning_agent.compaction.models import CompactMode, CompactSourceUnit
from learning_agent.learning_agent.compaction.prompts import (
    CompactPromptSpec,
    format_compact_summary,
    render_compact_prompt,
    retained_policy_for_mode,
    summary_position_for_mode,
    validate_compact_summary,
)


def test_compact_prompt_spec_renders_fixed_blocks_and_mode_titles():
    unit = CompactSourceUnit(
        unit_id="round-1",
        entry_ids=["e1", "e2"],
        event_ids=["evt-1", "evt-2"],
        source_event_start_seq=1,
        source_event_end_seq=2,
        transcript="USER: 能不能按调用顺序讲？\nASSISTANT: 可以，我们展开 decorator(fn)。",
    )
    mode = CompactMode.AUTO_PREFIX.value
    spec = CompactPromptSpec(
        session_id="sess-prompt",
        mode=mode,
        scope="full",
        summary_position=summary_position_for_mode(mode),
        source_event_start_seq=1,
        source_event_end_seq=2,
        source_snapshot_seq=2,
        current_user_event_id="evt-current",
        retained_policy=retained_policy_for_mode(
            mode,
            cut_point_entry_id="e3",
            current_user_event_id="evt-current",
            retained_entry_ids=["e3"],
        ),
        source_units=[unit],
    )

    prompt = render_compact_prompt(spec)

    assert prompt.index("<no_tools_preamble>") < prompt.index("<task>")
    assert prompt.index("<retained_policy>") < prompt.index("<source_units>")
    assert "mode: auto_prefix" in prompt
    assert "8. Work Completed in Summarized Portion" in prompt
    assert "9. Context for Continuing Recent Messages" in prompt


def test_format_and_validate_compact_summary_strips_analysis():
    unit = CompactSourceUnit(
        unit_id="round-1",
        entry_ids=["e1", "e2"],
        event_ids=["evt-1", "evt-2"],
        transcript="USER: 能不能按调用顺序讲？\nASSISTANT: 可以。",
    )
    raw = """
<analysis>
Coverage:
- internal checklist
</analysis>
<summary>
1. Primary Learning Request and Intent:
用户希望继续理解装饰器调用顺序。

2. Learning Context and Goals:
用户偏好调用顺序式讲解。

3. Key Concepts, Explanations, and Examples:
已提到 decorator(fn)。

4. Materials, Files, and External Artifacts:
无。

5. Errors, Misunderstandings, and Corrections:
无。

6. All User Messages and Feedback:
- “能不能按调用顺序讲？”

7. Pending Learning Tasks:
继续解释执行顺序。

8. Work Completed in Summarized Portion:
已确认讲解方式。

9. Context for Continuing Recent Messages:
关键原文锚点：“能不能按调用顺序讲？”
</summary>
"""

    summary = format_compact_summary(raw)
    validation = validate_compact_summary(
        summary,
        mode=CompactMode.AUTO_PREFIX.value,
        source_units=[unit],
    )

    assert "<analysis>" not in summary
    assert validation.valid is True
    assert validation.summary_hash
