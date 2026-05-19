# 技术设计文档：会话持久化切换为 JSONL 单事实源

> **目标**：基于当前项目四层边界，对会话持久化做一次小重构设计，明确采用 append-only JSONL event log 作为唯一事实源，并将 Agent / LLM / UI 三类消息收敛为派生视图。
> **范围**：`LearningAgentSystem`、`SessionManager`、`FileStore`、Web API/前端消息展示链路、compaction 输入链路
> **文档日期**：2026-05-19

---

## 一、文档定位

本文档回答下面七个问题：

1. 当前 `session.json + session.jsonl` 的 snapshot/delta 模型，为什么不再适合作为后续主方案。
2. 为什么本项目应切换为“append-only JSONL 为唯一事实源”。
3. Agent message、LLM message、UI message 各自处于哪一层，彼此是什么关系。
4. full compact / slact / message filtering 在新模型下应该放在哪一层。
5. 前端消息为什么不需要成为新的事实源，但仍然需要可恢复。
6. 为什么当前 session 应从树形分支模型收口为纯线性消息序列。
7. 这次小重构的第一阶段应该改哪些边界，不该一次性做哪些事。

本文档是**技术设计文档**，用于明确方向、数据边界和重构步骤；不是实现说明，也不是逐文件 patch 清单。

---

## 二、当前实现与问题

### 2.1 当前持久化模型

当前项目的 session 持久化采用：

```text
sessions/{session_id}.json
    作为完整 snapshot

sessions/{session_id}.jsonl
    作为 snapshot 之后的 delta 日志
```

系统恢复时：

1. 先读取 `session.json`
2. 再顺序 replay `session.jsonl`
3. 还原内存中的 `LearningSession`

系统定期执行 `FileStore.compact_session()` 时：

1. 将当前内存 session 全量写回 `session.json`
2. 将对应 `session.jsonl` 清空

本次重构后的目标不是在旧文件上继续修补，而是新增：

```text
sessions/{session_id}.events.jsonl
    作为唯一事实源
```

旧的 `sessions/{session_id}.json` 与 `sessions/{session_id}.jsonl` 不再保留为新模型主路径。迁移完成后，旧文件可以删除；如需临时迁移读取，只能作为一次性导入来源，不能继续参与运行时正确性。

### 2.2 当前模型的问题

这个模型的核心问题不是“不能运行”，而是它与当前项目后续目标逐渐不兼容。

#### 问题 1：双表示源带来一致性风险

虽然设计意图是：

```text
完整状态 = snapshot + deltas
```

但在工程上，`session.json` 与 `session.jsonl` 实际构成了两个强相关持久化对象。只要“写新 snapshot”和“清空 delta”之间没有严格事务边界，就会出现 crash consistency 风险。

典型风险：

1. 新 snapshot 已写成功
2. 旧 JSONL 尚未清空
3. 进程崩溃
4. 恢复时先读新 snapshot，再 replay 旧 JSONL
5. 导致 append 记录重复应用

新模型必须通过 `seq`、`event_id` 与幂等 replay 解决重复应用问题，而不是继续依赖“写 snapshot 后清空 delta”。

#### 问题 2：持久化层 snapshot compaction 与上下文语义 compaction 混淆

当前仓库已经在推进两类不同概念：

- 持久化层的 snapshot/delta 合并
- 会话上下文层的 full compact / slact

这两者不是一回事：

- 前者压缩的是**存储表示**
- 后者压缩的是**发给模型的上下文视图**

如果两者继续绑定，很容易把“上下文压缩”误做成“删除/清空原始 transcript”。

#### 问题 3：不利于学习型 Agent 保留完整原始轨迹

当前项目不是纯 chat bot，而是学习场景的 agent。会话原始消息、工具轨迹、模式切换和中间控制事件，都是后续：

- recall
- review
- study
- compaction 审计
- observability

的重要原始事实。

一旦把 JSONL 当作可被 checkpoint 吸收后清空的“临时增量层”，原始轨迹的定位就不稳定。

#### 问题 4：前端 / Agent / LLM 三种消息形态尚未显式分层

当前 session 模型里的 `entries` 同时承担了多种职责：

- 持久化事实
- agent 运行时上下文
- LLM 输入候选
- 前端展示消息来源

这会导致后续任何一个方向的演进都容易互相污染：

