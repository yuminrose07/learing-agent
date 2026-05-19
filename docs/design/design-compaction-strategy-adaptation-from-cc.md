# 基于 Claude Code 调研的 Compaction 适配技术设计

> 版本：v1.1
> 输入材料：
> - `docs/research/compaction-strategy-deep-dive.md`
> - `/Users/roseannk/claude-code-analysis/src`
> - 当前仓库既有分层、Memory、上下文压缩设计文档
>
> 目标：在不破坏当前四层边界和最小运行时结构的前提下，为本项目设计一套可实施、可回退、可观测的两层 compaction 方案，并明确 `SM` 文件在记忆子域中的定位。

---

## 1. 文档定位

本文档回答下面五个问题：

1. Claude Code 的压缩设计，哪些思想值得借鉴。
2. 当前项目为什么不应直接照搬 `Session Memory Compact`。
3. 为什么本项目应保留 `SM` 文件，但不让它承担 compaction 主路径职责。
4. 为什么 compaction 应收敛为 `Micro Compact + Full Compact` 两层。
5. 如何在当前真实主链中落地“首次全量、后续增量”的 Full Compact。

本文档聚焦 **compaction** 与 **SM 文件角色边界**。完整 recall、知识图谱、学习记忆提升策略不在本文展开。

---

## 2. 当前实现基线

### 2.1 当前真实主链

当前仓库真实生效的执行主链是：

```text
CLI / Web
    ->
LearningAgentSystem
    ->
SessionManager + AgentLoop
    ->
AgentLoopSession
    ->
ReActEngine / ToolExecutor / HookSystem / EventBus / Observability
    ->
Provider / FileStore
```

这意味着 compaction 设计必须服从以下边界：

- `LearningAgentSystem / SessionManager` 负责会话级决策、状态持久化、压缩边界维护。
- `AgentLoopSession` 负责单轮执行，不拥有产品级 compaction 策略。
- `ReActEngine` 负责上下文组装和上下文溢出兜底，但不应决定“是否压缩、从哪里开始压缩、压缩哪一段”。
- `FileStore` 只负责存储，不负责策略裁决。

### 2.2 当前已存在的相关能力

- 工具输出有界：
  - `read_file`、`grep`、`bash` 已有截断策略。
- Runtime 级兜底：
  - `ReActEngine.attempt_emergency_truncation()` 会在 `ContextLengthError` 后截断最长 TOOL 历史。
- Hook 切点稳定：
  - `before_agent_run`
  - `before_tool_execute`
  - `after_tool_execute`
  - `on_stream_chunk`
  - `after_response`
- Session 持久化机制存在：
  - `FileStore.compact_session()` 能做 snapshot + delta 合并
  - 但 `SessionManager.compact_session()` 仍缺少真正的产品级压缩语义

### 2.3 当前缺口

当前问题不是“没有任何压缩”，而是“没有一条会话级 compaction 主链”：

1. 旧版 `context_compressor` 依赖旧式 `BEFORE_CONTEXT_BUILD` 扩展切点，不是当前最小 runtime 的真实主路径。
2. 当前压缩更多发生在工具输出层和 `ContextLengthError` 之后，偏被动补救。
3. 缺少正式的 Full Compact 边界管理机制。
4. 缺少“首次全量、后续增量”的压缩元数据。
5. 缺少压缩区间内工具读写行为的结构化保留。

---

## 3. 设计总原则

### 3.1 SM 与 Compaction 解耦

本设计采用如下核心判断：

- `SM 文件` 继续保留。
- `SM 文件` 不作为 compaction 的压缩输入替代物。
- `SM 文件` 只用于记录会话状态，属于记忆系统的一部分。
- `Compaction` 只负责上下文预算管理，不负责承载长期会话状态。

换句话说：

```text
SM = 会话状态记忆
Compaction = transcript 上下文预算管理
```

这两者有关联，但不应绑定为同一条主链。

### 3.2 两层压缩而非三层压缩

本项目不采用 Claude Code 的 `Micro + SM Compact + Full Compact` 三层结构，而采用：

```text
L1：Micro Compact
    每次 API 调用前执行，清理旧工具结果

L2：Full Compact
    达到阈值后执行，首次全量、后续增量压缩
```

这里没有单独的 “SM Compact” 层，因为 `SM` 不再承担 compaction 职责。

### 3.3 严格保护消息契约

任何压缩都必须保证：

