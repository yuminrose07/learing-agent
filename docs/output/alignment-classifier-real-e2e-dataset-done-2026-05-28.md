# 对齐分类器真实 E2E 数据集 — 已完成清单（2026-05-28）

ASK 重构（[LLM 替规则 + popup 短路](../changes/2026-05-28-alignment-classifier-llm-driven-and-popup.md) — 已有独立 change doc，此处不重复）的真实环境验收数据集落地。本次只构造数据集，不运行。

---

## 交付物

| 文件 | 类型 | 作用 |
|---|---|---|
| [tests/e2e/real_datasets/alignment-classifier-popup-real.json](../../tests/e2e/real_datasets/alignment-classifier-popup-real.json) | 新增 | 7 个用例的真实 E2E 数据集 |
| [tests/e2e/real_baselines/alignment-classifier-popup-real.baseline.json](../../tests/e2e/real_baselines/alignment-classifier-popup-real.baseline.json) | 新增 | 首次跑前的验收基线（`status: not_yet_executed`） |
| [docs/changes/2026-05-28-alignment-classifier-real-e2e-dataset.md](../changes/2026-05-28-alignment-classifier-real-e2e-dataset.md) | 新增 | 变更说明（背景 / 变更 / 注意 / 后续） |

---

## 数据集 7 个用例

执行顺序由 `session_plan.ordered_case_ids` 固定。

| # | case_id | 验收点 | severity |
|---|---|---|---|
| 1 | `lm-align-clear-input-no-popup` | 清晰输入 → STUDY，不弹 popup，无 `alignment_started` 事件 | high |
| 2 | `lm-align-multi-candidate-high-cost-popup` | 多候选高代价 → 弹 modal，metadata 含 `alignment_popup` / `candidates` / `divergence_cost=high`，**不进 agent_loop** | high |
| 3 | `lm-align-multi-candidate-low-cost-suggestion` | 多候选低代价 → suggestion bar，**不弹** modal，`suggestion_count` 自增，STUDY 正常推进 | medium |
| 4 | `lm-align-continue-learning-context-aware` | 两轮："讲讲 attention" → "继续学"。第二轮 LLM 看到上下文应识别为 none — 回归 sess-27bf0af2 | high |
| 5 | `lm-align-clarification-cap-respected` | 两轮模糊："教" → "讲讲"。§9.3 #1 单卷启动期最多 1 次澄清 | high |
| 6 | `lm-align-popup-short-circuits-tool-calls` | popup 时 `response.content == placeholder_text`，长度 ≤ 200，无 tool_calls | medium |
| 7 | `lm-align-fail-open-when-model-missing` | `env_overrides.LA_ALIGNMENT_CLASSIFIER_MODEL` 注入不存在模型 → STUDY 静默回落，不挂不弹窗 | high |

---

## Baseline 的硬指标（不允许长期白名单化）

```
ask_mode_overlay_count                          = 0
popup_short_circuits_tool_calls_rate            = 1.0
fail_open_silent_when_model_missing_rate        = 1.0
clear_input_does_not_trigger_popup_rate         = 1.0
high_cost_popup_triggers_alignment_event_rate   = 1.0
clarification_cap_violation_count               = 0
```

这三类是 ASK 重构的契约红线：

- 任何路径把 `effective_mode` 切回 `AgentMode.ASK` → 硬故障
- popup case 进了 `agent_loop` / 跑了 tool_calls → 硬故障
- provider 真挂了不静默回落（返回 500 或抛异常给用户）→ 硬故障

---

## 前置工作（已完成、有独立 change doc，此处只索引）

- ASK 重构主干 — [docs/changes/2026-05-28-alignment-classifier-llm-driven-and-popup.md](../changes/2026-05-28-alignment-classifier-llm-driven-and-popup.md)
- 单测全套 — [tests/test_alignment_classifier.py](../../tests/test_alignment_classifier.py)（新增）+ [tests/test_alignment_policy.py](../../tests/test_alignment_policy.py)（修剪 schema/限流）+ [tests/test_mode_layering.py](../../tests/test_mode_layering.py)（active 不再 == ASK）+ [tests/test_chat_study_separation.py](../../tests/test_chat_study_separation.py)（attach fake classifier）

单测层 503 passed（含本次新增）。

---

## 关键决策记录

- **数据集只构造、不运行** — 用户明确要求。首次跑前需要补 runner（见 todo P0）。
- **baseline 把 `ask_mode_overlay_count = 0` 写死** — ASK 模式还没物理删除（CLI `/ask` 还在用，旧 session metadata 也有引用），但**学习卷路径**绝不能再退到 ASK；这条硬指标就是把"不退回"锁死。
- **`lm-align-popup-short-circuits-tool-calls` 用兜底口径** — 因为 LLM 真实输出存在概率，不一定每次都弹窗，所以断言是 `if_popup_then_*` 而不是无条件断言。这是真实数据集与 mock 单测的本质差别。
- **fail-open case 用 `env_overrides` 而不是停服务** — 这是验证 fail-open 最干净的方式（provider URL 还在、模型名假），不需要动 mock infra。
