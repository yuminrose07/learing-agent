"""Render a single-file HTML report from compaction dataset run artifacts.

Reads ``index.json`` + each case's ``report.json`` under
``.test_artifacts/compaction_dataset_runs/<mode>/latest/`` and produces
``compaction_dataset_report.html`` next to the index.

Usage::

    python3 tests/render_compaction_dataset_report.py            # real mode
    python3 tests/render_compaction_dataset_report.py scripted   # scripted mode
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ARTIFACT_BASE = Path("/Users/roseannk/my-agent/.test_artifacts/compaction_dataset_runs")


def _esc(text: Any) -> str:
    return html.escape(str(text) if text is not None else "", quote=True)


def _truncate(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _severity_color(severity: str) -> str:
    return {
        "none": "#22c55e",
        "low": "#84cc16",
        "medium": "#f59e0b",
        "high": "#ef4444",
    }.get(severity, "#94a3b8")


def _format_summary_block(summary: str) -> str:
    if not summary:
        return '<div class="muted">（无 summary）</div>'
    return f'<pre class="summary">{_esc(summary)}</pre>'


def _render_turn_card(idx: int, turn: dict[str, Any]) -> str:
    plan = turn.get("compaction_plan") or {}
    compact_badge = ""
    if plan.get("use_full_compact"):
        scope = plan.get("compact_scope") or "?"
        compact_badge = (
            f'<span class="badge compact-{_esc(scope)}">'
            f'⚡ full compact · scope={_esc(scope)}</span>'
        )
    elif plan.get("summary_block_present"):
        compact_badge = '<span class="badge compact-recent">summary block carried</span>'

    view = turn.get("view") or {}
    sys_blocks = view.get("system_blocks") or []
    non_sys = view.get("non_system_contents") or []
    summary_count = turn.get("summary_event_count", 0)
    summary_text = turn.get("latest_summary") or ""
    summary_len = len(summary_text)

    return f"""
<div class="turn-card">
  <div class="turn-head">
    <div class="turn-no">T{idx + 1}</div>
    <div class="turn-meta">
      summary events: <b>{summary_count}</b> ·
      summary len: <b>{summary_len}</b> chars ·
      sys blocks: <b>{len(sys_blocks)}</b> ·
      non-sys: <b>{len(non_sys)}</b>
      {compact_badge}
    </div>
  </div>
  <div class="turn-body">
    <div class="msg user">
      <div class="msg-label">USER</div>
      <div class="msg-body">{_esc(turn.get("user", ""))}</div>
    </div>
    <div class="msg assistant">
      <div class="msg-label">ASSISTANT</div>
      <div class="msg-body">{_esc(_truncate(turn.get("assistant", ""), 800))}</div>
    </div>
    <details>
      <summary>latest summary after this turn ({summary_len} chars)</summary>
      {_format_summary_block(_truncate(summary_text, 2400))}
    </details>
    <details>
      <summary>retained non-system view ({len(non_sys)} entries)</summary>
      <ol class="view-list">
        {''.join(f'<li><pre>{_esc(_truncate(c, 400))}</pre></li>' for c in non_sys)}
      </ol>
    </details>
  </div>
</div>
"""


def _render_sparkline(values: list[int], compact_turns: list[int]) -> str:
    if not values:
        return ""
    w, h, pad = 520, 110, 12
    vmax = max(values) or 1
    step = (w - 2 * pad) / max(1, len(values) - 1) if len(values) > 1 else 0
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = h - pad - (v / vmax) * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    marker_dots = []
    for ct in compact_turns:
        if 0 <= ct < len(values):
            x = pad + ct * step
            v = values[ct]
            y = h - pad - (v / vmax) * (h - 2 * pad)
            marker_dots.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ef4444" stroke="#fff" stroke-width="1.5"/>'
            )
    return f"""
