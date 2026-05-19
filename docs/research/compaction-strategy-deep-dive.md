# Claude Code 上下文压缩策略调研报告

## 1. 调研背景与目标

### 1.1 调研动机

在长会话 LLM 应用（尤其是编程助手场景）中，上下文窗口管理是核心工程挑战。Claude Code（CC）作为 Anthropic 官方推出的命令行编程助手，其对话长度往往达到数万甚至数十万 tokens。如何在接近上下文上限时有效释放空间，同时保留对后续任务有用的信息，直接影响用户体验和 API 成本。

本调研旨在通过源码分析，深度拆解 CC 的上下文压缩策略，提炼其设计决策背后的工程权衡，为构建类似系统提供参考。

### 1.2 调研范围

| 模块 | 文件 | 行数 | 调研重点 |
|------|------|------|----------|
| 自动触发 | `services/compact/autoCompact.ts` | 351 | 阈值计算、熔断机制 |
| 微压缩 | `services/compact/microCompact.ts` | 530 | Time-Based / Cache-Based 双路径 |
| 完整压缩 | `services/compact/compact.ts` | 1705 | Full Compact 引擎、上下文重建 |
| 摘要 Prompt | `services/compact/prompt.ts` | 374 | Fork Agent 的摘要生成指令 |
| 消息分组 | `services/compact/grouping.ts` | 63 | API Round 边界判定 |
| SM 压缩 | `services/compact/sessionMemoryCompact.ts` | 630 | SM Compact 实现、保留策略 |
| SM 提取 | `services/SessionMemory/sessionMemory.ts` | 495 | 后台笔记维护机制 |
| SM 工具 | `services/SessionMemory/sessionMemoryUtils.ts` | 207 | 配置与状态管理 |
| SM 模板 | `services/SessionMemory/prompts.ts` | 324 | 笔记模板结构、更新 Prompt |
| 清理 | `services/compact/postCompactCleanup.ts` | 77 | 压缩后状态清理 |

---

## 2. 核心发现：三层递进式压缩体系

### 2.1 总体架构

CC 采用**三层递进**策略，按优先级依次尝试，每层对应不同的成本级别和适用场景：

```
┌──────────────────────────────────────────────────────────────┐
│                      三层压缩体系                             │
├─────────────┬─────────────┬─────────────┬────────────────────┤
│    层级     │   触发时机   │   核心手段   │      API 成本      │
├─────────────┼─────────────┼─────────────┼────────────────────┤
│ L1 Micro    │ 每轮请求前  │ 清理工具结果 │        零          │
│    Compact  │ (预防性)    │            │                    │
├─────────────┼─────────────┼─────────────┼────────────────────┤
│ L2 Session  │ Token 超阈值│ 用预维护笔记 │        零          │
│    Memory   │ (反应性)    │ 替代旧消息   │                    │
│    Compact  │             │            │                    │
├─────────────┼─────────────┼─────────────┼────────────────────┤
│ L3 Full     │ L1/L2 失败  │ Fork Agent   │        高          │
│    Compact  │ (兜底)      │ 生成摘要     │ (LLM 调用 + 延迟)  │
└─────────────┴─────────────┴─────────────┴────────────────────┘
```

**关键洞察**：三层不是"同时作用"，而是按优先级**依次尝试**。Micro Compact 是日常预防，SM Compact 是压缩触发时的首选，Full Compact 是最后的兜底。

---

## 3. L1 层：Micro Compact 调研分析

### 3.1 设计动机

在编程场景中，上下文膨胀的主要驱动因素是**工具结果**（文件内容、Shell 输出、搜索结果等）。这些结果通常占据 token 的大头，但模型在生成下一轮回复时已经"看过"并消化了它们。Micro Compact 的核心假设是：**删除旧工具结果的详细内容，不会显著影响后续对话质量。**

### 3.2 双路径设计的工程考量

Micro Compact 包含两条**互斥**路径，二选一：

#### 路径 A：Time-Based

**触发条件**：距上次 Assistant 消息超过 `gapThresholdMinutes`。

**操作**：直接修改本地消息数组，将旧 `tool_result` 的 `content` 替换为固定字符串 `"[Old tool result content cleared]"`。

