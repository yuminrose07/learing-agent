# 研学模式 Phase 1B doc2 正式评审通过

> 日期：2026-05-31
>
> 类型：设计文档评审通过
>
> 范围：`docs/design/design-learning-mode-phase-1b-1e-skeleton.md`

---

## 前置 gate

Phase 1A 硬化已合入 `main`，`main` 当前包含：

- `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 事件常量与 emit 路径
- absorbing -> STUDY 路由硬化
- forge_state 真实数据集 runner 与 baseline 实跑回填
- baseline 回填 commit message 字面包含 `baseline_version=2026-05-30-v1`

因此 doc2 的前置 gate 已满足，可以从「预闸收口」推进为「正式评审通过」。

---

## 评审结论

doc2 作为 Phase 1B-1E 骨架文档，只保留边界与 invariants，不直接驱动实现。正式通过口径如下：

- 1B-1E 只覆盖「理解铸造场」中部四态，不写研学门厅 / 博物馆 / 镜子底座。
- 1B orientation 采用双事件：`LEARNING_UNIT_ORIENTATION_GENERATED` 与 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`。
- orientation 走独立 provider 通道，禁止进入 `TurnExecutionProfile.system_prompt` / `system_prompt_addendum` / 主对话 history。
- `forge_policy.maybe_advance_forge_stage` 仍是全项目唯一 forge_stage 推进入口。
- real dataset / baseline 开工前锁定路径，关闭前必须实跑生成。
- 子阶段必须按 1B -> 1C -> 1D -> 1E 顺序推进，每个子阶段独立 design + checklist + 单测 + real dataset + baseline。

---

## 后续 gate

下一步进入 doc3（`docs/design/design-learning-mode-phase-1b-orientation-context.md`）正式评审。doc3 通过前，不启动 doc4 checklist 实施。
