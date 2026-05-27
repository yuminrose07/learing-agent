# 对齐分类器真实 E2E 数据集

> 日期：2026-05-28
>
> 类型：新增测试数据集
>
> 范围：`tests/e2e/real_datasets/alignment-classifier-popup-real.json`、`tests/e2e/real_baselines/alignment-classifier-popup-real.baseline.json`

---

## 背景

ASK 重构已落地：`alignment_policy.should_run_alignment` 的规则启发式被替换为 LLM 驱动的 `AlignmentClassifier`（cheap-tier，qwen-turbo），输出仍是三档 `none / suggested / active`；其中 `active` 不再切 `AgentMode.ASK`，改走 `PreparedSessionTurn.alignment_popup` 短路 —— `stream_session_chat` 看到这个标记就只写一条确定性 placeholder assistant 消息，然后 return，不进 `agent_loop`、不调任何工具。

单测层已经把契约锁住：`tests/test_alignment_classifier.py` 覆盖档位映射 / fail-open / 限流集成，`tests/test_mode_layering.py` / `tests/test_chat_study_separation.py` 锁住路由和分离不变式。但单测全是 mock provider，真实环境下还有三类风险只能跑出来：

- LLM 在真实输入上**会不会**按预期分档（"继续学" 在上下文里被识别成 none，而不是 missing_learnable_target）；
- 短路路径在非流式 `/sessions/{id}/chat` 下，`response.content` 是否真的等于 placeholder（前端 modal 才能拿到稳定文本）；
- fail-open 在 provider 真的报错时是否静默回落，而不是抛 500。

用户明确要求：构造测试集，不运行。

---

## 变更

- 新增 `tests/e2e/real_datasets/alignment-classifier-popup-real.json`。
- 新增 `tests/e2e/real_baselines/alignment-classifier-popup-real.baseline.json`。

数据集 7 个用例（执行顺序由 `session_plan.ordered_case_ids` 固定）：

- `lm-align-clear-input-no-popup` — 清晰具体输入直接 STUDY，不弹 popup。
- `lm-align-multi-candidate-high-cost-popup` — 多候选高代价，弹 modal，不进 agent_loop。
- `lm-align-multi-candidate-low-cost-suggestion` — 多候选低代价走 suggestion bar，不弹 modal。
- `lm-align-continue-learning-context-aware` — 两轮："讲讲 attention" → "继续学"。回归 sess-27bf0af2：第二轮 LLM 看到上下文不应再弹窗。
- `lm-align-clarification-cap-respected` — 两轮模糊输入："教" → "讲讲"。§9.3 #1 单卷启动期最多 1 次澄清，第二次必须不再弹。
- `lm-align-popup-short-circuits-tool-calls` — popup 时 `response.content` 应等于 placeholder，会话最后一条 assistant 消息长度 ≤ 200，metadata 含 `alignment_popup`/`candidates`/`placeholder_text`。
- `lm-align-fail-open-when-model-missing` — 通过 `setup.env_overrides.LA_ALIGNMENT_CLASSIFIER_MODEL` 注入不存在的模型名，预期 STUDY 照常运行，不挂、不弹窗。

baseline 把以下三类放进 hard targets（不允许长期白名单化）：

- `ask_mode_overlay_count = 0` —— 任何路径切回 `AgentMode.ASK` 都是硬故障；
- `popup_short_circuits_tool_calls_rate = 1.0` —— popup 时绝不能进 agent_loop；
- `fail_open_silent_when_model_missing_rate = 1.0` —— provider 挂了不能 500。

---

## 注意

当前 `tests/e2e/frontend_real_runner_learning.py` 用非流式 `POST /sessions/{id}/chat`，只采集 `response.content` 和 session events，**没有**两块能力：

- `GET /learning-units/{id}` —— 不抓取就无法验证 `last_candidates` / `alignment_state` / `suggestion_count` 等卷态字段。
- `setup.env_overrides` —— 不支持就跑不了 `lm-align-fail-open-when-model-missing`（只能手工 export 后跑）。

这两个缺口已经写进 dataset 的 `runner_requirements.compatible_runner_note` 和 baseline 的 `implementation_gaps_to_track_on_first_run`。首次执行前要决定是补 runner 还是分阶段跑（前者更彻底）。

另外，前端 modal 是否成功打开属于 DOM 层断言，本数据集只能验证后端 metadata 提供了 `alignment_popup` / `candidates`，DOM 验证留给前端 e2e 或人工截图。

---

## 后续

- 补 `frontend_real_runner_learning.py` 的 `GET /learning-units/{id}` 抓取与 `setup.env_overrides` 支持，让本 suite 完整可跑。
- 首次跑通后回填 baseline 的 `status` / `quality_targets` / `failure_budgets`，并将真实 implementation_gap 记录归档。
- 跑通后手工复现一次 sess-27bf0af2 的两条关键 case 作为冒烟，留作后续回归基线。
