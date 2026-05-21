# Full Compact 与 Slact 压缩模板适配技术设计

> 版本：v1.0
> 日期：2026-05-19
> 输入材料：
> - `/Users/roseannk/claude-code-analysis/analysis/12-full-compact-prompt-templates.md`
> - `docs/design/design-compaction-strategy-adaptation-from-cc.md`
> - 当前项目四层架构与 compaction 现状
>
> 目标：在保留本项目学习 Agent 定位的前提下，吸收 Claude Code Full Compact 的优秀设计，明确 `auto compact`、用户触发的 `slact` 压缩、增量压缩与 rebase 压缩的实现边界、prompt 模板和消息重建规则。

---

## 0. 2026-05-21 讨论修订

本文档不是不可变标准。2026-05-21 讨论后，对初版设计做如下收口：

1. `sessions/{session_id}.events.jsonl` 是会话级唯一事实源；compact summary 首先应作为 `compaction.summary_added` 事件写回 JSONL。
2. 独立 summary 文件最多是 artifact/cache，用于大摘要或审计引用，不是 compact 的权威事实源。
3. `CompactMetadata` 不是事实源，而是从 JSONL replay 得到的派生索引或加速缓存；冲突时必须以 JSONL 为准。
4. Agent Runtime 不直接 append session 事实。Runtime 发布 agent 运行事件，Product/Application 接收并转译为 session event。
5. 压缩边界不能直接基于 raw JSONL 行号；必须先投影出 `CompactionSourceView`，再按 safe compact units 切分。
6. 当前 turn 的 user event 可以先写入 JSONL，但不进入本轮 compact source，必须进入 retained context。
7. 大型工具结果采用 preview + artifact reference；完整结果保存为本地 artifact，LLM 需要时通过切片读取。
8. 所有 Runtime / Product / Session 事件必须通过 `turn_id`、`run_id`、`attempt_id`、`user_event_id` 关联，避免重试、并发或中断污染会话事实。
9. Auto compact 的触发点固定在 Product 层写入当前 user event 之后、调用 Runtime 之前；本轮 compact source 必须排除当前 user。
10. 多次 compact 后，LLMInputView 只消费最新有效 compact summary；旧 summary 仍作为 JSONL 审计事实保留。
11. Runtime 到 Product 的事件投递按 at-least-once 设计，Product 写 SessionEvent 必须幂等。
12. `/slact` 或 UI 压缩操作是 ProductCommand，不默认等同于一条用户聊天消息。
13. Compact summary 不自动进入长期 memory；memory 抽取必须走独立领域流程，并引用原始 session event。

这些结论优先于本文档初版中“summary 文件落盘后再引用”的表述。后续实现与测试应按本节修订方向推进。

---

## 1. 核心结论

本项目不直接照搬 Claude Code 的所有触发形态，而采用下面的适配策略：

```text
Auto Compact:
    只采用“压缩旧消息 + 保留最近消息”的自动压缩路径。
    摘要放在 retained recent messages 之前，作为历史前言。

Slact:
    承接 Claude Code 的三类用户触发模板能力。
    用户可以选择 full / from / up_to 类似语义的显式压缩。

Summary Prompt:
    采用 Claude Code 的 analysis -> summary 双阶段输出。
    保存和注入上下文时只保留 summary，剥离 analysis。

Summary Structure:
    采用九章节结构，但章节内容从 coding agent 语境改造为学习 Agent 语境。

Incremental / Rebase:
    保留本项目已有“首次全量、后续增量、定期 rebase”的方向。
    增量摘要不能简单追加到旧 summary；rebase 需要生成新的 canonical summary。

Compression Source:
    压缩输入以持久化 JSONL transcript 为事实源。
    内存中的 message 数组只作为当前执行视图，不作为 full compact 的权威扫描来源。

Summary Persistence:
    compact summary 作为 compaction.summary_added 事件进入会话 JSONL。
    独立 summary 文件仅作为可选 artifact/cache，不作为权威事实源。

Event-Driven Boundary:
    Agent Runtime 发布 agent.* 运行事件。
    Product/Application 将确认后的运行事件转写为 session.* / message_* / tool.* / compaction.* 事实事件。
```

本设计中的 `slact` 指后续计划加入的用户可控压缩能力。它属于 Product/Application 层的产品功能，不属于 Agent Runtime 自主决策。

---

## 2. 设计原则

### 2.1 Auto Compact 与 Slact 分流

`auto compact` 和 `slact` 要解决的问题不同：

- `auto compact` 是系统自救机制，目标是不中断当前任务，尽量无感地让会话继续。
- `slact` 是用户显式整理上下文的能力，目标是让用户控制压缩方向、范围和保留策略。

因此二者不共用同一个触发语义：

| 场景 | 触发方 | 压缩策略 | 摘要位置 | 用户可控性 |
|---|---|---|---|---|
| Auto Compact | 系统 | 压缩旧消息，保留最近消息 | recent messages 之前 | 低 |
| Slact Full | 用户 | 压缩全部历史 | 替换原历史 | 高 |
| Slact From | 用户 | 保留早期，压缩某点之后 | 早期消息之后 | 高 |
| Slact Up To | 用户 | 压缩某点之前，保留近期 | 近期消息之前 | 高 |

### 2.2 Summary 是上下文替代物，不是附加说明

Full compact 成功后，发送给 provider 的上下文必须重建为：

```text
system prompt
compact summary message
preserved messages
current turn user input
```

禁止出现：

```text
system prompt
compact summary message
full original history
current turn user input
```

否则 compact 不会降低 token，还会制造摘要与原文并存的语义冲突。

compact summary 本身不应只存在于外部文件。成功 compact 后必须追加会话事实事件：

```text
compaction.summary_added {
    compact_id,
    mode,
    scope,
    schema_version,
    summary_text or summary_artifact_ref,
    summary_hash,
    source_event_hash,
    source_event_range,
    source_event_ids,
    source_entry_ids,
    retained_entry_ids,
    cut_point_entry_id,
    anchor_event_seq,
    current_user_event_id,
    previous_compact_event_id,
    template_version,
    validation_status
}
```

如果 summary 很长，可以把正文写入 artifact，再在事件中保存 `summary_artifact_ref` 与 hash；但 replay 时仍以 `compaction.summary_added` 事件作为权威入口。

### 2.3 反漂移信息必须强制保留

摘要不是普通概括，而是长会话继续执行的状态移交。因此必须保留：

- 用户的原始意图和重要纠正。
- 最近一次明确请求的直接引用。
- 当前学习目标和学习阶段。
- 最近正在处理的问题、卡点和下一步。
- 已确认事实、约束、偏好和不要重复犯的错误。

其中“直接引用用户最近请求”是硬要求，用于降低 summary 改写导致的意图漂移。

### 2.4 当前阶段由主代理执行摘要

现阶段不引入子代理、fork agent 或后台 summarizer。`auto compact` 和 `slact` 的摘要生成都由主代理所在的主链路执行，并复用当前主代理已有的重试、熔断和降级机制。

主链摘要执行必须遵守：

- 不读取文件。
- 不调用搜索。
- 不运行命令。
- 不直接修改 session；只能返回 summary 结果，由 Product 层提交 `compaction.summary_added` 事件。
- 只基于调用方传入的 transcript / retained context / metadata 生成文本摘要。

这条规则复用 Claude Code 的 no-tools 思路，但在本项目中更强调层级边界：摘要执行只处理 Product/Application 层准备好的输入，不额外探索，不扩大上下文来源。

后续若要引入子代理，只能作为替换 `SummaryExecutor` port 的可选实现，并且必须先重新评估权限、重试、熔断、观测和状态隔离。本设计的当前实施范围不包含子代理。

### 2.5 JSONL Transcript 是压缩事实源

当前项目后续每个 session 都有独立的 `sessions/{session_id}.events.jsonl` event log，作为唯一事实源。Full compact 的 source selection 必须从该 JSONL event log 读取和 replay，而不是直接依赖内存中的 `session.entries`。

原因：

- JSONL 是会话开始后持续追加的持久化事实源，更适合作为 compact 审计依据。
- 内存 message 数组是运行时视图，可能已经被发送侧 micro compact、runtime 过滤或临时上下文构建逻辑影响。
- 增量压缩的 anchor 应该指向 JSONL 中已经压缩到的位置，后续只扫描 anchor 之后的新 delta。
- rebase 可以基于 existing summary 加 JSONL 新增区间生成 canonical summary，而不必重读全部内存历史。
- 正常流式响应只有在 `message_end` 事件落盘后，才成为 full compact / slact 可压缩的完整 assistant 消息。
- 流式失败或中断消息默认不进入 compact source，除非 Product 层显式转译为安全、完整、可恢复的事件。

因此文档中提到的 `source_entry_ids`、`cut_point_entry_id`、`compact_anchor_entry_id` 都应能映射回 JSONL event log 的稳定位置。第一版可以用 entry id 作为逻辑 cursor；更稳妥的实现还应记录 event `seq`、JSONL line number、byte offset 或 delta sequence。

需要特别区分两种 compaction：

- 旧 `FileStore.compact_session()` 这类 snapshot/delta 合并不再保留。
- 本文讨论的 `Full Compact` / `Slact`，是会话上下文压缩，不应删除或清空原始 JSONL transcript。

### 2.6 Agent 事件与 Session 事实事件分离

本项目采用事件驱动主链，但需要区分四类事件 / 命令：

```text
ProductCommand:
    submit_user_message
    request_slact_compact
    cancel_turn

AgentRuntimeEvent:
    agent.turn_started
    agent.message_delta
    agent.message_end
    agent.tool_call_requested
    agent.tool_call_completed
    agent.turn_failed

SessionEvent:
    message.user_appended
    message_end
    tool.call_completed
    tool.call_failed
    compaction.summary_added
    session.mode_changed

ProductDerivedEvent:
    session.event_appended
    llm_view.invalidated
    ui_messages.updated
    compaction.required
    compaction.completed
```

`ProductCommand` 表达用户或接口意图；`AgentRuntimeEvent` 表达 Runtime 过程中发生了什么；`SessionEvent` 是写入 JSONL 的会话事实；`ProductDerivedEvent` 是给 UI、compact coordinator、observability 等订阅者使用的派生通知。

其中最容易混淆的是 `agent.message_end` 与 `message_end`：

- `agent.message_end` 是 Runtime 发出的候选完成事件，不是事实源。
- `message_end` 是 Product 层校验并写入 JSONL 的事实事件。

