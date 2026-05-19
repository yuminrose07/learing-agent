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
- 不修改 session。
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
- 持久化 compact metadata / summary。
- 生成 Runtime 可消费的 `CompactionPlan`。

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
- 上报 usage、context build、compact applied 等观测事件。

Runtime 不决定压缩范围，不判断 slact 语义，不执行 summary prompt 的产品策略。

### 3.4 Infrastructure

负责存储和 provider 调用：

- 保存 compact summary 文件。
- 保存 compact metadata。
- 记录 full transcript 的可回溯引用。
- 提供 summary provider 调用适配。

Infrastructure 不决定何时 compact，也不解释 slact 命令。

对于 full compact，Infrastructure 还需要提供稳定的 JSONL 读取能力：

- 按 session 读取 JSONL delta。
- 从指定 cursor 之后读取增量 delta。
- 将 delta replay 成可压缩的 `SessionEntry` 序列。
- 返回 entry id 到 JSONL line / offset 的映射，供 metadata 和审计使用。
- 过滤敏感 tool output，只向 compact source 提供安全摘要、状态、引用和必要元数据。

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
- `source_jsonl_cursor` 和 `next_jsonl_cursor` 用于把本次 compact 区间绑定到持久化 JSONL。

### 8.4 CompactMetadata

建议扩展：

```python
@dataclass
class CompactMetadata:
    session_id: str
    compact_anchor_entry_id: str | None = None
    last_cut_point_entry_id: str | None = None
    last_compact_mode: str | None = None
    last_compact_scope: str | None = None
    incremental_count_since_rebase: int = 0
    consecutive_failures: int = 0
    last_summary_file: str | None = None
    last_summary_hash: str | None = None
    last_source_entry_ids: list[str] = field(default_factory=list)
    last_retained_entry_ids: list[str] = field(default_factory=list)
    last_source_jsonl_cursor: str | None = None
    next_jsonl_cursor: str | None = None
    last_jsonl_path: str | None = None
    last_prompt_template: str | None = None
    last_compacted_at: float = 0.0
```

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
    从 JSONL 起点或 snapshot 起点 replay 到 cut_point。

incremental:
    从 metadata.next_jsonl_cursor 之后 replay 到本次 cut_point。

rebase:
    读取 existing summary，再读取 next_jsonl_cursor 之后的新 delta。
```

如果 JSONL 中存在 patch delta，压缩输入必须先按顺序 replay patch，不能只读取 append entry 的初始内容。

compact source 只能包含已经完成的消息事实。对于 assistant 流式输出，必须以 `message_end` 事件作为完整消息边界；`message.stream_failed`、`message.interrupted` 等失败事件默认只用于 UI/observability，不进入 summary，除非 Product 层转换为明确的学习上下文说明。

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

- source 从 `compact_anchor_entry_id` / `next_jsonl_cursor` 之后开始。
- 保留 recent window 或 slact 指定 retained messages。
- summary 生成时读取 existing summary。
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
已整理上下文：压缩 42 条消息，保留最近 8 条消息，summary 已保存。
```

不要把完整 summary 强行发给用户，除非用户要求查看。

---

## 12. 实施步骤

### 阶段 1：修正 Auto Compact 主链

目标：让 full compact 真正减少上下文。

任务：

1. `CompactionPlan` 增加 `source_entry_ids` 和 `retained_entry_ids`。
2. `build_full_compact_result` 返回 retained ids 时包含 cut point entry 本身。
3. `ReActEngine.build_context` 根据 retained ids 重建 history view。
4. `CompactionCoordinator` 从 session JSONL transcript 读取 compact source，而不是直接扫描内存 message 数组。
5. 增加测试：被 compact 的旧内容不再出现在 provider messages 中。

### 阶段 2：引入 Prompt 模板与后处理

目标：替换低保真 `_summarize_transcript`。

任务：

1. 新增 `learning_agent/learning_agent/compaction/prompts.py`。
2. 实现 auto prefix / slact full / slact from / slact up_to / incremental / rebase 模板。
3. 实现 `format_compact_summary()`，剥离 `<analysis>`。
4. 将九章节结构作为强约束。
5. 为每类模板加入学习场景 few-shot 示例，至少覆盖 auto/up_to、full/from、incremental/rebase 三组差异。

