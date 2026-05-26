# 研习模式现存问题清单（记忆系统 v1 前置核查）

> 项目：Learning-Agent
>
> 文档类型：缺陷与设计问题清单
>
> 首版日期：2026-05-26
>
> 最近核查：2026-05-26
>
> 背景：最初为“记忆系统 v1”寻找接入点时，核查了 `mode_service.py`、`main.py`、`web_server.py` 中的 mode / persona 链路。后续研习卷已经引入 adaptive alignment、TEACH、前端 learning-unit card，本文件同步更新为“当前研习模式”问题清单。
>
> 用途：记忆系统 v1 要把 profile 接进 persona / system_prompt / 学习卷行为链路。若研习模式本身仍有链路问题，会污染 v1 自测，分不清是“记忆系统没生效”还是“研习底层路径本来就不稳定”。

---

## 一、结论总览

| # | 问题 | 当前状态 | 严重性 | 和记忆系统 v1 的关系 | 建议处理时机 |
|---|------|----------|--------|----------------------|--------------|
| 1 | 旧 `is_confirmation_message` 子串误判 | 已基本消失 | 低 | 间接 | 不再修旧函数，仅保留回归认知 |
| 2 | 研习卷 absorbing 实际使用 `CHAT` profile，`STUDY_PROFILE` 与 memory 开关被绕开 | 仍存在 | 高 | 直接 | v1 前先定口径 |
| 3 | persona 在 system prompt 中仍处于弱势位置，且缺少行为级验证 | 仍存在 | 中高 | 直接 | v1 前验证或调整 |
| 4 | 首轮学习结构主要靠文本 prompt，前端未形成“学习地图 / 讲解 / 目标”的强视觉分块 | 仍存在 | 中 | 间接 | v1 前后均可，但会影响感知验收 |
| 5 | TEACH 入口与异步概念抽取存在竞态，可能过早进入 outputting 后直接跳过验收 | 仍存在 | 中高 | 直接 | v1 前修或加保护 |
| 6 | “讲讲看”和“做 3 题”两个按钮后端语义相同 | 仍存在 | 中 | 间接 | v1 前顺手修 |
| 7 | `STUDY_MODE_PROMPT` 仍弱，但显式 Study 顶层入口已下线 | 部分存在 | 低到中 | 间接 | 后续治理 |
| 8 | persona 的 `mode` 字段仍是 `home_mode` hint，但命名仍有轻微歧义 | 部分存在 | 低 | 无关 | cleanup |
| 9 | `seed_text` 缺少后端空值校验 | 仍存在 | 中 | 间接 | v1 前顺手修 |

---

## 二、已消失或降级的问题

### 问题 1：旧 `is_confirmation_message` 子串匹配误判

**当前判断：基本不存在。**

最初问题是：Ask `aligning` 状态下用 `is_confirmation_message()` 判断“没问题，开始吧”等确认句时，会因为命中“没 / 别 / 不用”而误判为否定。

当前代码里已经查不到 `is_confirmation_message` 的实现或调用。学习卷主链也已从旧的 `phase=aligning` 改成：

- `LearningUnit.phase` 只保留 `absorbing / outputting / consolidated / stopped`
- 对齐状态改为旁路字段 `alignment_state`
- `_prepare_learning_unit_turn()` 在需要澄清时临时把本轮覆写为 `ASK`
- 非学习卷会话收到 `AgentMode.ASK` 会归一到 `CHAT`

证据：

- [`learning_unit.py`](../../learning_agent/ai/learning_unit.py)：注释明确 `aligning` 已从主链移除，对齐降级为 `alignment_state` 旁路。
- [`main.py`](../../learning_agent/learning_agent/main.py)：`_prepare_learning_unit_turn()` 基于 `alignment_policy` 决定是否临时覆写为 `ASK`。
- [`main.py`](../../learning_agent/learning_agent/main.py)：`_prepare_session_turn()` 中非学习卷 `ASK` 被归一为 `CHAT`。

**剩余风险**

旧确认词 bug 本身不需要修。但文档和验收报告中仍有旧 `Ask 确认词判断` 的残留说法，后续查阅时容易误导。建议在相关设计文档里标注“旧 Ask 确认闭环已被 adaptive alignment 替代”。

---

## 三、当前仍存在的关键问题

