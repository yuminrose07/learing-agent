"""Render a single-file HTML timeline view from a session events.jsonl file.

Reads one ``<session_id>.events.jsonl`` (the L1 fact source) and produces a
self-contained, dependency-free HTML report visualising:

- chronological timeline (seq ascending)
- visibility chips (agent / system / observability)
- causal chain via ``parent_event_id`` — flat list with `↳ parent #N` indicator,
  NOT a tree (per architecture doc §4.4)
- failure highlight for ``tool.exec_failed`` and ``error_type``-bearing payloads
- click-to-expand full payload (``<details>``)

CLI::

    python3 tests/render_session_timeline.py <events.jsonl> --out <out.html>

The CSS borrows tone from ``tests/render_compaction_dataset_report.py`` so the
two reports feel like one family, but each file is independent and self-contained
— no shared import.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _esc(text: Any) -> str:
    return html.escape(str(text) if text is not None else "", quote=True)


def _short_ts(raw: str | None) -> str:
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return raw[:19] if len(raw) >= 19 else raw


def _full_ts(raw: str | None) -> str:
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw


_VIS_COLOR = {
    "agent": "#2563eb",
    "system": "#64748b",
    "ui": "#0891b2",
    "observability": "#ea580c",
}


def _vis_chip(visibility: str) -> str:
    color = _VIS_COLOR.get(visibility, "#64748b")
    label = _esc(visibility)
    return f'<span class="vis-chip" style="background:{color}1a;color:{color};border-color:{color}55">{label}</span>'


def _is_failure(event: dict[str, Any]) -> bool:
    event_type = event.get("type", "")
    if event_type in ("tool.exec_failed", "tool.call_failed", "message.stream_failed"):
        return True
    payload = event.get("payload") or {}
    return bool(payload.get("error_type") or payload.get("error"))


def _read_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"warning: skipping malformed line: {exc}", file=sys.stderr)
    events.sort(key=lambda e: int(e.get("seq") or 0))
    return events


def _render_payload(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return _esc(text)


def _render_event_row(
    event: dict[str, Any],
    event_id_to_seq: dict[str, int],
) -> str:
    seq = event.get("seq", "?")
    visibility = event.get("visibility", "agent")
    event_type = event.get("type", "")
    ts_short = _short_ts(event.get("ts"))
    ts_full = _full_ts(event.get("ts"))
    failure_class = " is-failure" if _is_failure(event) else ""
    parent_id = event.get("parent_event_id")
    parent_indicator = ""
    if parent_id:
        parent_seq = event_id_to_seq.get(parent_id)
        if parent_seq is not None:
            parent_indicator = (
                f'<span class="parent-link">↳ parent <a href="#evt-{parent_seq}">#{parent_seq}</a></span>'
            )
        else:
            parent_indicator = (
                f'<span class="parent-link parent-missing">↳ parent {_esc(parent_id[:12])}… (not in file)</span>'
            )
    payload_html = _render_payload(event.get("payload") or {})
    event_id = event.get("event_id", "")
    return f"""
<details class="event-row{failure_class}" id="evt-{_esc(seq)}">
  <summary>
    <span class="seq">#{_esc(seq)}</span>
    <span class="ts" title="{_esc(ts_full)}">{_esc(ts_short)}</span>
    <span class="type">{_esc(event_type)}</span>
    {_vis_chip(visibility)}
    {parent_indicator}
  </summary>
  <div class="event-body">
    <div class="event-meta">
      <code>event_id={_esc(event_id)}</code>
      <code>ts={_esc(ts_full)}</code>
      {f'<code>parent={_esc(parent_id)}</code>' if parent_id else ''}
    </div>
    <pre class="payload">{payload_html}</pre>
  </div>
</details>
"""


def _render_overview(events: list[dict[str, Any]], source_path: Path) -> str:
    if not events:
        return f"""
<section class="card">
  <h2>session events</h2>
  <div class="muted">source: <code>{_esc(source_path)}</code></div>
  <div class="muted" style="margin-top:8px">⚠️ 没有事件可显示</div>
</section>
"""
    session_ids = sorted({e.get("session_id", "?") for e in events})
    vis_counter = Counter(e.get("visibility", "agent") for e in events)
    type_counter = Counter(e.get("type", "?") for e in events)
    first_ts = _full_ts(events[0].get("ts"))
    last_ts = _full_ts(events[-1].get("ts"))
    failure_count = sum(1 for e in events if _is_failure(e))

    vis_chips = " ".join(
        f'<span class="vis-stat">{_vis_chip(v)} × <b>{n}</b></span>'
        for v, n in vis_counter.most_common()
    )
    top_types = "".join(
        f"<tr><td><code>{_esc(t)}</code></td><td>{n}</td></tr>"
        for t, n in type_counter.most_common(10)
    )

    session_label = ", ".join(session_ids) if session_ids else "?"

    return f"""