因此 `agent.*` 永远不能直接进入 `CompactionSourceView`。只有 Product 转写后的 `message_*`、`tool.*`、`compaction.*`、`session.*` 等 SessionEvent 才能被 replay 成 LLM / UI / compact 视图。

事件必须携带稳定关联字段：

```text
turn_id:
    用户发起的一轮产品级请求。

run_id:
    Runtime 对该 turn 的一次执行实例。

attempt_id:
    同一 run 内的一次 provider/tool 重试尝试。

user_event_id:
    本轮原始用户消息对应的 message.user_appended event_id。
```

SessionEvent 建议统一使用事件信封，至少包含：

```text
event_id:
    事件全局唯一 id。

session_id:
    会话 id。

seq:
    session 内单调递增序号，由 append SessionEvent 的存储层或 Product 层分配。

event_type:
    事件类型。

schema_version:
    该事件 payload 的 schema 版本。

created_at:
    事件写入时间。

actor_layer:
    product / runtime / infrastructure 等来源层标记。

parent_event_id:
    可选，用于记录由哪个事件转写或派生而来。

source_runtime_event_id:
    可选，用于记录对应的 agent.* 事件 id。

dedupe_key:
    可选，Product 幂等写入 SessionEvent 时使用。
```

`seq` 只能由会话事实写入点分配，Runtime 不能提前声明事实序号。Runtime 可以声明 `agent_event_id`、`turn_id`、`run_id`、`attempt_id`，但这些仍不是 JSONL 顺序。

Runtime 只声明“运行中发生了什么”，Product/Application 决定“哪些事件成为会话事实”。例如 assistant 完整输出由 Runtime 发布 `agent.message_end`，Product 层接收后校验 `turn_id/session_id`，再写入 `message_end`。

用户输入建议由 Product 层在调用 Runtime 前先写入 `message.user_appended`，并生成 `turn_id`。这样即使 Runtime 启动失败，本轮用户请求仍可恢复。该 user event 不进入本轮 compact source，但必须进入 retained context。

Runtime 不应直接调用 session append 或写 JSONL。推荐链路是：

```text
Interface/Product receives user input
    -> Product writes message.user_appended
    -> Product builds runtime input and calls Runtime
    -> Runtime publishes agent.* events
    -> Product consumes agent.* events
    -> Product appends confirmed SessionEvent records
    -> Product publishes derived product events for UI / compaction / observability
```

这条边界的核心是：Agent Runtime 事件不是会话事实，只有 Product 层确认并写入 `sessions/{session_id}.events.jsonl` 后，才成为后续 LLMInputView、UIViewMessage、compact source 的权威来源。

对于 Ask / Mode 等 Product 语义，必须保留“用户原话”和“Runtime 输入”之间的差异：

- 原始用户输入写入 `message.user_appended`。
- Product 层确认或改写后的 runtime input 不应冒充用户原话。
- 如需记录 resolved input，应写入独立事件，例如 `turn.runtime_input_resolved`，或在 metadata 中记录 `resolved_from_event_id`。

### 2.6.1 Auto Compact 触发顺序

Auto compact 的触发顺序固定为：

```text
Product receives user input
    -> append message.user_appended(turn_id, content)
    -> resolve mode / ask / runtime input
    -> build CompactionSourceView using events before current user seq
    -> if needed, append compaction.summary_added
    -> build LLMInputView with current user retained
    -> call Runtime
```

不建议在 Runtime 执行过程中临时触发会话级 full compact。Runtime 执行中只能做当前请求内的降级和恢复；会话级 compact 由 Product 层在调用 Runtime 前完成。

### 2.7 Large Tool Result Artifact 策略

大型工具结果不应通过 emergency truncation 改写会话事实。建议工具执行完成后写入：

```text
tool.call_completed {
    tool_id,
    tool_call_id,
    result_preview,
    artifact_ref,
    artifact_kind: "full_tool_result",
    full_result_available: true,
    is_error
}
```

LLM 可读消息只包含 preview、关键截断说明和 `artifact_ref`。完整结果保存到本地 artifact；如果后续需要原文，模型应通过专门的 artifact slice 工具按范围读取。

compact 时：

- artifact 本体不被压缩或删除。
- 旧 tool result preview 可以被 summary 替代。
- summary 必须保留 `artifact_ref`、工具用途、关键结论和是否仍需后续读取。
- 最近正在使用的 artifact 所在 safe unit 可以 retained。

Artifact 生命周期要求：

- artifact 路径必须由系统生成，避免泄露用户真实文件名、敏感路径或 provider 内部细节。
- session 删除时，必须同步删除该 session 关联的 tool result artifact、summary artifact 和敏感 observability 文件。
- artifact 应区分 sensitive / non-sensitive；sensitive artifact 不进入 compact source、UI view 或普通 LLMInputView。
- artifact slice 工具必须限制单次读取大小、记录审计事件，并返回 preview + range metadata。
- summary 可以保留 `artifact_ref`，但不鼓励模型默认读取完整 artifact；只有当前任务确实需要原文时，才通过受限 slice 工具读取。

`ContextLengthError` 的 emergency truncation 不应长期通过 patch 原始 session fact 解决。目标替代方案是：将过大的 tool result artifact 化，并在 LLMInputView 中替换为 preview + `artifact_ref`。

### 2.8 CompactionSourceView 与 Safe Compact Units

raw JSONL 是全局事实流，不只包含 LLM 可读消息，因此不能直接按 JSONL 行号或事件类型粗暴切分。压缩 source selection 应采用三层模型：

```text
Raw Session Event Log
    -> CompactionSourceView
    -> Safe Compact Units
```

`CompactionSourceView` 由 Product/Application 层从 JSONL replay 得到，负责过滤和转译：

- `message.user_appended`、完整 `message_end`、完整 `tool.call_completed` / `tool.call_failed` 可进入候选视图。
- `message.stream_failed`、`message.interrupted` 默认不进入 source，只能由 Product 显式转译为安全上下文说明。
- `agent.*` runtime 事件不进入 source，除非已被 Product 转写为 session fact。
- UI-only、observability-only、内部控制事件默认不进入 source。
- sensitive tool output 不进入 source；只保留安全摘要、状态、引用或 `artifact_ref`。
- 当前 turn 的 `message.user_appended` 已经写入 JSONL，但不进入本轮 compact source，必须进入 retained context。
- 旧 `compaction.summary_added` 事件可作为 existing summary 输入 incremental / rebase，但不是普通对话消息。

在 `CompactionSourceView` 上再构造 safe compact units：

```text
UserMessageUnit:
    message.user_appended

AssistantMessageUnit:
    message_end without tool_calls

ToolInteractionUnit:
    assistant message_end with tool_calls
    + all corresponding tool.call_completed / tool.call_failed results

CompactSummaryUnit:
    compaction.summary_added
```

cut point、slact pivot、retained/source 边界只能落在 safe unit 之间，不能落在 unit 内部。若用户指定的 pivot 或自动估算出的 cut point 落在 `ToolInteractionUnit` 中间，Product 层必须移动到安全边界。对于 auto prefix compact，默认移动到该 tool unit 的开头，让整个 tool interaction retained；这是最保守的协议安全策略。

本文后续提到的 `source_entry_ids` / `retained_entry_ids` 是 safe unit 展开后的 entry id 集合，而不是 raw JSONL 中任意事件 id 集合。

### 2.9 Compact 提交事务顺序

Compact 成功提交必须以 `compaction.summary_added` 事件为中心，而不是以 metadata 或外部文件为中心。推荐顺序：

```text
1. Product 构建 CompactionSourceView 和 safe units。
2. SummaryExecutor 生成候选 summary。
3. Product 校验 summary 结构、hash、source/retained 边界。
4. 如果 summary 超过 JSONL 内联阈值，先写入 summary artifact。
5. Product append compaction.summary_added 到 session JSONL。
6. Product 从 JSONL replay 或局部应用该事件，更新 CompactMetadata cache。
7. Product 发布 compaction.completed / llm_view.invalidated 等派生事件。
```

如果第 5 步失败，metadata cache 不能推进，Runtime 不能使用该 summary。若 artifact 已写入但 JSONL 事件写入失败，该 artifact 只能作为孤儿缓存清理，不能作为 compact 成功依据。

Compact failure 是否写入 session JSONL 需要单独判断：普通模型失败、超时、provider 错误优先进入 observability；只有会影响用户可恢复会话语义的失败，才由 Product 转写为安全的 SessionEvent。

### 2.10 事件投递语义与幂等

Runtime 到 Product 的事件投递应按 at-least-once 设计：Runtime 事件可能重复到达、延迟到达，甚至在取消后到达。Product 层必须通过 `turn_id`、`run_id`、`attempt_id`、`source_runtime_event_id` 和 `dedupe_key` 做幂等转写。

建议幂等规则：

- `agent.message_end` 转写为 `message_end` 时，幂等键至少包含 `session_id + turn_id + run_id + attempt_id + source_runtime_event_id`。
- 如果 provider 重试产生多个 attempt，只有被 Product 判定为最终有效的 attempt 可以写入 assistant 完整消息。
- `agent.tool_call_completed` 转写为 `tool.call_completed` 时，幂等键应包含 `tool_call_id` 和 `attempt_id`；重复完成事件不能写出多条同一结果。
- Product 收到冲突事件时，不凭内存判断覆盖，应 replay 当前 JSONL 后再决定忽略、补偿或记录观测告警。
- DerivedEvent 可以重复发布；SessionEvent 不能因为 derived event 重放而重复 append。

这意味着 Product 层事件桥接器需要有明确的“候选事件 -> 会话事实”状态机，而不是简单地把 `agent.*` 原样 append 到 JSONL。

### 2.11 并发、取消与进行中 Turn

Compact 需要一个稳定的 source snapshot。Product 层在计算 compact source 时必须记录 `source_snapshot_seq`，本次 summary 只能覆盖 `seq <= source_snapshot_seq` 且不包含当前 user event 的安全事件。

并发策略建议第一版保守处理：

- 同一 session 默认串行执行 turn；若已有 turn 在运行，新的用户输入排队或由 Product 明确取消旧 turn。
- Auto compact 只在调用 Runtime 前触发，不在 Runtime 执行中抢占式改写 LLMInputView。
- Slact 如果在 turn 运行中被触发，Product 应等待当前 turn 完成、取消当前 turn 后再执行，或拒绝并提示稍后重试。
- 取消后的 `agent.message_delta` 不写入 session fact；只有 Product 决定需要可恢复语义时，才写入安全的 `turn.cancelled` 或类似 SessionEvent。

