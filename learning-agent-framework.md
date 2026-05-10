# Learning-Agent 框架设计文档 v0.2

> 本文档面向个人学习场景，基于对 Claude Code 与 pi-mono 两个项目的架构分析，结合学习者的核心痛点与长期需求整理而成。
> **核心理念：最小核心，最大扩展。先让骨架跑起来，再通过 Hook 和扩展系统持续生长。**

---

## 目录

1. [设计背景与核心痛点](#1-设计背景与核心痛点)
2. [学习场景下被遗漏的问题](#2-学习场景下被遗漏的问题)
3. [系统架构](#3-系统架构)
4. [核心数据模型](#4-核心数据模型)
5. [核心流程](#5-核心流程)
6. [当前版本功能模块](#6-当前版本功能模块)
7. [扩展系统设计](#7-扩展系统设计)
8. [Provider 层与流式传输](#8-provider-层与流式传输)
9. [关键设计决策](#9-关键设计决策)
10. [学习路径建议](#10-学习路径建议)
11. [核心设计原则](#11-核心设计原则)
12. [待补充清单](#12-待补充清单)

---

## 1. 设计背景与核心痛点

### 1.1 为什么不做 Coding-Agent

现有主流 Agent（Claude Code、pi 等）面向编码场景优化，工具集（文件编辑、Bash 执行、LSP）和交互模式都围绕"解决当前任务"设计。学习场景有着本质不同的需求：时间跨度更长、目标更模糊、需要大量关联与记忆、强调内化而非执行。

### 1.2 原痛点与架构回应

| 痛点 | 具体表现 | 框架级回应 |
|------|---------|-----------|
| **1. 没有知识图谱与记忆系统** | 几乎无跨会话记忆，更无结构化知识沉淀 | 设计**记忆系统**作为一等公民，基础持久化 + 知识关联 |
| **2. Agent 无法持续进化** | 功能硬编码，新增能力需改动核心 | 设计**扩展系统（Hook + 事件驱动）**，功能可插拔、可热更新 |
| **3. 没有不同学习模式** | 分析代码和总结用同一套交互；总结只基于当前对话，无知识关联 | 学习模式作为**扩展**注册，核心不耦合具体模式逻辑 |
| **4. 长对话必须新开会话** | 新会话从零开始，无法延续之前的问题与上下文 | 设计**树形会话空间** + **话题锚点** |
| **5. 难清晰表达问题** | AI 生成答案偏离，反复修正浪费 token | 澄清协议作为**内置扩展**，通过 Hook 介入主循环 |
| **6. 保存不便且难联系旧知识** | 需手动要求保存，内容孤立无关联 | 设计**透明文件化存储** + **自动关联归档** |

---

## 2. 学习场景下被遗漏的问题

这些问题当前可能感受不深，但使用一个月后会逐渐暴露。

### 2.1 学习是目标导向的，不是对话导向的

Coding-Agent 以"完成当前任务"为目标，用完即走。学习是以月甚至年为单位的持续过程。

- **问题**：Agent 如何理解你的长期学习目标？比如"我要掌握分布式系统"，agent 应将此大目标拆解为知识路径，每次对话都是路径上的一步，而非零散问答。
- **回应**：引入 **Learning Objective Graph（学习目标图）**，agent 的所有行为围绕"当前目标进度"展开。
- **当前版本**：先支持基础目标创建与绑定，目标拆解通过后续扩展实现。

### 2.2 学习有"输入-内化-输出"的完整闭环

你只提到了"问 AI、让 AI 总结"（输入），但学习的关键在于**输出**。

- **问题**：读完一段源码，是否动手画了流程图？看完论文是否用自己的话复述？如果 agent 只给答案，你仍只是被动接收。
- **回应**：设计 **Output Prompting（输出倒逼）** 机制。agent 在合适时机要求你产出（写笔记、画关系图、做类比、提问题），并评估产出质量。
- **当前版本**：作为内置扩展 `core-output-prompting` 实现基础倒逼逻辑。

### 2.3 学习材料是多源异构的

你可能同时在学习：一本书的 PDF、一个 GitHub 仓库、一个视频课程、几篇博客、自己的笔记。

- **问题**：agent 怎么理解"这些材料都在讲同一个概念"？怎么在不同材料之间做交叉引用？
- **回应**：设计 **Material Ingestion Pipeline（材料摄入管线）**，所有材料统一提取为 **Knowledge Chunk（知识块）**，再做去重、关联、溯源。
- **当前版本**：先支持基础文本材料（Markdown / 纯文本），其他类型通过扩展实现。

### 2.4 学习需要遗忘管理

存下来的笔记如果从不复习，等于没存。

- **问题**：agent 怎么知道"这个知识点你快忘了"？怎么主动安排复习？
- **回应**：引入 **Spaced Repetition Engine（间隔重复引擎）**，结合记忆层级做主动复习提醒。不是 Anki 那种机械卡片，而是**在对话中自然植入复习**。
- **当前版本**：内置扩展 `core-review` 实现简化 SM-2 算法。

### 2.5 学习需要可验证的掌握度

"我好像懂了"和"真懂"之间差很远。

- **问题**：agent 怎么评估你对某个知识点的真实掌握？不能靠自我报告，需要通过**试探性提问、让你解释、让你应用**来验证。
- **回应**：设计 **Mastery Estimation（掌握度估计）**，每个 Knowledge Node 附带掌握度概率，agent 根据你的输出动态更新。
- **当前版本**：内置扩展 `core-mastery` 实现基础掌握度规则。

### 2.6 学习容易认知过载与挫败

AI 一次给太多信息，或解释太深/太浅，都会让学习者放弃。

- **问题**：agent 如何感知你的困惑？如何动态调节解释深度？
- **回应**：设计 **Cognitive Load Monitor（认知负荷监测）**，通过你的追问模式、停顿时间、否定词频率等信号，动态调整输出粒度。
- **当前版本**：暂不实现，标记为后续扩展。

### 2.7 学习需要来源可信度

AI 可能会胡说。在学习场景下，**错误知识的危害比"不知道"更大**。

- **问题**：agent 给出的知识，哪些来自你上传的材料？哪些来自模型训练数据？哪些来自之前的笔记？
- **回应**：设计 **Provenance Tracking（来源追溯）**，每个知识片段都标记来源类型和可信度等级。
- **当前版本**：知识节点基础字段支持来源标记。

### 2.8 学习是非线性探索的

你可能学 A 时联想到 B，跳到 B 后又回到 A，这种**联想式学习**是深度理解的关键。

- **问题**：agent 如何记录并鼓励这种"跳跃"？如何在跳跃后帮你保持上下文不丢失？
- **回应**：设计 **Associative Trail（联想轨迹）**，记录你的探索路径，允许随时回溯到任意节点。
- **当前版本**：暂不实现，标记为后续扩展。

---

## 3. 系统架构

### 3.1 核心原则：最小核心，最大扩展

一个 Agent 不可能一开始就造得很完美。需求会在使用中不断浮现，能力会在迭代中持续增强。因此：

- **核心只提供骨架**：会话管理、基础记忆、扩展系统、Provider 层、基础持久化
- **所有功能都通过扩展实现**：学习模式、意图解析、输出倒逼、间隔重复、掌握度估计……
- **Hook + 事件驱动**：让扩展可以在不修改核心的前提下，介入任何流程、响应任何状态变化

### 3.2 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  L5: 交互层 (Presentation Layer)                             │
│  Web UI / 知识图谱可视化 / 学习仪表盘 / 材料浏览器              │
│  【流式传输：实时渲染 Provider 返回的 token 流】              │
├─────────────────────────────────────────────────────────────┤
│  L4: 应用层 (Application Layer)                              │
│  会话管理 / 目标追踪 / 复习调度                               │
├─────────────────────────────────────────────────────────────┤
│  L3: 智能层 (Intelligence Layer)                             │
│  Agent 循环 / 意图解析 / 上下文组装 / 输出倒逼                  │
│  【通过 Hook 介入，通过 Provider 调用 LLM】                   │
├─────────────────────────────────────────────────────────────┤
│  L2.5: 扩展层 (Extension Layer)  ◄── 新增                    │
│  Hook 系统 / 事件总线 / 扩展管理器 / 工具注册                  │
│  【系统的"神经系统"，所有功能生长的土壤】                      │
├─────────────────────────────────────────────────────────────┤
│  L2: 记忆层 (Memory Layer)   ◄── 当前核心                    │
│  四层记忆体系 / 知识图谱 / 间隔重复引擎 / 掌握度估计            │
│  【基础实现优先，高级功能通过扩展完善】                        │
├─────────────────────────────────────────────────────────────┤
│  L1.5: Provider Layer  ◄── 新增                              │
│  OpenAI SDK / 流式传输 / 模型适配 / 重试策略                   │
│  【当前先支持 OpenAI，其他模型后续添加 Provider】              │
├─────────────────────────────────────────────────────────────┤
│  L1: 持久化层 (Persistence Layer)                            │
│  基础文件存储 / 材料摄入 / 索引                               │
│  【当前只做基础实现，高级解析通过扩展完善】                    │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 各层职责

| 层级 | 核心职责 | 不做什么 |
|------|---------|---------|
| L5 交互层 | 渲染 UI，处理用户输入事件，展示图谱与仪表盘；**实时渲染流式 token** | 不直接调用 LLM，不处理业务逻辑 |
| L4 应用层 | 管理学习目标、会话生命周期、复习提醒 | 不直接解析材料，不做向量检索 |
| L3 智能层 | LLM 调用编排（通过 Provider）、工具选择与执行、意图解析 | 不持久化数据，不管理会话树结构 |
| L2.5 扩展层 | 管理扩展生命周期、调度 Hook、广播事件 | 不实现具体业务逻辑 |
| L2 记忆层 | 记忆的读写、关联、检索、压缩、遗忘、复习调度 | 不调用 LLM，不感知 UI |
| L1.5 Provider 层 | 封装 LLM API，提供统一流式/非流式接口 | 不做业务语义判断 |
| L1 持久化层 | 文件的读写、基础材料的解析与分块 | 不做业务语义判断 |

---

## 4. 核心数据模型

以下是系统中最关键的实体。注意：这是逻辑模型，具体存储格式（SQL/JSON/MD）在"关键设计决策"中决定。

### 4.1 Learning Objective（学习目标）

学习不是零散对话，而是围绕目标的持续过程。

```yaml
id: "obj-uuid"
title: "掌握分布式系统共识算法"
description: "理解 Raft/Paxos 的核心机制，能独立分析其正确性证明"
parentId: "obj-parent-uuid"      # 可选，支持目标树
status: active | paused | completed | abandoned
createdAt: "2026-05-06T10:00:00Z"
targetDate: "2026-08-01"
completedAt: null
relatedMaterialIds: ["mat-1", "mat-2"]
progressMetrics:
  totalNodes: 42
  masteredNodes: 7
  inProgressNodes: 12
  lastActivity: "2026-05-06T22:00:00Z"
```

### 4.2 Knowledge Node（知识节点）

知识图谱的基本单元，也是记忆的核心载体。

```yaml
id: "kn-uuid"
type: concept | procedure | principle | analogy | question | code_pattern
content:
  text: "Raft 中 Leader 选举需要获得半数以上节点的投票"
  code: null
  formula: null
sourceMaterialId: "mat-1"
sourceLocation:
  type: pdf
  page: 127
  paragraph: 3
confidence: 0.95           # 来源可信度 [0-1]
createdAt: "2026-05-06T10:30:00Z"
updatedAt: "2026-05-06T15:00:00Z"
masteryLevel: estimated | familiar | understood | mastered
lastReviewedAt: "2026-05-06T15:00:00Z"
nextReviewAt: "2026-05-08T15:00:00Z"     # 由间隔重复计算
tags: ["raft", "consensus", "leader-election"]
relatedNodeIds: ["kn-2", "kn-3"]         # 知识图谱的边
conversationIds: ["sess-1"]              # 哪些会话涉及此节点
reviewHistory:
  - date: "2026-05-06T15:00:00Z"
    result: pass | struggle | fail
```

### 4.3 Learning Session（学习会话）—— 树形结构

不是线性对话，而是可分叉、可回溯的探索树。

```yaml
id: "sess-uuid"
objectiveId: "obj-uuid"
title: "Raft Leader 选举机制分析"      # 可自动生成或用户命名
rootEntryId: "entry-root"
currentLeafId: "entry-5"
status: active | archived | compacted
createdAt: "2026-05-06T10:00:00Z"
lastAccessedAt: "2026-05-06T22:00:00Z"
extractedKnowledgeIds: ["kn-1", "kn-2"]

entries:
  - id: "entry-1"
    parentId: null
    type: message
    role: user
    content: "帮我分析 Raft 的 Leader 选举"
    timestamp: "2026-05-06T10:00:00Z"
    metadata:
      mode: explore
      knowledgeNodesExtracted: []

  - id: "entry-2"
    parentId: "entry-1"
    type: message
    role: assistant
    content: "Raft 的 Leader 选举分为两个阶段..."
    timestamp: "2026-05-06T10:00:05Z"
    metadata:
      mode: explore
      toolCalls: ["read_material"]
      knowledgeNodesExtracted: ["kn-1"]

  - id: "entry-3"
    parentId: "entry-2"
    type: fork_point                  # 用户决定从这里分叉探索
    content: "分叉：探索选举安全性证明"
    timestamp: "2026-05-06T10:15:00Z"
    metadata: {}

  - id: "entry-4"
    parentId: "entry-2"               # 另一条分支：继续原话题
    type: message
    role: user
    content: "如果网络分区怎么办？"
    timestamp: "2026-05-06T10:16:00Z"

  - id: "entry-5"
    parentId: "entry-4"
    type: message
    role: assistant
    content: "网络分区时..."
    timestamp: "2026-05-06T10:16:05Z"
```

### 4.4 Material（学习材料）

所有学习输入的统一抽象。

```yaml
id: "mat-uuid"
type: pdf | web_page | video | code_repo | markdown_note | book | audio
title: "《设计数据密集型应用》第9章"
uri: "https://example.com/book.pdf"
localPath: "/data/materials/ddia-ch9.pdf"
ingestionStatus: pending | processing | indexed | failed
chunks:
  - id: "chunk-1"
    content: "共识问题是指..."
    embedding: null                   # 可选，不强依赖
    startLoc: { page: 300, paragraph: 1 }
    endLoc: { page: 300, paragraph: 5 }
extractedKnowledgeIds: ["kn-1", "kn-2"]
addedAt: "2026-05-01T10:00:00Z"
```

### 4.5 Extension（扩展）

扩展是系统能力增长的基本单元。

```yaml
id: "ext-core-intent"
name: "core-intent"
version: "0.1.0"
type: builtin | external
status: active | inactive

# 声明此扩展关心的 Hook 点
hooks:
  - agent.beforeIntentParse
  - agent.afterIntentParse

# 声明此扩展订阅的事件
events:
  - session.messageAdded

# 声明此扩展注册的工具
tools:
  - id: "parse_intent"
    description: "解析用户输入的学习意图"
    
# 配置项（用户可覆盖）
config:
  confidenceThreshold: 0.7
```

### 4.6 Event（事件）

系统内部通信的"神经脉冲"。

```yaml
id: "evt-uuid"
type: "knowledge.extracted"        # 事件类型
payload:
  nodes: ["kn-1", "kn-2"]
  source: "session-1"
source: "ext-core-memory"          # 发布者
timestamp: "2026-05-06T10:30:00Z"
sessionId: "sess-1"
```

---

## 5. 核心流程

### 5.1 学习主循环（流式版）

```
用户输入 / 材料引用 / 复习提醒 / 关联建议
        │
        ▼
┌───────────────┐
│  Hook 触发    │
│  agent.before │
│  IntentParse  │
└───────────────┘
        │
        ▼
┌───────────────┐
│  意图解析      │
│  Intent Parse  │
└───────────────┘
        │
    需要澄清？ ──→ [问题重构循环] ──→ 返回用户确认
        │ 否
        ▼
┌───────────────┐
│  Hook 触发    │
│  agent.after  │
│  IntentParse  │
└───────────────┘
        │
        ▼
┌───────────────┐
│  上下文组装    │
│  Context Build │
└───────────────┘
        │
        ├── 当前目标相关的 Knowledge Nodes（按掌握度排序）
        ├── 当前会话历史（从根到当前叶子的路径）
        ├── Relevant Memories（基于当前话题的相关记忆）
        ├── 间隔重复触发的待复习节点
        └── 联想轨迹的邻近节点
        │
        ▼
┌───────────────┐
│  Hook 触发    │
│  agent.before │
│  LLMCall      │
└───────────────┘
        │
        ▼
┌───────────────┐
│  Provider     │
│  streamChat   │
│  (OpenAI SDK) │
└───────────────┘
        │
        ├── 建立 SSE 连接
        ├── 逐 chunk 返回
        │
        ▼
┌───────────────┐
│  Hook 触发    │
│  agent.on     │
│  StreamChunk  │
└───────────────┘
        │
        ├── 发布 Event: agent.responseChunk
        ├── 实时推送到 Presentation Layer
        │
        ▼
┌───────────────┐
│  流结束       │
└───────────────┘
        │
        ▼
┌───────────────┐
│  Hook 触发    │
│  agent.after  │
│  Response     │
└───────────────┘
        │
        ├── 思考 / 回答 / 工具调用计划
        │
        ▼
┌───────────────┐
│  工具执行      │
│  Tool Exec    │
└───────────────┘
        │
        ├── 触发 Hook: agent.onToolCall
        ├── read_material      ── 读取学习材料
        ├── write_note         ── 写入笔记
        ├── create_knowledge_node  ── 创建知识节点
        ├── create_connection  ── 建立知识关联
        ├── ask_clarification  ── 向用户澄清
        ├── generate_quiz      ── 生成测验题
        ├── update_mastery     ── 更新掌握度
        ├── schedule_review    ── 安排复习
        └── switch_mode        ── 切换学习模式
        │
        ├── 触发 Hook: agent.afterToolResult
        │
        ▼
┌───────────────┐
│  结果处理      │
│  Result Proc  │
└───────────────┘
        │
        ├── 提取新知识节点 → 更新知识图谱
        ├── 评估用户输出 → 更新掌握度
        ├── 更新会话树 → 追加 entry
        ├── 检查是否需要输出倒逼（用户是否只是被动接收？）
        └── 生成关联建议 → 可能触发新的联想轨迹
        │
        ▼
┌───────────────┐
│  会话结束检查  │
│  End Check    │
└───────────────┘
        │
        ├── 过长？→ 生成 Session Summary → 可能 Compaction
        ├── 用户离开？→ 自动归档 + 提取待复习节点
        └── 检测到认知过载？→ 建议休息或简化
```

### 5.2 记忆生命周期

```
原始对话 / 材料内容
        │
        ▼
┌──────────────────┐
│ 实时提取          │
│ Real-time Extract │
└──────────────────┘
        │
        ├── 会话中识别 Knowledge Node 候选
        │
        ▼
┌──────────────────┐
│ 确认/修正         │
│ User Confirm      │
└──────────────────┘
        │
        ├── 自动确认（用户设置）或手动确认
        │
        ▼
┌──────────────────┐
│ L2: Working      │
│    Memory        │
└──────────────────┘
        │
        ├── 活跃的、近期使用的知识节点
        ├── 参与当前会话的上下文
        │
        ▼
┌──────────────────┐
│ 关联与结构化      │
│ Link & Structure  │
└──────────────────┘
        │
        ├── 链接到已有节点
        ├── 创建新边
        ├── 标记来源（Provenance）
        │
        ▼
┌──────────────────┐
│ 掌握度评估        │
│ Mastery Estimation│
└──────────────────┘
        │
        ├── 根据用户追问深度更新
        ├── 根据输出质量更新
        ├── 根据测验表现更新
        │
        ▼
┌──────────────────┐
│ 复习调度          │
│ Review Schedule   │
└──────────────────┘
        │
        ├── 间隔重复算法计算 nextReviewAt
        │
        ▼
┌──────────────────┐
│ L3: Long-term    │
│    Memory        │
└──────────────────┘
        │
        ├── 已掌握、低频访问但仍重要的知识
        ├── 归档状态，但仍参与复习调度
        │
        ▼
┌──────────────────┐
│ 遗忘/退化检测     │
│ Fading Detection  │
└──────────────────┘
        │
        ├── 太久未复习的 → 标记为 faded
        ├── 下次遇到时主动提醒复习
        │
        ▼
    [需要时重新激活 → 回到 Working Memory]
```

### 5.3 材料摄入管线（当前简化版）

```
材料输入（Markdown / 纯文本 / PDF / URL / 代码仓库 / 视频）
        │
        ▼
┌──────────────────┐
│ 解析              │
│ Parse             │
└──────────────────┘
        │
        ├── 文本 → 保持原有格式 + 解析链接
        ├── PDF → 通过扩展实现
        ├── 代码 → 通过扩展实现
        ├── 网页 → 通过扩展实现
        └── 视频 → 通过扩展实现
        │
        ▼
┌──────────────────┐
│ 语义分块          │
│ Semantic Chunking │
└──────────────────┘
        │
        ├── 不是固定长度切分
        ├── 按语义边界：章节/主题/定义/论证/代码块
        ├── 每个 chunk 保持上下文完整性
        │
        ▼
┌──────────────────┐
│ 去重              │
│ Deduplication     │
└──────────────────┘
        │
        ├── 与已有 chunks 对比
        ├── 避免重复摄入相同内容
        │
        ▼
┌──────────────────┐
│ 预提取            │
│ Pre-extract       │
└──────────────────┘
        │
        ├── 识别关键概念、定义、问题
        ├── 生成候选 Knowledge Nodes
        │
        ▼
┌──────────────────┐
│ 索引              │
│ Index             │
└──────────────────┘
        │
        ├── 存储 + 建立可检索索引
        ├── 可选：向量索引（通过扩展），但不强依赖
        │
        ▼
┌──────────────────┐
│ 关联提示          │
│ Link Suggestion   │
└──────────────────┘
        │
        └── 通知 agent：新材料与哪些已有知识相关
```

### 5.4 扩展加载与激活流程

```
系统启动
    │
    ▼
┌──────────────────┐
│ 扫描扩展目录      │
│ Scan Extensions   │
└──────────────────┘
    │
    ├── 读取内置扩展（core-*）
    ├── 读取外部扩展（extensions/ 目录）
    │
    ▼
┌──────────────────┐
│ 依赖解析          │
│ Resolve Deps      │
└──────────────────┘
    │
    ├── 检查扩展间依赖关系
    ├── 拓扑排序确定加载顺序
    │
    ▼
┌──────────────────┐
│ 逐个激活          │
│ Activate          │
└──────────────────┘
    │
    ├── 调用 extension.activate(context)
    ├── 注册 Hook 处理函数
    ├── 订阅 Event 类型
    ├── 注册 Tool
    │
    ▼
┌──────────────────┐
│ 发布事件          │
│ Publish Event     │
└──────────────────┘
    │
    └── extension.activated
```

---

## 6. 当前版本功能模块

> **原则：当前只实现最小可用集，其他功能通过扩展系统预留接口。**

### 模块 A：会话引擎（Conversation Engine）

**职责**：管理学习对话的生命周期与树形结构。

**当前实现**：
- ✅ 树形会话的创建、追加、fork、resume、导航
- ✅ 消息流的管理与状态同步
- ⏳ 会话 Compaction（压缩旧分支为摘要）—— 后续实现
- ⏳ 会话归档与检索 —— 后续实现

**关键设计问题**：
- fork 的粒度：是按用户消息 fork，还是任意节点都可 fork？
- 分支可视化：如何展示一棵对话树而不让用户困惑？

### 模块 B：记忆与知识系统（Memory & Knowledge）⭐ 当前核心

**职责**：所有记忆的持久化、关联、检索与生命周期管理。

**当前实现**：
- ✅ 四层记忆的读写与管理（基础接口）
- ✅ 知识节点的 CRUD 与基础关系维护
- ⏳ 间隔重复调度器（简化 SM-2）—— 内置扩展 `core-review`
- ⏳ 掌握度估计器（基础规则）—— 内置扩展 `core-mastery`
- ⏳ 记忆 Relevant Recall（基于当前话题选择相关记忆）—— 基础版本
- ⏳ 联想轨迹的记录与导航 —— 后续扩展

**关键设计问题**：
- 知识图谱用文件存储还是数据库？
- 如何表示"弱关联"（两个概念只是隐约相关）vs "强关联"（直接依赖）？

### 模块 C：智能核心（Agent Core）

**职责**：LLM 调用编排、工具系统、意图理解。

**当前实现**：
- ✅ LLM 流式调用与事件管理（通过 Provider Layer）
- ✅ 工具定义、调度、执行、错误处理
- ⏳ 意图解析与置信度评估 —— 内置扩展 `core-intent`
- ⏳ 问题重构循环（当意图不明确时）—— 内置扩展 `core-clarification`
- ⏳ 输出倒逼策略的执行 —— 内置扩展 `core-output-prompting`

**关键设计问题**：
- 工具调用是串行还是并行？
- 意图解析用一次 LLM 调用还是规则+模型混合？

### 模块 D：扩展系统（Extension System）⭐ 当前核心

**职责**：管理扩展的生命周期、Hook 调度、事件总线。

**当前实现**：
- ✅ Extension Manager：扩展的加载、激活、停用
- ✅ Hook System：Hook 注册、执行顺序管理
- ✅ Event Bus：事件的发布、订阅、广播
- ✅ Tool Registry：工具的注册与发现

**内置扩展清单**：

| 扩展 ID | 功能 | 优先级 |
|---------|------|--------|
| `core-intent` | 意图解析 | P0 |
| `core-clarification` | 澄清协议 | P0 |
| `core-context` | 上下文组装 | P0 |
| `core-output-prompting` | 输出倒逼 | P1 |
| `core-review` | 间隔重复调度 | P1 |
| `core-mastery` | 掌握度估计 | P1 |
| `core-material-text` | 文本材料解析 | P0 |

**关键设计问题**：
- Hook 执行顺序如何确定？（按优先级 + 依赖关系）
- 扩展出错时是否影响核心流程？（建议：Hook 错误可配置为忽略或中断）

### 模块 E：Provider 层（Provider Layer）⭐ 当前核心

**职责**：封装所有 LLM API 调用，提供统一接口。

**当前实现**：
- ✅ OpenAI Provider（基于 `openai` SDK）
- ✅ 流式传输（`stream: true`，SSE）
- ✅ 非流式调用兜底
- ✅ 基础错误处理与转换

**后续扩展**：
- ⏳ Anthropic Provider（Claude API）
- ⏳ Local Provider（Ollama / llama.cpp）
- ⏳ Azure OpenAI Provider

**关键设计问题**：
- 流式传输中断如何处理？（AbortController 信号传递）
- 不同 Provider 的 tool calling 格式差异如何统一？

### 模块 F：持久化层（Persistence Layer）

**职责**：所有数据的物理存储、材料摄入、索引构建。

**当前实现**：
- ✅ File Store：记忆文件、会话文件、配置文件的读写（Markdown / JSONL）
- ✅ 基础文本材料解析
- ✅ 简单文本索引

**后续通过扩展完善**：
- ⏳ PDF 解析器扩展
- ⏳ 网页解析器扩展
- ⏳ 代码解析器扩展
- ⏳ 向量索引扩展

### 模块 G：交互层（Interface）

**职责**：Web 前端的所有展示与交互。

**当前实现**：
- ✅ 对话界面（支持流式渲染）
- ⏳ 知识图谱可视化 —— 后续实现
- ⏳ 学习仪表盘 —— 后续实现
- ⏳ 材料浏览器与阅读器 —— 后续实现
- ⏳ 联想轨迹导航器 —— 后续实现
- ⏳ 复习提醒通知 —— 后续实现

**关键设计问题**：
- 流式 Markdown 如何实时解析？（可能需要流式 Markdown 解析器）
- 树形对话的 UI 怎么设计才不混乱？

---

## 7. 扩展系统设计

### 7.1 为什么必须做扩展系统

Agent 不可能一开始就造得完美：

1. **需求会涌现**：你用了一个月后，才发现自己需要"代码执行工具"或"PDF 高亮关联"
2. **能力会进化**：今天用 GPT-4，明天可能用 Claude 3，后天可能用本地模型
3. **场景会变化**：个人学习 → 小组学习 → 教学辅助，需求完全不同
4. **不想改动核心**：每加一个功能都改核心代码，系统会迅速腐化

### 7.2 扩展系统的两个支柱

#### 支柱一：Hook（流程介入）

Hook 让扩展可以**在流程的特定节点修改数据或改变行为**。

```typescript
// 扩展注册 Hook
extensionContext.registerHook('agent.afterResponse', async (response, context) => {
  // 检查用户是否过于被动
  const isPassive = checkPassiveMode(context.session);
  if (isPassive) {
    // 追加输出倒逼要求
    response.content += '\n\n💡 请用自己的话总结一下刚才的内容。';
  }
});

// Hook 可以修改数据
extensionContext.registerHook('memory.beforeStore', async (node, context) => {
  // 自动为知识节点生成标签
  node.tags = await autoTag(node.content);
});

// Hook 可以中断流程
extensionContext.registerHook('agent.beforeIntentParse', async (input, context) => {
  if (input.text.length < 3) {
    throw new HookAbortError('输入太短，无法解析意图');
  }
});
```

#### 支柱二：Event（状态通知）

Event 让扩展可以**响应系统的状态变化**，而无需关心是谁触发的。

```typescript
// 扩展订阅事件
extensionContext.subscribeEvent('knowledge.confirmed', async (event) => {
  // 新知识确认后，自动安排第一次复习
  const node = event.payload.node;
  await scheduleReview(node.id, '1d');
});

// 扩展也可以发布事件
extensionContext.publishEvent({
  type: 'custom.reviewDue',
  payload: { nodeIds: [...] },
  source: 'my-custom-extension'
});
```

### 7.3 扩展的生命周期

```
扫描 → 加载 → 依赖检查 → 初始化 → 激活（注册 Hook + 订阅 Event）→ 运行 → 停用
```

### 7.4 扩展的分类

| 类型 | 说明 | 示例 |
|------|------|------|
| **内置扩展** | 核心团队维护，随系统发布 | `core-intent`, `core-review` |
| **外部扩展** | 用户或第三方开发，动态加载 | `pdf-parser`, `anki-sync` |
| **临时扩展** | 单次会话有效，用户自定义规则 | 当前会话的"简化解释模式" |

---

## 8. Provider 层与流式传输

### 8.1 为什么需要 Provider 层

- **统一接口**：上层代码只调用 `provider.streamChat()`，不关心底层是 OpenAI、Claude 还是本地模型
- **流式优先**：现代 LLM API 都支持流式，Provider 层负责将不同 SDK 的流式输出统一为 `AsyncIterable`
- **容错处理**：自动重试、超时、降级，上层无需关心
- **测试友好**：可以注入 Mock Provider，无需联网即可测试

### 8.2 OpenAI Provider 实现

```typescript
import OpenAI from 'openai';

class OpenAIProvider implements Provider {
  private client: OpenAI;
  
  constructor(config: { apiKey: string; baseURL?: string }) {
    this.client = new OpenAI(config);
  }
  
  async *streamChat(params: ChatParams): AsyncIterable<ChatChunk> {
    const stream = await this.client.chat.completions.create({
      model: params.model,
      messages: params.messages,
      temperature: params.temperature,
      tools: params.tools,
      stream: true,  // 启用流式
    });
    
    for await (const chunk of stream) {
      yield {
        content: chunk.choices[0]?.delta?.content || '',
        toolCall: chunk.choices[0]?.delta?.tool_calls?.[0],
        finishReason: chunk.choices[0]?.finish_reason,
      };
    }
  }
  
  supportsToolCalling(): boolean { return true; }
  supportsVision(): boolean { return true; }
  getMaxContextLength(): number { return 128000; }
}
```

### 8.3 流式传输在系统中的流动

```
[Provider Layer]          [Extension Layer]          [Presentation Layer]
      │                           │                             │
      │  AsyncIterable<chunk>     │                             │
      │──────────────────────────>│                             │
      │                           │  Hook: agent.onStreamChunk  │
      │                           │  Event: agent.responseChunk │
      │                           │────────────────────────────>│
      │                           │                             │ 实时渲染
      │                           │                             │
```

### 8.4 流式中断

用户可以在生成过程中点击"停止"。中断信号通过 `AbortController` 传递：

```typescript
const abortController = new AbortController();

// 启动流式调用
const stream = provider.streamChat(params, { signal: abortController.signal });

// 用户点击停止
abortController.abort();

// Provider 捕获 AbortError，优雅关闭连接
```

---

## 9. 关键设计决策

### 决策 1：最小核心，最大扩展

**决策**：核心系统只包含会话管理、基础记忆、扩展系统骨架、Provider 层、基础持久化。所有其他能力都通过扩展系统实现。

**理由**：
- Agent 不可能一开始就造得完美，必须持续迭代
- 硬编码的功能无法在不改动核心的情况下替换或升级
- Hook + 事件驱动让第三方开发者也能扩展系统

### 决策 2：记忆存储格式

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 纯 Markdown 文件（Claude Code 式） | 最透明，人可直接读写，git 友好 | 检索慢，关系查询困难 |
| B. SQLite + Markdown 混合 | 结构化查询快，内容仍可读 | 实现复杂度中等，需要维护两套存储 |
| C. JSON/YAML 文件 | 机器友好，序列化简单 | 人难读，大文件性能差 |
| D. 图数据库（如 SQLite + 自建图层） | 关系查询强 | 引入新依赖，可能过度设计 |

**我的初步倾向**：B（SQLite 存索引和关系，Markdown 存内容）

**需要验证**：参考 Claude Code 的 `memdir` 设计后，确认纯文件方案在知识量大时是否可用。

### 决策 3：知识图谱实现

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. Wiki-link 风格（`[[节点名]]`） | 最简单，可直接写在 markdown 里 | 重命名节点时链接会断 |
| B. 独立图数据库 | 关系查询最强 | 重，对个人工具可能过度 |
| C. 内存图 + 定期序列化到 JSON | 查询快，实现简单 | 启动时加载慢，量大时内存压力大 |
| D. SQLite 邻接表 + 节点内容存文件 | 平衡方案，关系可查询 | 需要写更多查询代码 |

**我的初步倾向**：D

**需要验证**：学习 pi-mono 的 `SessionManager` 后，确认树结构存储方式是否可借鉴。

### 决策 4：复习算法

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. SM-2（Anki 经典） | 简单可靠，有大量实践验证 | 对新卡调度不够优 |
| B. FSRS（现代算法） | 更精准，考虑遗忘曲线参数 | 理解成本高，实现复杂 |
| C. 自定义简化版 | 完全可控，可针对学习场景优化 | 需要大量调试 |

**我的初步倾向**：A（先跑起来，后续可通过扩展替换为 B 或 C）

**需要验证**：确认学习场景中"复习"的形式是否适合传统间隔重复（学习不是背单词，复习可能是"用新上下文重新解释旧概念"）。

### 决策 5：LLM 上下文策略

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 全量加载相关记忆 | 简单，不漏信息 | token 消耗大，可能超出上下文 |
| B. Relevant Recall 轻量选择（Claude Code 式） | 可控，成本低 | 需要实现选择逻辑，可能选漏 |
| C. 分层摘要（先概览再细节） | 信息密度高 | 实现复杂，摘要质量依赖模型 |

**我的初步倾向**：B + C 的混合（先用 Relevant Recall 选相关记忆，对长记忆做分层摘要）

**需要验证**：精读 Claude Code 的 `findRelevantMemories.ts` 和 compaction 机制。

### 决策 6：本地 vs 云端

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 完全本地（本地服务 + 本地文件） | 隐私最好，数据完全可控 | 配置麻烦，跨设备同步需自行解决 |
| B. 本地数据 + 云端 LLM API | 平衡方案 | 仍需联网，API 有费用 |
| C. 可自托管的 Web 服务 | 可远程访问 | 部署维护成本 |

**我的初步倾向**：A（优先），未来可考虑 B 作为模型调用 fallback

**需要验证**：确认 Web 前端如何与本地文件系统交互（需要本地代理服务，还是 Electron/Tauri？）。

### 决策 7：输出倒逼强度

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 完全被动 | 用户体验最流畅 | 学习效果差，容易变成"只读" |
| B. 轻度提醒（偶尔建议） | 平衡 | 可能仍被用户忽略 |
| C. 强制交互（不输出不继续） | 学习效果最强 | 可能烦人，打断心流 |
| D. 用户可控（可设置强度） | 最灵活 | 实现复杂 |

**我的初步倾向**：B 起步，逐步引入 D（通过扩展系统的配置机制）

**需要验证**：结合自己的学习习惯，测试不同强度下的接受度。

### 决策 8：前端技术栈

| 选项 | 优点 | 缺点 |
|------|------|------|
| A. 纯 Web（React/Vue + 本地代理服务） | 最灵活，可视化库丰富 | 需要同时维护前后端 |
| B. Electron / Tauri | 可直连本地文件系统 | 包体积大，启动慢 |
| C. 终端 Web（类似 Claude Code 的 TUI） | 轻量 | 不适合知识图谱可视化 |

**我的初步倾向**：A（Web + 本地 Bun/Node 服务）

**需要验证**：评估 pi-mono 的 `packages/web-ui` 是否有可复用的组件思路。

### 决策 9：流式传输优先

**决策**：LLM 调用默认流式，非流式作为特殊场景备选。

**理由**：
- 用户体验：实时看到生成内容，减少等待焦虑
- 快速反馈：可以在生成过程中中断或修正
- 现代 LLM API（OpenAI、Claude）都原生支持流式
- 通过 Provider 层统一封装，上层代码无需关心流式细节

---

## 10. 学习路径建议

未来 5 天的目标：**让骨架跑起来——核心层 + 扩展系统 + Provider 层 + 一个内置扩展**。

| 天数 | 目标 | 产出 |
|------|------|------|
| **Day 1** | 搭建 Extension Layer 骨架 | 可运行的事件总线 + Hook 系统，能注册和触发 Hook |
| **Day 2** | 实现 Provider Layer + OpenAI Provider | 能流式调用 OpenAI API，返回 AsyncIterable |
| **Day 3** | 搭建 Memory Layer 基础 + Persistence Layer | 四层记忆的读写接口，知识节点的 CRUD，文件存储 |
| **Day 4** | 实现 Agent Loop + 第一个内置扩展 | Agent 主循环，内置扩展 `core-intent` 能介入意图解析 |
| **Day 5** | 端到端打通 | 用户输入 → Agent 循环 → Provider 流式调用 → 实时渲染 → 记忆存储 |

---

## 11. 核心设计原则

> **"学习 Agent 的核心不是让 AI 替你学，而是让 AI 成为你学习过程的'外接大脑'——它帮你记、帮你联、帮你提醒、逼你输出，但理解必须发生在你自己的大脑里。"**

### 11.1 架构层面

| 原则 | 说明 |
|------|------|
| **最小核心，最大扩展** | 核心只提供骨架，功能通过扩展生长 |
| **Hook + 事件驱动** | 扩展介入流程用 Hook，响应状态变化用 Event |
| **Provider 隔离** | 所有 LLM 调用必须通过 Provider Layer |
| **流式优先** | 默认流式传输，提升用户体验 |
| **透明持久化** | 用户数据以人类可读文件存储 |

### 11.2 警惕陷阱

| 陷阱 | 表现 | 如何避免 |
|------|------|---------|
| **更好的搜索引擎** | 只是更快地回答你的问题 | 强制输出倒逼，不让你只读不产 |
| **自动摘要器** | 帮你读材料、生成摘要 | 摘要后要求你用自己的话复述 |
| **无限对话机器** | 聊得开心但什么都没记住 | 每次会话结束强制提取知识节点 |
| **知识囤积癖** | 存了大量笔记但从不复习 | 间隔重复引擎主动安排复习 |
| **AI 幻觉传播器** | 把 AI 的错误知识当成自己的 | 来源追溯，区分"材料内知识"和"模型推断" |
| **功能堆砌** | 什么都想做，什么都做不精 | 坚持最小核心，新功能先以扩展实验 |

### 11.3 真正有价值的设计点

- 它记得你三个月前问过什么，并能在今天的新知识里**自动关联**
- 它知道你对某个概念"以为懂了但其实没懂"，并在关键时刻**揭穿你**
- 它逼你在看完材料后**用自己的话写点东西**，而不是看完就忘
- 它在你快遗忘的时候，用**新的上下文帮你复习**，而不是机械重复
- 它的能力可以**不断生长**，因为你随时可以写一个新的扩展

---

## 12. 待补充清单

### 12.1 当前版本必须完成

- [ ] 确定技术栈（前端框架、后端运行时、存储方案）
- [ ] 实现 Extension Layer 骨架（Event Bus + Hook System）
- [ ] 实现 Provider Layer + OpenAI Provider（流式传输）
- [ ] 实现基础 Memory Layer（四层记忆接口 + 知识节点 CRUD）
- [ ] 实现基础 Persistence Layer（文件存储）
- [ ] 实现 Agent Loop 主循环
- [ ] 实现第一个内置扩展 `core-intent`
- [ ] 确定文件目录结构（记忆文件、会话文件、材料文件、配置文件的组织方式）

### 12.2 后续通过扩展完善

- [ ] 内置扩展：`core-clarification`, `core-output-prompting`, `core-review`, `core-mastery`
- [ ] 学习模式体系（模式即扩展）
- [ ] 知识图谱的边类型（is_prerequisite / is_related / is_analogy / contradicts / extends）
- [ ] 间隔重复的具体算法实现（先 SM-2，后续可替换）
- [ ] 掌握度估计的评分规则
- [ ] 认知负荷监测的信号指标
- [ ] 会话 Compaction 的触发条件和摘要策略
- [ ] 向量检索（扩展）
- [ ] 多源材料解析器（PDF / 网页 / 代码 / 视频 / 音频，各作为扩展）
- [ ] 第三方集成（Anki、Notion、Zotero，各作为扩展）
- [ ] 多模型 Provider（Claude、本地模型）

---

> **下一步**：按"学习路径建议"开始 Day 1 的工作，先让 Extension Layer 和 Provider Layer 的骨架跑起来。遇到技术选型问题，优先选"简单可替换"的方案，不要过度设计。