**调研发现**：
- 保留策略是**按时间顺序保留最后 N 个**工具结果，与工具类型无关。这不是"同工具保留最新"，而是"保留最近 N 个"，不管它们是什么工具。
- 修改本地消息会导致 Server Cache 失效（前缀匹配失败），下次请求需全价处理。
- 触发后会重置 Cache-Based 的状态（`resetMicrocompactState()`），防止下次 Cache-Based 尝试编辑已不存在的 Cache 条目。

#### 路径 B：Cache-Based

**触发条件**：Feature gate 开启 + 模型支持 + 主线程 + 工具结果数超阈值。

**操作**：**不修改本地消息**。在 API 请求中附加 `cache_edits` 指令，让 Anthropic 服务器在内部逻辑删除指定工具结果。

**调研发现**：
- 本地消息完全不变，Server Cache 前缀仍然有效（Cache Hit）。
- 需要维护 `pinnedEdits` 状态，每轮请求都要重新发送 edits 指令。
- 仅对主线程生效，防止 Fork Agent 的工具结果污染主线程的 Cache 状态。

#### 为什么 Time-Based 优先于 Cache-Based？

代码注释给出了明确答案：
> "If the gap since the last assistant message exceeds the threshold, the server cache has expired and the full prefix will be rewritten regardless — so content-clear old tool results now... Cached MC is skipped when this fires: editing assumes a warm cache, and we just established it's cold."

**核心逻辑**：时间间隔超过阈值意味着 Cache 已冷，此时 Cache-Based 的"保 Cache"价值消失，不如直接改本地内容来得简单直接。

### 3.3 处理对象的限制

仅清理 `COMPACTABLE_TOOLS` 集合内的 8 类工具：ReadFile、Shell、Grep、Glob、WebSearch、WebFetch、FileEdit、FileWrite。

**为什么只有这 8 类？** 调研发现这些工具的输出特征是**大块文本内容**，占 token 大头且模型已消化。其他工具（如 Task、Agent 调用）的结果可能包含需要持续参考的语义信息，不清理。

### 3.4 与 Full Compact 的关系澄清

Micro Compact **不是**为了"减轻 Full Compact 的负担"。它的目标是**避免 Full Compact 被触发**。两者触发条件完全不同：

| | Micro Compact | Full Compact |
|---|---|---|
| 触发阈值 | 时间间隔 / 工具数量 | `contextWindow - 13K - reservedOutput` (~167K-179K) |
| 触发频率 | 每轮请求前都检查 | 仅在 token 超阈值时 |
| 目标 | 推迟压缩到来 | 解决已发生的上下文危机 |

---

## 4. L2 层：Session Memory Compact 调研分析

### 4.1 核心设计洞察：成本前置

Session Memory Compact 最关键的设计决策是**将压缩的成本分摊到平时**。

传统做法（Full Compact）是在压缩时才一次性支付高昂成本（Fork Agent + LLM 调用生成摘要）。CC 的做法是：在对话进行中**持续维护一个轻量摘要文件**，压缩时直接读取复用，实现**零 API 成本**的压缩。

### 4.2 Session Memory 文件的生命周期

**创建**：对话开始时创建空文件，写入默认 Markdown 模板（10 个固定章节）。

**更新**：通过 `registerPostSamplingHook` 在每轮模型采样后触发，由 Fork Agent 执行。

**触发条件**：
- 初始化：10K tokens
- 常规更新：Token 增长 ≥ 5K **且** Tool Call ≥ 3 个
- 自然停顿更新：Token 增长 ≥ 5K **且** 最后一轮无 Tool Call

**调研发现**：更新频率远高于压缩触发频率。以 200K 窗口为例，SM 在达到 170K 压缩阈值前已更新 30+ 次。

**权限严格限制**：Fork Agent 只能用 `FileEditTool` 且只能编辑 SM 文件本身，其他所有工具全部拒绝。这是防止子 Agent 在执行笔记任务时越界操作。

### 4.3 SM Compact 的压缩逻辑

SM Compact 不是"用 SM 文件替换全部历史消息"。它的逻辑是：

1. 找到 `lastSummarizedMessageId`（上次 SM 提取完成时的消息边界）
2. **保留该边界之后的原始消息**（`messagesToKeep`）
3. **用 SM 文件替代该边界之前的历史消息**