进行中的工具交互不能被压缩到一半：

- assistant tool call 已产生但结果未完成时，该 interaction 不是完整 `ToolInteractionUnit`。
- 如果它属于当前运行 turn，不进入 compact source。
- 如果历史中存在异常遗留的半截 tool unit，Product 必须整体 retained，或生成一条安全的上下文说明事件后再允许压缩。
- 并行 tool calls 必须等同一 assistant tool-call group 中所有 `tool_call_id` 都完成、失败或被安全终止后，才构成可压缩 unit。

### 2.12 Slact 命令与用户聊天消息

`/slact`、快捷键、右键菜单、Web action 本质上是 `ProductCommand.request_slact_compact`，不默认等同于一条用户聊天消息。是否写入 `message.user_appended` 要看它是否是用户希望继续对话的自然语言内容。

建议规则：

- 用户在聊天框输入“帮我整理一下上下文”这类自然语言请求时，可以写入 `message.user_appended`，然后由 Product 解析为 slact 意图。
- 用户使用 `/slact up_to <entry_id>` 或 UI 按钮时，不应把命令文本作为 LLM 可读 user message；可以写入 `compaction.requested` 这类控制 SessionEvent 用于审计。
- `compaction.requested` 默认不进入 `CompactionSourceView`，除非后续需要审计用户显式选择的 pivot。
- slact 完成后的 UI 提示是 derived view，不是 assistant reply；如果产品希望让聊天流中可见，应写入专门的系统/产品通知事件，而不是伪造 assistant 消息。

这样可以避免命令语法污染学习上下文，也避免模型把 `/slact` 当成需要回答的普通用户请求。

### 2.13 Summary 校验与 Grounding

SummaryExecutor 返回的是候选摘要，Product 提交前必须校验。第一版至少校验：

- 输出不包含 tool call、命令执行请求或文件读取意图。
- `<analysis>` 已被剥离，提交内容只来自 `<summary>`。
- 九章节结构完整，或缺失章节被记录为 validation warning。
- `summary_hash` 与内联正文或 artifact 内容一致。
- `source_event_ids`、`retained_entry_ids`、`current_user_event_id` 都能从 JSONL replay 验证。
- summary 不应声称覆盖 retained recent messages 中才存在的事实。
- 用户关键原话锚点至少有一处可以追溯到 source event。
- summary token 数没有超过预设预算。

校验失败时，Product 可以有限重试 summary；重试失败后必须进入 compact failure 路径，不推进 anchor，不写可被 LLMInputView 消费的 `compaction.summary_added`。

### 2.14 Compact 与长期 Memory 边界

Context compaction 与长期 memory 是两个不同领域：

- compact summary 是会话上下文替代物，服务于当前 session 的 LLMInputView。
- long-term memory 是跨 session 或长期偏好的领域事实，必须由 memory 子域独立抽取、校验和管理。
- `compaction.summary_added` 不应自动写入长期 memory。
- 如果 memory 抽取需要利用 compact summary，只能把 summary 当作索引或候选线索，最终 memory fact 仍应引用原始 `message.user_appended`、`message_end` 或其他 SessionEvent。
- Rebase 可以读取当前 memory state 作为辅助上下文，但 SummaryExecutor 不能直接修改 memory。

这样可以避免“摘要中的二手表述”覆盖用户原话，也避免同一事实同时以 raw event、compact summary、memory fact 三种形态互相污染。

---

## 3. 分层职责

### 3.1 Interface

负责暴露 slact 入口：

- CLI 命令，例如 `/slact`、`/slact from <entry_id>`、`/slact up_to <entry_id>`。
- Web 操作，例如选中某条消息后执行“压缩从这里开始”或“压缩到这里为止”。
- 参数校验和用户可见反馈。

Interface 不决定压缩边界、不生成摘要、不修改 session 真实状态。

### 3.2 Product/Application

负责 compaction 主策略：

- 判断 auto compact 是否触发。
- 解析 slact 的用户意图和 pivot。
- 选择压缩模板。
- 从 JSONL transcript 读取并 replay 压缩候选区间。
- 计算 compact source 和 retained messages。
- 调用摘要执行器。
- 将 compact 结果作为 `compaction.summary_added` 事实事件写回会话 JSONL。
- 维护可从 JSONL 重建的 compact metadata 派生索引。
- 生成 Runtime 可消费的 `CompactionPlan`。
- 管理 Runtime `agent.*` 事件到 SessionEvent 的幂等转写。
- 为 compact 计算稳定的 `source_snapshot_seq`。
- 校验 summary candidate，并决定失败、重试、降级或提交。
- 协调 compact 与长期 memory 子域边界，避免摘要自动污染 memory。

建议主要落在：

- `LearningAgentSystem`
- `SessionManager`
- `CompactionCoordinator`
- 后续新增的 `SlactService` 或 `CompactionCommandService`

### 3.3 Agent Runtime

负责消费压缩后的执行输入：

- 根据 `CompactionPlan` 构建上下文。
- 插入 compact summary message。
- 只保留 plan 指定的 retained history。
- 发布带 `turn_id` / `run_id` / `attempt_id` 的 `agent.*` 事件。
- 上报 usage、context build、compact applied 等观测事件。

Runtime 不决定压缩范围，不判断 slact 语义，不执行 summary prompt 的产品策略。

### 3.4 Infrastructure

负责存储和 provider 调用：

- append/read/delete session event log。
- 保存可选 compact summary artifact 或大型 tool result artifact。
- 保存可丢弃的 compact metadata cache。
- 记录 artifact 的可回溯引用。
- 提供 summary provider 调用适配。
- 为 session JSONL append 提供原子写入、单调 `seq` 和 read-after-append 语义。
- 提供 orphan summary/tool artifact 的清理能力。

Infrastructure 不决定何时 compact，也不解释 slact 命令。

对于 full compact，Infrastructure 还需要提供稳定的 JSONL 读取能力：

- 按 session 读取 JSONL delta。
- 从指定 cursor 之后读取增量 delta。
- 向 Product 层提供 raw event 读取能力。
- 返回 entry id / event id 到 JSONL line / offset 的映射，供审计使用。
- 保存敏感 tool output 的 observability/artifact 文件，但不把敏感全文写入 session event log。

---

## 4. 压缩类型

### 4.1 Auto Prefix Compact

这是 auto compact 的唯一主路径。

```text
source:
    old messages before cut_point

retained:
    recent messages from cut_point to latest

result context:
    compact summary
    retained recent messages
```

特点：

- 自动触发。
- 用户无感。
- 必须保留最近消息原文。
- summary 是历史前言，不重复最近消息已经完整表达的 current work。
- compact 后下一轮不应询问用户“是否继续”，而应直接接着执行。

### 4.2 Slact Full

用户显式要求整理全部上下文时使用。

```text
source:
    all messages in the current linear session sequence

retained:
    none, or only system/root marker

result context:
    compact summary
```

适用场景：

- 用户主动说“整理一下上下文”。
- 用户希望开启一个干净的继续会话。
- 当前历史高度冗余，近期消息也不需要原文保留。

### 4.3 Slact From

用户选择某个 pivot 后，保留早期上下文，压缩 pivot 之后的近期段。

```text
retained:
    messages before pivot

source:
    messages from pivot to latest

result context:
    retained early messages
    compact summary of recent portion
```

适用场景：

- 早期学习目标、用户偏好、课程设定很重要，必须原文保留。
- 后续讨论产生大量冗余推演，需要折叠。

### 4.4 Slact Up To

用户选择某个 pivot 后，压缩 pivot 之前的旧上下文，保留 pivot 之后的近期段。

```text
source:
    messages before pivot

retained:
    messages from pivot to latest

result context:
    compact summary as historical preface
    retained recent messages
```

适用场景：

- 当前学习任务正在近期消息里展开，不能丢原文。
- 旧历史只需要背景摘要。
- 这也是 auto compact 的基础形态，但 slact up_to 由用户显式选择 pivot。

---

## 5. 九章节摘要结构：学习 Agent 适配版

Claude Code 的九章节结构适合 coding handoff。本项目需要保留其“反漂移骨架”，但把章节内容改造为学习场景。

### 5.1 标准九章节

```text
1. Primary Learning Request and Intent
2. Learning Context and Goals
3. Key Concepts, Explanations, and Examples
4. Materials, Files, and External Artifacts
5. Errors, Misunderstandings, and Corrections
6. All User Messages and Feedback
7. Pending Learning Tasks
8. Current Learning State
9. Optional Next Step with Verbatim Anchor
```

### 5.2 章节说明

#### 1. Primary Learning Request and Intent

记录用户明确提出的学习目标、问题、任务和期望输出。

必须区分：

- 用户想学什么。
- 用户想完成什么。
- 用户希望助手扮演什么角色。
- 用户要求的回答风格或深度。

#### 2. Learning Context and Goals

记录更长期的学习背景：

- 当前 objective。
- 当前 mode，例如 Chat / Ask / Study。
- 当前学习阶段。
- 用户已有基础。
- 用户偏好的讲解方式。
- 已确认的约束。

#### 3. Key Concepts, Explanations, and Examples

记录已经讲过的重要概念、类比、例子和结论。

这部分替代 coding 场景里的 “Key Technical Concepts”，但仍要保留技术内容，尤其是：

- 概念定义。
- 推理链路。
- 用户已经理解或尚未理解的点。
- 对用户有效的例子。
- 不要重复使用的低效解释。

#### 4. Materials, Files, and External Artifacts

记录会话中涉及的学习材料和产物：

- 读过的文件。
- 生成或修改过的文档。
- 练习题、笔记、表格、代码片段。
- 外部资料引用。
- 每个材料为什么重要。

如果本轮是 coding 学习任务，也要保留关键文件、函数、测试输出和代码片段。

#### 5. Errors, Misunderstandings, and Corrections

记录错误和纠正：

- 用户指出过的理解偏差。
- 助手解释错、做错或方向偏离的地方。
- 后续如何修正。
- 哪些说法不要再重复。
- 哪些概念用户容易混淆。

这部分是学习 Agent 的高价值记忆，避免压缩后重复同样的误解。

#### 6. All User Messages and Feedback

列出压缩范围内所有非工具结果的用户消息。

要求：

- 尽量保留用户原话。
- 对长消息可以摘要，但关键请求和纠正必须原文引用。
- 标注用户反馈的语气和决策变化。

这部分是核心反漂移机制之一。

#### 7. Pending Learning Tasks

记录尚未完成的学习任务：