### 问题 2：研习卷 absorbing 实际使用 `CHAT` profile，`STUDY_PROFILE` 与 memory 开关被绕开

**当前判断：仍存在，且比旧清单中的“memory_read/write 是 mode 级”更关键。**

当前前端把“研习”作为 UI 产品模式，但发送到后端时统一映射为 `chat`：

```js
function backendModeForFrontendMode(_mode) {
    return 'chat';
}
```

后端学习卷 absorbing 阶段也明确默认走 `AgentMode.CHAT`，只有需要短暂澄清时才覆写为 `ASK`：

```python
effective_mode = (
    AgentMode.ASK if decision.mode == "active" else AgentMode.CHAT
)
profile = build_turn_profile(effective_mode, ...)
```

而 `STUDY_PROFILE` 是唯一默认打开 `memory_read=True`、`memory_write=True` 的 profile；`CHAT_PROFILE` 没有打开这两个开关。

### 影响

这造成三层语义断裂：

1. 用户看到的是“研习”
2. Product 层 learning unit absorbing 实际生成的是 `CHAT` turn profile
3. `STUDY_PROFILE` 中原本为学习场景配置的 `memory_read/write` 不会在研习卷主路径生效

如果记忆系统 v1 预期“研习模式读写记忆”，当前路径会让 v1 自测误判：

- 不是 profile 没读到用户记忆，而是研习卷根本没有走 `STUDY_PROFILE`
- 不是 memory 开关坏了，而是 learning unit absorbing 被建模成 `CHAT`

### 修复建议

先在 Product/Application 层定一个明确口径，不要让 UI 名称和 runtime profile 偶然耦合。

可选方案：

1. 新增学习卷专用 profile，例如 `LEARNING_ABSORBING_PROFILE`，不复活顶层 Study 入口，但让学习卷 absorbing 拥有自己的 memory / prompt / context 策略。
2. 让 learning unit absorbing 使用 `AgentMode.STUDY` profile，但前端仍只暴露“闲谈 / 研习”，不把 Study 作为顶层模式返回给用户。
3. 若 v1 明确不接学习卷 memory，则文档必须写清：v1 persona 默认值只影响 tone，不承诺 learning unit 读写长期记忆。

建议优先方案 1：语义最清楚，也符合“Product/Application 收口产品语义，Runtime 只消费执行输入”的分层规则。

---

### 问题 3：persona 在 system prompt 中仍处于弱势位置，且缺少行为级验证

**当前判断：仍存在。**

`build_system_prompt()` 的拼接顺序仍是：

```python
return (
    f"{NEUTRAL_GUARDRAILS}\n\n"
    f"{build_mode_prompt(mode)}\n\n"
    f"{persona.tone_prompt}"
)
```

persona 确实进入了 prompt，现有测试也覆盖了 `"苏格拉底" in profile.system_prompt`。但这只验证“文本被拼进去”，没有验证“行为上真的明显不同”。

### 影响

记忆系统 v1 的关键自测之一是：profile 或默认 persona 改变后，回答风格应明显不同。例如：

- `feynman` 应该更明显地拆解、类比、回问
- `socrates` 应该更明显地提问、收窄、检验前提

如果 persona 因为位置、强度或后续 addendum 被稀释，v1 会难以判断到底是：

1. profile 默认 persona 没读到
2. persona 读到了但 prompt 没足够生效

### 修复建议

v1 前做隔离验证：

- 固定同一个输入
- 分别构造 `build_turn_profile(..., persona_key="feynman")` 与 `persona_key="socrates"`
- 使用真实 provider 或最接近生产的 provider 跑 3 到 5 组输出
- 人工验收或轻量规则验收是否出现目标风格特征

若差异不明显，再调整拼接顺序或增强 persona 分隔：

```text
NEUTRAL_GUARDRAILS

本轮必须遵循的表达风格：
{persona.tone_prompt}

当前模式协议：
{mode_prompt}
```

---

### 问题 4：首轮学习结构主要靠文本 prompt，前端未形成强视觉分块

**当前判断：仍存在。**

后端已经有首轮开场模板，要求输出：

1. 工作目标卡片
2. 学习地图
3. 第一段实质讲解
4. 可选收窄建议

但当前实现主要是把这套结构作为 `system_prompt_addendum` 交给 LLM，让它在普通 assistant 气泡里输出。前端 learning-unit card 只渲染：

