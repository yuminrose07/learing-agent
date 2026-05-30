# 2026-05-30 Phase 1A hardening: absorbing 路由到 STUDY

## 运行环境 / 时间戳

- Worktree: `/Users/roseannk/my-agent/.claude/worktrees/learning-phase-1a-hardening`
- Branch: `feature/learning-phase-1a-hardening`
- 时间: `2026-05-30T17:02:09Z`
- Provider: 未调用外部 LLM/provider；本轮只做 Product 层调度与单元测试验证
- Commit hash: 本文件随同本裂缝修复提交落盘，提交后以 `git log -1 --format=%h` 为准

## 原始目标

doc1 裂缝 2 要求核查并收口：absorbing 学习卷不应继续走 `CHAT_PROFILE`，非 active 对齐轮必须进入 `STUDY_PROFILE`；active alignment popup 仍短路为 `ASK`，outputting 仍走 `TEACH`。

## 修复效果

- `_prepare_learning_unit_turn` 中 `unit.phase == "absorbing"` 且 `decision.mode != "active"` 时，`effective_mode` 从 `AgentMode.CHAT` 改为 `AgentMode.STUDY`。
- absorbing 首轮 opening addendum 的注入条件从 `CHAT` 改为 `STUDY`，避免学习卷首轮 prompt 仍贴在 chat profile 上。
- `_maybe_emit_first_value` 与 `_maybe_fire_concept_extraction` 的守卫从 `CHAT` 改为 `STUDY`，与 `forge_stage` 推进守卫保持一致。
- ASK 对齐轮仍不派生 `learning_action`，也不会触发 first value / forge stage 推进。

## 四层职责分类

| 文件 | 层级 | 说明 |
| --- | --- | --- |
| `learning_agent/learning_agent/main.py` | Product/Application | 学习卷 mode 编排、opening addendum 注入、first value 与概念抽取的 Product 侧守卫 |
| `tests/test_mode_layering.py` | Product/Application 测试 | 覆盖 absorbing→STUDY、active→ASK、outputting→TEACH、非学习卷 fallback→CHAT |
| `tests/test_learning_unit_events.py` | Product/Application 测试 | 覆盖 STUDY absorbing 首条有效回答触发 `learning_unit.first_value_delivered` |

Runtime / Interface / Infrastructure 本轮未改。

## 核查矩阵

| 入口 | 期望 effective_mode | 当前验证 |
| --- | --- | --- |
| absorbing + decision=none | `STUDY` | `tests/test_mode_layering.py::test_prepare_session_turn_absorbing_phase_yields_study_mode` |
| absorbing + decision=suggested | `STUDY` | `tests/test_mode_layering.py::test_first_absorbing_turn_with_suggested_appends_narrowing_block` |
| absorbing + decision=active | `ASK` | `tests/test_mode_layering.py::test_prepare_session_turn_absorbing_with_vague_input_yields_ask` |
| outputting | `TEACH` | `tests/test_mode_layering.py::test_prepare_session_turn_outputting_phase_yields_teach_mode` |
| 非学习卷 session | `CHAT` | `tests/test_mode_layering.py::test_prepare_session_turn_without_unit_falls_back_to_legacy_path` |

真实 E2E evidence（4 个 `learning-mode-phase-1a-forge-state-real` case 的 `session.json` / events）将在裂缝 1 baseline 实跑提交中回填。本提交不伪造真实 evidence。

## 验证

- `python3 -m pytest tests/test_forge_policy.py tests/test_learning_unit_store.py tests/test_learning_unit_events.py tests/test_mode_layering.py tests/test_learning_unit_api.py -q`
- 结果：`129 passed`

## 回滚路径

- 回滚本提交即可恢复 absorbing 使用 `CHAT` 的旧路由。
- 若回滚后继续推进 Phase 1B，必须同步回滚依赖 STUDY absorbing 的 1B 文档 gate；否则 `forge_stage_changed` 与 `FIRST_VALUE_DELIVERED` 会再次脱节。