- 等待解释的问题。
- 待练习的题目。
- 待复盘的概念。
- 待生成的学习材料。
- 用户明确要求后续继续做的事项。

只记录仍然有效的任务，避免复活已经完成的旧任务。

#### 8. Current Learning State

记录 compact 发生前的即时状态：

- 当前正在讲哪一段。
- 用户刚问到哪里。
- 助手刚做到哪里。
- 是否处于 Ask 对齐、Study 讲解、Chat 快答。
- 当前卡点或下一步准备。

对于 `Slact Up To` 和 `Auto Prefix Compact`，如果最近消息会原文保留，本节应改为 `Work Completed in Summarized Portion`，避免和 retained recent messages 重复。

#### 9. Optional Next Step with Verbatim Anchor

记录下一步，但必须遵守两个约束：

- 只有当下一步与最近明确请求直接相关时才写。
- 必须包含最近对话中的原文锚点，尤其是用户最近的明确请求。

示例：

```text
Next Step:
继续完善 full compact 的技术实现文档。

Verbatim Anchor:
用户说：“你基于这些写一份适配我项目技术实现文档。”
```

对于 `Slact Up To` 和 `Auto Prefix Compact`，本节应改为 `Context for Continuing Recent Messages`，因为真正的下一步会在 retained recent messages 中自然体现。

---

## 6. Prompt 模板

### 6.1 通用 No-Tools Preamble

所有摘要模板前置同一段硬约束：

```text
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use read_file, grep, write_file, edit_file, bash, web, memory, or any other tool.
- You already have all the context you need in the transcript provided by the caller.
- Tool calls will be rejected and will waste your only summarization turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
```

现阶段摘要由主代理主链路通过普通 provider 调用执行，不引入 agent fork。仍保留这段文本，是为了稳定约束模型行为，并避免摘要阶段把旧 transcript 当成需要继续执行的当前任务。

### 6.2 Auto Prefix Compact Template

用途：

- 系统自动压缩旧消息。
- 最近消息会原文保留。
- summary 注入在最近消息之前。

模板重点：

```text
Your task is to summarize the earlier portion of a learning-agent conversation.
Newer messages will be preserved verbatim after your summary.
Do not summarize or invent the newer messages; summarize only the transcript provided here.

The summary will be placed before the retained recent messages as historical context.
Someone reading your summary and then the retained recent messages should be able to continue naturally.
```

第 8、9 节使用：

```text
8. Work Completed in Summarized Portion
9. Context for Continuing Recent Messages
```

### 6.3 Slact Full Template

用途：

- 用户显式要求压缩整个会话。
- 原历史基本被 summary 替代。

模板重点：

```text
Your task is to create a detailed summary of the learning-agent conversation so far.
Pay close attention to the user's explicit learning goals, feedback, corrections, preferences, and the assistant's previous actions.
This summary should be sufficient for continuing the learning session without the original transcript.
```

第 8、9 节使用：

```text
8. Current Learning State
9. Optional Next Step with Verbatim Anchor
```

### 6.4 Slact From Template

用途：

- 用户选择从某处开始压缩。
- 早期消息原文保留。
- summary 放在 retained early messages 之后。

模板重点：

```text
Your task is to summarize the recent portion of the learning-agent conversation.
Earlier messages are retained verbatim and do not need to be summarized.
Focus only on what changed, what was learned, and what remains pending in the recent portion.
```

第 8、9 节使用：

```text
8. Current Learning State
9. Optional Next Step with Verbatim Anchor
```

### 6.5 Slact Up To Template

用途：

- 用户选择压缩某处之前。
- 近期消息原文保留。
- summary 放在 retained recent messages 之前。

模板重点：

```text
Your task is to summarize the earlier portion of the learning-agent conversation.
The newer messages are retained verbatim after this summary, but you do not see them here.
Write the summary as historical context needed to understand and continue the retained newer messages.
```

第 8、9 节使用：

```text
8. Work Completed in Summarized Portion
9. Context for Continuing Recent Messages
```

### 6.6 Incremental Template

用途：

- auto compact 或 slact 后，后续只压缩自上次 compact anchor 之后的新消息。

模板重点：

```text
You are updating an existing compact summary with a new transcript delta.
Preserve the stable learning intent, user preferences, corrections, and pending tasks from the existing summary.
Integrate only the new facts, changes, corrections, and next steps from the delta.
Do not simply append a log unless the caller explicitly requests an audit trail.
```

输出仍使用九章节结构。

### 6.7 Rebase Template

用途：

- 连续多次 incremental 后，生成新的 canonical summary。
- 防止 `[Incremental Update]` 无限堆积。

输入：

```text
existing canonical summary
new transcript delta since last compact anchor
optional session memory state
```

模板重点：

```text
Your task is to produce a new single canonical summary.
Use the existing summary as historical memory and the transcript delta as new evidence.
Merge them into one coherent nine-section summary.
Remove obsolete pending tasks and resolve contradictions in favor of newer explicit user feedback.
```

输出不应包含多个历史增量块，而是一份新的单一 summary。

### 6.8 Few-Shot 示例规则

所有正式压缩模板都必须内置少量 few-shot 示例，用来提高摘要稳定性。示例的作用不是让模型照抄内容，而是让模型学习：

- 九章节的完整输出形态。
- 学习场景下每个章节应该写什么。
- 如何保留用户原话作为反漂移锚点。
- 如何区分 summarized portion 与 retained recent messages。
- 如何删除已经完成或过期的 pending task。
- 如何把错误、误解、用户纠正写进摘要。

示例应遵守：

- 至少提供一个完整 `<analysis>...</analysis><summary>...</summary>` 结构示例。
- 示例内容必须是学习 Agent 场景，不只使用 coding 场景。
- 示例长度适中，避免把模板本身撑得过长。
- 示例中必须包含用户原话锚点。
- `Auto Prefix Compact` 和 `Slact Up To` 的示例必须展示第 8、9 节如何写成“已完成内容”和“继续近期消息的背景”，而不是重复 next step。
- `Slact Full` 和 `Slact From` 的示例必须展示第 8、9 节如何记录当前学习状态和下一步。
- `Incremental` 和 `Rebase` 的示例必须展示如何合并旧 summary 与新 delta，而不是机械追加。

推荐在 prompt 中加入如下短示例：

```text
<example>
<analysis>
I should summarize only the provided earlier transcript because newer messages
will be preserved. I need to keep the learner's goal, the concepts already
explained, the user's correction, and the exact latest relevant user wording.
The next-step section should be framed as context for the retained newer
messages, not as a new instruction.
</analysis>

<summary>
1. Primary Learning Request and Intent:
   用户希望理解 Python 装饰器，不只是记住语法，而是能解释“函数为什么可以被包一层”。

2. Learning Context and Goals:
   当前处于 Study 模式。用户偏好先给直观类比，再给小段代码验证理解。用户已经理解函数可以作为参数传递，但对闭包仍不稳定。

3. Key Concepts, Explanations, and Examples:
   - 已讲过“装饰器像给函数外面套一层检查入口”的类比。
   - 已解释 `wrapper(*args, **kwargs)` 用于保留原函数参数形态。
   - 用户对“返回 wrapper 而不是 wrapper()”产生过疑问，需要后续继续强化。

4. Materials, Files, and External Artifacts:
   - 无外部文件。
   - 已使用一个 `login_required(fn)` 的简短代码例子说明调用链。

5. Errors, Misunderstandings, and Corrections:
   - 助手一开始把闭包解释得太抽象，用户反馈“还是像背概念”。后续改用调用顺序拆解。
   - 用户容易混淆“定义装饰器时执行”和“调用被装饰函数时执行”。

6. All User Messages and Feedback:
   - “我想真的理解装饰器，不想只是会写 @xxx。”
   - “还是像背概念，能不能按调用顺序讲？”

7. Pending Learning Tasks:
   - 用一步步调用顺序继续解释 `@decorator` 展开后的执行流程。
   - 给用户一个小练习判断输出顺序。

8. Work Completed in Summarized Portion:
   已建立装饰器、wrapper、闭包的基础直觉，并确认用户更适合调用顺序式讲解。

9. Context for Continuing Recent Messages:
   后续近期消息会继续围绕调用顺序展开。关键原文锚点：“能不能按调用顺序讲？”
</summary>
</example>
```

针对 `Slact Full` 或 `Slact From`，第 8、9 节示例应改成：

```text
8. Current Learning State:
   正准备从 `@decorator` 的语法糖展开式开始，解释为什么 `say_hi = decorator(say_hi)`。

9. Optional Next Step with Verbatim Anchor:
   Next Step: 继续按调用顺序解释装饰器执行流程，并给一个输出顺序练习。
   Verbatim Anchor: 用户说：“能不能按调用顺序讲？”
```

针对 `Incremental` / `Rebase`，应加入一个迷你示例展示“更新旧结论”：

```text
<example>
Existing summary said:
用户偏好先看数学定义。

New delta says:
用户反馈“先别给公式，我要先看图像直觉”。

Correct merged summary:
用户当前偏好已更新：先用图像或直觉解释，再补数学定义。旧的“先看数学定义”偏好已过期。
</example>
```

### 6.9 正式 PromptSpec 结构

上面的模板说明不能只停留在自然语言描述。正式实现时，Product/Application 层应生成一个结构化 `CompactPromptSpec`，再渲染成 provider message。第一版 `template_version` 固定为：

```text
compact-summary-v1
```

`CompactPromptSpec` 至少包含：

```text
template_version:
    compact-summary-v1

mode:
    auto_prefix | slact_full | slact_from | slact_up_to

scope:
    full | incremental | rebase

summary_position:
    before_retained | after_retained | replace_history

source_kind:
    CompactionSourceView.safe_units

source_event_range:
    start_seq / end_seq / source_snapshot_seq

source_event_ids:
    本次 summary 允许覆盖的事件 id 列表。

source_entry_ids:
    本次 summary 允许覆盖的 entry id 列表。

retained_policy:
    描述哪些消息会原文保留，以及 summary 不应重复或发明这些消息。

current_user_event_id:
    当前 turn user event。auto compact 时它不在 source 内，但会进入 retained context。

existing_summary:
    incremental / rebase 使用；full 首次 compact 为空。

session_memory_state:
    可选，只作为背景输入，不允许 SummaryExecutor 修改 memory。

source_transcript:
    从 CompactionSourceView safe units 渲染出的 LLM 可读 transcript。

artifact_policy:
    tool result artifact 的引用、preview 与敏感内容规则。

required_output:
    analysis checklist + summary 九章节。
```