**保留策略的参数**：
- `minTokens: 10_000` — 至少保留 10K tokens
- `minTextBlockMessages: 5` — 至少保留 5 条含文本的消息
- `maxTokens: 40_000` — 最多保留 40K tokens（硬上限）

**调研发现**：`adjustIndexToPreserveAPIInvariants` 函数在切分后会向前扩展索引，确保不拆分 `tool_use/tool_result` 对，不丢失共享 `message.id` 的 thinking 块。这体现了 CC 对 API 契约的严格保护意识。

### 4.4 失败条件与回退

SM Compact 会在以下情况失败（返回 `null`，回退到 Full Compact）：

1. Feature gate 未开启
2. SM 文件不存在或为空模板
3. `lastSummarizedMessageId` 在当前消息中找不到（消息被修改过）
4. **压缩后 token 仍超阈值**（`postCompactTokenCount >= autoCompactThreshold`）

第 4 点值得注意：即使 SM Compact 成功执行了，如果 SM 文件（最大 12K）+ messagesToKeep（最大 40K）+ 其他上下文仍然超过压缩阈值，它会主动放弃，让位给 Full Compact。这说明 SM Compact 有**自我评估机制**，不会强行执行无效的压缩。

### 4.5 与 Full Compact 的交互问题

**调研发现一个重要副作用**：Full Compact 完成后会调用 `setLastSummarizedMessageId(undefined)`，重置边界标记。

这意味着：
- 下次 SM Compact 会进入"恢复会话"模式（`lastSummarizedIndex = messages.length - 1`）
- 此时 SM Compact 会尝试保留**所有消息**，SM 文件作为"全局摘要"插入
- 这更容易触发第 4 点失败条件（保留太多导致仍超阈值）

**结论**：Full Compact 和 SM Compact 之间存在状态耦合，Full Compact 后 SM Compact 的有效性会暂时下降。

---

## 5. L3 层：Full Compact 调研分析

### 5.1 消息分组机制

Full Compact 使用 `groupMessagesByApiRound` 将消息按 **Assistant `message.id` 变化**为界分组。

**调研发现**：
- `message.id` 来自 Anthropic API 服务器，不是 CC 客户端生成。
- Streaming 模式下，同一个 API 响应的所有 chunks 共享同一个 `message.id`。
- 分组边界天然保证了 `tool_use/tool_result` 配对的完整性（API 契约要求）。
- 一个 group 不是"用户发消息 → assistant 回复结束"，而是"从 assistant 开始生成，到下一个 assistant 开始生成之间的所有消息"。

### 5.2 摘要 Prompt 的设计特征

Full Compact 的 Prompt 要求生成高度结构化的摘要（9 个强制章节），体现了对**编程场景信息保留**的深入理解：

**必须保留的信息**：
- 用户的**所有显式请求**和意图（非工具结果的用户消息）
- **完整代码片段**（不是摘要，是原文）
- **文件路径、函数签名**
- **错误及修复方式**（尤其是用户的纠正反馈）
- **当前正在做的具体工作**（需引用最近对话原文）

**Prompt 的强制性要求**：
- "Include full code snippets where applicable"
- "Pay special attention to specific user feedback"
- "Include direct quotes from the most recent conversation"
- "Your entire response must be plain text: an `<analysis>` block followed by a `<summary>` block"

**调研发现**：Prompt 开头有强硬的 "NO TOOLS PREAMBLE"，明确禁止子 Agent 调用任何工具。这是因为子 Agent 的 `maxTurns: 1`，一旦尝试工具调用就会被拒绝，导致无文本输出。

### 5.3 压缩后上下文重建

Full Compact 不是简单删除历史只留摘要，而是设计了一套**重建机制**：

```typescript
POST_COMPACT_MAX_FILES_TO_RESTORE = 5       // 最近读取的 5 个文件
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000    // 每个文件最多 5K tokens
POST_COMPACT_TOKEN_BUDGET = 50_000          // 重建总预算
```

**调研发现**：压缩后会重新注入最近读取的文件附件。这是基于一个观察——用户最近看过的文件极可能在下一轮对话中被再次引用。通过保留这些文件内容（而非依赖摘要中的描述），减少了"压缩后模型忘记文件细节"的问题。

### 5.4 Prompt-Too-Long 的兜底逃生

