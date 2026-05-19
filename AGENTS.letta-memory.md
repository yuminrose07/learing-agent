# AGENTS.letta-memory.md — Letta 记忆系统学习指南

> 本文档用于指导 LLM 辅助学习 Letta 的记忆系统实现。当你被问及 Letta 记忆系统相关问题时，请基于本文档提供的代码地图和概念框架进行回答。
> 学习完成后，应能独立设计或复现一个分层记忆 Agent 系统。

---

## 1. 项目背景

本项目是 Letta 源码分析，核心目标是理解 Letta 如何通过**三层记忆架构**（Core / Recall / Archival）让 Agent 实现跨会话的长期记忆，以及 Agent 如何通过 Function Call 自主管理自己的记忆。

- **源码目录**: `/Users/roseannk/letta`
- **核心模块**: `letta/schemas/`（数据结构）、`letta/services/`（服务层）、`letta/orm/`（数据库模型）
- **总代码量**: 约 3.5 万行（核心记忆相关约 8000 行）
- **前置知识**: Python 异步编程、SQLAlchemy ORM、向量检索基础

---

## 2. 核心概念速查

### 2.1 三层记忆架构

Letta 采用经典的三层记忆模型，每种记忆有明确的职责边界、存储策略和暴露给 LLM 的工具接口：

| 层级 | 存储位置 | 生命周期 | 主要用途 | LLM 可见性 |
|------|----------|----------|----------|-----------|
| **Core Memory** | SQL `block` 表 | 随 Agent 持久化 | Agent 的"当前思维"——身份、用户偏好、技能状态 | 直接嵌入 System Prompt，始终可见 |
| **Recall Memory** | SQL `messages` 表 | 随 Agent 持久化 | 对话历史检索 | 通过 `conversation_search` 工具按需检索注入 |
| **Archival Memory** | SQL `archival_passages` + 向量库 | 随 Archive 持久化，可多 Agent 共享 | 长期知识、事实、跨会话摘要 | 通过 `archival_memory_search` 工具按需检索注入 |

**核心设计原则**: Core Memory 是**Prompt 的一部分**（`Prompt-as-Memory`），Recall 和 Archival 是**按需检索的外部存储**。Agent 通过工具调用自主管理全部三层记忆。

### 2.2 记忆所有权关系

```
Agent ──1:N──► Block (Core Memory)
    │
    └──1:N──► Message (Recall Memory)
    │
    └──N:M──► Archive ──1:N──► ArchivalPassage (Archival Memory)
```

- Core Memory: Agent 通过 `blocks_agents` 多对多表关联一组 Block
- Recall Memory: `messages.agent_id` 外键，一个 Agent 拥有全部历史消息
- Archival Memory: 通过 `archives_agents` 关联表，一个 Archive 可被多个 Agent 共享

### 2.3 向量存储双写策略

| 后端 | 角色 | 实现文件 |
|------|------|---------|
| **PostgreSQL / SQLite** | 权威持久化存储 | `letta/orm/passage.py` |
| **Turbopuffer** | 加速检索（Hybrid Search: 向量+全文） | `letta/helpers/tpuf_client.py` |
| **Pinecone** | 备选外部向量库 | `letta/helpers/pinecone_utils.py` |

**双写流程**: SQL 写入 → 成功后异步写入 Turbopuffer；搜索优先走 Turbopuffer，失败回退 SQL。

---

## 3. 代码地图（按学习优先级排序）

### Phase 1: 数据结构与概念建立（1-2 天）
理解 Letta 记忆系统的"名词"和"关系"。不要读实现，只读 Schema。

```
letta/schemas/block.py                 (209 行) — Block 定义：Core Memory 的原子单元
letta/schemas/memory.py                (884 行) — Memory 容器与渲染引擎（compile）
letta/schemas/passage.py               (~80 行) — Passage 定义：Archival Memory 的原子单元
letta/schemas/message.py               (~200 行) — Message 定义：Recall Memory 的原子单元
letta/schemas/archive.py               (~60 行) — Archive 定义：Archival Memory 的集合
```

**Phase 1 学习目标**:
- [ ] 能画出 Block 的字段和用途（label, value, limit, read_only 的作用）
- [ ] 能解释 Memory.compile() 为什么存在（Block → XML Prompt 的转换）
- [ ] 能区分 Passage 和 Message 的语义边界
- [ ] 能解释 Archive 为什么设计成"多 Agent 共享"

---

### Phase 2: Core Memory 编译与编辑（2-3 天）
理解 Agent 如何"看见"和"修改"自己的工作记忆。