<svg viewBox="0 0 {w} {h}" class="spark" xmlns="http://www.w3.org/2000/svg">
  <polyline fill="none" stroke="#3b82f6" stroke-width="2" points="{' '.join(pts)}"/>
  {''.join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" fill="#3b82f6"/>' for p in pts)}
  {''.join(marker_dots)}
  <text x="{pad}" y="{h - 2}" font-size="10" fill="#64748b">turn 1</text>
  <text x="{w - pad - 30}" y="{h - 2}" font-size="10" fill="#64748b">turn {len(values)}</text>
  <text x="{pad}" y="12" font-size="10" fill="#64748b">max={vmax} chars</text>
</svg>
"""


def _render_case(report: dict[str, Any]) -> str:
    name = report["case_name"]
    analysis = report.get("analysis") or {}
    drift = analysis.get("semantic_drift") or {}
    score = drift.get("score", 0)
    severity = drift.get("severity", "none")
    status = drift.get("status", "unknown")
    warnings = drift.get("warnings") or []
    turns = report.get("turn_reports") or []

    summary_lens = [len(t.get("latest_summary") or "") for t in turns]
    compact_turns = [
        i for i, t in enumerate(turns) if (t.get("compaction_plan") or {}).get("use_full_compact")
    ]

    matched = analysis.get("summary_retention", {}).get("matched", []) or []
    missing = analysis.get("summary_retention", {}).get("missing", []) or []
    pending_matched = analysis.get("pending_retention", {}).get("matched", []) or []
    pending_missing = analysis.get("pending_retention", {}).get("missing", []) or []
    recent_matched = analysis.get("recent_view_retention", {}).get("matched", []) or []
    recent_missing = analysis.get("recent_view_retention", {}).get("missing", []) or []

    color = _severity_color(severity)

    warning_chips = "".join(f'<span class="chip warn">{_esc(w)}</span>' for w in warnings) or '<span class="chip ok">no warnings</span>'

    return f"""
<section class="case" id="case-{_esc(name)}">
  <div class="case-head">
    <div>
      <h2>{_esc(name)}</h2>
      <div class="muted">{_esc(report.get("description", ""))}</div>
      <div class="muted small">provider: <b>{_esc(report.get("provider_label", ""))}</b> · mode: <b>{_esc(report.get("provider_mode", ""))}</b> · session: <code>{_esc(report.get("session_id", ""))}</code></div>
    </div>
    <div class="score-box" style="border-color:{color}">
      <div class="score-num" style="color:{color}">{score}</div>
      <div class="score-label">drift score · <span style="color:{color}">{_esc(severity)}</span> · {_esc(status)}</div>
    </div>
  </div>

  <div class="case-grid">
    <div class="card">
      <h3>压缩事件链路</h3>
      <table>
        <tr><td>summary 事件总数</td><td><b>{report.get("summary_event_count", 0)}</b></td></tr>
        <tr><td>full compact 触发轮次</td><td>{', '.join(f'T{i+1}' for i in compact_turns) or '—'}</td></tr>
        <tr><td>scope prefix (actual)</td><td><code>{_esc(analysis.get("event_flow", {}).get("actual_scope_prefix", []))}</code></td></tr>
      </table>
      {_render_sparkline(summary_lens, compact_turns)}
      <div class="caption">蓝线: 每轮结束后 latest summary 字符数；红点: 该轮触发了 full compact</div>
    </div>

    <div class="card">
      <h3>语义保持 (vs 期望 anchor 字符串)</h3>
      <div class="kpi-row">
        <div class="kpi"><div class="kpi-n">{len(matched)}</div><div class="kpi-l">summary 命中</div></div>
        <div class="kpi miss"><div class="kpi-n">{len(missing)}</div><div class="kpi-l">summary 缺失</div></div>
        <div class="kpi"><div class="kpi-n">{len(pending_matched)}</div><div class="kpi-l">pending 命中</div></div>
        <div class="kpi miss"><div class="kpi-n">{len(pending_missing)}</div><div class="kpi-l">pending 缺失</div></div>
        <div class="kpi"><div class="kpi-n">{len(recent_matched)}</div><div class="kpi-l">recent 命中</div></div>
        <div class="kpi miss"><div class="kpi-n">{len(recent_missing)}</div><div class="kpi-l">recent 缺失</div></div>
      </div>
      <div class="warnings">{warning_chips}</div>
    </div>
  </div>

  <details class="card wide" open>
    <summary><b>最终 canonical summary 全文 ({len(report.get("latest_summary") or "")} chars)</b></summary>
    {_format_summary_block(report.get("latest_summary") or "")}
  </details>

  <details class="card wide">
    <summary><b>Pending 区抽取段</b></summary>
    {_format_summary_block(report.get("pending_section") or "")}
  </details>

  <details class="card wide">
    <summary><b>逐轮对话与压缩计划 ({len(turns)} 轮)</b></summary>
    <div class="turns">{''.join(_render_turn_card(i, t) for i, t in enumerate(turns))}</div>
  </details>
</section>
"""


def _render_overview(index: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    severity_counts = index.get("severity_counts", {}) or {}
    total_cases = index.get("total_cases", 0)
    total_summary_events = sum(c.get("summary_event_count", 0) for c in cases)
    total_turns = sum(len(c.get("turn_reports") or []) for c in cases)
    total_compactions = sum(
        sum(1 for t in (c.get("turn_reports") or []) if (t.get("compaction_plan") or {}).get("use_full_compact"))
        for c in cases
    )
    avg_score = index.get("average_drift_score", 0)
    recur = index.get("top_recurring_warnings", []) or []
    case_rows = "".join(
        f"""
        <tr>
          <td><a href="#case-{_esc(c['case_name'])}">{_esc(c['case_name'])}</a></td>
          <td>{len(c.get("turn_reports") or [])}</td>
          <td>{c.get("summary_event_count", 0)}</td>
          <td><span class="dot" style="background:{_severity_color(((c.get('analysis') or {}).get('semantic_drift') or {}).get('severity', 'none'))}"></span>{((c.get('analysis') or {}).get('semantic_drift') or {}).get('severity', 'none')}</td>
          <td>{((c.get('analysis') or {}).get('semantic_drift') or {}).get('score', 0)}</td>
          <td>{len(((c.get('analysis') or {}).get('semantic_drift') or {}).get('warnings') or [])}</td>
        </tr>
        """
        for c in cases
    )
    recurring_html = (
        "".join(f'<li>{_esc(r["warning"])} <span class="muted">× {r["count"]}</span></li>' for r in recur)
        or "<li>无</li>"
    )

    return f"""
<section class="overview card wide">
  <h2>总览</h2>
  <div class="kpi-row big">
    <div class="kpi"><div class="kpi-n">{total_cases}</div><div class="kpi-l">case 数</div></div>
    <div class="kpi"><div class="kpi-n">{total_turns}</div><div class="kpi-l">总轮次</div></div>
    <div class="kpi"><div class="kpi-n">{total_summary_events}</div><div class="kpi-l">summary 事件总数</div></div>
    <div class="kpi"><div class="kpi-n">{total_compactions}</div><div class="kpi-l">full compact 总次数</div></div>
    <div class="kpi"><div class="kpi-n">{avg_score:.1f}</div><div class="kpi-l">平均 drift score</div></div>
  </div>
  <div class="grid-2">
    <div>
      <h3>case 状态汇总</h3>
      <table class="case-table">
        <thead><tr><th>case</th><th>turns</th><th>summary 事件</th><th>severity</th><th>score</th><th>warnings</th></tr></thead>
        <tbody>{case_rows}</tbody>
      </table>
    </div>
    <div>
      <h3>高频警告</h3>
      <ul class="recur">{recurring_html}</ul>
      <h3>severity 分布</h3>
      <div class="sev-bars">
        {''.join(f'<div class="sev-bar"><span class="lbl">{k}</span><span class="bar" style="width:{(severity_counts.get(k,0)/max(1,total_cases))*100:.0f}%;background:{_severity_color(k)}"></span><span class="num">{severity_counts.get(k,0)}</span></div>' for k in ("none","low","medium","high"))}
      </div>
    </div>
  </div>
</section>
"""


CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif;
       margin: 0; padding: 24px; background: #f8fafc; color: #1e293b; line-height: 1.55; }
h1 { margin: 0 0 4px; font-size: 26px; }
h2 { margin: 0 0 8px; font-size: 20px; }
h3 { margin: 12px 0 8px; font-size: 15px; color: #334155; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: #64748b; font-size: 13px; }
.small { font-size: 12px; }
code { background: #eef2ff; padding: 1px 6px; border-radius: 4px; font-size: 12px; }
.banner { background: linear-gradient(135deg,#0f172a 0%,#1e293b 100%); color:#e2e8f0;
          padding: 22px 28px; border-radius: 12px; margin-bottom: 22px; }
.banner .meta { font-size: 13px; color: #94a3b8; margin-top: 6px; }
.card { background: #fff; border-radius: 10px; border: 1px solid #e2e8f0;
        padding: 16px 18px; margin-bottom: 14px; }
.wide { display: block; }
details.card > summary { cursor: pointer; padding: 4px 0; }
.case { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
        margin-bottom: 22px; padding: 18px 22px; }
.case-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 14px; }
.case-head h2 { margin: 0 0 4px; }
.case-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.score-box { border: 3px solid #cbd5e1; border-radius: 12px; padding: 12px 18px; text-align: center; min-width: 130px; }
.score-num { font-size: 36px; font-weight: 700; line-height: 1; }
.score-label { font-size: 11px; color: #64748b; margin-top: 4px; text-transform: lowercase; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
table td, table th { padding: 6px 8px; border-bottom: 1px solid #f1f5f9; text-align: left; }
table th { font-weight: 600; color: #475569; font-size: 12px; }
.case-table tr:hover { background: #f8fafc; }
.kpi-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0; }
.kpi-row.big { gap: 16px; }
.kpi { background: #f1f5f9; padding: 10px 14px; border-radius: 8px; min-width: 88px; text-align: center; }
.kpi.miss { background: #fef2f2; }
.kpi-n { font-size: 22px; font-weight: 700; color: #0f172a; }
.kpi.miss .kpi-n { color: #b91c1c; }
.kpi-l { font-size: 11px; color: #64748b; }
.warnings { margin-top: 12px; }
.chip { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; margin: 2px 4px 2px 0; }
.chip.warn { background: #fef3c7; color: #92400e; }
.chip.ok { background: #dcfce7; color: #166534; }
.badge { display: inline-block; margin-left: 8px; padding: 2px 9px; border-radius: 999px;
         font-size: 11px; font-weight: 600; }
.compact-full { background: #fee2e2; color: #991b1b; }
.compact-incremental { background: #fef3c7; color: #92400e; }
.compact-recent { background: #dbeafe; color: #1e40af; }
.turn-card { border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px; margin: 8px 0; background: #fafbfc; }
.turn-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.turn-no { background: #0f172a; color: #fff; font-weight: 700; padding: 2px 10px; border-radius: 6px; font-size: 12px; }
.turn-meta { font-size: 12px; color: #475569; }
.msg { padding: 8px 10px; margin: 4px 0; border-left: 3px solid; border-radius: 4px; }
.msg.user { border-color: #3b82f6; background: #eff6ff; }
.msg.assistant { border-color: #10b981; background: #ecfdf5; }
.msg-label { font-size: 10px; font-weight: 700; color: #64748b; letter-spacing: 0.5px; }
.msg-body { white-space: pre-wrap; font-size: 13px; }
.summary { background: #f8fafc; padding: 10px; border-radius: 6px; white-space: pre-wrap;
           font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5;
           max-height: 480px; overflow: auto; border: 1px solid #e2e8f0; }
.spark { width: 100%; height: 120px; }
.caption { font-size: 11px; color: #64748b; margin-top: -4px; }
.grid-2 { display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; align-items: start; }
.recur { padding-left: 18px; font-size: 13px; }
.sev-bars { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.sev-bar { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.sev-bar .lbl { width: 50px; color: #475569; }
.sev-bar .bar { height: 12px; border-radius: 4px; }
.sev-bar .num { color: #475569; }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.view-list { padding-left: 20px; }
.view-list pre { background:#f8fafc; padding:8px; border-radius:4px; white-space:pre-wrap; font-size:12px; }
"""


def render(mode: str = "real") -> Path:
    artifact_root = ARTIFACT_BASE / mode / "latest"
    index_path = artifact_root / "index.json"
    if not index_path.exists():
        raise SystemExit(f"index.json not found: {index_path}. Run the dataset first.")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    case_reports: list[dict[str, Any]] = []
    for case_dir in sorted(artifact_root.iterdir()):
        if not case_dir.is_dir():
            continue
        report_file = case_dir / "report.json"
        if not report_file.exists():
            continue
        case_reports.append(json.loads(report_file.read_text(encoding="utf-8")))

    generated_at = index.get("generated_at") or datetime.utcnow().isoformat()
    provider_mode = index.get("provider_mode", mode)

    body = (
        _render_overview(index, case_reports)
        + "".join(_render_case(c) for c in case_reports)
    )

    output_html = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<title>Compaction Dataset Report · {_esc(provider_mode)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="banner">
  <h1>Session Compaction Dataset 真实回放报告</h1>
  <div class="meta">
    mode: <b>{_esc(provider_mode)}</b> ·
    artifact: <code>{_esc(artifact_root)}</code> ·
    generated: {_esc(generated_at)}
  </div>
</div>
{body}
<footer class="muted small" style="margin-top:30px;text-align:center">
  rendered by tests/render_compaction_dataset_report.py · dataset 来自 tests/fixtures/compaction_long_task_cases
</footer>
</body>
</html>"""

    output_path = artifact_root / "compaction_dataset_report.html"
    output_path.write_text(output_html, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "real"
    out = render(mode)
    print(f"wrote {out}")
