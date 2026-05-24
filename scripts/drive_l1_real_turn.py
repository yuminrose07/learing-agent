"""Drive one real agent turn + tool call to validate L1 event stream.

Steps:
1. Spin up LearningAgentSystem with a throwaway data dir (no pollution to real
   .learning_agent_data/).
2. Place a small target file in that dir.
3. Ask the agent (real LLM) to read it via the read_file tool.
4. Stream the response (printed live).
5. After the turn, dump the sessions/<id>.events.jsonl summary so we can confirm
   the causal chain: session.created → message.user_appended → tool.exec_started
   → tool.exec_completed → message_end.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/roseannk/my-agent")

from learning_agent.ai.models import AgentMode  # noqa: E402
from learning_agent.config import Config  # noqa: E402
from learning_agent.learning_agent.main import LearningAgentSystem  # noqa: E402


async def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="la-l1-real-"))
    # 注意：tool_guard 默认拦截工作目录外的路径，把目标文件放在项目内
    # 才能让 read_file 真正被执行；这样 tool.exec_* 事件链路才会触发。
    target_dir = Path("/Users/roseannk/my-agent/.test_artifacts/l1_real_turn")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "hello.txt"
    target.write_text("line1: hello\nline2: from L1 sanity check\nline3: bye\n", encoding="utf-8")

    config = Config()
    config.data_dir = str(tmpdir / "data")
    config.log_level = "WARNING"

    print(f"[Driver] data_dir = {config.data_dir}")
    print(f"[Driver] target file = {target}")

    system = LearningAgentSystem(config)
    await system.initialize()

    try:
        session_id = await system.start_session()
        print(f"[Driver] session_id = {session_id}")

        prompt = (
            f"Please use the read_file tool to read the file at this exact path: "
            f"{target}. After reading it, reply with one sentence summarizing what "
            f"the file contains."
        )
        print(f"\n[You] {prompt}\n")
        print("[Assistant] ", end="", flush=True)
        async for chunk in system.stream_session_chat(session_id, prompt, mode=AgentMode.CHAT):
            if chunk.content:
                print(chunk.content, end="", flush=True)
        print()
    finally:
        await system.shutdown()

    events_path = Path(config.data_dir) / "sessions" / f"{session_id}.events.jsonl"
    print(f"\n[Driver] events.jsonl = {events_path}")
    print(f"[Driver] file exists: {events_path.exists()}")
    if not events_path.exists():
        return

    by_visibility: dict[str, int] = {}
    business_type_seq: list[tuple[int, str]] = []
    obs_type_seq: list[tuple[int, str, str]] = []  # (seq, type, parent_event_id)
    started_event_ids: set[str] = set()
    completed_with_parent: list[str] = []

    with events_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            evt = json.loads(line)
            vis = evt.get("visibility", "agent")
            by_visibility[vis] = by_visibility.get(vis, 0) + 1
            if vis == "observability":
                obs_type_seq.append((evt["seq"], evt["type"], evt.get("parent_event_id")))
                if evt["type"] == "tool.exec_started":
                    started_event_ids.add(evt["event_id"])
                if evt["type"] in ("tool.exec_completed", "tool.exec_failed"):
                    if evt.get("parent_event_id"):
                        completed_with_parent.append(evt["parent_event_id"])
            else:
                business_type_seq.append((evt["seq"], evt["type"]))

    print("\n[Driver] event counts by visibility:")
    for k, v in sorted(by_visibility.items()):
        print(f"    {k:<14} {v}")

    print("\n[Driver] business event timeline (visibility != observability):")
    for seq, t in business_type_seq:
        print(f"    seq={seq:>3}  {t}")

    print("\n[Driver] observability event timeline (visibility == observability):")
    for seq, t, parent in obs_type_seq:
        parent_str = parent if parent else "—"
        print(f"    seq={seq:>3}  {t:<26} parent={parent_str}")

    chain_ok = all(pid in started_event_ids for pid in completed_with_parent)
    print(
        f"\n[Driver] causal-chain check: "
        f"{len(completed_with_parent)} completed/failed events, "
        f"{sum(1 for p in completed_with_parent if p in started_event_ids)} "
        f"correctly pointing at a started event → chain_ok={chain_ok}"
    )

    # Replay sanity: feed events through projection and verify observability events
    # do not leak into messages.
    from learning_agent.learning_agent.session_event_store import SessionEventStore
    from learning_agent.ai.file_store import FileStore
    from learning_agent.learning_agent.session_projection import replay_events

    fs = FileStore(config.data_dir)
    store = SessionEventStore(fs)
    events = store.read_events(session_id)
    snapshot = replay_events(events, session_id=session_id)

    obs_payloads_in_messages = []
    for m in snapshot.messages:
        if isinstance(m.content, str) and "tool.exec_" in m.content:
            obs_payloads_in_messages.append(m.content[:80])

    print(f"\n[Driver] replay rebuilt {len(snapshot.messages)} messages")
    print(f"[Driver] corrupt events: {len(snapshot.corrupt_events)}")
    print(
        f"[Driver] observability leakage into messages: "
        f"{len(obs_payloads_in_messages)} (expect 0)"
    )

    print(f"\n[Driver] keep data dir for inspection: {tmpdir}")


if __name__ == "__main__":
    asyncio.run(main())