PromptSpec 的输入来源必须已经被 Product 层裁剪和过滤。SummaryExecutor 不再读取 raw JSONL、不读取文件、不调用工具、不自行决定 source/retained。

### 6.10 Prompt 输入块顺序

最终 prompt 按固定顺序组装，避免不同模式下字段漂移：

```text
<compact_prompt version="compact-summary-v1">
<no_tools_preamble>
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- Do NOT use read_file, grep, write_file, edit_file, bash, web, memory, or any other tool.
- You already have all the context you need in the structured blocks below.
- Tool calls will be rejected and will waste your only summarization turn.
- Output exactly one <analysis> block followed by exactly one <summary> block.
</no_tools_preamble>

<task>
mode: {mode}
scope: {scope}
summary_position: {summary_position}
source_kind: CompactionSourceView.safe_units
source_event_range: {source_event_start_seq}..{source_event_end_seq}
source_snapshot_seq: {source_snapshot_seq}
current_user_event_id: {current_user_event_id}
template_version: compact-summary-v1
</task>

<task_instruction>
{mode_specific_instruction}
</task_instruction>

<hard_rules>
- Summarize ONLY <source_transcript>.
- Do not summarize, invent, or update retained messages that are described only in <retained_policy>.
- Preserve the user's explicit goals, corrections, preferences, and latest relevant wording.
- Keep tool calls and tool results as completed interactions; never imply a tool result exists if it is not in source.
- Preserve artifact_ref values when they are needed to continue work, but do not expand or invent artifact content.
- Sensitive content omitted from source must remain omitted.
- Remove obsolete pending tasks when source proves they were completed.
- Resolve contradictions in favor of newer explicit user feedback or current session state.
- The compact summary is not long-term memory and must not state that memory has been updated.
</hard_rules>

<retained_policy>
{retained_policy_text}
</retained_policy>

<existing_summary>
{existing_summary_or_empty}
</existing_summary>

<session_memory_state>
{session_memory_state_or_empty}
</session_memory_state>

<source_units>
{safe_unit_metadata_and_boundaries}
</source_units>

<source_transcript>
{source_transcript}
</source_transcript>

<required_output>
{required_output_contract}
</required_output>
</compact_prompt>
```

`source_transcript` 必须使用边界明显的 safe unit 标记，至少包含 `unit_id`、`entry_ids`、`event_ids` 和 role transcript：

```text
<unit id="round-12" entry_ids="e1,e2,e3" event_ids="s10,s11,s12">
USER: ...
ASSISTANT: ...
TOOL_CALLS: read_file
TOOL: result_preview ... artifact_ref=...
ASSISTANT: ...
</unit>
```

如果 source 内容过长，Product 层应先缩小 source 或改用 rebase 策略，而不是让 SummaryExecutor 自行截断 transcript。确实需要截断时，必须在 `source_units` 中显式标注 `truncated=true`、保留头尾和 artifact_ref，并在 validation 中记录 warning。

### 6.11 模式差异参数

六类压缩共享同一 PromptSpec，只替换 `mode_specific_instruction`、`summary_position`、第 8/9 节标题和 retained 说明。

| 模式 | summary_position | source | retained_policy | 第 8 节 | 第 9 节 |
|---|---|---|---|---|---|
| `auto_prefix` | `before_retained` | 旧历史，排除当前 user | 近期消息和当前 user 会原文保留 | `Work Completed in Summarized Portion` | `Context for Continuing Recent Messages` |
| `slact_full` | `replace_history` | 用户指定的全部可压缩历史 | 通常不保留对话原文 | `Current Learning State` | `Optional Next Step with Verbatim Anchor` |
| `slact_from` | `after_retained` | pivot 之后 | pivot 之前会原文保留 | `Current Learning State` | `Optional Next Step with Verbatim Anchor` |
| `slact_up_to` | `before_retained` | pivot 之前 | pivot 之后会原文保留 | `Work Completed in Summarized Portion` | `Context for Continuing Recent Messages` |
| `incremental` | 继承触发模式 | 上次 compact anchor 后的新 delta | 依赖触发模式 | 依赖触发模式 | 依赖触发模式 |
| `rebase` | 继承触发模式 | existing summary + 新 delta | 依赖触发模式 | 依赖触发模式 | 依赖触发模式 |

`mode_specific_instruction` 推荐文本：

```text
auto_prefix:
You are summarizing the earlier portion of a learning-agent conversation.
Newer messages and the current user message will be preserved verbatim after
your summary. Do not invent those newer messages. Write historical context
that lets the conversation continue naturally from the retained messages.

slact_full:
You are creating a canonical summary of the selected conversation history.
The original selected history will be replaced by this summary. Preserve enough
detail for the session to continue without rereading the original transcript.

slact_from:
You are summarizing the later portion of the conversation after a user-selected
pivot. Earlier messages remain verbatim before your summary. Focus on what
changed, what was decided, what the user corrected, and what remains pending.

slact_up_to:
You are summarizing the earlier portion of the conversation up to a user-selected
pivot. Later messages remain verbatim after your summary. Write only the
historical context needed to understand those later messages.

incremental:
You are updating an existing compact summary with a new source delta. Produce
a fresh canonical nine-section summary, not an append-only changelog. Preserve
stable facts from the existing summary and integrate newer source facts.

rebase:
You are rebasing an existing compact summary and new source delta into one
canonical summary. Remove stale tasks, resolve contradictions in favor of newer
explicit user feedback, and output a single coherent summary.
```

### 6.12 Required Output Contract

正式输出必须是：

```text
<analysis>
Coverage:
- Which source range and safe units were summarized.

Anchors:
- Verbatim user wording selected as anti-drift anchors.

Contradictions:
- Newer corrections or decisions that override older summary/source statements.

Omitted:
- Sensitive, tool-only, UI-only, or retained content that was intentionally not summarized.

Validation Notes:
- Any missing section, weak anchor, truncated unit, or artifact reference risk.
</analysis>

<summary>
1. Primary Learning Request and Intent:
...

2. Learning Context and Goals:
...

3. Key Concepts, Explanations, and Examples:
...

4. Materials, Files, and External Artifacts:
...

5. Errors, Misunderstandings, and Corrections:
...

6. All User Messages and Feedback:
...

7. Pending Learning Tasks:
...

8. {mode_specific_section_8_title}:
...

9. {mode_specific_section_9_title}:
...
</summary>
```

这里的 `<analysis>` 不是可长期保存的思维链，而是供 Product 层做校验的结构化抽取清单。持久化到 `compaction.summary_added` 和注入 LLMInputView 时，只能保留 `<summary>`。

第 6 节 `All User Messages and Feedback` 的要求比普通摘要更高：

- 短用户消息尽量保留原文。
- 长用户消息可以摘要，但关键请求、纠正、否定、确认必须原文引用。
- slash command / UI action 不默认写入本节，除非 Product 明确把它作为用户可见会话语义。
- 当前 turn user 如果不在 source 内，不能写进本节；它会作为 retained context 进入 LLMInputView。

第 4 节 `Materials, Files, and External Artifacts` 必须保留 artifact 引用：

```text
- artifact_ref: artifact://session/{session_id}/tool-result/{artifact_id}
  用途：保存完整 read_file 输出；summary 中只记录 preview 和关键结论。
  后续：只有当前任务需要原文时，才通过 artifact slice 工具读取。
```

### 6.13 Prompt Assembly 示例

Auto prefix compact 的完整结构示例：

```text
<compact_prompt version="compact-summary-v1">
<no_tools_preamble>
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- Do NOT use read_file, grep, write_file, edit_file, bash, web, memory, or any other tool.
- You already have all the context you need in the structured blocks below.
- Tool calls will be rejected and will waste your only summarization turn.
- Output exactly one <analysis> block followed by exactly one <summary> block.
</no_tools_preamble>

<task>
mode: auto_prefix
scope: full
summary_position: before_retained
source_kind: CompactionSourceView.safe_units
source_event_range: 10..88
source_snapshot_seq: 91
current_user_event_id: evt-91
template_version: compact-summary-v1
</task>

<task_instruction>
You are summarizing the earlier portion of a learning-agent conversation.
Newer messages and the current user message will be preserved verbatim after
your summary. Do not invent those newer messages. Write historical context
that lets the conversation continue naturally from the retained messages.
</task_instruction>

<hard_rules>
- Summarize ONLY <source_transcript>.
- Do not summarize, invent, or update retained messages that are described only in <retained_policy>.
- Preserve the user's explicit goals, corrections, preferences, and latest relevant wording.
- Keep tool calls and tool results as completed interactions; never imply a tool result exists if it is not in source.
- Preserve artifact_ref values when they are needed to continue work, but do not expand or invent artifact content.
- Sensitive content omitted from source must remain omitted.
- Remove obsolete pending tasks when source proves they were completed.
- Resolve contradictions in favor of newer explicit user feedback or current session state.
- The compact summary is not long-term memory and must not state that memory has been updated.
</hard_rules>

<retained_policy>
Recent safe units after cut_point_entry_id=e-88 and current_user_event_id=evt-91
will be preserved verbatim after this summary. Do not summarize those retained
messages and do not write a next step that competes with the retained latest
user request.
</retained_policy>

<existing_summary>
</existing_summary>

<session_memory_state>
mode: chat
objective: 帮用户持续完善 Agent 上下文压缩设计。
constraints:
- 事件驱动。
- JSONL 是唯一事实源。
</session_memory_state>

<source_units>
<unit id="round-0" entry_ids="e-10,e-11" event_ids="evt-10,evt-11">
USER: 我想解决上下文压缩的问题。
ASSISTANT: 我们先看持久化 JSONL 和压缩边界。
</unit>
</source_units>

<source_transcript>
USER: 我想解决上下文压缩的问题。
ASSISTANT: 我们先看持久化 JSONL 和压缩边界。
</source_transcript>

<required_output>
Use the exact output contract. Section 8 must be "Work Completed in Summarized Portion".
Section 9 must be "Context for Continuing Recent Messages".
</required_output>
</compact_prompt>
```

### 6.14 Summary 校验规则

Product 层在提交 `compaction.summary_added` 前必须校验：

