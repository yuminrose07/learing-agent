# Phase 1A 硬化清单（4 条裂缝补齐）

> 日期: 2026-05-30
> 类型: 执行清单 / 补地基（非新功能）
> 范围: Phase 1A 已发能力的真实可验证性收尾，不引入新阶段、不重写策略
> 优先级: P0 阻塞 Phase 1B 入局

## 0. 背景与边界（只引用，不重写）

- 本文档解决 Phase 1A 留下的 4 条裂缝，不解释 Phase 1A 设计本身，请见 `docs/design/design-learning-mode-phase-1a-forge-state-skeleton.md`（已存在）。
- 总盘子与子阶段切分见 `docs/output/learning-mode-development-plan-2026-05-27.md`（已存在）。
- AGENTS.md 中的 invariants 全部生效（append-only / 四层职责 / N=1 / 学习卷主链不变 / 共享工作树 git 安全），不再重述。
- 本文档是给 codex 的可执行 checklist，不是设计文档。任何"为什么这样设计"请回原 Phase 1A 设计。
- **本文档为 LEARNING_UNIT_ALIGNMENT_RATE_LIMITED 事件的唯一事实源**，该事件的 schema / emit 时机 / visibility 决策均由本文档 §5 定义。

## 1. 通用约束

1. 每条裂缝的修复 MUST 落一份留痕：`docs/changes/2026-05-30-phase-1a-hardening-<crack-id>.md`，格式参照 `docs/changes/2026-05-21-append-only-jsonl-rule.md`。
   - 留痕文件必须填写以下章节：运行环境/时间戳、commit hash、provider 版本、原始目标与修复效果、本次新增/修改代码的四层职责分类（Product/Runtime/Interface/Infrastructure）、回滚路径。
2. 任何事件新增 MUST 遵守 append-only（写入 `sessions/<id>.events.jsonl`）、MUST 在 `learning_agent/learning_agent/session_events.py` 集中注册、MUST 写 unit 单测覆盖序列化。
3. 不动 Phase 1A 已发事件的 schema（`LEARNING_UNIT_*` 现有 14 个枚举的 payload key 一律不改名、不删字段；只允许往里加 optional）。
4. 每条裂缝独立可提交、可回滚；提交粒度 = 一条裂缝一个提交（如某条内部需要拆，子提交也必须各自能跑通测试）。
5. 提交规范：中文 commit message，`git add` 点名文件，禁止整树破坏性操作。
6. 优先级：四条裂缝全部 P0，顺序建议 裂缝 1 → 裂缝 2 → 裂缝 3 → 裂缝 4（1/2 是核查，3/4 是新增观测）。

---

## 2. 裂缝 1：1A baseline 实跑验证

### 2.1 现状

- dataset 已构造：`tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json`（4 cases，dataset_version 2026-05-27-v1）。
- baseline 已构造但 **未实跑**：`tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` 第 5 行 `"status": "not_yet_executed"` —— 即 critic 指出的"所谓 8/8 pass 未验证"。
- runner 路径统一走 `tests/e2e/real_runner.py`（CLI 见 1717-1764 行）。

### 2.2 根因调查步骤

跑实跑前 codex 先确认下列 4 件事，任一项不满足必须先修，否则跑出来的证据不可信：

```bash
# 1) 数据集 schema dry run
python tests/e2e/real_runner.py --dataset tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json \
  --output /tmp/_dry --dry-run

# 2) dev server 必须真实可达（dataset 要求 requires_real_web_server=true）
curl -fsS http://localhost:8000/health

# 3) LA_DATA_DIR 与 dev server 一致，否则 unresolved_failures.jsonl 过滤会失败
echo $LA_DATA_DIR

# 4) 当前分支已包含 Phase 1A 全部代码（forge_policy / temperature_state / forge_stage 字段）
grep -n "forge_stage" learning_agent/ai/learning_unit.py | head -3
grep -n "FORGE_STAGE_CHANGED" learning_agent/learning_agent/session_events.py
```

### 2.3 修复目标