- 不拆 assistant `tool_call` 与对应 `tool_result`
- 不破坏当前分支的消息链
- 不在 Ask 对齐期改写最近关键消息
- 压缩失败时不污染原始 session 状态

---

## 4. 从 Claude Code 借什么，不借什么

### 4.1 借鉴点

从 Claude Code 借鉴以下原则：

1. **压缩前置**
   - 每轮请求前先做低成本清理。
2. **阈值驱动**
   - 上下文超阈值后自动执行 full compact。
3. **边界保护**
   - 切割点必须避开 `tool_call / tool_result` 配对区间。
4. **格式化摘要**
   - 压缩材料和压缩结果都应采用严格模板。
5. **失败熔断**
   - 避免在不可恢复的超长上下文上无限重试压缩。

### 4.2 不直接照搬的部分

以下设计不直接采用：

1. **`cache_edits`**
   - Anthropic 专有能力，不符合 provider-neutral 边界。

2. **SM 文件参与压缩替代旧消息**
   - 当前项目明确将 `SM` 保留在记忆子域，不作为 compaction 主路径。

3. **后台 fork agent 持续维护 session memory 文件**
   - 当前仓库尚未具备稳定、安全、低耦合的摘要子代理主链。

### 4.3 适配后的核心结论

本项目的 compaction 方案应表达为：

```text
SM 文件：
    记录 objective、状态、关键事实、开放问题、下一步
    属于记忆系统，不参与 compaction 主链

Micro Compact：
    每次 API 前清理旧工具结果

Full Compact：
    首次全量压缩
    后续按上次压缩边界之后的新增区间做增量压缩
```

---

## 5. SM 文件定位

### 5.1 SM 文件是什么

`SM` 文件是 **会话状态记忆文件**，不是压缩摘要文件。

它的职责是为系统提供稳定、可持续引用的会话状态，而不是替代 transcript。

### 5.2 SM 文件记录什么

建议 `SM` 文件只记录稳定状态，不记录完整对话展开：

- 当前 objective
- 当前 mode
- 当前子任务
- 已确认事实
- 关键决策
- 已知约束
- 关键文件
- 未解决问题
- 下一步建议

### 5.3 SM 文件不记录什么

不建议放入：

- 每轮原始消息 transcript
- 大段工具输出
- 压缩摘要正文
- 每次 full compact 的临时增量 delta

### 5.4 为什么这样划分

因为如果让 `SM` 同时承担“状态记忆”和“压缩替代物”两种职责，会出现三个问题：

1. 语义混淆：无法区分“当前系统状态”和“被折叠的历史内容”。
2. 生命周期冲突：状态记忆应长期稳定，压缩摘要应随历史推进不断改写。
3. 产品边界混乱：记忆系统和上下文预算系统耦合过深。

---

## 6. 两层压缩方案

## 6.1 第一层：Micro Compact

### 定位

Micro Compact 是发送侧的保守预处理策略，用来在不破坏上下文连贯性的前提下，优先清理已经被后续对话“消化”的旧工具结果，尽可能推迟 Full Compact 的到来。

### 执行时机

不是每次 turn 必做。

真正向 provider 发起请求前，先基于原始 history 估算本轮上下文占用；只有当预计上下文占用超过总 budget 的 `60%` 时，才进入 Micro Compact 候选检查。

若预计占用未超过 `60%`，则完全跳过 Micro Compact，直接使用原始 history。

### 处理对象

第一版只处理白名单内、且位于“最近保留窗口”之外的旧工具结果。

建议白名单如下：

- `read` / `Read`
- `bash` / `powershell` 等 shell 类工具
- `grep` / `Grep`
- `glob` / `Glob`
- `web_search`
- `web_fetch`
- `str_replace_editor` / `FileEdit`
- `write` / `FileWrite`

### 基本策略

1. 仅处理“旧”工具结果，不处理当前 turn 产生的新结果。
2. 以“工具交互组”作为最小单位，而不是按单条 `TOOL` message 处理。
3. 永远保留最近 `8` 个工具交互组的原始内容，避免破坏当前工作面的短期记忆。
4. 只对更早的历史组做候选筛选；若候选组中的工具命中白名单，则允许进入替换流程。
5. 替换动作应发生在发送侧上下文视图中；原始 JSONL transcript 不直接删除内容。
6. 替换后保留可回溯引用，例如该工具结果在持久化 JSONL 中的 entry id / offset / range。
7. 不触碰用户消息和 assistant 的自然语言结论。
8. 不打断 `assistant tool_calls -> tool_result` 配对。