1. 输出包含且只包含一个 `<analysis>` 和一个 `<summary>`。
2. 持久化正文来自 `<summary>`，不能包含 `<analysis>`、few-shot 示例或 prompt 输入块。
3. 九章节标题完整，且第 8/9 节标题与 mode 匹配。
4. `All User Messages and Feedback` 至少包含一个可从 `source_event_ids` 追溯的用户原话锚点；如果 source 没有 user message，记录 warning。
5. summary 不引用 retained-only 消息中的新事实；auto/up_to 模式尤其要检查。
6. summary 不包含 raw sensitive output，不扩写 artifact 内容。
7. 所有保留的 `artifact_ref` 必须来自 source 或 existing summary。
8. incremental / rebase 输出必须是单一 canonical summary，不能只追加 `[Incremental Update]`。
9. summary token 数不超过 `summary_token_budget`。
10. 校验失败时可有限重试；重试失败不写可消费的 `compaction.summary_added`。

### 6.15 Prompt 与事件字段绑定

PromptSpec 必须能回填到 `compaction.summary_added`：

```text
template_version        -> template_version
source_event_range      -> source_event_range
source_event_ids        -> source_event_ids
source_entry_ids        -> source_entry_ids
source_snapshot_seq     -> anchor_event_seq 或独立 source_snapshot_seq
current_user_event_id   -> current_user_event_id
existing_summary event  -> previous_compact_event_id
summary text hash       -> summary_hash
validation warnings     -> validation_status / observability
```

如果 PromptSpec 与提交事件字段不一致，以 JSONL replay 和提交时校验为准，丢弃候选 summary。

---

## 7. Summary 后处理

### 7.1 剥离 analysis

摘要执行器返回：

```text
<analysis>
...
</analysis>

<summary>
...
</summary>
```

持久化和注入上下文时：

- 删除 `<analysis>` 整段。
- 提取 `<summary>` 内容。
- 如果没有 `<summary>` 标签，进入 fallback 清洗逻辑，并记录 warning。

### 7.2 包装注入消息

注入到主上下文前，summary 应包装为系统 continuation block：

```text
[Compact Summary]
This session is being continued after context compaction.
The summary below covers earlier conversation content.
Recent messages may be preserved verbatim after this block.
Do not acknowledge this summary to the user.
Continue naturally from the latest user-visible request.

...
```

Auto compact 必须额外包含：

```text
This compact summary was inserted automatically.
Do not ask follow-up questions just because this summary exists.
Resume the current task directly.
```

### 7.3 Transcript Escape Hatch

如果保留完整 transcript 文件路径，应在 summary block 中记录：

```text
Full transcript reference:
<path or session entry range>
```

这不是让模型默认读取文件，而是为后续显式恢复提供线索。摘要执行器本身不能主动使用工具读取。

---

## 8. 数据模型调整建议

### 8.1 CompactMode

建议新增枚举：

```python
class CompactMode(str, Enum):
    AUTO_PREFIX = "auto_prefix"
    SLACT_FULL = "slact_full"
    SLACT_FROM = "slact_from"
    SLACT_UP_TO = "slact_up_to"
```

### 8.2 CompactScope

保留并收紧已有 scope：

```python
class CompactScope(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    REBASE = "rebase"
```

`mode` 表示用户/系统选择的压缩形态，`scope` 表示本次摘要如何处理历史连续性。

### 8.3 CompactionPlan

建议扩展：

```python
@dataclass
class CompactionPlan:
    use_micro_compact: bool = False
    use_full_compact: bool = False
    compact_mode: str | None = None
    compact_scope: str | None = None
    summary_block: str | None = None
    compact_event_id: str | None = None
    previous_compact_event_id: str | None = None
    current_user_event_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    source_snapshot_seq: int | None = None
    compact_request_event_id: str | None = None
    source_entry_ids: list[str] = field(default_factory=list)
    retained_entry_ids: list[str] = field(default_factory=list)
    retained_position: str = "after_summary"
    cut_point_entry_id: str | None = None
    pivot_entry_id: str | None = None
    compact_anchor_entry_id: str | None = None
    source_jsonl_cursor: str | None = None
    next_jsonl_cursor: str | None = None
    recent_token_budget: int = 0
```

其中：

- `source_entry_ids` 是被 summary 替代的消息。
- `retained_entry_ids` 是仍需原文进入上下文的消息。
- `retained_position` 用于区分 summary 在 retained messages 前还是后。
- `compact_event_id` 指向本轮或最近一次 `compaction.summary_added` 事实事件。
- `previous_compact_event_id` 记录本次 compact 接在哪个旧 summary 之后，支撑 incremental / rebase 审计。
- `current_user_event_id` 标识本轮必须 retained、但不进入本轮 source 的用户消息。
- `turn_id` / `run_id` 用于将 Product turn 与 Runtime 执行实例关联起来。
- `source_snapshot_seq` 固定本次 summary 允许覆盖的 JSONL 上界，避免并发 append 污染 source。
- `compact_request_event_id` 可指向 slact 命令或 UI action 写入的审计事件。
- `source_jsonl_cursor` 和 `next_jsonl_cursor` 用于把本次 compact 区间绑定到持久化 JSONL。
- `summary_block` 是从 `compaction.summary_added` 事件或其 artifact 引用渲染出来的 Runtime 输入片段，不是新的事实源。

### 8.4 CompactMetadata

`CompactMetadata` 的定位需要收紧：它不是事实源，而是从 `sessions/{session_id}.events.jsonl` replay 得到的派生索引 / 加速缓存。Product 层可以使用它快速构建下一次 compaction plan，但任何字段都必须能从 JSONL 中恢复；如果 cache 与 JSONL 冲突，以 JSONL 为准，并记录校验告警。

建议扩展：

```python
@dataclass
class CompactMetadata:
    session_id: str
    last_compact_event_id: str | None = None
    previous_compact_event_id: str | None = None
    compact_anchor_entry_id: str | None = None
    compact_anchor_event_seq: int | None = None
    last_cut_point_entry_id: str | None = None
    last_compact_mode: str | None = None
    last_compact_scope: str | None = None
    incremental_count_since_rebase: int = 0
    consecutive_failures: int = 0
    last_summary_artifact_ref: str | None = None
    last_summary_hash: str | None = None
    last_source_snapshot_seq: int | None = None
    schema_version: str = "v1"
    last_source_entry_ids: list[str] = field(default_factory=list)
    last_retained_entry_ids: list[str] = field(default_factory=list)
    last_source_jsonl_cursor: str | None = None
    next_jsonl_cursor: str | None = None
    last_jsonl_path: str | None = None
    last_prompt_template: str | None = None
    last_compacted_at: float = 0.0
```

`consecutive_failures` 这类运行治理字段如果无法从成功 compact 事件恢复，可以通过独立 observability / runtime cache 维护；它不应影响 JSONL 作为会话事实源的权威性。

重启恢复时，Product 层应优先从 JSONL replay 最新有效 `compaction.summary_added` 事件得到 `CompactMetadata`。如果存在 `memory/compact/*.meta.json` 之类缓存，只能作为启动加速；必须校验 `last_compact_event_id`、summary hash 与 cursor，校验失败时丢弃缓存并从 JSONL 重放。

### 8.5 JSONL Cursor

建议第一版定义一个轻量 cursor：

```python
@dataclass
class JsonlCursor:
    session_id: str
    seq: int | None = None
    event_id: str | None = None
    line_no: int | None = None
    byte_offset: int | None = None
    last_entry_id: str | None = None
    last_delta_ts: str | None = None
```

实现可以先用 `last_entry_id` 作为逻辑 cursor，但 metadata 结构应预留 `seq` / `event_id` / `line_no` / `byte_offset`，方便后续避免全量扫描并支持幂等 replay。

读取规则：

```text
full:
    从 JSONL 起点 replay 到 cut_point，并投影为 CompactionSourceView。

incremental:
    从最近一次 compaction.summary_added 事件记录的 next_jsonl_cursor 之后 replay 到本次 cut_point。

rebase:
    从最近一次 compaction.summary_added 事件读取 existing summary，再读取 next_jsonl_cursor 之后的新 delta。
```

如果 JSONL 中存在 patch delta，压缩输入必须先按顺序 replay patch，不能只读取 append entry 的初始内容。

compact source 只能包含已经完成的消息事实。对于 assistant 流式输出，必须以 `message_end` 事件作为完整消息边界；`message.stream_failed`、`message.interrupted` 等失败事件默认只用于 UI/observability，不进入 summary，除非 Product 层转换为明确的学习上下文说明。

当前 turn 的 user event 是特殊边界：Product 可以先将其写入 JSONL，以保证恢复能力；但本轮 compact 的 source 上界必须排除该 user event，且 LLMInputView 必须将该 user event 放入 retained context。

### 8.6 Checkpoint / Cache 定位

长会话 replay 可能变慢，允许未来引入 checkpoint/cache，但必须遵守：

```text
checkpoint = 可丢弃性能缓存
```

checkpoint 必须记录：

- `checkpoint_seq`
- `checkpoint_event_id`
- `source_event_hash`
- `created_at`
- `schema_version`

恢复时如果 checkpoint 与 JSONL 不一致，必须丢弃 checkpoint，从 JSONL 起点或最近可信 checkpoint 重新 replay。checkpoint 不能替代 JSONL，也不能成为 compact summary、UI message 或 AgentSnapshot 的权威来源。

---

## 9. 消息重建规则

### 9.1 Auto Prefix Compact

```text
messages = [
    system_prompt,
    compact_summary_message,
    ...history entries whose ids are in retained_entry_ids,
    current_user_input,
]
```

注意：

- `retained_entry_ids` 必须包含 cut point entry 本身。
- source entry 不得再进入 provider context。
- 原始 session transcript 仍持久化保留，不在存储层破坏。

### 9.2 Slact Full

```text
messages = [
    system_prompt,
    compact_summary_message,
    current_user_input,
]
```

如需保留极少量 root/system marker，应由 Product 层显式加入 retained ids。

### 9.3 Slact From

```text
messages = [
    system_prompt,
    ...retained early messages,
    compact_summary_message,
    current_user_input,
]
```

### 9.4 Slact Up To

```text
messages = [
    system_prompt,
    compact_summary_message,
    ...retained recent messages,
    current_user_input,
]
```

### 9.5 Tool Call 配对保护

无论哪种模式，source 和 retained 的边界必须落在 safe round boundary：

- 不能把 assistant `tool_calls` 留下，却压掉对应 `tool` result。
- 不能把 `tool` result 留下，却压掉对应 assistant `tool_calls`。
- 如果 pivot 落在工具配对中间，Product 层必须把 pivot 移动到最近安全边界。

更准确地说，边界必须落在 `CompactionSourceView` 的 safe compact units 之间。Tool interaction 的 safe unit 应包含 assistant tool call message 与所有对应 tool result；如果 result 使用 `artifact_ref`，unit 中保留的是 preview + artifact reference，而不是 artifact 全文。