- 引入 compaction summary 时，不知道它是事实、缓存还是展示消息
- 引入 agent-only 控制消息时，不知道是否该直接暴露给前端
- 引入 UI 折叠消息时，不知道是否要写回 session 主状态

#### 问题 5：旧树形 session 分支模型不再作为目标能力

当前 `LearningSession` 仍保留 `root_entry_id`、`current_leaf_id`、`SessionEntry.parent_id` 和 `fork_point` 等树形会话字段。但后续产品方向不再采用旧分支探索模型，而是收口为纯线性消息序列。

如果继续保留“当前 leaf path”作为主语义，会带来额外复杂度：

- event log 投影需要同时解释分支、leaf、parent 链。
- full compact / slact 的 source range 需要区分当前 branch，anchor 也可能误用于其他 branch。
- UI 恢复需要解释 fork_point 展示语义。
- LLMInputView 构建容易重新依赖 `get_path_to_leaf()`，让线性 event log 的收益变弱。

因此本次重构应明确：新模型下 session 主消息流是 append-only 线性序列，旧分支字段只作为迁移兼容数据，不再作为主路径设计约束。

---

## 三、设计目标

### 3.1 本次重构要达到的目标

1. 明确 append-only JSONL event log 是会话唯一事实源。
2. 明确 Agent / LLM / UI 三种消息都属于派生视图，不再并列为事实源。
3. 明确 compaction 不删除原始 transcript，只新增压缩语义。
4. 明确前端消息“可恢复”但不是“独立真相副本”。
5. 明确 session 主消息模型改为纯线性序列，不再采用旧树形分支模型。
6. 明确四层边界下谁负责投影、谁负责过滤、谁负责展示。

### 3.2 本次重构不做的事情

1. 不在第一阶段引入外部 memory runtime。
2. 不在第一阶段实现完整 event sourcing 框架。
3. 不在第一阶段重写全部 Web/前端逻辑。
4. 不在第一阶段删除所有现有 session 结构。
5. 不在第一阶段清理与 session 持久化无关的临时缓存。
6. 不在第一阶段实现新的分支、回溯或多 leaf 能力。

注意：这里的“不删除所有现有 session 结构”指代码迁移可分阶段进行，不代表保留旧 `session.json + session.jsonl` 作为运行时事实源。目标态中旧持久化文件会被删除或停用。

---

## 四、核心结论

### 4.1 单事实源结论

后续会话持久化主方案采用：

```text
session.events.jsonl
    = 唯一事实源
```

其原则是：

- append-only
- 不原地删除历史事件
- 不依赖“写 snapshot 后清空日志”的合并模式维持正确性
- 文件路径固定为 `sessions/{session_id}.events.jsonl`
- 旧 `sessions/{session_id}.json` 与 `sessions/{session_id}.jsonl` 不再作为 checkpoint/cache 保留

旧 `FileStore.compact_session()` 不保留。后续项目中只有两类与 compact 相关的能力：

- `full compact`
- `slact`

它们都属于上下文消费视图压缩，不承担持久化层 snapshot/delta 合并职责。

### 4.2 三类派生视图结论

在单事实源之上，系统保留三类派生视图：

```text
JSONL events
    -> Agent message / Agent snapshot
    -> LLM message view
    -> UI message view
```

这三类视图的共同原则：

- 可以缓存
- 可以重建
- 不成为新的并列事实源

### 4.3 compaction 结论

在新模型下：

- full compact / slact 的输入来自 JSONL event log 或其投影
- compact 结果以“新增语义”的方式进入系统
- 原始 transcript 不因为 compact 被删除或清空

换句话说：

```text
compact = 生成新的可消费摘要视图
不是
compact = 删除原始历史
```

### 4.4 线性 session 结论

后续 session 主模型采用纯线性消息序列：

```text
session.events.jsonl
    -> ordered events by seq
    -> ordered Agent messages
    -> LLM/UI views
```

原则：

- 新事件只追加到序列尾部。
- 不再创建 `fork_point` 作为产品主能力。
- 不再通过 `parent_id -> current_leaf_id` 计算当前会话路径。
- compact source range、slact pivot、UI message order 都以线性 `seq` / `entry_id` 顺序为准。

旧字段处理：

- `root_entry_id`、`current_leaf_id`、`parent_id`、`EntryType.FORK_POINT` 在第一阶段可以保留为兼容字段。
- 新写入事件不再依赖这些字段表达主路径。
- replay 旧数据时，可以按旧 `get_path_to_leaf()` 先投影成线性历史，再暴露为新模型的线性视图。
- 待迁移完成后，再逐步移除或降级这些字段。