### 工具交互组定义

工具交互组是发送侧 compact 的基本单位：

- 起点：一条带 `tool_calls` 的 assistant message
- 后续：与之配对的一个或多个 `TOOL` result message
- 终点：遇到下一条普通 assistant / user message，或下一组 tool call 起点

按组处理的目的，是避免只压掉半段工具结果，导致上下文残缺。

### 第一版触发算法

建议落地为如下保守流程：

```text
准备发送 API
    ->
读取原始 history
    ->
估算上下文占用比例
    ->
若 <= 60%，跳过 Micro Compact
    ->
若 > 60%，按工具交互组切分 history
    ->
保留最近 8 组原始内容
    ->
检查更早历史中是否存在命中白名单的候选组
    ->
从最旧候选组开始做发送侧替换
    ->
每替换一组就重新估算上下文
    ->
当占用回落到安全区间（建议 45%~50%）或候选耗尽时停止
```

### 替换形式

第一版不建议“清空为完全不可读的占位符”，而应保留最小语义壳，方便模型知道这里曾经发生过什么，也方便后续诊断与恢复。

建议替换为类似结构：

```text
[Earlier tool result compacted]
tool: read
jsonl_ref: <entry_id_or_offset>
note: detailed tool output omitted; use persisted transcript for recovery
```

对于一整个工具交互组，可以只保留 assistant 原消息的简短自然语言结论，并把对应的旧 `TOOL` result 替换为上述引用占位。

### 白名单命中后的保护规则

即使命中白名单，也不代表一定可替换；第一版应增加保守保护条件：

1. 最近 `8` 个工具交互组一律不动。
2. 当前 turn 内产生的工具结果一律不动。
3. shell 类输出若包含明显失败信号，则不替换。
4. shell 类工具若退出码非 `0`，则不替换。
5. 包含高价值诊断信息的结果不替换，例如：
   - `error`
   - `exception`
   - `traceback`
   - `failed`
   - test failure / compile failure / stack trace
6. 若后续 assistant 明确继续引用某段精确输出，则该组不应优先替换。

换句话说，第一版遵循“宁可少压，也不误压”的原则。

### 发送侧与存储侧的建议

推荐优先做 **发送侧替换**：

- provider 看到的是已 compact 的上下文视图
- session 原始 transcript 保持完整，便于调试、恢复和审计

若后续确实需要在存储侧增加 compact 标记，也应仅追加元数据，而不是直接破坏原始内容，例如：

- `micro_compacted = true`
- `micro_compact_scope = send_view_only`
- `jsonl_ref`
- `original_length`
- `compacted_at`

### 与当前项目的结合方式

Micro Compact 应并入当前上下文构建路径，而不是继续依赖旧版 `context_compressor` 扩展主入口。

当前项目第一版的推荐实现边界如下：

- Product / Application 层负责：
  - 估算上下文使用率
  - 判断是否超过 `60%`
  - 按工具交互组筛选候选
  - 应用白名单与保护规则
  - 生成发送侧 compact 视图
- Agent Runtime 层只消费 compact 后的 history 视图，不负责决定是否 compact
- 持久化层继续保留原始 JSONL；若需要追溯，则通过 `jsonl_ref` 回到原始记录

---

## 6.2 第二层：Full Compact

### 定位

Full Compact 是正式的会话级压缩路径，在 Micro Compact 执行后，若上下文仍超过阈值，则自动触发。

### 执行时机

执行顺序固定为：

```text
准备发送 API
    ->
先做 Micro Compact
    ->
计算当前上下文大小
    ->
若超过 Full Compact 阈值，则执行 Full Compact
```

### Full Compact 的核心规则

1. 首次压缩采用 **全量压缩**。
2. 后续压缩采用 **增量压缩**。
3. 每次压缩都保留一段最近消息。
4. 压缩区间按 round unit 切割，不在工具配对中间截断。

---

## 7. Full Compact 详细设计

### 7.1 两个关键边界

Full Compact 需要维护两个边界：

- `compact_anchor`
  - 本次压缩区间的起始边界
- `cut_point`
  - 为保留 recent messages 而确定的切割点

本次压缩区间为：

```text
(compact_anchor, cut_point]
```

本次保留区间为：

```text
(cut_point, latest]
```

### 7.2 首次全量压缩

第一次执行 Full Compact 时：