- 阶段 pill
- objective text
- status badge
- assumption note
- suggestion bar
- TEACH / stop / reuse 动作

没有把“学习地图”和“第一段讲解”拆成稳定的视觉区块。MVP 任务拆解里曾明确说：不要把三段拼成单个长文本气泡，视觉分块是学习模式区别于 Chat 的可感知信号。

### 影响

研习模式即使后端已经比 Chat 多了状态机，用户感知上仍可能像：

```text
开了一个带顶部卡片的长聊天
```

这会削弱 MVP 的核心验收标准：“用户明显感知它比 Chat 更适合学习”。

### 修复建议

不一定要在记忆 v1 前完成，但建议至少登记为 P1：

- 后端给首轮返回结构化 metadata，例如 `learning_map: list[str]`、`working_objective`
- 或前端对首轮固定 markdown 标题做安全解析，但优先结构化 metadata
- learning-unit card 或专属区域渲染“目标 / 地图 / 当前讲解进度”

---

### 问题 5：TEACH 入口与异步概念抽取存在竞态，可能过早跳过验收

**当前判断：仍存在。**

absorbing 回答结束后，概念抽取是异步尾任务：

```python
task = asyncio.create_task(
    self._run_concept_extraction_tail(unit_id, response_text)
)
```

TEACH 入口则允许用户在 absorbing 阶段随时点“讲讲看”或“做 3 题”。进入 outputting 时，题目生成器依赖当前 `unit.concept_list`：

```python
questions = await self.teach_generator.generate(
    objective_text=unit.objective.text,
    concepts=unit.concept_list,
)
```

如果此时 `concept_list` 为空或抽取任务尚未完成，`TeachQuestionGenerator.generate()` 会返回 `[]`，后端将 `verification_status="skipped"`，并自动跨过 outputting 进入 consolidated。

### 影响

用户可能刚看完首轮讲解就点“讲讲看”，结果系统因为概念列表尚未沉淀而跳过验收。表面上是用户完成了一卷，实际上没有真正 outputting。

这会污染几个判断：

- `teach_entry_rate` 看起来进入了 TEACH，但实际可能是 skipped
- `unit_completion_rate` 看起来完成，但完成质量弱
- 记忆系统 v1 如果依赖 consolidated 后的学习结果，会拿到低质量甚至空反馈

### 修复建议

至少加一个保护：

1. 前端：`concept_list.length === 0` 或 `first_value_delivered_at` 缺失时，禁用或弱化 TEACH 入口，文案改为“再学一点后讲讲看”。
2. 后端：`advance_learning_unit(..., "outputting")` 在概念为空时不要直接 consolidate，可返回 400 或回到 absorbing 并提示“还没有可验收概念”。
3. 更稳方案：跟踪 extraction pending / completed 状态，TEACH 入口等尾任务完成或超时后再开放。

---

### 问题 6：“讲讲看”和“做 3 题”两个按钮后端语义相同

**当前判断：仍存在。**

前端渲染两个按钮：

```html
<button data-action="advance-outputting">讲讲看</button>
<button data-action="advance-outputting-3">做 3 题</button>
```

但 `handleAction()` 中两个 action 走完全相同逻辑：

```js
case 'advance-outputting':
case 'advance-outputting-3': {
    await api('POST', `/learning-units/${uid}/advance`, { target_phase: 'outputting' })
}
```

后端 `AdvanceLearningUnitRequest` 也只有 `target_phase`，没有题量、验收形态或入口意图。

### 影响

用户以为自己在选择两种不同验收方式，实际没有区别。这是典型的产品承诺和实现不一致，会降低对研习闭环的信任。

### 修复建议

二选一：

1. MVP 保守：只保留一个按钮“讲讲看”，删除“做 3 题”。
2. 真分叉：`advance` 请求增加可选字段，如 `quiz_size=3` 或 `teach_mode="self_explain" | "quiz"`，并传给题目生成器。

建议先走方案 1，避免引入不必要的 API 面。

---

### 问题 7：`STUDY_MODE_PROMPT` 仍弱，但显式 Study 顶层入口已下线

**当前判断：部分存在，优先级下降。**

`STUDY_MODE_PROMPT` 仍是软约束，和 Chat 的边界不强。但当前前端只暴露“闲谈 / 研习”，`Study` 顶层入口已经下线，研习卷 absorbing 也没有走 `STUDY_PROFILE`。