### 阶段 3：接入主链 Summary Executor

目标：在不引入子代理的前提下，为 auto compact 和 slact 提供统一摘要执行入口，并复用主代理已有的重试、熔断和降级机制。

任务：

1. 定义 `SummaryExecutor` port。
2. 第一版由主代理主链路通过 provider single-pass 调用执行摘要。
3. 接入现有 retry / circuit breaker / failure metadata。
4. 强制 no-tools 行为。
5. 暂不实现子代理或 fork agent。

### 阶段 4：实现 Slact

目标：提供用户可控压缩。

任务：

1. Interface 增加 `/slact` 命令或 Web action。
2. Product 层新增 `SlactService`。
3. 支持 full / from / up_to / rebase。
4. pivot 自动移动到 safe round boundary。
5. 完成用户反馈和观测事件。

### 阶段 5：完善 Incremental 与 Rebase

目标：避免 summary 越来越长和语义漂移。

任务：

1. incremental 生成新的 canonical summary，而不是简单追加。
2. rebase 合并 existing summary 与 delta。
3. 增加 summary token budget。
4. 增加 obsolete pending task 清理规则。
5. 用 JSONL cursor 推进 compact anchor，保证增量压缩只处理持久化 transcript 的新增区间。

---

## 13. 测试计划

### 13.1 单元测试

覆盖：

- safe boundary 计算。
- auto prefix source / retained 划分。
- slact full / from / up_to 的消息重建顺序。
- `<analysis>` 剥离。
- 九章节缺失时的 fallback 或 validation warning。
- few-shot 示例不会被保存到 compact summary。
- incremental 合并不丢用户最新纠正。
- rebase 输出单一 canonical summary。

### 13.2 集成测试

覆盖：

- compact 后 provider messages 不包含 source entry 原文。
- retained recent messages 原文保留。
- compact source 来自 JSONL replay，且 patch delta 被正确应用。
- incremental 只读取 JSONL cursor 之后的新增区间。
- compact source 只包含 `message_end` 后的完整 assistant 消息，不包含失败或中断的半成品流式内容。
- 敏感 tool output 不进入 compact source，summary 只能基于安全摘要和引用生成。
- auto compact 后 assistant 不向用户复述 summary。
- slact 命令完成后 metadata 与 summary 文件一致。
- ContextLengthError 前置 compact 生效。

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

---

## 14. 当前代码风险对应

当前实现需要重点修复：

1. `ReActEngine.build_context` 不能在注入 summary 后继续加入完整 history。
2. `find_cut_point` 与 `list_entries_after` 的边界语义需要统一，避免漏掉 cut point entry。
3. `_summarize_transcript` 不能继续作为正式摘要实现，只能作为 fallback。
4. `merge_incremental_summary` 不应长期简单追加 `[Incremental Update]`。
5. `render_summary_block` 需要加入 continuation wrapper 和 auto compact suppress-follow-up 指令。
6. trace summary 需要从字符串判断升级为结构化工具轨迹。
7. full compact 的 source selection 应从持久化 JSONL transcript 读取和 replay，不能只依赖内存 `session.entries`。
8. 需要区分持久化层 snapshot/delta 合并与对话上下文 full compact，避免上下文压缩误清空原始 JSONL transcript。

---

## 15. 非目标

第一版不做：

- 不实现 Claude Code 的 provider cache edits。
- 不让 SM 文件承担 compact summary 主路径。
- 不在 Runtime 层解释 slact 命令。
- 不删除原始 session transcript。
- 不把内存 message 数组作为 full compact 的唯一事实源。
- 不要求 summary executor 主动读取文件补信息。
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
8. Full compact 从持久化 JSONL transcript 读取压缩区间，并通过 JSONL cursor 支持增量压缩。
9. Runtime 上下文中不再出现已被 source summary 替代的旧消息原文。
10. compact 失败不污染 session 状态，并触发熔断或降级。