- `compact_anchor` 视为“第一条有效消息之前”
- 也即压缩区间从第一条消息开始
- 保留最近消息窗口
- 把窗口之前的全部内容按“首次全量模板”压缩

### 7.3 后续增量压缩

后续 Full Compact 时：

- `compact_anchor` 不再是第一条消息
- 而是“上次压缩边界的下一个消息”
- 也就是说，只压缩自上次压缩之后新增的那一段历史
- 之前已被折叠进入 compact summary 的历史，不重复压缩原始 transcript

### 7.4 为什么要做增量压缩

增量压缩比每次全量重压更适合当前项目：

- 成本更低
- 输入更短
- 更容易保留最近工作上下文
- 更适合长会话 coding 场景

### 7.5 建议增加 rebase 机制

虽然主路径是增量压缩，但建议加入一个保护规则：

- 连续 `N` 次增量压缩后
- 触发一次 `summary rebase`

`rebase` 的含义不是重读全部原始消息，而是：

- 读取当前 canonical compact summary
- 再读取自上次边界后的新增区间
- 生成一份新的单一 summary

这样可以避免长期堆积多个 delta，导致语义漂移和摘要链膨胀。

---

## 8. Round Unit 切割策略

### 8.1 为什么不能直接按消息条数切

因为本项目消息里存在：

- assistant 文本
- assistant tool_calls
- tool_result

如果切割点落在 `tool_call` 和 `tool_result` 之间，压缩后上下文会破坏协议完整性。

### 8.2 本项目的切割单位

本项目不直接照搬 Claude Code 的 `groupMessagesByApiRound()`，而是定义自己的 `round unit`：

1. 一个 assistant 带 `tool_calls`
2. 加上其后连续对应的 `tool_result`
3. 作为一个完整 unit

如果 assistant 没有 `tool_calls`，则该 assistant 文本轮单独成为一个 unit。

### 8.3 `cut_point` 的寻找方式

在保留最近消息窗口时：

1. 先设定 `recent_token_budget`
2. 从最新消息开始向上回溯
3. 以 unit 为单位累计 token
4. 当累计达到预算后，确定 `cut_point`
5. 若 `cut_point` 落点不安全，则继续向更老的 unit 边界扩展

### 8.4 建议默认值

- `recent_token_budget = 12_000` 到 `24_000`
- `min_recent_text_messages = 4`
- `min_recent_round_units = 2`

这些值用于保护最近工作上下文，不让 Full Compact 把“马上要继续的工作面”压掉。

---

## 9. 压缩输入格式转换

### 9.1 为什么要转换

原始 transcript 是带 role 的结构化 JSONL 消息。

如果把这些结构直接原样扔给 LLM 做压缩，容易出现：

- 模型把旧 `user` 消息误当成当前用户最新要求
- 模型把工具输出误判成自然对话内容
- 模型忽略轮次边界和工具执行顺序

### 9.2 转换原则

在执行 Full Compact 前，需要将原始压缩区间转成 **纯文本 role transcript**：

```text
[USER]
...

[ASSISTANT]
...

[TOOL_CALL]
tool=read_file
arguments=...

[TOOL_RESULT]
...
```

### 9.3 转换要求

- 保留 role 语义
- 保留消息顺序
- 保留工具名称
- 必要时保留工具参数摘要
- 不再把 JSON 结构直接暴露给压缩模型

### 9.4 好处

- 降低角色污染风险
- 降低模型把旧 user 指令当成当前输入的概率
- 让模板更稳定
- 更方便在压缩结果中抽取读写轨迹

---

## 10. 压缩结果模板设计

### 10.1 两套模板

Full Compact 需要两套模板：

1. **首次全量压缩模板**
2. **后续增量压缩模板**

### 10.2 首次全量压缩模板应覆盖

- 全局 objective
- 已完成的主要工作
- 关键决策
- 重要事实
- 当前卡点
- 已执行的重要工具轨迹
- 关键读操作
- 关键写操作
- 下一步

### 10.3 后续增量压缩模板应覆盖

- 自上次压缩以来新增了什么
- 新读了哪些文件
- 新写了哪些文件
- 修复或引入了哪些问题
- 当前工作面如何变化
- 下一步如何接续

### 10.4 建议的结构化产物

建议最终产物采用如下结构：

```text
[Compact Summary]
Scope: full | incremental
Objective:
Mode:
Summary:
Reads:
Writes:
Searches:
Failures:
Key Decisions:
Next Step:
```