---

## 五、四层职责收口

### 5.1 Infrastructure：只负责记录事实

Infrastructure / Persistence 层只负责：

- 追加写入 session 事件
- 读取指定 session 的事件流
- 提供按 cursor / id / sequence 的读取能力

它不负责：

- 判定哪些事件给 LLM 看
- 判定哪些事件给 UI 看
- 决定 compact 哪一段

### 5.2 Product/Application：负责投影与产品语义

Product 层负责：

- 从事件流构建 `AgentSnapshot`
- 维护 mode、ask_state、compaction metadata 等产品状态
- 将旧树形 session 数据投影为线性消息序列
- 从 `AgentSnapshot` 生成 `LLMInputView`
- 从 `AgentSnapshot` 生成 `UIViewMessage[]`
- 决定是否进入 Ask / Chat / Study 的不同视图规则

它是“产品如何理解这批事实”的收口点。

### 5.3 Agent Runtime：只消费 LLM 输入视图

Runtime 只负责：

- 消费已经构造好的 `LLMInputView`
- 执行单轮推理、工具调用、重试、降级
- 发出运行时事件

它不负责：

- 维护前端展示消息真相
- 决定哪些内部事件可见给 UI
- 自己编排产品级 compaction 策略

### 5.4 Interface：只负责 UI 展示适配

Interface 层负责：

- 请求解析
- 调用 Product facade
- 返回 `UIViewMessage[]`
- SSE 流式展示时把运行时事件映射成前端可渲染的消息块

它不持有服务端会话权威状态。

---

## 六、四种消息形态

这次重构之后，项目里应明确有四种“消息形态”，但不是四个同等级持久化存储。

### 6.1 形态 1：Event Log

`session.events.jsonl` 记录真实发生过的事件。

建议最小字段：

```json
{
  "seq": 123,
  "event_id": "evt-...",
  "session_id": "sess-001",
  "ts": "2026-05-19T12:00:00Z",
  "type": "message.append",
  "payload": {},
  "visibility": "agent"
}
```

这里的 `visibility` 不是前端渲染指令，而是帮助上层构建不同视图的基础标签。

### 6.1.1 幂等与恢复

事件流必须支持幂等 replay：

- `seq` 在单 session 内严格单调递增。
- `event_id` 全局唯一，重复读取时用于去重。
- replay 时如果发现已应用的 `event_id`，必须跳过。
- 如果发现 `seq` 缺口、倒退或重复但 payload 不一致，必须记录 corrupt event，并停止或进入恢复流程。
- 每个派生视图都只能由 event log 重建，不能靠修改 snapshot 绕过 event log。

由于目标态不保留旧 checkpoint/cache，恢复流程应从 `sessions/{session_id}.events.jsonl` 完整 replay 得到 `AgentSnapshot`。如果未来为了性能重新引入 checkpoint，也只能作为可丢弃缓存，且必须记录 `checkpoint_seq`，不能取代 event log。

### 6.2 形态 2：Agent Message / Agent Snapshot

这是 Product 层投影，不是底层事实。

它回答的问题是：

```text
站在当前产品语义下，系统认为这个 session 现在处于什么状态
```

其内容不只包括给 LLM 看的消息，还包括：

- 当前 mode
- ask_state
- compact summary
- tool 轨迹
- 产品控制消息
- 供后续 recall / review 使用的结构

### 6.3 形态 3：LLM Message View

这是某一轮请求前，从 `AgentSnapshot` 过滤出的模型输入视图。

它回答的问题是：

```text
这一轮真正发送给模型的上下文是什么
```

它必须满足：

- 只保留本轮需要的 system / user / assistant / tool 消息
- 遵守 token budget
- 允许使用 compact summary 替代部分长历史
- 不自动暴露 agent-only 内部状态

### 6.4 形态 4：UI Message View

这是展示给前端的视图。

它回答的问题是：

```text
用户在聊天界面里应该看到什么
```

它可能会：

- 隐藏内部事件
- 折叠工具调用细节
- 把 compaction summary 转为“历史已折叠”提示
- 把角色和元信息转换成前端文案

---

## 七、为什么前端消息不单独作为事实源

前端消息当然需要“可恢复”，否则刷新页面后会丢会话内容。