### 9.6 当前 Turn User 保留规则

推荐 turn 顺序：

```text
Product receives user input
    -> append message.user_appended(turn_id, content)
    -> evaluate compact using source events before current user seq
    -> build LLMInputView with current user retained
    -> call Runtime
```

因此本轮 user message 不应进入本次 summary source。它必须作为 retained context 的最后一条用户消息进入 provider messages。这样既保证用户输入可恢复，又避免刚收到的请求被摘要改写造成意图漂移。

---

## 10. 增量与 Rebase 策略

### 10.1 首次 Compact

首次 compact 时：

- `scope = full`
- `compact_anchor_entry_id = None`
- source 从第一条有效历史开始。
- auto compact 保留 recent window。
- slact full 可不保留 recent window。

### 10.2 后续 Incremental

后续 compact 时：

- source 从最近一次 `compaction.summary_added` 事件记录的 `compact_anchor_entry_id` / `next_jsonl_cursor` 之后开始。
- 保留 recent window 或 slact 指定 retained messages。
- summary 生成时从最近一次 `compaction.summary_added` 事件读取 existing summary，必要时再通过 `summary_artifact_ref` 读取 artifact。
- 新 summary 应融合旧 summary 和 delta，不建议只拼接 `[Incremental Update]`。

建议逻辑：

```text
if existing_summary exists and scope == incremental:
    summary_input = existing_summary + new_delta_transcript
    summary_template = incremental_template
    output = new canonical nine-section summary
```

### 10.3 Rebase

触发条件：

- 连续 incremental 达到阈值，例如 5 次。
- summary token 超过预算。
- summary 中增量块数量超过阈值。
- 用户通过 slact 显式要求整理。

Rebase 不是重读全部原始消息，而是：

```text
existing canonical summary
new delta since anchor
session memory state
    ->
new canonical summary
```

rebase 后：

- `incremental_count_since_rebase = 0`
- `last_compact_scope = rebase`
- `compact_anchor_entry_id` 推进到本次 source 的最后一个安全 entry。

### 10.4 冲突解决

当 existing summary 与 delta 冲突时：

1. 用户较新的明确反馈优先。
2. session mode / ask state 的当前真实状态优先。
3. retained recent messages 原文优先于 summary。
4. summary 中旧的 pending task 若已完成，必须删除。

### 10.5 最新有效 Summary 选择

JSONL 中可以存在多条 `compaction.summary_added` 事件。它们都是审计事实，但 LLMInputView 默认只消费最新有效 summary。

最新有效 summary 的选择规则：

1. 从 JSONL 顺序 replay `compaction.summary_added`。
2. 忽略 hash 校验失败、artifact 缺失且无法内联恢复、schema 不兼容的 compact 事件，并记录告警。
3. 选择最后一条校验通过的 canonical summary 事件作为 current compact summary。
4. incremental compact 的 `previous_compact_event_id` 指向它基于的上一条有效 summary。
5. rebase 成功后，旧 summary 不再参与 LLMInputView，但仍保留在 JSONL 中用于审计。

如果没有有效 compact summary，LLMInputView 应回退到未压缩或仅 micro compact 的历史视图，而不是使用不完整 summary。

### 10.6 Compact Failure 记录策略

普通 compact 失败不应推进 anchor，也不应产生可被 LLMInputView 消费的 summary。

失败记录默认进入 observability，包括：

- provider timeout / retry exhausted
- summary validation failed
- artifact write failed
- JSONL append failed

只有当失败本身需要成为用户可恢复会话语义时，Product 层才写入安全的 SessionEvent。无论是否写入 SessionEvent，失败都不能改变 latest valid compact summary。

---

## 11. Slact 接口设计

### 11.1 命令形态

建议第一版支持：

```text
/slact
/slact full
/slact from <entry_id>
/slact up_to <entry_id>
/slact rebase
```

默认 `/slact` 可等价于 `/slact up_to <auto-selected-safe-cut-point>`，因为这最符合“保留当前工作面”的安全策略。

### 11.2 Service 接口

建议新增：

```python
class SlactService:
    def compact_full(session_id: str) -> CompactionResult: ...
    def compact_from(session_id: str, pivot_entry_id: str) -> CompactionResult: ...
    def compact_up_to(session_id: str, pivot_entry_id: str) -> CompactionResult: ...
    def rebase(session_id: str) -> CompactionResult: ...
```

`SlactService` 调用 `CompactionCoordinator` 的底层能力，但负责用户命令语义和 pivot 校验。

### 11.3 用户反馈

slact 完成后 Interface 可以展示简短结果：

```text
已整理上下文：压缩 42 条消息，保留最近 8 条消息，summary 事件已写入会话记录。
```

不要把完整 summary 强行发给用户，除非用户要求查看。

### 11.4 UI 显示规则

`compaction.summary_added` 默认不作为 assistant 聊天气泡展示。UI view 可以基于该事件生成一条折叠提示，例如：

```text
历史上下文已整理，保留最近 8 条消息。
```

UI 显示约束：

- 默认不展示完整 summary，除非用户主动展开或请求查看。
- 折叠提示是 UI 派生视图，不是新的 session fact。
- 展开 summary 时应显示 compact mode、source range、retained count、summary hash 等审计信息。
- sensitive artifact 引用不应直接暴露真实路径。

---

## 12. 实施步骤

### 阶段 0：事件驱动边界收口

目标：让 Runtime 不再直接写 session 事实，由 Product 层消费 agent 事件并写入会话 JSONL。

任务：

1. 定义 Runtime 到 Product 的 `agent.*` 事件契约，例如 `agent.turn_started`、`agent.message_delta`、`agent.message_end`、`agent.tool_call_completed`、`agent.turn_failed`。
2. 定义 SessionEvent 信封：`event_id`、`session_id`、`seq`、`event_type`、`schema_version`、`source_runtime_event_id`、`dedupe_key`。
3. 为每个 turn 生成 `turn_id`、`run_id`、`attempt_id`、`user_event_id`，并要求 Runtime 事件原样携带。
4. Product 层在收到用户输入后先写入 `message.user_appended`，生成 `turn_id`，再调用 Runtime。
5. Product 层消费 `agent.message_end` 后写入 `message_end`；消费 `agent.tool_call_completed` 后写入 `tool.call_completed`。
6. Product 层需要对 Runtime 事件做幂等校验，重复的 `agent.message_end` 不能重复 append。
7. 明确 `agent.*` 事件不是会话事实，只有 Product 写入的 SessionEvent 才能进入 LLMInputView / UIViewMessage / CompactionSourceView。
8. 明确 at-least-once 投递语义，补齐重复、延迟、取消后到达的 Runtime 事件处理。
9. 增加测试：Runtime 事件不会直接污染 session event log；Product 转译后的事件可 replay。

### 阶段 1：修正 Auto Compact 主链

目标：让 full compact 真正减少上下文。

任务：

1. 从 session JSONL replay 出 `CompactionSourceView`，不要直接在 raw JSONL 行或内存 `session.entries` 上切分。
2. 定义 safe compact units，并保证 cut point / pivot 只能落在 unit 边界。
3. `CompactionPlan` 增加 `compact_event_id`、`source_entry_ids` 和 `retained_entry_ids`。
4. `build_full_compact_result` 返回 retained ids 时包含 cut point 所在 safe unit。
5. `build_llm_input_view` 根据 retained ids 重建 history view；Runtime 只消费结果，不决定 source/retained。
6. 当前 turn user 先写入 JSONL，但不进入本轮 compact source，必须进入 retained context。
7. compact 成功后追加 `compaction.summary_added` 事件，summary 文件只作为可选 artifact/cache。
8. compact source 计算时记录 `source_snapshot_seq`，不压缩 snapshot 之后新追加的事件。
9. `compaction.summary_added` payload 记录 `previous_compact_event_id`、`current_user_event_id`、source/retained ids、cursor、source event hash 和 summary hash。
10. 增加测试：被 compact 的旧内容不再出现在 provider messages 中。

### 阶段 2：引入 Prompt 模板与后处理

目标：替换低保真 `_summarize_transcript`。

任务：

1. 新增 `learning_agent/learning_agent/compaction/prompts.py`。
2. 实现 `compact-summary-v1` PromptSpec，输入块包括 task、retained_policy、existing_summary、session_memory_state、source_units、source_transcript、required_output。
3. 实现 auto prefix / slact full / slact from / slact up_to / incremental / rebase 的 mode-specific instruction。
4. 实现 `format_compact_summary()`，剥离 `<analysis>`，只返回 `<summary>`。
5. 实现 `validate_compact_summary()` 或等价校验，覆盖九章节、用户原话锚点、artifact_ref、retained-only 事实和 token budget。
6. 将九章节结构作为强约束。
7. 为每类模板加入学习场景 few-shot 示例，至少覆盖 auto/up_to、full/from、incremental/rebase 三组差异。

### 阶段 3：接入主链 Summary Executor

目标：在不引入子代理的前提下，为 auto compact 和 slact 提供统一摘要执行入口，并复用主代理已有的重试、熔断和降级机制。

任务：

1. 定义 `SummaryExecutor` port。
2. 第一版由主代理主链路通过 provider single-pass 调用执行摘要。
3. 接入现有 retry / circuit breaker / failure metadata。
4. 强制 no-tools 行为。
5. Summary executor 只返回候选 summary，不直接写 session；Product 层通过 `compaction.summary_added` 提交事实。
6. Product 层增加 summary validation，校验失败时有限重试或进入 compact failure。
7. 暂不实现子代理或 fork agent。

### 阶段 4：大型 Tool Result Artifact 化

目标：避免大文件工具结果撑爆 JSONL 与 LLM context，同时保留完整可追溯内容。

任务：

1. 工具执行层为超大 result 生成 `result_preview` 与 `artifact_ref`。
2. 完整工具结果保存到本地 artifact；敏感完整结果进入 observability/artifact 管理，不进入 session event log。
3. Session event log 的 `tool.call_completed` 只保存 preview、artifact_ref、状态、必要元数据。
4. LLM 可通过专门的 artifact slice 工具按需读取完整结果片段。
5. compact summary 可以压缩旧 preview，但必须保留 artifact_ref、用途和关键结论。

### 阶段 5：实现 Slact

目标：提供用户可控压缩。

任务：

