# 研学模式 Phase 1B close：OrientationContext 入局情境

> 日期：2026-05-31
>
> 类型：新增需求功能 / 关闭记录
>
> 范围：Phase 1B 入局情境生成、双事件、独立 provider 通道、SSE/API/前端展示、真实 E2E baseline

---

## 背景

Phase 1B 的目标是在学习卷 `forge_stage=entry` 的首轮研习里生成一次 `OrientationContext`，让用户进入 collision 前先看到可立即回应的入局情境。该阶段受 doc2/doc3/doc4 共同约束：

- orientation 必须走独立 provider 通道，不进入 `TurnExecutionProfile.system_prompt`。
- 事件必须拆为 `learning_unit.orientation_generated` 与 `learning_unit.orientation_fallback_used` 双事件，禁止只靠 `source` 字段分叉。
- `forge_policy.maybe_advance_forge_stage` 仍是唯一 forge_stage 推进入口。
- 旧 1A unit 兼容，不因缺少 `orientation_context` 被阻塞。

---

## 本次落地

- `LearningUnit` 新增 `orientation_context`，并新增 `OrientationContext` 模型。
- 新增 `learning_agent/learning_agent/orientation_policy.py`，完成独立 provider 调用、6 秒硬超时、静态 fallback、digest 与双事件 emit。
- `forge_policy.maybe_advance_forge_stage` 在 `entry → collision` 前增加 `orientation_context is not None` gate，并保留唯一推进入口。
- `main.py` 在 Product/Application 层接入 orientation 生成，不写入主回合 prompt/profile。
- `GET /learning-units/{id}` 返回 `orientation_context`；SSE 只透传 `orientation_context_present` 与 `hook_kind` 轻量元数据。
- 前端学习卷卡片新增 `[data-testid="orientation-context"]` 容器，并在收到 `orientation_context_present=true` 后主动刷新一次 unit snapshot。
- 新增 Phase 1B real dataset 与 baseline：`tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`。

---

## 验收结果

- 单元测试：153 passed。
- 前端静态测试：14 passed。
- Phase 1B 真实 E2E：5/5 pass，run_id=`2026-05-31T083023Z_learning-mode-phase-1b-orientation-context-real`。
- Phase 1A 回归：4/4 pass，run_id=`2026-05-31T083209Z_learning-mode-phase-1a-forge-state-real`。

证据入口：

- `docs/output/acceptance-report-learning-mode-phase-1b-2026-05-31.md`
- `tests/e2e/artifacts/2026-05-31/learning-mode-phase-1b-orientation-context-real-v3/summary.json`
- `tests/e2e/artifacts/2026-05-31/learning-mode-phase-1a-forge-state-real-v4/summary.json`

---

## 例外说明

doc4 原关闭条件要求前端截图。2026-05-31 用户明确指示“`不用截图了，补齐验收材料就行了`”，本 close 以 real E2E artifact、baseline、事件 JSONL、API payload 与静态前端测试替代截图证据。

---

## 后续

Phase 1B 已关闭。后续若进入 Phase 1C，必须继续沿用 `forge_policy.maybe_advance_forge_stage` 唯一推进入口，不得新增第二入口。