<section class="card overview">
  <h2>session events</h2>
  <div class="muted">source: <code>{_esc(source_path)}</code></div>
  <div class="kpi-row">
    <div class="kpi"><div class="kpi-n">{len(events)}</div><div class="kpi-l">events</div></div>
    <div class="kpi"><div class="kpi-n">{len(session_ids)}</div><div class="kpi-l">sessions</div></div>
    <div class="kpi {'miss' if failure_count else ''}"><div class="kpi-n">{failure_count}</div><div class="kpi-l">failures</div></div>
  </div>
  <div class="muted small">session: <code>{_esc(session_label)}</code></div>
  <div class="muted small">range: <code>{_esc(first_ts)}</code> → <code>{_esc(last_ts)}</code></div>
  <div class="vis-stats">{vis_chips}</div>
  <details>
    <summary>事件类型 Top 10</summary>
    <table>{top_types}</table>
  </details>
</section>
"""


CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #f8fafc; color: #1e293b; line-height: 1.55; }
h1 { margin: 0 0 4px; font-size: 24px; }
h2 { margin: 0 0 8px; font-size: 18px; }
.muted { color: #64748b; font-size: 13px; }
.small { font-size: 12px; }
code { background: #eef2ff; padding: 1px 6px; border-radius: 4px; font-size: 12px; font-family: ui-monospace, monospace; }
.banner { background: linear-gradient(135deg,#0f172a 0%,#1e293b 100%); color:#e2e8f0;
          padding: 18px 24px; border-radius: 12px; margin-bottom: 18px; }
.banner .meta { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.card { background: #fff; border-radius: 10px; border: 1px solid #e2e8f0;
        padding: 16px 18px; margin-bottom: 16px; }
.overview .kpi-row { display: flex; gap: 14px; margin: 12px 0; }
.kpi { background: #f1f5f9; padding: 10px 14px; border-radius: 8px; min-width: 88px; text-align: center; }
.kpi.miss { background: #fef2f2; }
.kpi-n { font-size: 24px; font-weight: 700; color: #0f172a; }
.kpi.miss .kpi-n { color: #b91c1c; }
.kpi-l { font-size: 11px; color: #64748b; }
.vis-stats { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 12px; font-size: 13px; color: #475569; }
.vis-stat b { color: #0f172a; }
.vis-chip { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
            font-weight: 600; border: 1px solid; vertical-align: middle; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
table td { padding: 4px 8px; border-bottom: 1px solid #f1f5f9; }
.timeline { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 4px 0; }
.event-row { padding: 8px 16px; border-left: 3px solid #e2e8f0; margin: 2px 12px;
             border-radius: 0 6px 6px 0; background: #fafbfc; }
.event-row.is-failure { background: #fef2f2; border-left-color: #ef4444; }
.event-row.is-failure summary .type { color: #b91c1c; font-weight: 700; }
.event-row > summary { cursor: pointer; list-style: none; display: flex; flex-wrap: wrap;
                       gap: 10px; align-items: center; font-size: 13px; }
.event-row > summary::-webkit-details-marker { display: none; }
.event-row > summary::before { content: '▸'; color: #94a3b8; font-size: 10px; margin-right: 2px; }
.event-row[open] > summary::before { content: '▾'; }
.event-row .seq { font-family: ui-monospace, monospace; font-weight: 700; color: #475569;
                  min-width: 42px; display: inline-block; }
.event-row .ts { color: #64748b; font-family: ui-monospace, monospace; font-size: 12px; }
.event-row .type { color: #0f172a; font-family: ui-monospace, monospace; font-weight: 500; }
.parent-link { font-size: 12px; color: #64748b; margin-left: 4px; }
.parent-link a { color: #2563eb; text-decoration: none; }
.parent-link a:hover { text-decoration: underline; }
.parent-link.parent-missing { color: #b91c1c; }
.event-body { margin-top: 10px; padding: 10px; background: #fff; border-radius: 6px;
              border: 1px solid #f1f5f9; }
.event-meta { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: #64748b; margin-bottom: 8px; }
.payload { background: #f8fafc; padding: 10px; border-radius: 6px; white-space: pre-wrap;
           font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.45;
           max-height: 480px; overflow: auto; border: 1px solid #e2e8f0; margin: 0; }
:target.event-row { border-left-color: #2563eb; box-shadow: 0 0 0 2px #2563eb33; }
"""


def render(events_path: Path, output_path: Path) -> Path:
    events = _read_events(events_path)
    event_id_to_seq = {e["event_id"]: int(e["seq"]) for e in events if "event_id" in e and "seq" in e}

    timeline_html = "".join(_render_event_row(e, event_id_to_seq) for e in events)
    overview_html = _render_overview(events, events_path)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    output_html = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>Session Events Timeline · {_esc(events_path.name)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="banner">
  <h1>Session Events Timeline</h1>
  <div class="meta">
    source: <code>{_esc(events_path)}</code> · generated: {_esc(generated_at)}
  </div>
</div>
{overview_html}
<section class="timeline">
  {timeline_html or '<div class="muted" style="padding:20px;text-align:center">empty</div>'}
</section>
<footer class="muted small" style="margin-top:24px;text-align:center">
  rendered by tests/render_session_timeline.py · L3 offline viewer for L1 events.jsonl
</footer>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_html, encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("events", help="path to <session>.events.jsonl")
    parser.add_argument("--out", "-o", required=True, help="output HTML path")
    args = parser.parse_args(argv)

    events_path = Path(args.events).expanduser().resolve()
    if not events_path.is_file():
        print(f"error: {events_path} not found", file=sys.stderr)
        return 2

    output_path = Path(args.out).expanduser().resolve()
    written = render(events_path, output_path)
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
