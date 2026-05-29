# 对齐分类器真实 E2E 数据集 — 待办清单（2026-05-28 起）

配套 [alignment-classifier-real-e2e-dataset-done-2026-05-28.md](alignment-classifier-real-e2e-dataset-done-2026-05-28.md)。

分四档：**P0 跑 suite 的前置工作** / **P1 首次跑通后的回填** / **P2 可选优化** / **P3 决策记录**。

---

## P0 · 跑 suite 的前置工作（不补就跑不全 / 跑不准）

数据集已写，但当前 [tests/e2e/frontend_real_runner_learning.py](../../tests/e2e/frontend_real_runner_learning.py) 缺两个能力，会让 5/7 个 case 的关键断言无法验证。

### 1. runner 抓 `GET /learning-units/{id}` payload
- **影响 case**：`high-cost-popup` / `low-cost-suggestion` / `continue-learning` / `clarification-cap` / `popup-short-circuits` / `fail-open`（6/7）
- **断言点**：`learning_unit_alignment_state_after_turn` / `learning_unit_last_candidates_min_count` / `learning_unit_suggestion_count_after_turn` / `learning_unit_clarification_count_after_turn`
- **改法**：每个 turn 跑完后，若 session 关联了 learning_unit_id，再调一次 `GET /learning-units/{id}`，落 `cases/<case_id>/learning_unit.json`（如多轮，落 `learning_unit.turn-N.json`）
- **锚点**：[learning_agent/web/web_server.py:_learning_unit_payload](../../learning_agent/web/web_server.py) 已经把 `alignment_state` / `last_candidates` / `suggestion_count` / `clarification_count` 都 expose 出来，runner 只需要拉一下

### 2. runner 支持 `setup.env_overrides`
- **影响 case**：`lm-align-fail-open-when-model-missing`（1/7）
- **断言点**：注入 `LA_ALIGNMENT_CLASSIFIER_MODEL=qwen-this-model-does-not-exist-xxxx` 后，STUDY 静默回落
- **改法**：每个 case 起 dev server / 复用 dev server 时，把 `setup.env_overrides` 的 K/V 注入子进程环境变量；case 结束后还原。**或者**简单做法 — 把 fail-open case 标 `manual_only: true`，让用户手工 export 后跑。
- **取舍**：N=1 项目，先简单做法（标 manual_only）也 OK，后面真需要再正式做。

### 3. 决定要不要做 SSE 流式抓取
- 当前 runner 用非流式 `POST /sessions/{id}/chat`，只拿 `{session_id, content}`。
- popup case 的 metadata（`alignment_popup` / `candidates` / `placeholder_text`）目前在**非流式响应里到底是不是返回**？这点需要先在 dev server 上 curl 一下确认（猜测：非流式 endpoint 只返回 content，metadata 走 SSE）。如果是后者，断言 `assistant_metadata_should_include` 就无法在非流式 runner 下验证 — 要么补 SSE 抓取，要么从 session.entries 末尾那条 assistant 消息的 metadata 取。
- **优先做的探查**：起 dev server，手工 POST 一个高代价输入，看返回的 JSON 结构。3 分钟事。

---

## P1 · 首次跑通后的回填（每次跑都要更新 baseline）

baseline 现在 `status: not_yet_executed`，跑通一次后回填：

- `status` → `last_executed_pass` / `last_executed_with_known_gaps`
- `quality_targets` 里数字基线（context-aware / low-cost-suggestion 通过率）
- `failure_budgets` 里观察到的实际计数
- `implementation_gaps_to_track_on_first_run` 里实际发现的缺口归档

跑完冒烟还要手工复现一次 sess-27bf0af2 的两条 case：
1. "我准备做一个新的项目,帮我梳理目标和范围" → 应该弹窗
2. 让模型答一轮后说"继续学" → 应该 STUDY 正常推进、`_maybe_fire_concept_extraction` 触发

第二条尤其关键 — 它是 ASK 重构的 motivation case。

---

## P2 · 可选优化（实际跑出问题再做）

### 数据集覆盖扩展
- **suggestion cap 用例**：现在 `clarification-cap` 覆盖了 §9.3 #1，但 §9.3 #2（`suggestion_count >= MAX_SUGGESTIONS_PER_UNIT` 时 suggested → none）和 §9.3 #3（cooldown）没有真实环境用例 — 单测里有，真实环境暂时不补，等真实使用中观察到 nag 行为再补。
- **多卷干扰**：现在每个 case `session_per_case: true`，没验证"两张卷在同一 session 里互不干扰"。这是 single-session 数据集的范畴，暂时不混进来。
- **divergence_cost 边界**：现在只测了 `low` / `high`，没有专门的"模型瞎返回 medium / null" 真实用例（单测有）。LLM 真实环境概率低，暂不补。

### 评估口径升级
- popup case 的 `if_popup_then_*` 是兜底句法，连续跑 N 次取**触发率**可能更稳；但要等 runner 支持 N 次重跑同 case 取 stats。
- 候选 schema 校验现在只看 `objective` + `first_step` 非空，可以加"first_step 不能是 `objective` 的子串"等启发式 — 但真出问题再加，先看 LLM 实际输出质量。

---

## P3 · 决策记录（避免下次又问一遍）

- **数据集只构造，不跑** — 用户每次都明确要求，避免误以为"写完 = 跑通"。
- **baseline 不放 `allowed_failures` 兜底** — 对齐分类器是一进门就会被触发的关键路径，ASK 回退 / popup 不短路 / fail-open 失败都是硬故障，不允许长期白名单化。首次执行发现的缺口记为 `implementation_gap`，**补 runner 而不是降低验收**。
- **fail-open 用假模型名而不是断网** — 干净、可复制、不动 mock infra。
- **不在数据集里塞 DOM 断言** — modal 是否真的开起来属于前端 e2e 范畴，本数据集只验后端 metadata 提供了 `alignment_popup` 与 `candidates`，DOM 留给前端 e2e 或人工截图。
- **保留 `ASK_PROFILE` / `AgentMode.ASK` 不动** — CLI `/ask` 还在用，旧 session metadata 也有引用。本数据集只锁"学习卷路径不再退到 ASK"，物理删除等后续。