---

## 11. 工具执行轨迹保留

### 11.1 为什么必须保留读写轨迹

在 coding agent 场景下，最容易在压缩时丢失的不是闲聊，而是：

- 看过哪些文件
- 改过哪些文件
- 为什么改
- 哪些尝试失败了

如果这些信息丢掉，模型即使保留了“任务大意”，也难以继续接着工作。

### 11.2 建议保留的轨迹类型

- `reads`
  - 读了哪些文件
  - 读这些文件的原因
  - 读出的关键结论
- `writes`
  - 改了哪些文件
  - 改动意图
  - 是否成功
- `searches`
  - 搜索了哪些关键词
  - 命中了什么区域
- `failures`
  - 尝试过哪些失败路径
  - 失败原因

### 11.3 与工具协议的关系

压缩结果里不需要保存所有原始参数全文，但应保留可恢复工作的摘要级操作轨迹。

---

## 12. 模式适配策略

当前项目存在 `Chat / Ask / Study` 三模式，因此 Full Compact 的触发和保留策略不能完全一致。

### 12.1 Chat

- 启用 Micro Compact
- 启用自动 Full Compact
- recent messages 重点保留当前任务工作面

### 12.2 Ask

- `aligning` 阶段默认只启用 Micro Compact
- 非必要不自动做 Full Compact
- 若确实超限，优先保护最近对齐对话，不改写确认语义

### 12.3 Study

- 启用完整两层压缩
- `SM` 文件更新频率最高
- compact summary 重点保留学习过程中的读写轨迹、关键解释、当前学习状态

---

## 13. 分层职责设计

## 13.1 Product / Application 层

由 Product 层负责：

- 维护 `SM` 文件
- 维护 `compact_anchor`
- 决定是否执行 Micro Compact
- 决定是否执行 Full Compact
- 计算 `recent_token_budget`
- 选择首次全量模板或增量模板
- 记录压缩后的元数据

建议新增编排对象：

```text
CompactionCoordinator
```

### Product 层输出物

Product 层输出一个显式计划，由 runtime 消费：

```python
@dataclass
class CompactionPlan:
    use_micro_compact: bool
    use_full_compact: bool
    full_compact_scope: str | None = None  # full | incremental | rebase
    compact_anchor_entry_id: str | None = None
    cut_point_entry_id: str | None = None
    recent_token_budget: int = 0
    summary_block: str | None = None
```

## 13.2 Runtime 层

Runtime 只负责：

1. 执行 Product 层下发的压缩计划
2. 消费 summary block 和 recent messages
3. 保证上下文消息协议完整

Runtime 不负责：

- 决定 anchor
- 决定阈值
- 决定何时做首次全量或增量压缩

## 13.3 Infrastructure 层

Infrastructure 提供：

- token 估算
- transcript / compact 元数据落盘
- provider 调用
- 可选摘要模型封装

不得在这一层写入：

- mode 专属业务规则
- memory 提升策略
- Ask 阶段的产品决策

---

## 14. 数据结构建议

### 14.1 SM 文件 schema

建议将 `SM` 文件定义为稳定的会话状态快照，而不是 transcript 摘要：

```python
from dataclasses import dataclass, field
from typing import List


@dataclass
class SessionMemoryState:
    session_id: str
    objective: str = ""
    mode: str = "chat"
    current_subtask: str = ""
    confirmed_facts: List[str] = field(default_factory=list)
    key_decisions: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    key_files: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    updated_at: float = 0.0
```

设计要求：

- `SM` 只保留稳定状态，不记录逐轮 transcript。
- `SM` 可被 memory / study 子域读取，但不直接注入为 compact summary。
- `SM` 更新频率高于 Full Compact，但其内容比 compact summary 更稳定。

### 14.2 Compact 元数据 schema

建议为每个 session 单独维护一份 compact 元数据：

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CompactMetadata:
    session_id: str
    compact_anchor_entry_id: Optional[str] = None
    last_cut_point_entry_id: Optional[str] = None
    last_compact_scope: Optional[str] = None   # full | incremental | rebase
    incremental_count_since_rebase: int = 0
    consecutive_failures: int = 0
    last_summary_file: Optional[str] = None
    last_summary_hash: Optional[str] = None
    last_recent_token_budget: int = 0
    last_compacted_at: float = 0.0
