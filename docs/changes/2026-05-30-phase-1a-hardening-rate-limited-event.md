# 2026-05-30 Phase 1A hardening: alignment rate-limited event

## 运行环境 / 时间戳

- Worktree: `/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening`
- Branch: `feature/learning-phase-1a-hardening`
- 时间: `2026-05-30T17:14:04Z`
- Provider: 未调用外部 LLM/provider；本轮只改 Product 层 policy / event / replay 契约
- Commit hash: 本文件随同本裂缝修复提交落盘，提交后以 `git log -1 --format=%h` 为准

## 原始目标

doc1 裂缝 4 要求把 `_apply_rate_limits` 的 suggested 静默降级路径显性化，新增唯一事件：

`LEARNING_UNIT_ALIGNMENT_RATE_LIMITED = "learning_unit.alignment_rate_limited"`

该事件只表达 `suggested -> none` 的限流降级，不覆盖 active alignment 的 `clarification_count >= 1` 短路。

## 方案选择

采用 doc1 推荐的方案 A。

- `alignment_policy.classify_alignment(...) -> (AlignmentDecision, AlignmentRateLimitInfo | None)`
- `should_run_alignment(...)` 保持旧返回值，只返回 `AlignmentDecision`，避免破坏既有调用方
- `LearningAgentSystem._prepare_learning_unit_turn` 使用 `classify_alignment`
- `LearningAgentSystem._apply_alignment_decision` 统一 emit `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED`

## 事件 schema 与互斥语义

事件 payload 使用 `_emit_unit_event` 的学习卷公共字段，并追加：

| 字段 | 取值 |
| --- | --- |
| `original_mode` | `suggested` |
| `downgraded_to` | `none` |
| `rate_limit_rule` | `max_suggestions_per_unit` 或 `nag_cooldown` |
| `suggestion_count` | 降级发生时的实际值 |
| `max_suggestions` | 当前实现为 `2` |
| `nag_cooldown_remaining` | 降级发生时的实际值 |
| `trigger` | `classifier_suggested` |

同一 turn 内 `learning_unit.alignment_rate_limited` 与 `learning_unit.alignment_suggested` 互斥：限流后最终 `decision.mode == "none"`，不再发 suggested。

同时命中 `suggestion_count >= max` 与 `nag_cooldown_remaining > 0` 时，按代码顺序优先记录 `max_suggestions_per_unit`。

## visibility / replay 决策

- visibility: `agent`，沿用 `_emit_unit_event` 默认写入 `sessions/<id>.events.jsonl`
- replay: 显式 no-op。`session_projection._apply_event` 新增 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 分支，只保留时间轴事件，不改写 snapshot / messages / mode_metadata

## 四层职责分类

| 文件 | 层级 | 说明 |
| --- | --- | --- |
| `learning_agent/learning_agent/alignment_policy.py` | Product/Application | 输出限流降级的结构化原因 |
| `learning_agent/learning_agent/main.py` | Product/Application | 在学习卷调度链内追加 JSONL 产品事件 |
| `learning_agent/learning_agent/session_events.py` | Product/Application | 集中注册事件常量 |
| `learning_agent/learning_agent/session_projection.py` | Product/Application | replay 显式 no-op，防止 timeline-only 事件污染派生视图 |
| `tests/test_alignment_policy.py` | Product/Application 测试 | 覆盖两条限流规则的信息输出 |
| `tests/test_learning_unit_events.py` | Product/Application 测试 | 覆盖事件 emit 与 suggested 互斥 |
| `tests/test_session_projection.py` | Product/Application 测试 | 覆盖 replay no-op |

Runtime / Interface / Infrastructure 本轮未改。

## 验证

- `python3 -m py_compile learning_agent/learning_agent/alignment_policy.py learning_agent/learning_agent/main.py learning_agent/learning_agent/session_events.py learning_agent/learning_agent/session_projection.py`
- `python3 -m pytest tests/test_alignment_policy.py tests/test_learning_unit_events.py tests/test_session_projection.py tests/test_mode_layering.py -q`
- 结果：`105 passed`

## 回滚路径

- 回滚本提交会删除事件常量、策略返回结构、emit 与 replay no-op 分支。
- 已写入 JSONL 的历史 `learning_unit.alignment_rate_limited` 事件仍是 append-only 事实；回滚后旧 replay 逻辑会把未知事件静默忽略，不应修改既有 JSONL。
