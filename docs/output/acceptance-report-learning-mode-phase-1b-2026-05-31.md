# Phase 1B OrientationContext 验收报告

> 日期：2026-05-31
>
> 范围：研学模式 Phase 1B 入局情境生成
>
> 结论：通过

---

## 验收摘要

Phase 1B 已完成并通过验收。核心能力包括：

- 新学习卷首轮 STUDY absorbing 会生成 `OrientationContext`。
- LLM 成功路径 emit `learning_unit.orientation_generated`。
- provider 失败/超时路径使用静态 fallback，并 emit `learning_unit.orientation_fallback_used`。
- `orientation_context` 不进入主回合 `TurnExecutionProfile.system_prompt`。
- `forge_policy.maybe_advance_forge_stage` 仍是唯一 forge_stage 推进入口。
- ASK 对齐轮不生成 orientation，也不推进 forge_stage。
- 前端卡片支持 orientation 容器和 SSE presence 后主动刷新。

---

## Baseline

Phase 1B baseline：

- 文件：`tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`
- status：`pass`
- run_id：`2026-05-31T083023Z_learning-mode-phase-1b-orientation-context-real`
- suite：`learning-mode-phase-1b-orientation-context-real`
- 结果：5/5 pass

关键指标：

- `learning_unit_creation_rate=1.0`
- `non_empty_response_rate=1.0`
- `orientation_context_rate=0.8`（ASK 对齐 case 预期无 orientation）
- `orientation_context_present_metadata_rate=0.8`
- `orientation_digest_consistency_rate=1.0`
- `ask_alignment_stage_advance_count=0`
- `orientation_generated_event_count=3`
- `orientation_fallback_event_count=1`

---

## 真实 E2E 证据

Phase 1B：

- summary：`tests/e2e/artifacts/2026-05-31/learning-mode-phase-1b-orientation-context-real-v3/summary.json`
- artifacts：同目录下共 5 个 case，每个 case 保留 `request.json` / `response.json` / `session.json` / `learning_unit.json` / `events.jsonl`

通过 case：

- `lm-p1b-orientation-generated-on-first-turn`
- `lm-p1b-no-attempt-blocks-stage-advance`
- `lm-p1b-orientation-fallback-on-llm-failure`
- `lm-p1b-ask-alignment-does-not-trigger-orientation`
- `lm-p1b-orientation-defer-after-active-alignment`

事件序列样例（`lm-p1b-orientation-generated-on-first-turn`）：

- `learning_unit.created`
- `learning_unit.orientation_generated`
- `message_end`
- `learning_unit.first_value_delivered`
- `learning_unit.forge_stage_changed`

Phase 1A 回归：

- summary：`tests/e2e/artifacts/2026-05-31/learning-mode-phase-1a-forge-state-real-v4/summary.json`
- run_id：`2026-05-31T083209Z_learning-mode-phase-1a-forge-state-real`
- 结果：4/4 pass

---

## 测试结果

Python targeted tests：

```text
pytest -q tests/test_orientation_policy.py tests/test_forge_policy.py tests/test_learning_unit_events.py tests/test_mode_layering.py tests/test_learning_unit_api.py tests/test_learning_unit_store.py
153 passed
```

Frontend static tests：

```text
node --test tests/test_web_static_app.js
14 passed
```

Real E2E：

```text
python3 tests/e2e/real_runner.py --dataset tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json --output tests/e2e/artifacts/2026-05-31/learning-mode-phase-1b-orientation-context-real-v3 --base-url http://127.0.0.1:8012 --data-dir .learning_agent_data_phase1b_baseline2
Summary: 5/5 passed; status=pass
```

1A regression：

```text
python3 tests/e2e/real_runner.py --dataset tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json --output tests/e2e/artifacts/2026-05-31/learning-mode-phase-1a-forge-state-real-v4 --base-url http://127.0.0.1:8012 --data-dir .learning_agent_data_phase1b_baseline2
Summary: 4/4 passed; status=pass
```

---

## 截图说明

doc4 原要求验收报告包含前端截图。2026-05-31 用户明确指示“`不用截图了，补齐验收材料就行了`”，因此本报告不附截图。前端证据由静态测试、SSE metadata、REST payload 与 real E2E artifact 替代。

---

## 关闭判定

Phase 1B 可关闭：

- 双事件 schema 已落地。
- 独立 provider 通道已落地。
- `orientation_context` baseline 已实跑回填。
- 1A 回归通过。
- 文档索引、close change、验收报告已补齐。