```

字段说明：

- `compact_anchor_entry_id`
  - 下次增量压缩的逻辑起点。
- `last_cut_point_entry_id`
  - 上次 recent messages 的切割边界，便于调试和观测。
- `incremental_count_since_rebase`
  - 用来决定是否触发 `rebase`。
- `consecutive_failures`
  - 用于 compact circuit-breaker。

### 14.3 Full Compact 输入模型

建议在进入 Full Compact 之前，构造一个显式输入对象：

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CompactSourceUnit:
    unit_id: str
    start_entry_id: str
    end_entry_id: str
    estimated_tokens: int
    text_blocks: List[str] = field(default_factory=list)
    tool_reads: List[str] = field(default_factory=list)
    tool_writes: List[str] = field(default_factory=list)
    tool_searches: List[str] = field(default_factory=list)
    tool_failures: List[str] = field(default_factory=list)


@dataclass
class FullCompactInput:
    session_id: str
    scope: str  # full | incremental | rebase
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    recent_token_budget: int
    source_units: List[CompactSourceUnit] = field(default_factory=list)
    existing_summary: Optional[str] = None
    sm_state: Optional[SessionMemoryState] = None
```

### 14.4 Compact 结果模型

建议 Full Compact 的输出也结构化：

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CompactTraceSummary:
    reads: List[str] = field(default_factory=list)
    writes: List[str] = field(default_factory=list)
    searches: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)


@dataclass
class FullCompactResult:
    session_id: str
    scope: str
    summary_text: str
    compact_anchor_entry_id: Optional[str]
    cut_point_entry_id: str
    next_anchor_entry_id: Optional[str]
    preserved_entry_ids: List[str] = field(default_factory=list)
    trace_summary: CompactTraceSummary = field(default_factory=CompactTraceSummary)
    estimated_tokens_after: int = 0
```

---

## 15. Full Compact 模板建议

### 15.1 首次全量压缩模板

首次全量压缩的目标是把最早到 `cut_point` 之间的历史，压成一份单一、权威的 canonical summary。

建议模板：

```text
你正在为一个长会话 coding agent 生成首次全量压缩摘要。

你的任务：
1. 总结这段历史中已经完成的工作。
2. 保留对后续继续执行真正有用的信息。
3. 严格区分读操作、写操作、搜索、失败路径。
4. 不要把历史中的旧 user 消息误写成“当前用户的新要求”。

输入是按 role 转换后的纯文本 transcript。
请输出以下结构，且仅输出这些标题：

[Compact Summary]
Scope: full
Objective:
Mode:
Summary:
Confirmed Facts:
Key Decisions:
Reads:
Writes:
Searches:
Failures:
Open Issues:
Next Step:
```

额外约束：

- `Summary` 只写当前仍然影响后续执行的工作状态。
- `Reads` 记录关键读过的文件和得到的结论。
- `Writes` 记录已修改或计划修改的文件与意图。
- `Searches` 记录关键检索关键词和命中区域。
- `Failures` 记录重要失败尝试及其原因。
- 不要输出 JSON，不要输出代码块，不要输出额外解释。
```

### 15.2 后续增量压缩模板

增量压缩的目标不是重述整个会话，而是总结“自上次压缩后新增的变化”。

建议模板：

```text
你正在为一个长会话 coding agent 生成增量压缩摘要。

输入包括：
1. 已有的 canonical compact summary
2. 自上次压缩边界之后新增的 role transcript

你的任务：
1. 总结新增区间产生了哪些新信息。
2. 保留新的读写搜索失败轨迹。
3. 只描述“变化”，不要重复已有摘要里的稳定内容。

请输出以下结构，且仅输出这些标题：

[Compact Summary]
Scope: incremental
Delta Summary:
New Reads:
New Writes:
New Searches:
New Failures:
Updated Decisions:
Updated Open Issues:
Next Step:
```

额外约束：

- `Delta Summary` 只写新增变化。
- 如果某项没有变化，写 `None`。
- 不要复述旧的全局 objective，除非本轮发生变化。
- 不要输出 JSON，不要输出额外说明。
```

### 15.3 Rebase 模板

当增量压缩次数达到阈值后，建议执行一次 `rebase`：

```text
你正在把一份已有 compact summary 与新增增量摘要合并为新的 canonical summary。

目标：
1. 去重并消除冲突。
2. 保留仍对后续执行有价值的信息。
3. 输出单一的 canonical summary，供未来继续增量压缩。

请输出以下结构：