```
letta/schemas/memory.py                (重点看 143-350 行) — 三种渲染模式（standard / line_numbered / git）
letta/services/tool_executor/core_tool_executor.py  (1068 行)
  - 重点方法: core_memory_replace() / core_memory_append()
  - 重点方法: memory_replace() / memory_insert() / memory_rethink()
letta/functions/function_sets/base.py  (~500 行) — 工具的函数签名定义（LLM 看到的接口）
letta/prompts/prompt_generator.py      (~300 行) — System Prompt 组装时如何插入 compiled memory
```

**Phase 2 学习目标**:
- [ ] 能写出标准模式下 `<memory_blocks>` 的 XML 结构
- [ ] 能解释行号模式为什么只给 Anthropic 模型用
- [ ] 能画出 core_memory_replace 的完整调用链（Tool Call → DB → Prompt 重建）
- [ ] 能解释 read_only 在 tool executor 中如何被检查

---

### Phase 3: Recall Memory 检索机制（2-3 天）
理解消息历史如何被存储、索引和搜索。

```
letta/orm/message.py                   (~200 行) — Message ORM 模型，注意 sequence_id 的设计
letta/services/message_manager.py      (~600 行) — Recall Memory 管理器
  - 重点方法: create_message() / search_messages_async()
  - 重点理解: _extract_message_text() 的消息标准化策略
letta/helpers/tpuf_client.py           (~2150 行) — Turbopuffer 客户端（关注 query_messages_by_agent_id）
  - 重点理解: Hybrid Search（vector + fts）+ RRF 融合排序
```

**Phase 3 学习目标**:
- [ ] 能解释 sequence_id 的作用（分页 vs 时序）
- [ ] 能画出 Turbopuffer Hybrid Search 的流程（向量检索 → 全文检索 → RRF 融合）
- [ ] 能解释为什么 assistant + tool 消息要合并后再生成 embedding
- [ ] 能写出 conversation_search 工具返回的数据结构

---

### Phase 4: Archival Memory 存储与检索（2-3 天）
理解长期知识如何被持久化、嵌入和跨 Agent 共享。

```
letta/orm/passage.py                   (~150 行) — BasePassage / ArchivalPassage / SourcePassage 继承体系
letta/orm/archive.py                   (~80 行) — Archive ORM 模型
letta/services/passage_manager.py      (~400 行) — Passage CRUD、embedding、标签管理
letta/services/archive_manager.py      (~300 行) — Archive 生命周期、Agent 关联
letta/services/agent_manager.py        (~800 行)
  - 重点方法: search_agent_archival_memory_async()
  - 重点方法: query_agent_passages_async()
```

**Phase 4 学习目标**:
- [ ] 能解释 ArchivalPassage 和 SourcePassage 的区别
- [ ] 能画出 passage 插入的完整流程（文本 → embedding → SQL → 可选 Turbopuffer）
- [ ] 能解释 Archive 的 vector_db_provider 字段如何影响检索路径
- [ ] 能解释 embedding 维度 padding（pgvector 补零 vs Turbopuffer 不补）的原因

---

### Phase 5: Agent 集成与上下文管理（2-3 天）
理解记忆系统如何被 Agent 生命周期调用，以及 Context Window 压力管理。

```
letta/agents/base_agent.py             (~400 行) — BaseAgent 抽象类
  - 重点方法: _rebuild_memory_async() — System Prompt 重建触发点
letta/agents/letta_agent.py            (~600 行) — LettaAgent 实现（message/passage manager 集成）
letta/agent.py                         (~1500 行) — 主 Agent 类
  - 重点方法: get_context_window() — Context Window 全景计算
  - 重点方法: summarize_messages_inplace() — 消息摘要（Memory Pressure 处理）
```

**Phase 5 学习目标**:
- [ ] 能画出 _rebuild_memory_async 的触发时机和判断逻辑
- [ ] 能解释 ContextWindowOverview 中每个 token 计数项的含义
- [ ] 能画出 summarize_messages_inplace 的完整流程（cutoff 计算 → LLM 总结 → 消息替换）
- [ ] 能解释 memory_warning_threshold 和 context_window 的关系

---

### Phase 6: Git-Backed Memory 高级特性（可选，2 天）
理解版本化记忆的设计。

```
letta/services/memory_repo/            (~800 行)
  - git_operations.py                  — Git commit、diff、branch
  - block_markdown.py                  — Block 与 Markdown 互转
  - memfs_client_base.py               — Memory filesystem 客户端
  - storage/local.py                   — 本地文件系统存储
```

