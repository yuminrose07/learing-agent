#!/usr/bin/env python3
"""Diagnose adaptive-alignment side events from session JSONL logs.

This script is intentionally read-only. It scans ``sessions/*.events.jsonl`` and
summarizes whether the Phase 1A alignment-side events are present in real data.
Use ``LA_DATA_DIR`` or ``--data-dir`` to point at the same data directory used by
the dev server.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


EVENT_SOURCES: dict[str, str] = {
    "learning_unit.alignment_suggested": (
        "learning_agent/learning_agent/main.py:_apply_alignment_decision"
        " suggested branch"
    ),
    "learning_unit.alignment_skipped": (
        "learning_agent/learning_agent/main.py:accept_assumption"
    ),
    "learning_unit.alignment_resolved": (
        "learning_agent/learning_agent/main.py:_apply_alignment_decision"
        " user_request branch"
    ),
    "learning_unit.assumption_accepted": (
        "learning_agent/learning_agent/main.py:accept_assumption"
    ),
    "learning_unit.objective_refined": (
        "learning_agent/learning_agent/main.py:refine_objective"
    ),
    "learning_unit.alignment_rate_limited": (
        "learning_agent/learning_agent/main.py:_apply_alignment_decision"
        " rate_limit branch"
    ),
}


def _resolve_sessions_dir(data_dir: Path) -> Path:
    if data_dir.name == "sessions":
        return data_dir
    return data_dir / "sessions"


def _sample_for_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return {
        "seq": event.get("seq"),
        "ts": event.get("ts"),
        "session_id": event.get("session_id"),
        "payload_keys": sorted(str(key) for key in payload.keys()),
    }


def _sort_sample_key(event: dict[str, Any]) -> tuple[str, str, int]:
    seq = event.get("seq")
    if not isinstance(seq, int):
        seq = -1
    return (
        str(event.get("ts") or ""),
        str(event.get("session_id") or ""),
        seq,
    )


def diagnose(data_dir: Path) -> dict[str, Any]:
    sessions_dir = _resolve_sessions_dir(data_dir)
    event_files = sorted(sessions_dir.glob("*.events.jsonl"))
    tracked = {
        event_type: {
            "event_type": event_type,
            "total_count": 0,
            "unique_session_ids": set(),
            "latest_events": [],
            "emit_source": emit_source,
        }
        for event_type, emit_source in EVENT_SOURCES.items()
    }
    parse_error_count = 0
    parse_error_samples: list[dict[str, Any]] = []

    for path in event_files:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_error_count += 1
                    if len(parse_error_samples) < 5:
                        parse_error_samples.append(
                            {
                                "path": str(path),
                                "line": line_no,
                                "error": str(exc),
                            }
                        )
                    continue
                event_type = event.get("type")
                if event_type not in tracked:
                    continue
                row = tracked[event_type]
                row["total_count"] += 1
                session_id = event.get("session_id")
                if session_id:
                    row["unique_session_ids"].add(str(session_id))
                row["latest_events"].append(event)

    event_summaries = []
    for event_type in EVENT_SOURCES:
        row = tracked[event_type]
        latest_events = sorted(
            row["latest_events"],
            key=_sort_sample_key,
            reverse=True,
        )[:5]
        event_summaries.append(
            {
                "event_type": row["event_type"],
                "total_count": row["total_count"],
                "unique_sessions": len(row["unique_session_ids"]),
                "latest_5_samples": [
                    _sample_for_event(event) for event in latest_events
                ],
                "emit_source": row["emit_source"],
            }
        )

    return {
        "data_dir": str(data_dir),
        "sessions_dir": str(sessions_dir),
        "session_event_files": len(event_files),
        "events": event_summaries,
        "parse_errors": {
            "count": parse_error_count,
            "samples": parse_error_samples,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize learning-unit alignment events from JSONL logs."
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LA_DATA_DIR", ".learning_agent_data"),
        help=(
            "Learning Agent data directory, or its sessions/ subdirectory. "
            "Defaults to LA_DATA_DIR or .learning_agent_data."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact JSON instead of indented JSON.",
    )
    args = parser.parse_args()

    report = diagnose(Path(args.data_dir).expanduser().resolve())
    indent = None if args.compact else 2
    print(json.dumps(report, ensure_ascii=False, indent=indent, sort_keys=True))


if __name__ == "__main__":
    main()