如果 Full Compact 的请求本身也触发了 `prompt_too_long`（上下文大到连压缩请求都塞不下），CC 有一个最后的逃生通道 `truncateHeadForPTLRetry`：

- 按 API Round 分组
- 从**最老**的组开始丢弃
- 保留至少一组用于摘要
- 丢弃比例：如果能解析 token gap 则按需丢弃，否则丢弃 20%

**调研发现**：这是一个"有损但安全"的兜底策略。它避免了用户在极端情况下完全卡住，代价是丢失最老的历史上下文。

---

## 6. 触发机制与熔断设计

### 6.1 阈值计算

```
Effective Context Window = ContextWindow - min(MaxOutputTokens, 20_000)
Auto Compact Threshold = EffectiveContextWindow - 13_000
Blocking Limit = EffectiveContextWindow - 3_000
```

**调研发现**：
- `13_000` 的缓冲是为了压缩后仍有空间继续对话
- `20_000` 的摘要输出预留是基于 p99.99 的 compact summary 输出为 17,387 tokens 的统计
- `3_000` 的 manual compact 缓冲是给用户手动压缩留的余地

### 6.2 熔断机制

```typescript
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3
```

**调研发现的真实数据**：
> "BQ 2026-03-10: 1,279 sessions had 50+ consecutive failures (up to 3,272) in a single session, wasting ~250K API calls/day globally."

**熔断后的行为**：
- Proactive AutoCompact 直接跳过（不再尝试）
- 但后续还有多层防线：Blocking Limit 预拦截、API 返回 413 后的 Reactive Compact、Context Collapse 等
- 最终最坏情况：用户看到 "Prompt is too long"，需手动 `/compact`

**调研结论**：熔断不是"躺平不管"，而是"停止无效尝试，把机会留给后面的防线"。

---

## 7. 设计决策分析

### 7.1 为什么三层策略的优先级是这样？

| 策略 | 为什么优先？ | 本质原因 |
|------|-------------|---------|
| SM Compact 优先于 Micro Compact | SM Compact 在压缩触发时才执行，Micro Compact 是每轮预防；两者不在同一阶段竞争 | 阶段不同 |
| SM Compact 优先于 Full Compact | 零 API 成本 vs 高成本 | 成本递减原则 |
| Micro Compact 优先于 Full Compact | Micro Compact 在 Full Compact 之前就执行了（预防性） | 时机不同 |

### 7.2 为什么 SM 文件要持续维护而不是临时生成？

**传统思路**：压缩时才让 LLM 读历史写摘要。
**CC 思路**：平时就 Fork Agent 持续更新摘要，压缩时直接读文件。

**优劣对比**：

| | 临时生成（传统） | 持续维护（CC） |
|---|---|---|
| 压缩时延迟 | 5-15 秒（用户可感知卡顿） | 毫秒级（读本地文件） |
| 压缩时成本 | 一次 LLM API 调用 | 零 |
| 摘要质量 | 一次性生成，可能遗漏 | 持续维护，更连贯 |
| 平时成本 | 零 | 每次更新需 Fork Agent + LLM 调用 |
| 复杂度 | 低 | 高（需维护文件、阈值、并发控制） |

**调研结论**：CC 选择了"平时持续支付小成本，压缩时零成本"的权衡。这对于**高频长会话**场景（如编程助手）是合理的，因为压缩会被触发多次，每次节省的延迟和 API 费用累积后超过平时维护的成本。

### 7.3 为什么 Micro Compact 要分两条路径？

**核心矛盾**：清理旧工具结果时，"改本地内容"简单但破坏 Cache，"不改本地内容"复杂但保 Cache。

**CC 的解法**：根据 Cache 状态选择路径——Cache 冷时用简单路径（Time-Based），Cache 热时用复杂路径（Cache-Based）。

---

## 8. 优势与局限

### 8.1 优势

1. **分层成本模型**：不同场景用不同成本的方案，避免过度支付。
2. **成本前置**：SM 文件的持续维护将压缩的"大成本"拆分为"小成本"。
3. **Cache 协同**：Cache-Based Microcompact 展示了与 Provider 特性深度配合的可能性。
4. **严格的安全边界**：Fork Agent 的权限限制、Query Source 的递归保护、API 不变性保护。
5. **多层防线**：熔断后有 Blocking Limit、Reactive Compact 等兜底，不轻易让用户卡住。

