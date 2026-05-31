#!/usr/bin/env python3
"""Minimal real-environment runner for JSON E2E datasets."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUNNER_VERSION = "phase1b-orientation-real-1"
SUPPORTED_SUITES = {
    "learning-mode-phase-1a-forge-state-real",
    "learning-mode-phase-1b-orientation-context-real",
}
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass
class HttpResult:
    status: int
    data: Any = None
    raw_text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None


@dataclass
class ChatStreamResult:
    content: str
    payloads: list[dict[str, Any]]
    raw_events: list[str]
    error: str | None = None


@dataclass
class CaseRun:
    case_id: str
    title: str
    duration_ms: int
    verdict: str
    fail_reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    learning_unit_id: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_id_for(suite_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return f"{stamp}_{suite_id}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def http_json(
    method: str,
    base_url: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float,
) -> HttpResult:
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            return HttpResult(status=resp.status, data=parsed, raw_text=raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return HttpResult(
            status=exc.code,
            data=parsed,
            raw_text=raw,
            error=f"HTTP {exc.code}",
        )
    except Exception as exc:  # noqa: BLE001 - runner reports transport failures
        return HttpResult(status=0, error=f"{type(exc).__name__}: {exc}")


def stream_chat(
    base_url: str,
    session_id: str,
    message: str,
    *,
    timeout: float,
) -> ChatStreamResult:
    body = {"message": message, "stream": True}
    req = urllib.request.Request(
        f"{base_url}/sessions/{urllib.parse.quote(session_id)}/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    payloads: list[dict[str, Any]] = []
    raw_events: list[str] = []
    content_parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buffer = ""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    if not event_str.strip():
                        continue
                    raw_events.append(event_str)
                    for line in event_str.splitlines():
                        if not line.startswith("data: "):
                            continue
                        raw_payload = line[len("data: ") :]
                        try:
                            payload = json.loads(raw_payload)
                        except json.JSONDecodeError:
                            payload = {"_decode_error": raw_payload}
                        payloads.append(payload)
                        content = payload.get("content")
                        if isinstance(content, str):
                            content_parts.append(content)
        return ChatStreamResult(
            content="".join(content_parts),
            payloads=payloads,
            raw_events=raw_events,
        )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return ChatStreamResult(
            content="".join(content_parts),
            payloads=payloads,
            raw_events=raw_events,
            error=f"HTTP {exc.code}: {raw[:500]}",
        )
    except Exception as exc:  # noqa: BLE001 - runner reports transport failures
        return ChatStreamResult(
            content="".join(content_parts),
            payloads=payloads,
            raw_events=raw_events,
            error=f"{type(exc).__name__}: {exc}",
        )


def validate_dataset(dataset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    suite_id = dataset.get("suite_id")
    if suite_id not in SUPPORTED_SUITES:
        errors.append(
            f"unsupported suite_id={suite_id!r}; expected one of {sorted(SUPPORTED_SUITES)!r}"
        )
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("dataset.cases must be a non-empty list")
        return errors
    seen: set[str] = set()
    for index, case in enumerate(cases):
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"case[{index}].id must be a non-empty string")
            continue
        if case_id in seen:
            errors.append(f"duplicate case id: {case_id}")
        seen.add(case_id)
        setup = case.get("setup") or {}
        if not setup.get("create_learning_unit"):
            errors.append(f"{case_id}: setup.create_learning_unit must be true")
        if not isinstance(setup.get("seed_text"), str) or not setup.get("seed_text"):
            errors.append(f"{case_id}: setup.seed_text must be a non-empty string")
        turns = case.get("turns")
        if not isinstance(turns, list) or len(turns) != 1:
            errors.append(f"{case_id}: turns must contain exactly one turn")
        elif turns[0].get("role") != "user" or not isinstance(turns[0].get("message"), str):
            errors.append(f"{case_id}: turns[0] must be a user message")
    return errors


def latest_metadata(payloads: list[dict[str, Any]], key: str) -> Any:
    for payload in reversed(payloads):
        if key in payload:
            return payload.get(key)
    return None


def metadata_values(payloads: list[dict[str, Any]], key: str) -> list[Any]:
    return [payload.get(key) for payload in payloads if key in payload]


def any_metadata_has(payloads: list[dict[str, Any]], key: str) -> bool:
    return any(key in payload for payload in payloads)


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("type") or "") for event in events]


def event_payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("type") or "") != event_type:
            continue
        payload = event.get("payload")
        out.append(payload if isinstance(payload, dict) else {})
    return out


def orientation_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event_type in (
        "learning_unit.orientation_generated",
        "learning_unit.orientation_fallback_used",
    ):
        out.extend(event_payloads(events, event_type))
    return out


def load_events(events_result: HttpResult) -> list[dict[str, Any]]:
    if not events_result.ok or not isinstance(events_result.data, dict):
        return []
    events = events_result.data.get("events")
    return events if isinstance(events, list) else []


def get_first_user_message(case: dict[str, Any]) -> str:
    turns = case["turns"]
    return str(turns[0]["message"])


def append_fail(fail_reasons: list[str], condition: bool, reason: str) -> None:
    if not condition:
        fail_reasons.append(reason)


def judge_case(
    case: dict[str, Any],
    *,
    create_result: HttpResult,
    chat_result: ChatStreamResult,
    unit_result: HttpResult,
    session_result: HttpResult,
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    expect = case.get("expect") or {}
    forbid = case.get("forbid") or {}
    payloads = chat_result.payloads
    text = chat_result.content
    final_unit = unit_result.data if isinstance(unit_result.data, dict) else {}
    session = session_result.data if isinstance(session_result.data, dict) else {}
    types = event_types(events)
    fail_reasons: list[str] = []

    learning_unit_created = (
        create_result.ok
        and isinstance(create_result.data, dict)
        and bool(create_result.data.get("id"))
        and bool(create_result.data.get("session_id"))
    )
    non_empty_response = bool(text.strip())
    has_forge_metadata = any_metadata_has(payloads, "forge_stage")
    has_temperature_metadata = any_metadata_has(payloads, "temperature_state")
    has_learning_action = any_metadata_has(payloads, "learning_action")
    unit_has_forge = isinstance(final_unit, dict) and "forge_stage" in final_unit
    unit_has_temperature = isinstance(final_unit, dict) and "temperature_state" in final_unit
    orientation_context = (
        final_unit.get("orientation_context") if isinstance(final_unit, dict) else None
    )
    has_orientation_context = isinstance(orientation_context, dict)
    orientation_source = (
        orientation_context.get("source") if isinstance(orientation_context, dict) else None
    )
    orientation_digest = (
        orientation_context.get("orientation_digest")
        if isinstance(orientation_context, dict)
        else None
    )
    orientation_hook_kind = (
        orientation_context.get("hook_kind") if isinstance(orientation_context, dict) else None
    )
    has_orientation_presence_metadata = any_metadata_has(
        payloads, "orientation_context_present"
    )
    latest_orientation_presence = latest_metadata(
        payloads, "orientation_context_present"
    )
    hook_kind_metadata = latest_metadata(payloads, "hook_kind")
    frontend_backend_consistent = (
        has_forge_metadata
        and has_temperature_metadata
        and unit_has_forge
        and unit_has_temperature
    )
    alignment_mode = (
        latest_metadata(payloads, "mode") == "ask"
        or any(payload.get("alignment") is True for payload in payloads)
    )
    forge_stage_changed = "learning_unit.forge_stage_changed" in types
    first_value_signal = (
        "learning_unit.first_value_delivered" in types or forge_stage_changed
    )
    final_forge_stage = final_unit.get("forge_stage") if isinstance(final_unit, dict) else None
    final_temperature_state = (
        final_unit.get("temperature_state") if isinstance(final_unit, dict) else None
    )
    generated_events = event_payloads(events, "learning_unit.orientation_generated")
    fallback_events = event_payloads(events, "learning_unit.orientation_fallback_used")
    orientation_event_payloads = generated_events + fallback_events
    orientation_event_count = len(orientation_event_payloads)
    orientation_event_sources = {
        str(payload.get("source")) for payload in orientation_event_payloads
    }
    orientation_digest_consistent = True
    if has_orientation_context and orientation_event_payloads:
        orientation_digest_consistent = any(
            payload.get("orientation_digest") == orientation_digest
            for payload in orientation_event_payloads
        )
    orientation_hook_consistent = True
    if has_orientation_context and hook_kind_metadata is not None:
        orientation_hook_consistent = hook_kind_metadata == orientation_hook_kind

    if chat_result.error:
        fail_reasons.append(f"chat_stream_error: {chat_result.error}")
    if any(payload.get("error") for payload in payloads):
        fail_reasons.append("sse_payload_error")
    if any(payload.get("rescue") for payload in payloads):
        fail_reasons.append("sse_rescue_used")

    if expect.get("learning_unit_created"):
        append_fail(fail_reasons, learning_unit_created, "learning_unit_not_created")
    if expect.get("non_empty_response") or forbid.get("empty_response"):
        append_fail(fail_reasons, non_empty_response, "empty_response")

    for banned in forbid.get("contains") or []:
        append_fail(
            fail_reasons,
            str(banned) not in text,
            f"response_contains_forbidden:{banned}",
        )

    expected_phase = expect.get("learning_unit_phase")
    if expected_phase:
        append_fail(
            fail_reasons,
            final_unit.get("phase") == expected_phase,
            f"learning_unit_phase_mismatch:{final_unit.get('phase')!r}",
        )

    for key in expect.get("learning_unit_payload_should_include") or []:
        append_fail(
            fail_reasons,
            key in final_unit,
            f"learning_unit_payload_missing:{key}",
        )

    for key in expect.get("assistant_metadata_should_include") or []:
        append_fail(
            fail_reasons,
            any_metadata_has(payloads, str(key)),
            f"assistant_metadata_missing:{key}",
        )

    allowed_forge = expect.get("allowed_forge_stage_after_turn")
    if allowed_forge:
        append_fail(
            fail_reasons,
            final_forge_stage in allowed_forge,
            f"forge_stage_not_allowed:{final_forge_stage!r}",
        )

    allowed_temperature = expect.get("allowed_temperature_state_after_turn")
    if allowed_temperature:
        append_fail(
            fail_reasons,
            final_temperature_state in allowed_temperature,
            f"temperature_state_not_allowed:{final_temperature_state!r}",
        )

    allowed_action = expect.get("allowed_learning_action")
    if allowed_action:
        action = latest_metadata(payloads, "learning_action")
        append_fail(
            fail_reasons,
            action in allowed_action,
            f"learning_action_not_allowed:{action!r}",
        )

    if expect.get("orientation_context_present"):
        append_fail(
            fail_reasons,
            has_orientation_context,
            "orientation_context_missing",
        )
    if expect.get("orientation_context_absent"):
        append_fail(
            fail_reasons,
            not has_orientation_context,
            "orientation_context_unexpected",
        )
    if expect.get("orientation_context_present_metadata"):
        append_fail(
            fail_reasons,
            has_orientation_presence_metadata
            and latest_orientation_presence is True,
            "orientation_context_present_metadata_missing",
        )
    expected_orientation_source = expect.get("orientation_source")
    if expected_orientation_source:
        append_fail(
            fail_reasons,
            orientation_source == expected_orientation_source,
            f"orientation_source_mismatch:{orientation_source!r}",
        )
    expected_orientation_event = expect.get("orientation_event")
    if expected_orientation_event:
        append_fail(
            fail_reasons,
            expected_orientation_event in types,
            f"orientation_event_missing:{expected_orientation_event}",
        )
    if expect.get("orientation_event_count") is not None:
        append_fail(
            fail_reasons,
            orientation_event_count == int(expect["orientation_event_count"]),
            f"orientation_event_count_mismatch:{orientation_event_count}",
        )
    if expect.get("orientation_digest_matches_event"):
        append_fail(
            fail_reasons,
            bool(orientation_digest) and orientation_digest_consistent,
            "orientation_digest_mismatch",
        )
    if expect.get("hook_kind_metadata_matches_unit"):
        append_fail(
            fail_reasons,
            orientation_hook_consistent,
            "hook_kind_metadata_mismatch",
        )
    if expect.get("forbid_orientation_events_when_alignment") and alignment_mode:
        append_fail(
            fail_reasons,
            orientation_event_count == 0,
            "orientation_event_during_alignment",
        )
    if expect.get("no_forge_stage_changed_before_orientation_event"):
        try:
            first_orientation_index = next(
                i
                for i, event_type in enumerate(types)
                if event_type
                in {
                    "learning_unit.orientation_generated",
                    "learning_unit.orientation_fallback_used",
                }
            )
        except StopIteration:
            first_orientation_index = None
        try:
            first_stage_index = next(
                i
                for i, event_type in enumerate(types)
                if event_type == "learning_unit.forge_stage_changed"
            )
        except StopIteration:
            first_stage_index = None
        append_fail(
            fail_reasons,
            first_orientation_index is not None
            and (
                first_stage_index is None
                or first_orientation_index < first_stage_index
            ),
            "forge_stage_changed_before_orientation_event",
        )

    forbidden_events = expect.get("forbid_events") or []
    for forbidden_event in forbidden_events:
        append_fail(
            fail_reasons,
            forbidden_event not in types,
            f"forbidden_event_present:{forbidden_event}",
        )

    if expect.get("frontend_backend_consistent"):
        append_fail(
            fail_reasons,
            frontend_backend_consistent,
            "frontend_backend_mismatch",
        )

    if expect.get("first_value_signal_required"):
        append_fail(fail_reasons, first_value_signal, "missing_first_value_signal")

    event_contains_any = expect.get("event_contains_any")
    if event_contains_any:
        append_fail(
            fail_reasons,
            any(event_type in types for event_type in event_contains_any),
            f"event_contains_any_missing:{event_contains_any}",
        )

    expected_ask_stage = expect.get("if_alignment_true_then_forge_stage_should_remain")
    ask_advanced = False
    if alignment_mode and expected_ask_stage:
        ask_advanced = final_forge_stage != expected_ask_stage
        append_fail(
            fail_reasons,
            not ask_advanced,
            f"forge_stage_advanced_during_ask:{final_forge_stage!r}",
        )
    if alignment_mode and expect.get("forbid_forge_stage_changed_event_when_alignment"):
        append_fail(
            fail_reasons,
            not forge_stage_changed,
            "forge_stage_changed_event_during_alignment",
        )
        ask_advanced = ask_advanced or forge_stage_changed
    if forbid.get("stage_advanced_during_ask") and alignment_mode:
        append_fail(
            fail_reasons,
            not ask_advanced,
            "forbid_stage_advanced_during_ask",
        )

    session_learning_unit_id = session.get("learning_unit_id") if isinstance(session, dict) else None
    false_chat_forge = (
        session_learning_unit_id is None
        and (has_forge_metadata or has_temperature_metadata or has_learning_action)
    )

    metrics = {
        "learning_unit_created": learning_unit_created,
        "non_empty_response": non_empty_response,
        "forge_stage_metadata": has_forge_metadata,
        "temperature_state_metadata": has_temperature_metadata,
        "learning_action_metadata": has_learning_action,
        "learning_unit_payload_forge_stage": unit_has_forge,
        "orientation_context_present": has_orientation_context,
        "orientation_context_present_metadata": (
            has_orientation_presence_metadata
            and latest_orientation_presence is True
        ),
        "orientation_generated_event_count": len(generated_events),
        "orientation_fallback_event_count": len(fallback_events),
        "orientation_event_count": orientation_event_count,
        "orientation_digest_consistent": orientation_digest_consistent,
        "orientation_hook_consistent": orientation_hook_consistent,
        "orientation_source": orientation_source,
        "orientation_event_sources": sorted(orientation_event_sources),
        "frontend_backend_stage_consistent": frontend_backend_consistent,
        "chat_session_false_forge_stage": false_chat_forge,
        "alignment_mode": alignment_mode,
        "ask_alignment_stage_advanced": bool(alignment_mode and ask_advanced),
        "first_value_signal": first_value_signal,
        "forge_stage_changed_event_count": types.count("learning_unit.forge_stage_changed"),
        "alignment_suggested_or_rate_limited": (
            "learning_unit.alignment_suggested" in types
            or "learning_unit.alignment_rate_limited" in types
        ),
        "final_forge_stage": final_forge_stage,
        "final_temperature_state": final_temperature_state,
        "latest_learning_action": latest_metadata(payloads, "learning_action"),
    }
    return metrics, fail_reasons


def run_case(
    case: dict[str, Any],
    *,
    base_url: str,
    output_dir: Path,
    timeout: float,
) -> CaseRun:
    start = time.monotonic()
    case_id = case["id"]
    title = case.get("title") or case_id
    case_dir = output_dir / "cases" / case_id
    seed_text = case["setup"]["seed_text"]
    message = get_first_user_message(case)
    create_body = {
        "seed_text": seed_text,
        "source": case.get("setup", {}).get("source", "user_written"),
    }
    if case.get("setup", {}).get("source_ref"):
        create_body["source_ref"] = case["setup"]["source_ref"]
    chat_body = {"message": message, "stream": True}
    request_payload = {
        "case_id": case_id,
        "title": title,
        "create_learning_unit": {
            "method": "POST",
            "path": "/learning-units",
            "body": create_body,
        },
        "chat": {
            "method": "POST",
            "path_template": "/sessions/{session_id}/chat",
            "body": chat_body,
        },
    }
    write_json(case_dir / "request.json", request_payload)

    create_result = http_json(
        "POST",
        base_url,
        "/learning-units",
        create_body,
        timeout=timeout,
    )
    session_id = None
    unit_id = None
    if create_result.ok and isinstance(create_result.data, dict):
        session_id = create_result.data.get("session_id")
        unit_id = create_result.data.get("id")

    chat_result = ChatStreamResult(content="", payloads=[], raw_events=[])
    unit_result = HttpResult(status=0, error="learning unit was not created")
    session_result = HttpResult(status=0, error="session was not created")
    events_result = HttpResult(status=0, error="session was not created")
    events: list[dict[str, Any]] = []
    cleanup_result: HttpResult | None = None

    try:
        if session_id:
            chat_result = stream_chat(
                base_url,
                str(session_id),
                message,
                timeout=timeout,
            )
            session_result = http_json(
                "GET",
                base_url,
                f"/sessions/{urllib.parse.quote(str(session_id))}",
                timeout=timeout,
            )
            events_result = http_json(
                "GET",
                base_url,
                f"/sessions/{urllib.parse.quote(str(session_id))}/events",
                timeout=timeout,
            )
            events = load_events(events_result)
        if unit_id:
            unit_result = http_json(
                "GET",
                base_url,
                f"/learning-units/{urllib.parse.quote(str(unit_id))}",
                timeout=timeout,
            )
    finally:
        if unit_id:
            cleanup_result = http_json(
                "POST",
                base_url,
                f"/learning-units/{urllib.parse.quote(str(unit_id))}/stop",
                {"reason": "e2e_cleanup"},
                timeout=timeout,
            )

    write_json(
        case_dir / "response.json",
        {
            "create_response": {
                "status": create_result.status,
                "data": create_result.data,
                "error": create_result.error,
            },
            "chat_stream": {
                "content": chat_result.content,
                "payloads": chat_result.payloads,
                "raw_events": chat_result.raw_events,
                "error": chat_result.error,
            },
            "session_events_endpoint": {
                "status": events_result.status,
                "error": events_result.error,
            },
            "cleanup_response": (
                {
                    "status": cleanup_result.status,
                    "data": cleanup_result.data,
                    "error": cleanup_result.error,
                }
                if cleanup_result is not None
                else None
            ),
        },
    )
    write_json(case_dir / "session.json", session_result.data or {})
    write_json(case_dir / "learning_unit.json", unit_result.data or {})
    write_jsonl(case_dir / "events.jsonl", events)

    metrics, fail_reasons = judge_case(
        case,
        create_result=create_result,
        chat_result=chat_result,
        unit_result=unit_result,
        session_result=session_result,
        events=events,
    )
    if not session_result.ok:
        fail_reasons.append(f"session_fetch_failed:{session_result.error}")
    if not events_result.ok:
        fail_reasons.append(f"events_fetch_failed:{events_result.error}")
    if cleanup_result is not None and not cleanup_result.ok:
        fail_reasons.append(f"cleanup_failed:{cleanup_result.error}")

    duration_ms = int((time.monotonic() - start) * 1000)
    return CaseRun(
        case_id=case_id,
        title=title,
        duration_ms=duration_ms,
        verdict="fail" if fail_reasons else "pass",
        fail_reasons=fail_reasons,
        metrics=metrics,
        session_id=str(session_id) if session_id else None,
        learning_unit_id=str(unit_id) if unit_id else None,
    )


def rate(results: list[CaseRun], metric_name: str) -> float:
    if not results:
        return 0.0
    passed = sum(1 for result in results if result.metrics.get(metric_name) is True)
    return round(passed / len(results), 4)


def count(results: list[CaseRun], metric_name: str) -> int:
    return sum(1 for result in results if result.metrics.get(metric_name) is True)


def sum_metric(results: list[CaseRun], metric_name: str) -> int:
    total = 0
    for result in results:
        value = result.metrics.get(metric_name)
        if isinstance(value, bool):
            total += int(value)
        elif isinstance(value, int):
            total += value
    return total


def build_summary(dataset: dict[str, Any], results: list[CaseRun]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.verdict == "pass")
    failed = total - passed
    hard_targets = {
        "learning_unit_creation_rate": rate(results, "learning_unit_created"),
        "non_empty_response_rate": rate(results, "non_empty_response"),
        "forge_stage_metadata_rate": rate(results, "forge_stage_metadata"),
        "temperature_state_metadata_rate": rate(results, "temperature_state_metadata"),
        "learning_unit_payload_forge_stage_rate": rate(
            results, "learning_unit_payload_forge_stage"
        ),
        "frontend_backend_stage_consistency_rate": rate(
            results, "frontend_backend_stage_consistent"
        ),
        "chat_session_false_forge_stage_count": count(
            results, "chat_session_false_forge_stage"
        ),
        "ask_alignment_stage_advance_count": count(
            results, "ask_alignment_stage_advanced"
        ),
        "orientation_context_rate": rate(results, "orientation_context_present"),
        "orientation_context_present_metadata_rate": rate(
            results, "orientation_context_present_metadata"
        ),
        "orientation_digest_consistency_rate": rate(
            results, "orientation_digest_consistent"
        ),
    }
    quality_targets = {
        "first_value_case_pass_count": count(results, "first_value_signal"),
        "first_value_case_total": total,
        "forge_stage_changed_event_count": sum_metric(
            results, "forge_stage_changed_event_count"
        ),
        "orientation_generated_event_count": sum_metric(
            results, "orientation_generated_event_count"
        ),
        "orientation_fallback_event_count": sum_metric(
            results, "orientation_fallback_event_count"
        ),
        "orientation_event_count": sum_metric(results, "orientation_event_count"),
        "alignment_suggested_or_rate_limited_count": count(
            results, "alignment_suggested_or_rate_limited"
        ),
    }
    return {
        "suite_id": dataset["suite_id"],
        "dataset_version": dataset.get("dataset_version"),
        "total_cases": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "status": "pass" if failed == 0 else "fail",
        "hard_targets": hard_targets,
        "quality_targets": quality_targets,
        "case_results": [
            {
                "case_id": result.case_id,
                "title": result.title,
                "verdict": result.verdict,
                "fail_reasons": result.fail_reasons,
                "duration_ms": result.duration_ms,
                "session_id": result.session_id,
                "learning_unit_id": result.learning_unit_id,
                "metrics": result.metrics,
            }
            for result in results
        ],
    }


def run_dataset(
    dataset: dict[str, Any],
    *,
    dataset_path: Path,
    output_dir: Path,
    base_url: str,
    data_dir: str,
    timeout: float,
) -> int:
    run_id = run_id_for(dataset["suite_id"])
    start_time = utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "runner_version": RUNNER_VERSION,
        "run_id": run_id,
        "suite_id": dataset["suite_id"],
        "dataset": str(dataset_path),
        "dataset_version": dataset.get("dataset_version"),
        "base_url": base_url,
        "data_dir_arg": data_dir,
        "la_data_dir_env": os.environ.get("LA_DATA_DIR"),
        "started_at": start_time,
    }
    write_json(output_dir / "manifest.json", manifest)

    health = http_json("GET", base_url, "/health", timeout=timeout)
    if not health.ok:
        write_json(
            output_dir / "summary.json",
            {
                "suite_id": dataset["suite_id"],
                "status": "fail",
                "transport_error": f"health check failed: {health.error}",
            },
        )
        print(f"Health check failed: {health.error}", file=sys.stderr)
        return 2

    results: list[CaseRun] = []
    for case in dataset["cases"]:
        result = run_case(
            case,
            base_url=base_url,
            output_dir=output_dir,
            timeout=timeout,
        )
        results.append(result)
        print(
            f"{result.verdict.upper():4} {result.case_id} "
            f"({result.duration_ms} ms)"
        )
        if result.fail_reasons:
            for reason in result.fail_reasons:
                print(f"     - {reason}")

    end_time = utc_now()
    summary = build_summary(dataset, results)
    summary.update(
        {
            "run_id": run_id,
            "started_at": start_time,
            "ended_at": end_time,
            "base_url": base_url,
            "data_dir_arg": data_dir,
            "output_dir": str(output_dir),
        }
    )
    manifest["ended_at"] = end_time
    manifest["status"] = summary["status"]
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "summary.json", summary)
    write_jsonl(
        output_dir / "results.jsonl",
        [
            {
                "case_id": result.case_id,
                "title": result.title,
                "verdict": result.verdict,
                "fail_reasons": result.fail_reasons,
                "duration_ms": result.duration_ms,
                "session_id": result.session_id,
                "learning_unit_id": result.learning_unit_id,
                "metrics": result.metrics,
            }
            for result in results
        ],
    )
    print(
        f"Summary: {summary['passed']}/{summary['total_cases']} passed; "
        f"status={summary['status']}"
    )
    return 0 if summary["status"] == "pass" else 1


def dry_run(dataset: dict[str, Any], *, dataset_path: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "runner_version": RUNNER_VERSION,
        "dataset": str(dataset_path),
        "suite_id": dataset.get("suite_id"),
        "dataset_version": dataset.get("dataset_version"),
        "case_count": len(dataset.get("cases") or []),
        "case_ids": [case.get("id") for case in dataset.get("cases") or []],
        "status": "ok",
    }
    write_json(output_dir / "dry_run.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real E2E JSON datasets.")
    parser.add_argument("--dataset", required=True, help="Path to dataset JSON.")
    parser.add_argument("--output", required=True, help="Evidence output directory.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Dev server base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("LA_DATA_DIR", ".learning_agent_data"),
        help="Expected LA data dir for this run; recorded in manifest.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout seconds per request.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate dataset shape and write dry_run.json without HTTP calls.",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    dataset = read_json(dataset_path)
    errors = validate_dataset(dataset)
    if errors:
        for error in errors:
            print(f"dataset error: {error}", file=sys.stderr)
        return 2
    if args.dry_run:
        return dry_run(dataset, dataset_path=dataset_path, output_dir=output_dir)
    return run_dataset(
        dataset,
        dataset_path=dataset_path,
        output_dir=output_dir,
        base_url=normalize_base_url(args.base_url),
        data_dir=args.data_dir,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