但“可恢复”不等于“必须成为新的持久化真相副本”。

推荐方案是：

```text
后端持久化 Event Log
    ->
后端按需重建 UI Message View
    ->
前端重新拉取并展示
```

这样做的好处：

1. UI 展示规则变更后，旧会话也能按新规则重新生成。
2. 避免维护第二份长期 UI 真相副本。
3. 前端只关心展示，不反向定义会话事实。

如果未来为了性能需要，也可以落一份 `UIViewMessage` cache，但它只能是：

- 物化视图
- 缓存
- 可丢失

不能反过来成为事实源。

---

## 八、事件类型建议

第一阶段无需把所有领域事件都建完，但至少要覆盖下面几类。

### 8.1 会话消息事件

- `message.append`
- `message.patch`
- `message_end`

流式输出期间，SSE chunk 只用于前端临时展示和 observability，不作为最终会话消息事实。每次流式传输正常结束后，Runtime / Product 边界必须发出 `message_end` 事件，基于该事件写入：

- session event log
- AgentSnapshot
- LLMInputView 后续可见消息
- UIViewMessage 后续可恢复消息

如果流式失败或中断，未完成 assistant 消息不能作为完整 `message_end` 写入。应写入明确失败事件，例如：

- `message.stream_failed`
- `message.interrupted`

失败事件可供 UI 展示错误状态和 observability 诊断，但默认不进入后续 LLMInputView，除非 Product 层显式转换为可消费上下文。

### 8.2 产品状态事件

- `session.mode_changed`
- `session.ask_state_updated`
- `session.title_updated`
- `session.status_changed`

### 8.3 工具轨迹事件

- `tool.call_started`
- `tool.call_completed`
- `tool.call_failed`

敏感 tool output 不写入 session event log。session event log 中只保留工具调用的结构化摘要、状态、必要引用和可展示安全信息。完整敏感输出如需诊断，只能写入 observability 关联文件，并受 session 删除流程统一清理。

### 8.4 compaction 事件

- `compaction.summary_added`
- `compaction.anchor_moved`
- `compaction.rebase_completed`

关键原则：

- compaction 事件是新增语义
- 不是删除原始消息
- `compaction.summary_added` 不是普通 assistant 消息
- 默认不进入 UI 聊天气泡，只可由 UI view 显示为“历史已折叠”等提示
- 默认可进入 LLMInputView，作为被压缩历史的替代上下文
- payload 必须包含 summary 文件路径、summary hash、source range、anchor seq、template version、compact mode/scope
- compact 失败只能写 failure / metric / observability 事件，不得推进 anchor

### 8.4.1 Compaction 事务边界

一次 full compact / slact 成功提交必须满足：

1. summary 生成成功。
2. summary 质量校验通过。
3. summary 文件写入成功。
4. `compaction.summary_added` 写入 event log 成功。
5. compact metadata / anchor 推进成功。

任一步失败，都不能推进 anchor，也不能让 Runtime 使用半成品 summary。失败应进入主代理已有重试、熔断和降级机制。

### 8.5 线性消息事件

新模型下不再新增分支类事件。第一阶段明确不设计：

- `branch.created`
- `branch.switched`
- `fork_point.created`
- `leaf.changed`

如果旧数据中存在 fork 相关 entry，迁移投影时处理为兼容历史；新事件流不再以分支作为主语义。

---

## 九、Agent Snapshot 设计原则

### 9.1 作用

`AgentSnapshot` 是 Product 层的会话投影，建议作为内存态主工作对象。

### 9.2 应包含的内容

- session 基本信息
- mode 与 mode metadata
- ask_state
- 逻辑消息序列
- compact summary 引用或摘要块
- 工具轨迹引用
- 当前 UI/LLM 投影所需的辅助索引

其中“逻辑消息序列”在新模型下必须是线性序列。它可以由旧树形数据兼容投影而来，但投影完成后，LLM/UI/compact 都不再直接消费 branch path。

### 9.3 不应成为事实源

`AgentSnapshot` 可以：

- 在内存里长期存在
- 由 event log 随时重建

但它不能替代 `JSONL events` 的地位。否则会重新回到“双事实源”问题。

---

## 十、LLM 过滤链路

### 10.1 过滤发生在哪里

过滤规则属于产品语义的一部分，应收口在 Product/Application 层，而不是散落在 Runtime hook 里。