**Phase 6 学习目标**:
- [ ] 能解释 git_enabled=True 时 Memory.compile() 的行为差异
- [ ] 能画出 Git-backed memory 的读写流程
- [ ] 能解释 path-style label（system/persona, skills/xxx）的设计意图

---

## 4. 关键数据结构与类型

### 4.1 Memory（Core Memory 容器）

```python
class Memory(BaseModel):
    agent_type: Optional[Union["AgentType", str]]  # 控制 Prompt 渲染方式
    git_enabled: bool                              # 是否使用 Git-backed 记忆
    blocks: List[Block]                            # 核心记忆块列表
    file_blocks: List[FileBlock]                   # 附加文件记忆块
    
    def compile(self, ...) -> str:
        # 将 Block 集合渲染为 XML 字符串，嵌入 System Prompt
```

### 4.2 Block（Core Memory 原子单元）

```python
class Block(BaseBlock):
    id: str
    value: str                    # 实际文本内容
    limit: int = 5000             # 字符上限
    label: Optional[str]          # 标签/命名空间
    read_only: bool = False       # Agent 是否可编辑
    description: Optional[str]    # 描述（渲染进 Prompt）
```

### 4.3 Passage（Archival Memory 原子单元）

```python
class Passage(LettaBase):
    id: str
    text: str
    embedding: Optional[List[float]]
    tags: Optional[List[str]]
    metadata: Optional[dict]
    archive_id: Optional[str]
    source_id: Optional[str]
```

### 4.4 ContextWindowOverview（上下文全景）

```python
class ContextWindowOverview(BaseModel):
    context_window_size_max: int
    context_window_size_current: int
    num_tokens_system: int
    num_tokens_core_memory: int
    num_tokens_summary_memory: int
    num_tokens_messages: int
    num_tokens_functions_definitions: int
    num_tokens_external_memory_summary: int
```

---

## 5. LLM 辅助学习指南

当用户提出以下类型的问题时，请参考对应策略：

### 5.1 "帮我解释这段代码"
- 先定位代码属于哪个 layer：`Interface` / `Product` / `Agent Runtime` / `Infrastructure`
- 解释时要说明："这是哪一层"、"上层怎么调用它"、"下层依赖什么"
- 举例：解释 `core_tool_executor.py` 时，要联系 `function_sets/base.py` 的工具签名和 `agent_manager.py` 的持久化调用

### 5.2 "这个设计为什么这样？"
- 从**工程权衡**角度回答：延迟 vs 质量、存储成本 vs 检索精度、灵活性 vs 一致性
- 关键权衡点：
  - 为什么 Core Memory 要嵌入 Prompt 而不是向量检索？（延迟最低、始终可见、确定性最强）
  - 为什么用字符限制而非 Token 限制？（避免频繁调用 tokenizer，简化计算）
  - 为什么 Archive 要设计成多 Agent 共享？（知识复用，避免重复存储）
  - 为什么用双写而非单存向量库？（SQL 是权威数据源，向量库只是加速器）

### 5.3 "画一张流程图/架构图"
- 优先用文字 ASCII 图或 Mermaid 语法
- 区分：记忆读取流、记忆写入流、Prompt 重建流、Context Window 压缩流

### 5.4 "对比 A 和 B"
常见对比需求：
- `Core Memory` vs `Recall Memory` vs `Archival Memory`
- `Block` vs `Passage` vs `Message`
- `Standard render` vs `Line-numbered render` vs `Git render`
- `SQL search` vs `Turbopuffer hybrid search`
- `core_memory_replace` vs `memory_replace` vs `memory_rethink`

### 5.5 "找出所有相关的代码"
- 使用 grep 搜索以下模式：
  - `compile(` / `compile_async(` — 所有 Prompt 渲染点
  - `search_messages_async` — Recall Memory 检索入口
  - `query_passages` / `search_agent_archival_memory` — Archival Memory 检索入口
  - `update_memory_if_changed` — Core Memory 变更持久化
  - `summarize_messages_inplace` — Context Window 压缩
  - `get_context_window` — 上下文使用量计算

### 5.6 "讲一个机制/流程/设计"（教学方法规范）

当用户要求解释某个机制（如 Core Memory 编辑、Archival 检索、Prompt 重建）时，采用以下循序渐进教学法：

#### 教学四步法

