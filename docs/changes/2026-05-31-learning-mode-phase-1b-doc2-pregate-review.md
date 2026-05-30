# 研学模式 Phase 1B doc2 预闸评审收口

> 日期：2026-05-31
>
> 类型：设计文档收口
>
> 范围：`docs/design/design-learning-mode-phase-1b-1e-skeleton.md`

---

## 背景

进入 doc2 评审时，远端 `main` 尚未包含 Phase 1A 硬化分支，因此本次只能做 doc2 预闸评审，不标记 doc2 正式通过。评审目标是先消除骨架文档与 doc1/doc3/doc4 之间会影响后续 codex 执行的口径漂移。

---

## 收口内容

- 将 1B-1E real dataset / baseline 的门禁拆成「开工前锁定路径」与「关闭前实跑生成」，避免 1B 尚未实现却要求 baseline 实跑的死锁。
- 将 1B orientation 事件注册锚点改为 doc1 新增的 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 之后，和 doc3/doc4 的符号锚点一致。
- 收紧 orientation 独立 provider 通道约束：禁止进入 `TurnExecutionProfile.system_prompt` / `system_prompt_addendum` / 主对话 history。
- 明确 1C 之后可以在 `forge_policy.maybe_advance_forge_stage` 唯一入口内部做阶段分发，但外部不得新增第二个 forge 推进入口。
- 对齐 SSE 边界：1B 只允许透传轻量 presence / hook 元数据，不把完整 `OrientationContext` 结构体塞进 SSE。
- 对齐 1B replay 口径：`ORIENTATION_GENERATED` / `ORIENTATION_FALLBACK_USED` 为 AGENT 可观测事件，不参与 replay。
- 明确 active alignment C 档期间 forge / temperature 均不得被触动；若 1E 要改口，必须由 1E design 显式翻案并给出边界。

---

## 当前状态

本次变更不进入 Phase 1B 实现，不创建 1B 分支。正式顺序仍是：

`doc1 合入 main -> doc2 正式评审通过 -> doc3 正式评审通过 -> doc4 开工`