更准确的链路应是：

```text
events
    -> AgentSnapshot
    -> build_llm_input_view(...)
    -> runtime.run_turn(...)
```

Runtime hook 可以做最后一层轻量适配，但不应承担“哪些消息给模型看”的主判断。

### 10.2 过滤原则

第一阶段建议遵守：

1. agent-only 事件默认不进入 LLM。
2. UI-only 展示文案不反向进入 LLM。
3. compact summary 是 LLM 输入中的可选替代块，不是事实层消息删除。
4. Ask / Chat / Study 三种模式可以使用不同的过滤策略。
5. LLM 输入按线性消息序列过滤，不再按 branch leaf path 过滤。
6. assistant `tool_calls` 与对应 `tool` result 必须成组保留或成组压缩，禁止在配对中间插入 compact summary 或 UI-only 消息。
7. agent-only / UI-only / observability-only 事件默认不进入 LLMInputView。

---

## 十一、UI 适配链路

### 11.1 前端需要的不是原始事件流

前端真正关心的是：

- 聊天气泡
- 工具执行状态
- 错误提示
- 历史折叠提示
- 当前模式和角色

因此不应该直接把底层事件流原样暴露给前端聊天区。

### 11.2 建议的 UI 视图构建方式

```text
events
    -> AgentSnapshot
    -> build_ui_messages(...)
    -> REST/SSE 返回前端
```

这里的 `build_ui_messages(...)` 需要负责：

- 合并 stream chunk 为稳定 assistant 消息
- 折叠 tool 细节
- 决定 compaction summary 是否显示以及如何显示
- 屏蔽内部控制事件

---

## 十二、对现有实现的第一阶段调整建议

### 12.1 持久化层

第一阶段建议：

1. 删除或停用 `compact_session()`，不再保留 snapshot/delta 合并能力。
2. 新增稳定的 append-only event log 接口。
3. 新文件使用 `sessions/{session_id}.events.jsonl`。
4. 旧 `sessions/{session_id}.json` 与 `sessions/{session_id}.jsonl` 只允许作为一次性迁移输入，迁移完成后删除。
5. 新写入的 message event 使用线性 `seq` 排序，不再写入 fork/fork_point 语义。
6. 写入必须保证 `event_id` 唯一、`seq` 单调，并支持 replay 幂等。

### 12.2 Product 层

第一阶段建议：

1. 新增 `AgentSnapshot` 或同等投影结构。
2. 将 mode、ask_state、compaction metadata 的重建逻辑显式放到投影阶段。
3. 将旧 `LearningSession.entries` 的树形路径兼容投影为线性消息序列。
4. 增加两个 facade：
   - `build_llm_input_view(session_id, ...)`
   - `build_ui_messages(session_id, ...)`

### 12.3 Runtime 层

第一阶段建议：

1. 逐步改为只消费 `LLMInputView`。
2. 保留 runtime 事件发布能力。
3. 不在 runtime 中直接处理 UI 可见性或产品级压缩边界。

### 12.4 Interface 层

第一阶段建议：

1. REST 获取消息列表时，返回 `UI message` 视图。
2. SSE 流式消息结束后，由 Product 层沉淀事件，再通过 UI 映射回前端。
3. 前端不再假定后端 session.entries 就是最终聊天气泡数组。

---

## 十三、与 full compact / slact 的关系

这次小重构不是替代已有 full compact 设计，而是为其提供更稳定的事实源与视图边界。

在新模型下：

- full compact 的 source 来自 event log 投影
- compact 输出进入 `AgentSnapshot` / `LLMInputView`
- UI 是否展示 compact 结果由 UI 视图层决定

因此：

```text
Event Log
    是“发生过什么”

Full Compact
    是“长历史如何变成模型可消费摘要”
```

二者不能混用。

---

## 十四、迁移策略

### 14.1 阶段 1：引入新语义，不立刻删除旧结构

目标：

- 先把事实源、投影、UI/LLM 视图概念立住
- 保持系统仍可运行

建议动作：

1. 保留旧 `LearningSession.entries` 读取逻辑作为一次性迁移兼容层。
2. 同时增加 event log 写入与 replay 能力。
3. 让 compaction 先切到“原始 JSONL 不清空”的原则。
4. 停止新增 fork_point；旧 fork 数据只作为历史兼容投影。
5. 将旧 `session.json + session.jsonl` 导入为 `session.events.jsonl` 后，删除旧文件。