### 影响

这不再是当前研习 UI 的直接问题，而是内部 profile 语义债：

- `STUDY_PROFILE` 还存在
- `STUDY_MODE_PROMPT` 还存在
- 但主产品路径已经迁移到 learning unit

如果后续记忆系统 v1 或别的入口误以为 `AgentMode.STUDY` 就是当前研习模式，会再次混淆。

### 修复建议

先不要重写 `STUDY_MODE_PROMPT`。优先处理问题 2，决定 learning unit 是否拥有独立 profile。之后再判断是否：

- 删除或兼容保留 `STUDY_PROFILE`
- 把 `STUDY` 明确标记为 legacy/internal
- 或把 `STUDY` 重新定义为 learning unit absorbing profile

---

## 四、低优先级设计卫生问题

### 问题 8：persona 的 `mode` 字段仍是 `home_mode` hint，但命名仍有轻微歧义

`PersonaProfile` 仍有字段 `mode`，运行时 `resolve_persona()` 会 `del mode`，说明 persona 不受 mode 限制。Web 层则把它输出为 `home_mode`。

当前代码注释已经写清楚“natural home mode, used only as UI hint”，所以风险比最初低。但字段名仍可能让维护者误以为 persona 运行时受 mode 约束。

建议后续 cleanup：

- `PersonaProfile.mode` 改名为 `home_mode`
- 或保留字段但在模型注释中进一步注明“not runtime restriction”

### 问题 9：`seed_text` 缺少后端空值校验

`CreateLearningUnitRequest.seed_text` 是普通 `str`，`LearningUnitStore.create()` 也直接写入 `UnitObjective(text=objective_text)`。前端 `sendMessage()` 会拦截空输入，但 API 层和 store 层仍允许空白目标。

### 影响

这违反 MVP 对学习卷的基本要求：学习模式至少要有一个工作目标。若外部调用或测试直接打 API，可能创建一个 objective 为空的学习卷，后续首轮模板只能用“待定”兜底，TEACH / metrics 也会出现低质量数据。

### 修复建议

- 在 `CreateLearningUnitRequest` 加 `min_length` 或 model validator，拒绝纯空白。
- 在 `LearningAgentSystem.create_learning_unit()` 或 `LearningUnitStore.create()` 再做一次防御式 strip 校验。
- 补 API 测试：`POST /learning-units {"seed_text": "   "}` 返回 400。

---

## 五、建议修复顺序

1. **先定问题 2 的产品层口径**：learning unit absorbing 到底走独立 learning profile、复用 Study profile，还是明确继续走 Chat profile。这个决定会影响记忆系统 v1 接入点。
2. **处理问题 5 和问题 6**：TEACH 入口不要过早跳过验收，两个按钮不要假装不同。
3. **验证问题 3**：persona 行为差异必须先隔离证明，否则 v1 的 persona 默认值自测不可靠。
4. **顺手修问题 9**：空目标校验改动小、收益明确。
5. **后续治理问题 4 / 7 / 8**：视觉分块、legacy Study 语义、persona 字段命名，适合在研习体验 P1 中处理。

---

## 六、记忆系统 v1 接入点的当前判断

在问题 2 未定口径前，不建议把“研习模式会读写记忆”作为 v1 验收标准。

当前更稳的 v1 范围应收敛为：

- profile 持久化 `preferred_persona_key`
- 当调用方未显式指定 persona 时，从 profile 读取默认 persona
- 复用现有 persona registry，不新增 prompt 注入机制
- v1 自测先验证 persona 默认值能进入 `build_turn_profile()`，再验证真实输出差异

如果 v1 要覆盖研习卷：

- 必须先解决 learning unit absorbing profile 的归属问题
- 否则 memory 开关、persona prompt、learning prompt 三者会继续分裂

---

## 七、本次核查过的验证点

- `pytest -q tests/test_alignment_policy.py tests/test_mode_layering.py tests/test_learning_unit_store.py tests/test_learning_unit_api.py tests/test_learning_unit_events.py tests/test_learning_unit_acceptance.py tests/test_learning_unit_metrics.py`
- `node --check web/static/app.js`
- `node --check web/static/learning-unit-ui.js`

上述测试说明当前 adaptive alignment、learning unit API、事件和基础模式分层是可跑通的；本文件记录的是现存设计缺口和体验风险，不代表当前测试失败。