**Step 1: 先铺地基——"这东西在什么位置"**
- 用一句话讲清：这个机制在整个系统中的**角色**（是工作区？是仓库？是检索台？）
- 讲清**触发时机**：是 Agent 初始化时、每轮开始时、还是 Tool Call 时？
- 讲清**输入输出**：它接收什么、产出什么、失败了会怎样？

**Step 2: 用生活化类比建立直觉**
- 本项目推荐使用 **"研究员工作室"** 类比

**推荐角色映射表**:

| 代码概念 | 工作室角色 | 说明 |
|---------|-----------|------|
| Core Memory | **研究员桌上的便签纸** | 始终摊在眼前，随时可读可改 |
| Block | **便签纸上的不同分区** | 有人物区、任务区、技能区 |
| Recall Memory | **录音笔里的对话记录** | 每次对话都录下来，需要时翻查 |
| Archival Memory | **档案柜里的文献资料** | 长期保存的知识，按主题归档 |
| Archive | **档案盒** | 一个主题一盒，可多研究员共用 |
| Memory.compile() | **整理便签贴到视野中央** | 把分散的便签整理成一块展示板 |
| Tool Call | **研究员主动伸手去翻/写** | Agent 自己决定什么时候查档案、改便签 |
| System Prompt | **研究员的视野范围** | 有限的空间，决定了当前能同时看到什么 |
| Context Window 压缩 | **收拾桌子** | 东西太多放不下，把旧的归纳成摘要 |
| Turbopuffer | **档案室的电子检索系统** | 比手工翻档案快，但档案原件仍在柜子里 |

**Step 3: 按时间线走一遍完整流程**
- 用**轮次编号**（初始化 → 第 1 轮 → 第 N 轮）展示状态如何变化
- 标注每个关键节点的**数值变化**（如 core_memory chars_current 从 200 → 450）
- 用箭头图或缩进文本展示调用链
- **必须回答用户最关心的实际问题**："Agent 会自己改记忆吗？"、"改了之后下次对话还在吗？"

**Step 4: 对比表钉死易混淆概念**
- 把用户最容易搞混的两个东西拉成表格
- 对比维度：存储位置、谁拥有、LLM 怎么访问、修改方式、生命周期

#### 讲解风格禁忌

- ❌ 不要一上来就贴代码和术语（`BasePassage`、`sequence_id`、`RRF`）
- ❌ 不要把 Core Memory 和 Archival Memory 混为一谈——这是最大的理解陷阱
- ❌ 不要跳步：如果讲"Prompt 重建"，必须先讲"Memory.compile 输出什么"，再讲"谁触发重建"
- ❌ 不要给用户讲跑了：每讲完一个抽象层，回到类比里对应一下

#### 当用户说"没看懂"时的处理

1. **先确认卡点**：问清是"不知道这东西在干嘛"、"不知道流程顺序"、还是"不知道和我有什么关系"
2. **换粒度**：如果宏观流程懂了但代码看不懂，降到函数级；如果连宏观都不懂，回到类比层
3. **用具体数字**：不要只说"超过 limit"，要说"假设 limit=5000，当前 value 有 4500 字符"
4. **回答终极问题**：机制讲完后，**必须明确回答**——"Agent 能自己改吗？"、"数据持久化吗？"、"用户能干预吗？"

---

## 6. 常见问题（FAQ）

**Q: Agent 改了自己的 Core Memory，下次对话还在吗？**
A: 在。Core Memory 的 Block 存储在 SQL `block` 表中，通过 `blocks_agents` 关联。Agent 调用 `core_memory_replace` → `tool_executor` 修改 `agent_state.memory` → `agent_manager.update_memory_if_changed_async()` 持久化到 DB → 下轮对话加载时从 DB 读取最新值。

**Q: 为什么 Core Memory 不用向量检索？**
A: Core Memory 的设计目标是"始终可见的低延迟工作记忆"。嵌入 Prompt 的延迟为 0，确定性为 100%。如果走向量检索，需要额外一次 embedding + 搜索调用，且存在检索遗漏风险。Core Memory 的内容量可控（字符限制 5000），直接嵌入是工程最优解。

**Q: Archival Memory 和 Recall Memory 有什么区别？**
A: Recall Memory 是**时间线**——记录 Agent 和用户之间发生的每句话；Archival Memory 是**知识库**——记录 Agent 认为值得长期保存的事实、摘要、知识。Recall 按时间检索，Archival 按语义检索。

**Q: 一个 Archive 能被多个 Agent 共享吗？**
A: 数据模型支持（`archives_agents` 多对多表），但当前代码限制每个 Agent 默认只关联一个 Archive。这是产品层决策，不是运行时限制。