### 14.2 阶段 2：收口投影入口

目标：

- 所有 LLM 输入都从 `build_llm_input_view(...)` 进入 runtime
- 所有前端历史消息都从 `build_ui_messages(...)` 输出

### 14.3 阶段 3：删除旧 snapshot/delta 主路径

目标：

- `session.json` 不再作为会话事实主基线
- `session.jsonl` 不再作为 delta 层
- `compact_session()` 不再存在
- 不保留 checkpoint/cache 作为第一版目标态
- `root_entry_id`、`current_leaf_id`、`parent_id` 不再参与主路径构建

---

## 十五、风险与防御

### 15.1 风险：事件流越来越长

这是单事实源天然代价。

对策：

- 引入 cursor 加速 replay
- 按 session 做分段或归档
- 暂不引入 checkpoint/cache；如果未来引入，也只能是可丢弃性能优化，不能替代 event log

### 15.2 风险：视图规则散落

如果 LLM 过滤、UI 显示、compact 替代逻辑分散在多个 hook 中，边界会重新混乱。

对策：

- Product 层统一提供投影与过滤入口

### 15.3 风险：AgentSnapshot 演变为第二事实源

如果后续直接修改 snapshot 并落盘，再跳过 event log，就会回到旧问题。

对策：

- 明确 snapshot 只能重建、不能越级替代事实源

### 15.4 风险：旧分支语义残留

如果实现继续在部分路径使用 `get_path_to_leaf()`，而另一部分使用线性 event log，会重新产生两套消息顺序。

对策：

- 第一阶段明确一个兼容投影入口，把旧树形 session 投影成线性历史。
- 新写入路径不再创建 fork/fork_point。
- LLM/UI/compact 全部消费线性投影结果。

### 15.5 风险：敏感工具输出进入长期事实源

如果完整 tool output 直接进入 session event log，会增加长期隐私与删除风险。

对策：

- session event log 只保存安全摘要、状态、引用和必要元数据。
- 敏感 tool output 仅进入 observability 关联文件。
- 删除 session 时，必须同时删除 event log、compact summary、metadata、可能存在的临时 checkpoint/cache、observability 关联文件。

### 15.6 风险：session event log 与 observability log 混淆

session event log 记录可恢复会话事实；observability 记录诊断事实。二者不能互相替代。

对策：

- 影响会话继续的 user / assistant / tool 摘要 / mode / compact 事件进入 session event log。
- span、token chunk、内部 retry、完整敏感 tool output 进入 observability。
- Product 层决定哪些 observability 信息需要转译为安全的 session event。

---

## 十六、验收标准

完成这次小重构设计后，后续实现至少要满足：

1. session 原始事件流是 `sessions/{session_id}.events.jsonl`，append-only，不因 compact 被清空。
2. 前端刷新后能从后端恢复聊天消息，但前端展示消息不是新的事实源。
3. runtime 消费的是显式构建的 `LLMInputView`，而不是直接扫描持久化原始事件。
4. UI 展示链路可以隐藏内部事件、折叠工具消息，而不污染事实层。
5. full compact 只改变消费视图，不删除原始 transcript。
6. 新 session 消息按线性 `seq` 顺序恢复，不再依赖 branch/current_leaf 路径。
7. 旧分支数据通过兼容投影转为线性视图，新写入路径不再产生 fork_point。
8. 旧 `session.json` / `session.jsonl` 在迁移完成后删除，`compact_session()` 不再保留。
9. event replay 基于 `seq` / `event_id` 幂等恢复。
10. 正常流式结束后以 `message_end` 作为最终消息落盘边界；失败或中断消息不作为完整 assistant 消息进入 LLMInputView。
11. 敏感 tool output 不进入 session event log，删除 session 时同步删除 event log、compact summary 和 observability 关联文件。

---

## 十七、一句话总结

本次小重构的核心不是“把 JSON 换成 JSONL”这么简单，而是把会话系统从：

```text
snapshot + delta + 混合消息职责
```

收口成：

```text
append-only event log
    + Product 层投影
    + Runtime 输入视图
    + UI 展示视图
```

这能同时解决：

- 持久化正确性
- compaction 语义边界
- 前端消息恢复
- Agent / LLM / UI 三类消息职责混淆

并且与当前项目“四层收敛、Product 编排、Runtime 最小消费”的整体方向一致。