### 8.2 局限

1. **SM 文件大小上限（12K）可能不足以摘要超长复杂对话**：如果对话涉及大量文件、复杂架构，12K 的笔记可能过于精简。
2. **Full Compact 后 SM Compact 的有效性下降**：`lastSummarizedMessageId` 被重置导致"恢复会话"模式。
3. **Cache-Based 依赖 Anthropic 专有 API**：`cache_edits` 不是通用标准，难以移植到其他 Provider。
4. **Session Memory 的更新阈值（5K + 3 tool calls）可能过于保守**：在快速迭代的对话中，笔记更新可能滞后于实际进展。
5. **Time-Based 和 Cache-Based 的状态干扰**：Time-Based 触发后必须重置 Cache-Based 状态，增加了系统复杂性。

---

## 9. 可借鉴模式与建议

### 9.1 可直接借鉴的模式

#### 模式 A：分层渐进式压缩

```
L1：结构化数据清理（工具结果、日志等大体积但已消化内容）
L2：预维护摘要的复用（后台持续维护笔记/摘要）
L3：LLM 现场生成摘要（兜底）
```

**适用条件**：会话长度可能达到上下文上限的长对话系统。

#### 模式 B：后台预维护摘要

**核心做法**：
- 使用 Hook 机制在后台触发更新
- Fork 隔离的 Sub Agent 执行
- 严格限制 Sub Agent 权限
- 设置合理的更新阈值（避免过于频繁）

**适用条件**：对话有明显"轮次"或"任务"概念的系统。

#### 模式 C：Cache-Aware 压缩

**核心做法**：
- 区分"改内容会破坏 Cache"和"发指令不会"
- Cache 冷时直接改内容
- Cache 热时用元数据操作

**适用条件**：使用支持 Prompt Cache 的 LLM Provider。

### 9.2 需要谨慎对待的设计

1. **SM 文件的大小上限**：如果业务场景比编程更复杂（如法律文档分析、多轮医疗问诊），12K 的笔记上限可能不够，需要更大的预算或分层笔记结构。

2. **Full Compact 后的状态重置**：如果借鉴 SM Compact，需要考虑 Full Compact 后如何恢复 SM 的边界标记，避免进入"恢复会话"模式。

3. **Fork Agent 的并发控制**：SM 提取使用 `sequential` 包装防止并发，但等待超时只有 15 秒。在高并发场景下可能需要更 robust 的队列机制。

### 9.3 适配建议

如果要借鉴到非编程场景的 LLM 系统：

1. **先识别"大体积但已消化"的内容类型**：不同场景膨胀来源不同（客服场景可能是知识库检索结果，医疗场景可能是检查报告）。

2. **设计适合业务场景的摘要模板**：CC 的 10 章节模板针对编程优化，其他场景需要重新设计。

3. **评估"成本前置"是否划算**：如果会话通常很短（<50K tokens），SM 文件的持续维护可能是过度设计。

4. **考虑 Provider 特性**：如果没有 `cache_edits` 类似机制，Cache-Based Microcompact 路径需要重新设计或放弃。

---

## 10. 结论

Claude Code 的三层压缩策略是一个**经过大量生产数据验证**的上下文管理体系。其核心设计哲学可以总结为：

1. **成本分层**：从"零成本清理"到"零成本复用"到"高成本生成"，避免在简单场景过度支付。
2. **成本前置**：通过后台持续维护摘要文件，将压缩的大成本拆分为平时的小成本。
3. **防御深度**：多层防线（Micro → SM → Full → Reactive → Blocking Limit），不轻易让用户卡住。
4. **契约保护**：在任何删除/替换操作中都严格维护 API 不变性（Tool 配对、Streaming 块关联）。

对于构建类似系统的建议是：**先实现 Micro Compact 识别并清理大体积已消化内容，再评估是否需要 SM 文件的预维护机制，最后保留 Full Compact 作为兜底。** 三层不必一次性全部实现，可以按优先级逐步迭代。

---

*调研完成时间：2026-05-17*
*源码版本：基于 `/Users/roseannk/claude-code-analysis/src` 目录下的压缩与记忆相关代码*