[Compact Summary]
Scope: rebase
Objective:
Mode:
Summary:
Confirmed Facts:
Key Decisions:
Reads:
Writes:
Searches:
Failures:
Open Issues:
Next Step:
```

约束：

- 不要保留过时步骤。
- 如果旧摘要和新增区间冲突，以更新后的状态为准。
- 保持内容简洁，但不能丢失关键读写轨迹。
```

---

## 16. 实现任务拆解

### 16.1 Phase 1：Schema 与存储

任务：

1. 定义 `SessionMemoryState`
2. 定义 `CompactMetadata`
3. 在 `FileStore` 中新增：
   - `save_session_memory_state(session_id, data)`
   - `load_session_memory_state(session_id)`
   - `save_compact_metadata(session_id, data)`
   - `load_compact_metadata(session_id)`
4. 在 `SessionManager` 中挂载 session 级 compact metadata 读写能力

验收：

- 新建 session 时可创建空 `SM` 状态和空 compact metadata
- 重启后可恢复

### 16.2 Phase 2：Micro Compact

任务：

1. 抽出旧工具结果识别逻辑
2. 定义工具结果保留窗口
3. 在上下文构建前执行发送侧清理
4. 为清理过的 TOOL 结果补 metadata

验收：

- 普通对话不受影响
- 长工具输出明显下降
- 不会破坏 tool 配对

### 16.3 Phase 3：Full Compact

任务：

1. 实现 round unit 构建
2. 实现 `cut_point` 计算
3. 实现首次全量压缩路径
4. 实现后续增量压缩路径
5. 实现 role transcript 转换
6. 实现 compact summary 落盘
7. 成功后推进 `compact_anchor`

验收：

- 首次 full compact 后可继续对话
- 第二次 full compact 能正确只压新增区间
- `compact_anchor` 推进正确

### 16.4 Phase 4：Rebase 与熔断

任务：

1. 加入 `incremental_count_since_rebase`
2. 到达阈值后执行 rebase
3. 加入 `consecutive_failures`
4. 接入 compact circuit-breaker

验收：

- 多轮增量 compact 不会无限堆 delta
- 不可恢复超限会停止自动 compact 重试

### 16.5 Phase 5：观测与调优

任务：

1. 记录 compact 前后 token
2. 记录读写搜索失败轨迹数量
3. 记录 full / incremental / rebase 比例
4. 调整 `recent_token_budget`
5. 调整 rebase 阈值

验收：

- 能基于指标分析 compact 效果
- 能识别压缩过轻或过重的 session

---

## 17. 推荐执行流

建议的执行流如下：

```text
用户输入
    ->
LearningAgentSystem._prepare_session_turn()
    ->
CompactionCoordinator.evaluate(session, profile)
    ->
执行 Micro Compact
    ->
重新计算上下文大小
    ->
若超阈值，执行 Full Compact
    ->
PreparedSessionTurn(compaction_plan)
    ->
AgentLoop.run()
    ->
ReActEngine.build_context(...)
```

### 17.1 正常时序

1. 每轮调用前先执行 Micro Compact。
2. 计算当前上下文 token。
3. 若未超阈值，直接进入正常调用。
4. 若超阈值，执行 Full Compact。
5. Full Compact 完成后写回新边界和 compact 元数据。

### 17.2 Full Compact 时序

1. 计算 `recent_token_budget`
2. 由最新消息向上寻找 `cut_point`
3. 根据是否首次压缩确定 `compact_anchor`
4. 抽取 `(compact_anchor, cut_point]` 区间
5. 转成纯文本 role transcript
6. 套用全量或增量模板
7. 产出 canonical compact summary
8. 保留 `(cut_point, latest]` recent messages

---

## 18. 阈值与熔断

### 18.1 预算定义

```text
effective_context_window =
    provider_max_context_length - reserved_output_tokens
```

建议：

- `reserved_output_tokens = min(8_000, effective_context_window * 0.15)`

### 18.2 触发阈值

建议：

- `full_compact_threshold = 0.85`
- `blocking_limit = 0.93`

执行逻辑：

- 每轮先做 Micro Compact
- Micro Compact 后再测 token
- 若仍超过 `full_compact_threshold`，自动执行 Full Compact

### 18.3 熔断

建议保留：

- `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`

熔断后：

- 停止自动 Full Compact
- 保留 manual compact
- 保留 emergency truncation
- 观测层标记当前 session 已进入 compact circuit-breaker

---

## 19. 消息与协议不变式