**Q: Agent 怎么知道什么时候该搜索 Archival Memory？**
A: Agent 不知道——它通过 Function Call 自主决定。System Prompt 中会描述 `archival_memory_search` 工具的存在和用途，Agent 根据自己的判断选择是否调用。这和人类研究员决定是否去查档案柜是一样的逻辑。

**Q: Git-backed Memory 和普通 Memory 的核心差异是什么？**
A: 普通 Memory 的 Block 是纯数据库记录；Git-backed Memory 的 Block 对应 Git 仓库中的文件，支持版本历史、diff 查看、结构化路径标签（`system/persona` → `persona.md`）。渲染时也不同：普通模式用扁平 XML 标签，Git 模式用嵌套 XML + 文件树投影。

**Q: 消息 Summarization 会丢失信息吗？**
A: 会丢失细节，但保留语义骨架。Summarizer 用 LLM 将旧消息压缩为摘要消息，被摘要的原始消息从上下文中移除。丢失的是具体措辞和 tool result 细节，保留的是决策结论和关键状态。这是上下文窗口受限时的必要权衡。

---

## 7. 学习检查清单

完成全部学习后，应能不看代码回答：

### 基础层
- [ ] Block 的五个核心字段及其作用（value, label, limit, read_only, description）
- [ ] Memory.compile() 的存在意义和三种渲染模式的差异
- [ ] Core Memory 为什么不走向量检索，而是直接嵌入 Prompt
- [ ] Core Memory 编辑工具的完整调用链（Tool Call → 内存修改 → DB 持久化 → Prompt 重建）

### 进阶层
- [ ] Recall Memory 的两种搜索后端（SQL vs Turbopuffer）及各自适用场景
- [ ] Turbopuffer Hybrid Search 的 RRF 融合机制
- [ ] Archival Memory 的双写策略（SQL 权威 + 向量库加速）
- [ ] Archive 与 Agent 的多对多关系设计意图

### 高级层
- [ ] ContextWindowOverview 的完整构成及各部分 token 计算方式
- [ ] summarize_messages_inplace 的触发条件和执行流程
- [ ] _rebuild_memory_async 如何判断"Prompt 是否需要重建"
- [ ] Git-backed Memory 的版本化设计和路径标签体系

### 设计能力层
- [ ] 能独立设计一个三层记忆系统的 Schema
- [ ] 能写出 Core Memory 编译渲染的逻辑
- [ ] 能设计一套让 LLM 自主管理记忆的工具接口
- [ ] 能在 Context Window 压力下设计合理的压缩/降级策略

---

## 8. 扩展阅读方向

如果用户学完后想深入：

1. **Embedding 管理**: `letta/services/file_processor/embedder/` — 文件处理和 embedding 生成流水线
2. **工具沙箱**: `letta/services/tool_sandbox/` — Core Memory 工具的实际执行隔离环境
3. **Provider 抽象**: `letta/llm_api/` — LLM 调用如何与记忆检索解耦
4. **多 Agent 协作**: `letta/groups/` — Agent Group 中记忆如何共享/隔离
5. **Prompt 工程**: `letta/prompts/system_prompts/` — System Prompt 模板与记忆注入点的设计

---

## 9. 配套实践任务

每完成一个 Phase，建议完成对应的实践任务以巩固理解：

| Phase | 实践任务 | 验证标准 |
|-------|---------|---------|
| 1 | 手写 Memory + Block 的 Pydantic Schema | 能正确序列化/反序列化，包含全部关键字段 |
| 2 | 实现一个简化版 Memory.compile() | 输出符合标准 XML 格式，包含 metadata |
| 3 | 用纯 SQL 实现 conversation_search | 支持按 role + 时间范围 + 文本模糊匹配 |
| 4 | 实现 Passage 插入 + 向量相似度搜索 | 支持 embedding 生成和 cosine 相似度排序 |
| 5 | 实现 Context Window 监控 + 简单 Summarization | 超过阈值时触发，用 LLM 生成摘要替换旧消息 |
| 6（可选） | 为 Memory 添加 Git 版本控制 | 每次修改自动 commit，支持 diff 查看 |

---

*最后更新: 2026-05-19 — 初版，覆盖 Letta 记忆系统三层架构完整分析*
*适用范围: /Users/roseannk/letta 下的记忆系统相关代码*
*配套学习计划见上文 Phase 1-6，预计总学习周期 10-15 天（每天 2-3 小时）*
