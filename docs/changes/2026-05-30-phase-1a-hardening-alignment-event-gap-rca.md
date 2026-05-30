# 2026-05-30 Phase 1A hardening: 对齐侧面事件缺失 RCA

## 运行环境 / 时间戳

- Worktree: `/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening`
- Branch: `feature/learning-phase-1a-hardening`
- 时间: `2026-05-30T17:30:42Z`
- 数据源：`/Users/roseannk/my-agent/.learning_agent_data/sessions/*.events.jsonl`
- Provider: 未调用外部 LLM/provider；本轮只做 append-only JSONL 离线诊断

## 原始目标

doc1 裂缝 3 要求解释 5 个对齐侧面事件在真实 JSONL 中长期观察不到的原因，并给每个事件打 verdict：

- `learning_unit.alignment_suggested`
- `learning_unit.alignment_skipped`
- `learning_unit.alignment_resolved`
- `learning_unit.assumption_accepted`
- `learning_unit.objective_refined`

本裂缝只调查，不补 UI。若发现 route bug，才做最小 emit 修复。

## 新增诊断脚本

新增只读脚本：`scripts/diagnose_alignment_events.py`

脚本行为：

- 默认读取 `LA_DATA_DIR`，未设置时读取 `.learning_agent_data`
- 可用 `--data-dir` 显式指定数据目录或 `sessions/` 目录
- 只扫描 `sessions/*.events.jsonl`，不修改任何事件、unit 或 session 状态
- 输出固定排序 JSON，包含每个事件的 `total_count`、`unique_sessions`、`latest_5_samples`、`emit_source`

## 真实数据统计

执行命令 1：

```bash
LA_DATA_DIR=/Users/roseannk/my-agent/.learning_agent_data python3 scripts/diagnose_alignment_events.py --compact
```

执行命令 2：

```bash
python3 scripts/diagnose_alignment_events.py --data-dir /Users/roseannk/my-agent/.learning_agent_data --compact
```

两次输出一致。关键结果：

| 指标 | 值 |
| --- | --- |
| session event files | 239 |
| parse_errors | 0 |
| `learning_unit.alignment_suggested` | 0 |
| `learning_unit.alignment_skipped` | 0 |
| `learning_unit.alignment_resolved` | 0 |
| `learning_unit.assumption_accepted` | 0 |
| `learning_unit.objective_refined` | 0 |
| `learning_unit.alignment_rate_limited` | 0 |

`learning_unit.created`、`learning_unit.alignment_started`、`learning_unit.first_value_delivered`、`learning_unit.phase_changed`、`learning_unit.stopped` 等学习卷事件在同一批 JSONL 中存在，说明问题不是 event store 整体未写入。

## Verdict

| 事件 | emit source | verdict | 说明 |
| --- | --- | --- | --- |
| `learning_unit.alignment_suggested` | `_apply_alignment_decision` suggested branch | `rate_limited_silent` | emit 路径存在，单测覆盖；历史数据里 suggested 与 rate-limited 都是 0。裂缝 4 之前 suggested 被限流降级时没有事件，真实数据无法区分“未触发 B 档”和“触发后静默降级”。裂缝 4 已补 `alignment_rate_limited`，后续实跑应能观察到 SUGGESTED 或 RATE_LIMITED 二选一。 |
| `learning_unit.alignment_skipped` | `accept_assumption` | `gap_unexposed` | 后端端点存在，前端按钮只在 suggestion bar 出现时可点；真实数据中 suggestion bar 上游事件为 0，因此用户入口实际不可达或极窄。 |
| `learning_unit.alignment_resolved` | `_apply_alignment_decision` user_request branch | `gap_unexposed` | `/align` 只把状态置为 active + user_request，RESOLVED 需要下一轮 chat 消费该状态才会 emit；前端入口同样依赖 suggestion bar。 |
| `learning_unit.assumption_accepted` | `accept_assumption` | `gap_unexposed` | 与 `alignment_skipped` 同源，后端 emit 存在，但真实 UI 触发依赖 suggestion bar。 |
| `learning_unit.objective_refined` | `refine_objective` | `gap_unexposed` | 后端 `/refine-objective` 端点和单测存在，当前前端未暴露改写目标控件。 |

未发现 route bug；本裂缝不修改产品代码。

## 四层职责分类

| 文件 | 层级 | 说明 |
| --- | --- | --- |
| `scripts/diagnose_alignment_events.py` | Infrastructure-adjacent diagnostic script | 离线读取 JSONL 文件并输出诊断摘要，不进入运行时路径 |
| `docs/changes/2026-05-30-phase-1a-hardening-alignment-event-gap-rca.md` | 文档留痕 | 记录真实数据统计、verdict 与后续边界 |

Product / Runtime / Interface 本轮未改。脚本只消费 Product 层 append-only 事件事实源。

## 后续边界

- `gap_unexposed` 不在本裂缝修 UI；入口补全留给 Phase 1B 或独立任务。
- `rate_limited_silent` 已由裂缝 4 的 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 事件收口。
- 裂缝 1 baseline 实跑时，`lm-p1a-ask-does-not-advance-stage` 这类 suggested 输入应验证：同一 case 至少出现 `learning_unit.alignment_suggested` 或 `learning_unit.alignment_rate_limited` 之一。

## 验证

- `python3 -m py_compile scripts/diagnose_alignment_events.py`
- `LA_DATA_DIR=/Users/roseannk/my-agent/.learning_agent_data python3 scripts/diagnose_alignment_events.py --compact`
- `python3 scripts/diagnose_alignment_events.py --data-dir /Users/roseannk/my-agent/.learning_agent_data --compact`