compaction 必须始终满足以下不变式：

1. 不拆 assistant `tool_call` 与对应 `tool_result`。
2. 不破坏当前 session 分支的 `current_leaf_id` 语义。
3. 不在 Ask `aligning` 阶段改写关键最近消息。
4. Full Compact 后仍可继续组装合法 provider 消息。
5. compact summary 与 recent messages 的拼接顺序稳定。

---

## 20. 与现有代码的落点映射

### 17.1 新增文件建议

- `learning_agent/learning_agent/compaction/coordinator.py`
- `learning_agent/learning_agent/compaction/full_compact.py`
- `learning_agent/learning_agent/compaction/micro_compact.py`
- `learning_agent/learning_agent/compaction/models.py`

### 17.2 修改文件建议

- `learning_agent/learning_agent/main.py`
  - 在 `_prepare_session_turn()` 中引入 compaction 评估
- `learning_agent/learning_agent/mode_service.py`
  - 为 `ModeProfile / TurnExecutionProfile` 增加 compaction policy
- `learning_agent/learning_agent/session_manager.py`
  - 维护 compact anchor、last_compact_metadata、SM 状态文件引用
- `learning_agent/agent/session_runtime.py`
  - 消费 `PreparedSessionTurn` 下发的 compaction plan
- `learning_agent/agent/react_engine.py`
  - 在上下文构建路径中执行 Micro Compact，并消费 Full Compact 结果
- `learning_agent/ai/file_store.py`
  - 提供 compact metadata、SM 文件、summary block 落盘接口

### 17.3 对旧版 `context_compressor` 的处理建议

旧版 `learning_agent/learning_agent/extensions/context_compressor.py` 不建议继续作为主方案推进。

原因：

- 它依赖旧 Hook 模型
- 它更像“扩展内自裁决”
- 不符合当前“Product 决策、Runtime 消费”的边界

建议：

- 将其中可复用的 token budget 和 turn-unit 逻辑抽出来
- 不再保留它作为默认压缩主入口

---

## 21. 观测与回退

### 18.1 必须新增的指标

- Micro Compact 命中次数
- Full Compact 触发次数
- Full Compact 成功率
- 首次全量压缩次数
- 增量压缩次数
- rebase 次数
- compact 后 token 降幅
- 被保留 recent messages 的 token 数
- 读轨迹条数
- 写轨迹条数
- consecutive failure 次数
- emergency truncation 次数

### 18.2 回退顺序

推荐回退路径：

```text
Micro Compact
    ->
Full Compact
    ->
emergency truncation
    ->
blocking / manual compact
```

注意：

- 任意压缩失败都不得破坏原始 transcript
- 增量压缩失败时，不清空上一次 compact summary
- 只有在新 compact 成功落盘后，才推进 `compact_anchor`

---

## 22. 分阶段实施建议

### Phase 1：确立 SM 与 Compaction 边界

目标：

- 保留 `SM` 文件
- 从文档和实现上明确 `SM` 不参与 compaction 主路径

产出：

- `SM` schema
- compact metadata schema

### Phase 2：落地 Micro Compact

目标：

- 每轮 API 前稳定清理旧工具结果

产出：

- 发送侧 Micro Compact
- 工具结果保留窗口

### Phase 3：落地 Full Compact

目标：

- 实现首次全量、后续增量压缩

产出：

- `compact_anchor`
- `cut_point`
- 两套模板
- role transcript 转换

### Phase 4：补齐增量 rebase 与观测

目标：

- 避免摘要链膨胀
- 让增量 compact 长时间可持续

产出：

- canonical summary 重写
- 更完整的读写轨迹指标

---

## 23. 最终结论

当前项目最合适的路线不是把 Claude Code 的 `SM Compact` 原样搬进来，而是做下面这个收敛：

1. **保留 SM 文件，但把它留在记忆系统中**
2. **压缩主链只保留 Micro Compact 和 Full Compact 两层**
3. **Full Compact 首次全量、后续增量**
4. **压缩区间按 round unit 切割，严格保护 tool 协议完整**
5. **压缩前将 JSONL role 消息转换为纯文本 role transcript**
6. **压缩结果中结构化保留读写搜索失败轨迹**

这条路线既吸收了 Claude Code 在阈值、切割、不变式保护上的经验，又符合当前项目已经明确的四层边界，同时也更贴近本项目对 `SM` 的真实产品定位：**SM 是记忆，不是压缩替代物。**