- baseline 文件 `status` 由 `not_yet_executed` 翻新为 `pass` 或 `fail`（不允许保留中间态）。
- 4 个 case 的真实 evidence 包（manifest.json / summary.json / results.jsonl / cases/<id>/*）齐全。
- hard_targets / quality_targets / failure_budgets 全部用真实数据回填。
- 若结果为 fail，实施前需补充到 baseline 的 `implementation_gap` 数组（顶层字段，与 hard_targets 同级）。

### 2.4 实施步骤（原子）

1. **新增留痕文档** `docs/changes/2026-05-30-phase-1a-hardening-baseline-rerun.md`，说明本次实跑的运行环境、commit、provider、时间窗、dev server 启动命令（例如 `make dev` 或 `uvicorn learning_agent.learning_agent.main:app --reload --port 8000`）。
2. **启动 dev server**：依照第 2.2 步骤 1 的 dry-run 成功为前提，启动开发服务器。
3. **执行实跑**：
   ```bash
   python tests/e2e/real_runner.py \
     --dataset tests/e2e/real_datasets/learning-mode-phase-1a-forge-state-real.json \
     --output tests/e2e/artifacts/2026-05-30/learning-mode-phase-1a-forge-state-real \
     --base-url http://localhost:8000
   ```
4. **回填 baseline**：用实跑产物 summary.json 的真实数字，替换 `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` 中的 status / 各 rate / counts。若任何 rate < 1.0 或 count > 0，补充到 baseline 顶层 `implementation_gap` 数组，每条形如 `{case_id, metric_name, observed, expected, root_cause_hypothesis, followup_task_id}`。
5. **更新留痕**：把 evidence 包路径、关键数字、若有 implementation_gap 全部列入 1 中的 docs/changes/。
6. **提交**：
   ```bash
   git add tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json \
           docs/changes/2026-05-30-phase-1a-hardening-baseline-rerun.md
   git commit -m "chore(1a/baseline): 实跑回填 forge-state baseline, baseline_version=2026-05-30-v1"
   ```

   **Commit message 字面量约束（invariant）**：commit message MUST 包含字面量 `baseline_version=<值>` 前缀（即 `baseline_version=` 这 17 个字符必须原样出现在 commit message 中），后接本次 baseline 版本号（如 `2026-05-30-v1`）。该字面量是下游 doc4 §2 前置准备与 §10 #2 用 `git log --grep='baseline_version='` 锁定本次 commit hash 的唯一依据。不允许写成 `baseline version` / `baseline_ver` / `bv=` 等任何变体；不允许只在 commit body 出现而 subject 缺失（`git log --grep` 默认只搜 subject 与 body 的首行匹配窗口，subject 必须含该字面量）。示例合规 commit message：`chore(1a/baseline): 实跑回填 forge-state baseline, baseline_version=2026-05-30-v1`。

### 2.5 验收条件

- baseline 文件 `status ∈ {pass, fail}`，不允许 `not_yet_executed`。
- 4 个 case 全部产生 `cases/<id>/{request.json,response.json,session.json,learning_unit.json,events.jsonl}`（dataset 第 34-40 行要求）。
- hard_targets 中 `learning_unit_creation_rate / non_empty_response_rate / forge_stage_metadata_rate / temperature_state_metadata_rate / learning_unit_payload_forge_stage_rate / frontend_backend_stage_consistency_rate` 全 = 1.0；`chat_session_false_forge_stage_count = 0`、`ask_alignment_stage_advance_count = 0`。
- 每个 case session.json/SSE metadata 必须包含 metadata_required 全 5 项：`learning_unit_id / learning_unit_phase / forge_stage / temperature_state / learning_action`。
- `must_pass_cases`（`lm-p1a-fuzzy-index-stage`、`lm-p1a-ask-does-not-advance-stage`）必须 pass。

### 2.6 失败时的根因调查清单

按以下顺序定位（不要直接改 baseline 数字以"凑过"）：

| 症状 | 优先排查位置 |
| --- | --- |
| `learning_unit_created=false` | `main.py:_prepare_learning_unit_turn` (983-1133) 是否被走、`/learning-units` 接口返回是否含 id |
| `forge_stage/temperature_state` metadata 缺 | `main.py:1066-1082` unit_metadata 构造 |
| `forge_stage_advanced_during_ask > 0` | `forge_policy.maybe_advance_forge_stage`（委托器 `main.py:1405-1421`）是否在 alignment_popup 短路（`main.py:1491-1522`）后误推进 |
| `chat_session_false_forge_stage_count > 0` | 非学习卷 session 是否误带 forge_stage —— 应只在 STUDY+absorbing 分支注入 metadata |
| `forge_stage_changed_event` 实际为 0 | events.jsonl 是否有 `learning_unit.forge_stage_changed`；若无，定位 `maybe_advance_forge_stage` (`main.py:1548-1550`) 在 finally 里是否真的执行到（`completed=True`）|

### 2.7 失败后的处理与回滚

- 若实跑结果为 fail 且症状在 2.6 表格中无对应行，**不要修改代码**，只把 baseline 的 `status=fail` 落实并把 fail 症状写进 `implementation_gap` 字段；新建 follow-up 任务而非把数字硬调到 pass。
- 若发现 baseline 文件本身写错（如把 case 名拼错），**不允许** `git checkout` 把已实跑结论回滚到 `not_yet_executed`；应新做一次提交修正数字并留痕说明。实跑事实只能向前演进，不允许时光倒流。
- 若已提交后发现无法恢复至 pass，`git revert <commit>` 整体回滚提交。

---

## 3. 裂缝 2：absorbing 是否真走 STUDY 核查

### 3.1 现状

- `_prepare_learning_unit_turn` 在 absorbing 分支末尾硬编码 `effective_mode = AgentMode.STUDY`（`main.py:1058`）。
- 但 alignment_popup 短路（`main.py:1491-1522`）不调 agent_loop，直接以 STUDY profile 的 metadata 走 placeholder，可能让 STUDY_PROFILE 的 system_prompt 看起来"被生效"但实际没消费。
- critic 担心 profile 迁移没收尾 —— 即 `mode_service.py:99-149` 四模式 profile 是否在 absorbing 真实路径上被加载、profile.assistant_message_metadata 中的 `mode` 字段是否真的是 `study`。

### 3.2 根因调查步骤

```bash
# 1) 看 _prepare_learning_unit_turn 的所有分支末尾 effective_mode 赋值
grep -n "effective_mode" learning_agent/learning_agent/main.py | head -30

# 2) 看 build_turn_profile 在 STUDY/CHAT 下产出的 profile 字段差异
grep -n "build_turn_profile\|STUDY_PROFILE\|CHAT_PROFILE" learning_agent/learning_agent/mode_service.py | head -20

# 3) 看 stream_session_chat 实际写出的消息 metadata mode 字段
#    需要一次真实跑：检查 sessions/<sid>.events.jsonl 中 message.assistant_started 的 metadata.mode
```

核查矩阵（codex 必须在裂缝 1 实跑产生的证据包里逐条核对，写进 docs/changes 留痕）：

| 入口 | 期望 effective_mode | 期望 profile | 验证点 |
| --- | --- | --- | --- |
| absorbing + decision=none | STUDY | STUDY_PROFILE | `cases/lm-p1a-fuzzy-index-stage/session.json` 中 assistant message metadata.mode = `study` |
| absorbing + decision=suggested | STUDY | STUDY_PROFILE | message metadata.mode = `study`；system_prompt addendum 含 `ABSORBING_OPENING_SUGGESTION_BLOCK` |
| absorbing + decision=active (alignment_popup) | STUDY | STUDY_PROFILE | placeholder 走 short-circuit；assistant message metadata.mode = `study` 且 `alignment_popup=True` |
| outputting | TEACH | TEACH_PROFILE | 走 `_stream_teach_answer_flow`（`main.py:1479-1483`），message metadata.mode = `teach` |
| 非学习卷 session | CHAT | CHAT_PROFILE | 不进 `_prepare_learning_unit_turn`，走 `_prepare_session_turn` |

### 3.3 修复目标

- 不允许 absorbing 路径上出现 metadata.mode = `chat`（残留 CHAT_PROFILE）。
- 不允许 alignment_popup 短路时 profile 实际是 CHAT_PROFILE。
- 上述事实必须由真实 evidence（裂缝 1 实跑产物）和 2 个新单测共同支撑。

### 3.4 实施步骤（原子）

1. **新增留痕文档** `docs/changes/2026-05-30-phase-1a-hardening-absorbing-routes-to-study.md`，列出核查矩阵的真实结果（对应 4 个 case 实跑结论）。

2. **新增单测准备**：参考 `tests/test_mode_layering.py` 中现有的 `service` fixture 与 `_apply_rate_limits` 包裹模式（`test_mode_layering.py:73` 附近），复用同样的 LearningAgentService + LearningSessionEventStore + mock alignment_classifier 构造路径。

3. **新增单测** `tests/test_mode_layering.py::test_absorbing_routes_to_study_profile`：
   - 构造 absorbing unit + clarification_count=0；
   - mock alignment_classifier 返回 decision 其中 `mode=none`；
   - 调 `_prepare_learning_unit_turn`；
   - 断言 `prepared_turn.effective_mode == AgentMode.STUDY` 且 `prepared_turn.profile` 来自 STUDY_PROFILE（断言 `profile.assistant_message_metadata["mode"] == "study"`）。

4. **新增单测** `tests/test_mode_layering.py::test_alignment_popup_short_circuit_keeps_study_profile`：
   - 构造同上，但 mock decision 返回 `mode=active`；
   - 跑 `stream_session_chat` 一轮，吃掉所有 chunk；
   - 断言写到 session 的 assistant message metadata.mode == `study`，且 `alignment_popup=True`。

5. **若核查发现真实路径有残留 CHAT 路由**（极不可能但要预案）：
   - 修复点只允许在 `main.py:1009-1058` 范围内（absorbing 分支体）；
   - **不允许** 改 `mode_service.py`、不允许动 STUDY_PROFILE 提示词内容。

6. **提交**：
   ```bash
   git add tests/test_mode_layering.py docs/changes/2026-05-30-phase-1a-hardening-absorbing-routes-to-study.md
   git commit -m "硬化: 核查 absorbing 真实路由至 STUDY 分支"
   ```
   （若 5 触发则加 `learning_agent/learning_agent/main.py`）

### 3.5 验收条件

- 上述 2 个单测全绿。
- 裂缝 1 实跑的 4 个 case 的 `session.json` 中所有 absorbing assistant message metadata.mode 全 = `study`。
- 留痕文档中给出 4 个 case 的 metadata.mode 实测值（逐 case 列）。

### 3.6 失败/异常处理

- 若发现某 case 出现 metadata.mode = `chat`：先确认不是 dataset 误配（dataset 第 13 行 `target_surface=frontend_or_api`），再回 `_prepare_learning_unit_turn` 找漏分支。**禁止**通过修改 dataset 来掩盖。

### 3.7 回滚

- 仅新增单测和留痕文档；回滚时 `git revert <commit>` 撤销提交即可。
- 若 5 触发了代码修复，回滚需走 `git revert <commit>`，不允许 `reset --hard`。

---

## 4. 裂缝 3：对齐侧面事件真实数据缺失根因调查

### 4.1 现状

代码里 5 个对齐侧面事件全部已注册：

| 事件 | enum 位置 | emit 位置 |
| --- | --- | --- |
| `LEARNING_UNIT_ALIGNMENT_SUGGESTED` | `session_events.py:48` | `alignment_policy.py:244-253` |
| `LEARNING_UNIT_ALIGNMENT_SKIPPED` | `session_events.py:44` | `main.py:848-857`（accept_assumption 内） |
| `LEARNING_UNIT_ALIGNMENT_RESOLVED` | `session_events.py:42` | `alignment_policy.py:238-243`（user_request 一次性消费） |
| `LEARNING_UNIT_ASSUMPTION_ACCEPTED` | `session_events.py:45` | `main.py:848-857` |
| `LEARNING_UNIT_OBJECTIVE_REFINED` | `session_events.py:46` | `main.py:891-897` |

但真实 JSONL 长期观察不到 —— critic 怀疑被 `_apply_rate_limits` (`alignment_policy.py:75-92`) 静默降级，或者调用入口太窄。

### 4.2 根因调查步骤

```bash
# 1) 真实 events.jsonl 里这 5 个事件的历史频率
LA_DATA_DIR=${LA_DATA_DIR:-.learning_agent_data}
for evt in alignment_suggested alignment_skipped alignment_resolved assumption_accepted objective_refined; do
  count=$(find "$LA_DATA_DIR/sessions" -name "*.events.jsonl" -exec grep -l "learning_unit.$evt" {} + 2>/dev/null | wc -l)
  echo "$evt: $count sessions"
done

# 2) 看 _apply_rate_limits 的调用频率与降级路径（emit 点实施前临时打 logger.info 观测）
grep -n "_apply_rate_limits" learning_agent/learning_agent/alignment_classifier.py

# 3) 看每个 emit 点的入口是否真的会被触发
#    - SUGGESTED: classifier 必须先判 suggested 且未被限流 → alignment_classifier.classify
#    - SKIPPED + ASSUMPTION_ACCEPTED: 只在 /align/accept_assumption 接口被调时触发 → 前端 UI 是否真的有按钮
#    - RESOLVED: 用户主动 /align 端点 → 前端弹窗"接受"路径
#    - OBJECTIVE_REFINED: refine_objective 接口被调 → 前端是否有 UI
grep -n "accept_assumption\|refine_objective\|/align" web/static/observability.js web/index.html 2>/dev/null | head -20
```

按下表确认每个事件的"未观测"是路由问题还是入口问题：

| 事件 | 假设入口 | 调查动作 |
| --- | --- | --- |
| SUGGESTED | classifier 判 suggested → apply_alignment_decision | 4.2.1) classifier 阈值统计；4.2.2) `_apply_rate_limits` 把 suggested 静默降 none 的命中率 |
| SKIPPED | 前端"先按这个学"按钮 → accept_assumption | UI 是否暴露、是否曾被点击 |
| RESOLVED | /align 端点 user_request 消费 | /align 调用日志 |
| ASSUMPTION_ACCEPTED | 同 SKIPPED | 与 SKIPPED 同发，应同频 |
| OBJECTIVE_REFINED | refine_objective 接口 | UI/接口是否暴露 |

### 4.3 修复目标

- 用真实数据回答"每个事件为什么没观察到"，结果只有三种：
  1. **入口未暴露**（前端没按钮 / 接口没接）→ 记 implementation_gap，列入"不本期解决"。
  2. **被限流静默降级** → 由裂缝 4 解决（新增 RATE_LIMITED 事件让降级显形）。
  3. **路由 bug**（代码路径根本没走到 emit）→ 在本裂缝内最小修复。
- 输出一份"观测脚本 + 当前事件实测频率快照"（落 docs/changes/）。

### 4.4 实施步骤（原子）

1. **新增观测脚本** `scripts/diagnose_alignment_events.py`（codex 自定脚手架，不进入产品代码），输出格式：
   ```json
   {
     "event_type": "learning_unit.alignment_suggested",
     "total_count": 5,
     "unique_sessions": 3,
     "latest_5_samples": [
       {"seq": 42, "ts": "2026-05-30T12:34:56Z", "payload_keys": ["learning_unit_id", "phase", "alignment_reason"]}
     ],
     "emit_source": "alignment_policy.py:244-253"
   }
   ```
   脚本应过滤 `visibility=agent` 类型、按 type 聚合、并输出 emit 源代码锚点。

2. **新增留痕文档** `docs/changes/2026-05-30-phase-1a-hardening-alignment-event-gap-rca.md`，包含：
   - 4.2 调查的真实数字与截图；
   - 每个事件的 verdict（gap_unexposed / rate_limited_silent / route_bug）；
   - 若 verdict=route_bug，列具体修复点。

3. **若发现 route_bug**：仅修复 emit 路径，不重写 alignment_policy 整体结构。修复必须带 1 个新单测覆盖。

4. **不修复 gap_unexposed**：本裂缝只调查不补 UI；前端入口补全由 Phase 1B/独立任务承接。

5. **回归方式**：先把裂缝 4（RATE_LIMITED）落了，再跑一次 4 case 实跑，验证 suggested 卷里能稳定看到 SUGGESTED 或 RATE_LIMITED 二选一（不应两者皆无）。

6. **提交**：
   ```bash
   git add scripts/diagnose_alignment_events.py docs/changes/2026-05-30-phase-1a-hardening-alignment-event-gap-rca.md
   git commit -m "硬化: 对齐侧面事件可观测性根因调查"
   ```
   （若 3 触发则加对应代码与测试文件）

### 4.5 验收条件

- docs/changes 文件给出 5 个事件的 verdict 表，所有 verdict 必须落到三种之一，禁止 `unknown`。
- `scripts/diagnose_alignment_events.py` 可重复跑、输出稳定（同样 `$LA_DATA_DIR` 跑两次结果一致）。
- 若有 route_bug 修复，新增单测全绿，并在 docs/changes 里给 before/after 真实事件计数。

### 4.6 回滚

- 留痕和脚本是新增文件，回滚 `git rm` 或 `git revert <commit>` 即可。
- route_bug 修复独立提交，回滚 `git revert <commit>`。

---

## 5. 裂缝 4：引入 LEARNING_UNIT_ALIGNMENT_RATE_LIMITED 事件

### 5.1 修复目标

让 `_apply_rate_limits` 的两条静默降级路径（`alignment_classifier.py:160-163` 调用处）从此**有事件**，否则 §9.3 #2/#3 的存在与否对外界完全不可观测，等于黑盒。

### 5.2 事件 schema

```json
{
  "type": "learning_unit.alignment_rate_limited",
  "visibility": "agent",
  "payload": {
    "learning_unit_id": "lu-...",
    "phase": "absorbing",
    "original_mode": "suggested",
    "downgraded_to": "none",
    "rate_limit_rule": "max_suggestions_per_unit | nag_cooldown",
    "suggestion_count": 2,
    "max_suggestions": 2,
    "nag_cooldown_remaining": 0,
    "trigger": "classifier_suggested"
  }
}
```

字段约束：

- `original_mode` 当前实现只有 `suggested` 一种（因为 `_apply_rate_limits` 只对 suggested 生效，见 `alignment_classifier.py:86-87`）。**下游若要给 active 短路加事件，单开 enum，不复用本事件 type**（N=1 invariant 的具体指导）。
  - 注：`active` 阻塞限流由 `main.py:1023-1028` 的 `clarification_count >= 1` 短路实现（不在本事件覆盖范围），该路径若后续要可观测需单开事件 type。
- `downgraded_to` 固定为 `none`（这是 _apply_rate_limits 当前唯一降级目标）。
- `rate_limit_rule ∈ {"max_suggestions_per_unit", "nag_cooldown"}`，对应 #2/#3 两条规则；同时命中时按 `max_suggestions_per_unit` 优先（与代码顺序一致）。
- payload 必须包含 `suggestion_count / max_suggestions / nag_cooldown_remaining` 三组当时实际值（不取假设值），便于离线复盘。
- 不携带分类器 candidates / divergence_cost（避免 PII 上限与冗余；这些字段在 SUGGESTED 事件已经出现的情况下即可，没出现就是被本事件拦截了）。

### 5.3 enum 注册位置

`learning_agent/learning_agent/session_events.py:51` 之后（与其他 `LEARNING_UNIT_FORGE_STAGE_CHANGED` 同段，紧跟既有学习卷事件，保持枚举聚集）：

> **行号漂移容许说明**：上述 `:51` 是落稿时（2026-05-30）的真实行号锚点，便于 codex 快速定位。若在本裂缝实施前已有其它新事件先于本事件合入 `session_events.py`（导致 `LEARNING_UNIT_FORGE_STAGE_CHANGED` 行号 ≠ 51），实施时**按符号位置追加**——即紧跟 `LEARNING_UNIT_FORGE_STAGE_CHANGED` 同一段学习卷枚举的末行追加，**允许行号漂移**，不必硬卡 `:51`。该策略与 doc3 §11 Step 1 的行号容许漂移策略一致：行号锚点用于人工导航，符号锚点用于实际落位。

```python
LEARNING_UNIT_ALIGNMENT_RATE_LIMITED = "learning_unit.alignment_rate_limited"
```

修改后 session_events.py 的 LEARNING_UNIT_* 枚举总数从 14 变为 15。

### 5.4 emit 点与时机

**实际 emit 点**：`alignment_classifier.py:160-163`（_apply_rate_limits 真正调用的地方）。

**实施方案**（二选其一，选中后在 docs/changes 里明确拍板）：

**方案 A（推荐）**：修改 `_apply_rate_limits` 返回值结构
- 现状：`_apply_rate_limits(unit: LearningUnit, decision: AlignmentDecision) -> AlignmentDecision`
- 改为：`_apply_rate_limits(unit, decision) -> (AlignmentDecision, Optional[RateLimitInfo])`
  - 其中 `RateLimitInfo = dataclass(original_mode, downgraded_to, rule, suggestion_count, max_suggestions, nag_cooldown_remaining, trigger)`
- 调用方 `alignment_classifier.classify` 在 160-163 处改为接收元组，判断是否 `rate_limit_info is not None`；若非 None 则由 classifier 返回 `(decision_post, rate_limit_info)` 二元组给上游，由上游 `main.py:_prepare_learning_unit_turn` (1033-1055 阿段) emit。
- 优点：_apply_rate_limits 保持纯函数、单测易写、状态转移清晰。

**方案 B**：直接在 classifier.classify 内 emit
- classifier 与上游共享 `emit_unit_event` callable；classify 内部调用 _apply_rate_limits 后，若返回结果不同于原始 decision，直接 emit RATE_LIMITED。
- 缺点：classifier 多了副作用、单测需 mock emit；优点：不扩签名。

**emit 互斥语义**（二选一均适用）：
- **同一 turn 内 RATE_LIMITED 与 SUGGESTED/STARTED/RESOLVED 互斥**：命中限流时不再 emit SUGGESTED（降级后就是 none，没有建议），nag_cooldown 减 1 仍照常发生（在 apply_alignment_decision 内）。
- 该互斥性由 alignment_classifier.classify → _apply_rate_limits 的调用顺序保证：raw_decision.mode==suggested 且限流后 decision.mode==none，则不再向 apply_alignment_decision 递 suggested。

### 5.5 visibility 与 replay 决策

**已拍板**：`visibility = AGENT`（与现有 5 个对齐侧事件保持一致，便于离线时间轴重建）。

**replay 处理**：
- 参与 replay（session_projection.replay_events）；
- **在 session_projection._apply_event 中需新增显式分支**（不能靠 fall-through）：
  ```python
  elif event.type == EventType.LEARNING_UNIT_ALIGNMENT_RATE_LIMITED:
      # 纯日志性事件，不改写 alignment_state snapshot，只用于时间轴重建
      # 不修改任何 state 字段，直接 return
      return
  ```
  这样保证：即使 rollback enum 后，已写入的 RATE_LIMITED 事件也不会抛异常或被计入 corrupt_events，只是静默保留在 JSONL 中。

### 5.6 open_questions（实施前必须澄清）

1. **_apply_rate_limits 调用顺序与 main.py 强升逻辑**：
   - `main.py:1038-1052` 存在强升 active 的覆盖逻辑（user_initiated_alignment / legacy_pending_alignment）。
   - 若被限流降为 none、后续又被强升为 active，本轮是否仍 emit RATE_LIMITED？
   - **拍板建议**：只在最终 decision.mode 保持 none 的情况下才 emit RATE_LIMITED；强升到 active 时不 emit（因为最终状态已是 active，不体现限流）。

2. **visibility=AGENT 是否需扩 _emit_unit_event 签名**：
   - 当前 `_emit_unit_event(self, unit, event_type, extra=None)` 所有学习卷事件统一 AGENT。
   - 若未来有其他事件需 OBSERVABILITY，再扩签名。本期 RATE_LIMITED 可直接用既有 _emit_unit_event。

### 5.7 实施步骤（原子）

1. **新增留痕文档** `docs/changes/2026-05-30-phase-1a-hardening-rate-limited-event.md`，明确选择方案 A/B、emit 互斥语义、visibility 与 replay 决策。

2. **选定方案并改造 _apply_rate_limits**：
   - 若选方案 A：改返回值、新增 RateLimitInfo dataclass（可临时放 alignment_classifier.py 或独立模块）。
   - 若选方案 B：加 emit_unit_event 形参。

3. **注册 enum**：编辑 `learning_agent/learning_agent/session_events.py:51`，新增一行。

4. **修改调用方**：
   - `alignment_classifier.py:160-163` 接收返回值或触发 emit；
   - `main.py:_prepare_learning_unit_turn:1033-1055` 可选接收 rate_limit_info 并 emit（若选方案 A）。

5. **新增 session_projection 分支**：`session_projection.py` 的 `_apply_event` 末尾新增 RATE_LIMITED 处理分支。

6. **新增单测** `tests/test_alignment_policy.py::test_rate_limited_event_emitted_when_max_suggestions_hit`：
   - 构造 unit.suggestion_count = MAX_SUGGESTIONS_PER_UNIT（2）；
   - decision.mode=suggested；
   - 调 apply_alignment_decision；
   - 断言 emit_unit_event 被调一次，事件 type=`learning_unit.alignment_rate_limited`，payload.rate_limit_rule=`max_suggestions_per_unit`。

7. **新增单测** `tests/test_alignment_policy.py::test_rate_limited_event_emitted_during_cooldown`：
   - 构造 unit.nag_cooldown_remaining=2；
   - decision.mode=suggested；
   - 断言 emit 一次，rule=`nag_cooldown`。

8. **新增单测** `tests/test_alignment_policy.py::test_rate_limited_event_not_emitted_when_no_downgrade`：
   - decision.mode=active 时不 emit。

9. **新增单测** `tests/test_session_projection.py::test_apply_event_rate_limited_noop`：
   - 喂入 RATE_LIMITED 事件，验证 replay 不抛、alignment_state snapshot 不变。

10. **回归裂缝 3 的实跑**：跑一次 dataset `learning-mode-phase-1a-forge-state-real`，在 case `lm-p1a-ask-does-not-advance-stage`（dataset 第 132 行 `"教"` 模糊输入，触发 suggested）的 events.jsonl 里应至少观测到 SUGGESTED 或 RATE_LIMITED 之一（互斥，不应两者皆无）。

11. **提交**：
    ```bash
    git add learning_agent/learning_agent/session_events.py \
            learning_agent/learning_agent/alignment_classifier.py \
            learning_agent/learning_agent/alignment_policy.py \
            learning_agent/learning_agent/session_projection.py \
            tests/test_alignment_policy.py \
            tests/test_session_projection.py \
            docs/changes/2026-05-30-phase-1a-hardening-rate-limited-event.md
    git commit -m "硬化: 新增 LEARNING_UNIT_ALIGNMENT_RATE_LIMITED 事件使降级路径可观测"
    ```

### 5.8 验收条件

- 新增 enum 在 `session_events.py` 唯一一处注册，全项目仅一个字符串字面量 `"learning_unit.alignment_rate_limited"`。
- 上述 4 个新单测 + 1 个 replay 测试全绿。
- 重新跑裂缝 1 的实跑后，4 个 case 至少出现 1 次 SUGGESTED 或 1 次 RATE_LIMITED（不能两者皆 0，否则证明 emit 路径仍未走通）。
- Phase 1A 既有 14 个 LEARNING_UNIT_* 事件类型的 schema 完全没变（grep 验证：`grep -c 'LEARNING_UNIT_' learning_agent/learning_agent/session_events.py` 从 14 变为 15）。

### 5.9 回滚

- 单一提交内变更可 `git revert <commit>` 一键回滚。
- 若已有线上 JSONL 写入了 RATE_LIMITED 事件再回滚 enum：replay 会走 _apply_event 的 fall-through，无声忽略该事件（不抛异常、不进 corrupt_events），但事件仍留在 JSONL 中。离线分析脚本需自行处理未知 type。

---

## 6. 不做事项（明确边界）

| 不做项 | 原因 |
| --- | --- |
| 引入除 RATE_LIMITED 外的新事件类型 | 本期硬化只补"应有却无"的可观测性，不扩面 |
| 重写 alignment_policy / forge_policy | 边界不动；裂缝 3 若发现 route_bug 也只做最小修复 |
| 修改 STUDY_PROFILE 提示词内容 | 裂缝 2 只核查路由，prompt 优化留 Phase 1B 或独立任务 |
| 引入 Runtime mode 分支硬化 | 属于后续巩固期，参见 `docs/output/learning-mode-development-plan-2026-05-27.md` |
| 多元 stop_reason / stop modal meta / ALIGNMENT_POPUP 与 suggestion_bar 竞争 | P2 留后；critic 已提示 |
| 持久化 event-sourcing 完整化 | Phase 1A 范围外 |
| 抽 LearningForgeState 子模型 | Phase 1A 文档已明示约束，不动 |
| 给 Phase 1B-1E 的事件占位 | N=1 不预留底座；Phase 1B 用到时再加 |

---

## 7. 全裂缝跨切关注点

1. **共享工作树**：四条裂缝在同棵 `/Users/roseannk/my-agent` 工作树串行做，提交粒度 = 一裂缝一 commit；禁止任何 `git add -A` / `git stash` / `git reset --hard` / `git clean` / `git rebase`。

2. **学习卷主链不变**：四条修复都不动 `absorbing⇄outputting→consolidated` 的 `_ALLOWED_TRANSITIONS`（`learning_unit.py:237-246`）。

3. **append-only**：裂缝 4 的新事件 MUST 写入 JSONL，不允许只在内存里 emit。

4. **四层职责**（每个裂缝的留痕必须标注）：
   - **Product 层**：模型调度、数据流、事件定义、policy 决策
   - **Runtime 层**：async 调度、并发控制、错误处理
   - **Interface 层**：HTTP 路由、WebSocket、session 接口
   - **Infrastructure 层**：存储、日志、监控
   - 本次硬化全部应落在 Product 层（新事件、路由核查、baseline 回填），Runtime/Interface/Infrastructure 不应被动到。

5. **测试与 dataset**：除裂缝 1 重跑既有 dataset 外，本期**不新增** dataset；新增的都是单测（裂缝 2/4）与诊断脚本（裂缝 3）。

---

## 8. 完成定义（DoD）

四条裂缝全部满足下列条件，本硬化阶段才算关闭：

- [ ] 裂缝 1：baseline status ∈ {pass, fail}，hard_targets 全部用真实数字回填，若有 fail 则补充 implementation_gap，留痕落盘，提交含 baseline_version
- [ ] 裂缝 2：核查矩阵 5 行全部真实数据回填，2 个新单测全绿，留痕落盘
- [ ] 裂缝 3：5 个对齐侧事件全部给出 verdict，观测脚本可重复跑，route_bug 若有则修复+单测，留痕落盘
- [ ] 裂缝 4：enum 注册、选定方案 A/B 并实施、4 个测试全绿、回归实跑能观察到 SUGGESTED 或 RATE_LIMITED 之一（互斥），留痕落盘，_apply_event 新增 RATE_LIMITED 分支
- [ ] 四个提交全部独立可 revert，且未触发禁用 git 命令
- [ ] docs/changes/ 留痕齐全，每份文件规约参照 2026-05-21-append-only-jsonl-rule.md

满足后 Phase 1B 入局情境生成（最细颗粒度技术设计）方可启动。