1. Interface 增加 `/slact` 命令或 Web action。
2. Product 层新增 `SlactService`。
3. 支持 full / from / up_to / rebase。
4. pivot 自动移动到 safe round boundary。
5. 区分自然语言用户请求与 slash/UI command；命令不默认写成 `message.user_appended`。
6. Slact 在运行中 turn 存在时必须等待、取消或拒绝，不能并发改写同一 LLMInputView。
7. 完成用户反馈和观测事件。

### 阶段 6：完善 Incremental 与 Rebase

目标：避免 summary 越来越长和语义漂移。

任务：

1. incremental 生成新的 canonical summary，而不是简单追加。
2. incremental / rebase 从最近一次 `compaction.summary_added` 事件读取 existing summary 与 cursor。
3. 定义 latest valid summary 选择规则，确保 LLMInputView 只消费最新有效 summary。
4. 增加 summary token budget。
5. 增加 obsolete pending task 清理规则。
6. compact 成功事件中记录 JSONL cursor，保证增量压缩只处理持久化 transcript 的新增区间。

### 阶段 7：UI 与 Cache 治理

目标：避免 UI 和 cache 重新变成事实源。

任务：

1. UI view 基于 `compaction.summary_added` 生成折叠提示，但不把提示写回 session JSONL。
2. 用户主动展开 summary 时，只读取 latest valid summary 或指定 compact event。
3. 如引入 checkpoint/cache，必须记录 `checkpoint_seq`、event hash 和 schema version。
4. cache 与 JSONL 冲突时丢弃 cache，从 JSONL replay。

### 阶段 8：Memory 与 Schema 治理

目标：避免 compact summary 和长期 memory、事件 schema 演进互相污染。

任务：

1. 明确 `compaction.summary_added` 不自动进入长期 memory。
2. Memory 抽取如需参考 compact summary，最终 memory fact 必须引用原始 SessionEvent。
3. 为 `compaction.summary_added`、`message_end`、`tool.call_completed` 定义 schema version 与未知字段兼容策略。
4. Unknown SessionEvent 默认保留在审计流中，但不进入 CompactionSourceView，除非 Product 显式支持。

---

## 13. 测试计划

### 13.1 单元测试

覆盖：

- safe boundary 计算。
- CompactionSourceView 过滤和转译。
- auto prefix source / retained 划分。
- slact full / from / up_to 的消息重建顺序。
- `<analysis>` 剥离。
- 九章节缺失时的 fallback 或 validation warning。
- few-shot 示例不会被保存到 compact summary。
- `compact-summary-v1` PromptSpec 按固定输入块顺序渲染。
- mode-specific instruction 与第 8/9 节标题匹配。
- `format_compact_summary()` 只提取 `<summary>`，不会保存 `<analysis>`、few-shot 或 prompt 输入块。
- incremental 合并不丢用户最新纠正。
- rebase 输出单一 canonical summary。
- 当前 turn user 已写入 JSONL 但不会进入本轮 compact source。
- `CompactMetadata` 可由 JSONL replay 重建，cache 冲突时以 JSONL 为准。
- `compaction.summary_added` payload 包含 previous compact、current user、source/retained/cursor/hash 字段。
- summary validation 失败时不写可消费的 `compaction.summary_added`。
- summary validation 能识别 retained-only 事实、缺失原话锚点、artifact_ref 伪造和 `[Incremental Update]` 机械追加。
- latest valid summary 选择规则忽略损坏或 schema 不兼容的 compact event。
- checkpoint/cache 冲突时可丢弃并从 JSONL replay。
- Runtime 重复、延迟、取消后到达的 `agent.*` 事件不会重复写 SessionEvent。
- `/slact` 命令事件不默认进入 LLM 可读 user message。
- compact summary 不会自动生成长期 memory fact。

### 13.2 集成测试

覆盖：

- compact 后 provider messages 不包含 source entry 原文。
- retained recent messages 原文保留。
- compact source 来自 JSONL replay，且 patch delta 被正确应用。
- incremental 只读取 JSONL cursor 之后的新增区间。
- compact summary 来自 `compaction.summary_added` 事件，而不是独立 summary 文件事实源。
- compact source 只包含 `message_end` 后的完整 assistant 消息，不包含失败或中断的半成品流式内容。
- 敏感 tool output 不进入 compact source，summary 只能基于安全摘要和引用生成。
- 大型 tool result 的 event log 只包含 preview + artifact_ref，完整内容可通过 artifact slice 读取。
- Runtime 只发布 `agent.*` 事件，Product 转写后才进入 session event log。
- Runtime 重试产生多个 `attempt_id` 时，Product 不会把半成品或重复 end event 写成多条事实。
- 并发或取消场景下，compact 使用固定 `source_snapshot_seq`，不会覆盖 snapshot 后的事件。
- 历史半截 tool interaction 会被整体 retained 或转写为安全说明，不会被切开压缩。
- auto compact 后 assistant 不向用户复述 summary。
- slact 命令完成后 `compaction.summary_added` 事件、派生 metadata 与可选 summary artifact 一致。
- ContextLengthError 前置 compact 生效。
- UI 只显示 compact 折叠提示，不把提示反写为 session fact。
- session 删除时清理 summary artifact、tool result artifact 和敏感 observability 文件。

### 13.3 观测指标

建议记录：

- `compaction.triggered`
- `compaction.mode`
- `compaction.scope`
- `compaction.source_entries`
- `compaction.retained_entries`
- `compaction.summary_tokens`
- `compaction.jsonl_cursor_before`
- `compaction.jsonl_cursor_after`
- `compaction.estimated_tokens_before`
- `compaction.estimated_tokens_after`
- `compaction.failure`
- `compaction.rebase.count`
- `compaction.source_snapshot_seq`
- `compaction.validation.warning`
- `compaction.validation.failure`
- `session_event.deduped`
- `runtime_event.late_ignored`
- `compact_artifact.orphan_cleaned`

---

## 14. 当前代码风险对应

当前实现需要重点修复：

1. Runtime 仍可能直接 append session message，需要改为 Product 消费 `agent.*` 事件后写 SessionEvent。
2. `ReActEngine.build_context` / `build_llm_input_view` 不能在注入 summary 后继续加入完整 history。
3. `find_cut_point` 与 `list_entries_after` 的边界语义需要统一，避免漏掉 cut point 所在 safe unit。
4. `_summarize_transcript` 不能继续作为正式摘要实现，只能作为 fallback。
5. `merge_incremental_summary` 不应长期简单追加 `[Incremental Update]`。
6. `render_summary_block` 需要加入 continuation wrapper 和 auto compact suppress-follow-up 指令。
7. trace summary 需要从字符串判断升级为结构化工具轨迹。
8. full compact 的 source selection 应从持久化 JSONL transcript replay 出 `CompactionSourceView`，不能只依赖内存 `session.entries`。
9. compact summary 应作为 `compaction.summary_added` 事件进入 JSONL；独立 summary 文件不能作为权威事实源。
10. Product 需要用 `turn_id`、`run_id`、`attempt_id` 和 `user_event_id` 做幂等和归属校验。
11. 多次 compact 后需要 latest valid summary 选择规则，避免旧 summary 与新 summary 同时进入 LLMInputView。
12. 需要区分持久化层 snapshot/delta 合并与对话上下文 full compact，避免上下文压缩误清空原始 JSONL transcript。
13. Runtime 事件投递如果按 exactly-once 假设实现，重试、取消和进程恢复都会造成重复或丢失事实。
14. 如果 compact 没有固定 `source_snapshot_seq`，并发 append 可能导致 summary 声称覆盖了它没有读取过的事件。
15. Slact 命令若直接写成 LLM 可读 user message，会污染学习上下文并诱发模型回答命令文本。
16. 缺少 summary validation 会让结构损坏、artifact hash 不一致或脱离 source 的摘要进入主上下文。
17. Compact summary 若自动进入长期 memory，会放大二手摘要错误并覆盖用户原话。

---

## 15. 非目标

第一版不做：

- 不实现 Claude Code 的 provider cache edits。
- 不让 SM 文件承担 compact summary 主路径。
- 不在 Runtime 层解释 slact 命令。
- 不删除原始 session transcript。
- 不把内存 message 数组作为 full compact 的唯一事实源。
- 不把独立 summary 文件作为 compact 权威事实源。
- 不要求 summary executor 主动读取文件补信息。
- 不把 UI 折叠提示写回 session JSONL 当成事实。
- 不让 checkpoint/cache 取代 JSONL。
- 不把 `/slact` 这类控制命令默认当成 LLM 可读用户消息。
- 不把 compact summary 自动提升为长期 memory fact。
- 现阶段不引入子代理、fork agent 或后台 summarizer。

---

## 16. 验收标准

实现完成后应满足：

1. Auto compact 只压缩旧消息并保留最近消息。
2. Slact 支持三类用户触发模板：full / from / up_to。
3. 所有正式摘要均采用 analysis -> summary 输出，并在注入前剥离 analysis。
4. 摘要采用学习 Agent 版九章节结构。
5. 压缩模板内置学习场景 few-shot 示例，用于提高摘要生成质量。
6. 用户最新明确请求和关键纠正以原文锚点保留。
7. Incremental 和 rebase 都生成 canonical summary，不无限堆积 delta。
8. Compact summary 作为 `compaction.summary_added` 事件写入会话 JSONL；summary artifact/cache 不是事实源。
9. Full compact 从持久化 JSONL transcript 投影出 `CompactionSourceView`，并通过 JSONL cursor 支持增量压缩。
10. Runtime 上下文中不再出现已被 source summary 替代的旧消息原文。
11. Runtime 只发布 `agent.*` 事件，Product 转写后的 SessionEvent 才进入会话事实源。
12. 大型 tool result 使用 preview + artifact_ref，完整结果按需切片读取，不通过改写事实源做 emergency truncation。
13. `turn_id`、`run_id`、`attempt_id`、`user_event_id` 能关联 user、assistant、tool、usage 与 compact 事件。
14. 多次 compact 后，LLMInputView 只消费最新有效 summary；旧 summary 只用于审计。
15. UI compact 提示是派生视图，不成为 session fact。
16. checkpoint/cache 可丢弃，冲突时以 JSONL replay 为准。
17. compact 失败不污染 session 状态，并触发熔断或降级。
18. Runtime 事件按 at-least-once 处理，Product 写 SessionEvent 保持幂等。
19. Compact 使用固定 `source_snapshot_seq`，并发或取消不会污染 source range。
20. Slact 控制命令与自然语言用户消息可区分。
21. Compact summary 与长期 memory 之间有独立抽取和引用边界。
