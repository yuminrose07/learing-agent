# 上下文压缩与 Memory 实施方案

> 版本：v1.0
> 目标：在不破坏现有四层边界的前提下，为当前项目落地一套可分阶段上线的上下文压缩与 memory 主链方案。

---

## 1. 文档目标

本文档回答四个问题：

1. 当前项目的上下文压缩与 memory 真正缺口是什么。
2. 哪些业界方案适合当前仓库，哪些不适合直接接入。
3. 应如何按四层架构拆分职责并分阶段上线。
4. 每个阶段应修改哪些文件、引入哪些数据结构、如何验收与回退。

---

## 2. 当前现状与问题归因

### 2.1 已经落地且有效的能力

- 工具层大输出截断已经存在：
  - `learning_agent/learning_agent/extensions/truncate_utils.py`
  - `learning_agent/learning_agent/extensions/code_tools.py`
  - `learning_agent/learning_agent/extensions/grep_tools.py`
- Runtime 已有 `ContextLengthError` 应急兜底：
  - `learning_agent/agent/react_engine.py`
- Product 层已有会话与记忆领域对象：
  - `learning_agent/learning_agent/session_manager.py`
  - `learning_agent/memory/memory_manager.py`
  - `learning_agent/memory/knowledge_graph.py`
  - `learning_agent/memory/spaced_repetition.py`

### 2.2 真实缺口

当前缺口不是“完全没有压缩和 memory”，而是**没有形成主链闭环**：

1. `context_compressor` 扩展存在，但未稳定接入默认主链。
2. `MemoryManager.relevant_recall()` 已定义，但未自动注入 prompt。
3. `SessionManager.compact_session()` 仍为占位实现。
4. 目前主要依赖工具层截断和 runtime 应急兜底，缺少“会话级 checkpoint / compaction”。
5. 目前 recall 更偏关键词/轻语义，尚未形成生产级 hybrid retrieval。

### 2.3 根因判断

问题根因是**接线与分层未完成**，而不是算法不足。

如果在这个阶段直接引入新的云 memory 产品或复杂压缩模型，会放大系统复杂度，却无法解决“memory 没进 prompt、compaction 没进 session、扩展没进主链”这些基础问题。

---

## 3. 设计原则

### 3.1 分层原则

必须严格遵守：

- `Interface` 只负责协议输入输出。
- `Product/Application` 负责 session、memory、mode、checkpoint、持久化编排。
- `Agent Runtime` 负责单轮执行、上下文组装、工具调度、错误兜底。
- `Infrastructure` 负责 provider、存储、索引与外部检索实现。

禁止做法：

- 在 Runtime 中直接做产品级 memory 编排。
- 在 Interface 中维护权威 session 状态。
- 在 Infrastructure 中硬编码产品模式分支。

### 3.2 方案选择原则

面向当前仓库，优先采用以下组合：

1. **搜索优先 + 有界工具输出**
2. **代码感知裁剪**
3. **会话级 compaction / checkpoint**
4. **小批量、相关性驱动的 memory 注入**
5. **prompt caching 作为成本优化层**

不作为当前主线优先项：

- 直接把 `LLMLingua` 用于代码与 tool 协议主链
- 直接引入外部云 memory API 作为主存储
- 在 recall 尚未接通前先重投入复杂知识图谱扩展

---

## 4. 目标方案总览

### 4.1 五层治理模型

```text
第 1 层：Search-first / Bounded IO
    先 grep / search，再分页 read；所有工具输出默认有界

第 2 层：Goal-aware Code Pruning
    按当前任务目标筛选代码块，而非做通用 token 删除

第 3 层：Session Checkpoint / Compaction
    长会话自动生成 checkpoint；provider 支持时优先走官方 compaction

第 4 层：Relevant Memory Injection
    每轮只注入少量相关记忆，不把长期 memory 全量塞回 prompt

第 5 层：Prompt Caching
    固定静态前缀与工具清单顺序，降低成本与 TTFT
```

### 4.2 为什么该组合适合当前项目

- 与现有工具层截断机制兼容，不推翻已有实现。
- 与四层架构一致，便于把 session / memory 编排留在 Product 层。
- 可以先规则版上线，再逐步演进到 hybrid retrieval 或轻量模型裁剪。
- 能优先补上“主链闭环”，而不是继续堆孤立能力。

---

## 5. 分层职责设计

### 5.1 Product / Application 层

新增或收口职责：

- 决定何时触发 checkpoint / compaction。
- 决定本轮是否做 memory recall、召回多少条、如何注入。
- 维护 session 级 checkpoint 数据结构。
- 管理 episodic / semantic / user preference 的提升与去重。

建议归属对象：

- `LearningAgentSystem`
- `SessionManager`
- `MemoryManager`
- `ModeService`

### 5.2 Agent Runtime 层

保留职责：

- 构建本轮 messages
- 触发扩展 Hook
- 调用 provider
- 执行工具
- 做单轮级异常兜底

新增职责仅限：

- 在上下文构建前暴露稳定 Hook 或等效插点
- 接收 Product 层给出的“已压缩上下文 / memory 注入块 / turn profile”

### 5.3 Infrastructure 层

后续演进职责：

- provider compaction 封装
- 本地 hybrid retrieval sidecar 或索引实现
- prompt cache 相关配置与请求透传

---

## 6. Memory 模型设计

### 6.1 四类记忆

| 类型 | 所属层 | 生命周期 | 作用 |
|---|---|---|---|
| Working Memory | Runtime | 单轮 / 短期 | 本轮目标、临时变量、工具中间结果 |
| Episodic Memory | Product | 单 session / 跨 session 可保留 | 某次任务中的关键事件、决策、失败路径 |
| Semantic Memory | Product | 长期 | 稳定事实、架构知识、领域规则 |
| User Preference Memory | Product | 长期 | 用户偏好、协作约束、输出风格 |

### 6.2 注入策略

每轮 memory 注入采用以下约束：

- 默认注入 `1-3` 条
- 总预算控制在 `400-1200 tokens`
- 先 `episodic`，再 `semantic`，最后 `user preference`
- 发生冲突时，以“当前用户输入 > 当轮 session 状态 > 长期 memory”为准

### 6.3 提升策略

- 用户显式确认的事实：直接进入 `semantic` 或 `user preference`
- 本轮刚出现、尚未验证的经验：先进入 `episodic`
- 在多个 session 中重复出现的稳定模式：再提升为 `semantic`

---

## 7. 压缩策略设计

### 7.1 工具输出层

沿用现有机制，不做推翻式重构：

- `read_file` 保留头部优先截断
- `bash` 保留尾部优先截断
- `grep` 作为导航工具优先于全文读取

新增改造重点：

- 为工具读取结果加入可选 `focus_hint` / `context_goal`
- 根据目标优先保留函数、类、错误路径、调用链附近代码块

### 7.2 会话层

新增 `SessionCheckpoint` 概念，记录：

- 当前 objective
- 已确认事实
- 已尝试失败路径
- 当前 session / mode 状态
- 下一步建议动作

触发条件建议：

- 上下文预算达到 `80%`
- 子目标完成
- 工具调用累计达到 `12-20` 次

### 7.3 Provider 层

优先级如下：

1. provider 支持官方 compaction：优先使用官方原语
2. provider 不支持：走本地 checkpoint summary
3. 两者都不可用：保留现有 runtime 紧急截断兜底

---

## 8. 实施阶段

## 8.1 Phase 1：打通上下文压缩主链

### 目标

让上下文压缩扩展真正进入默认执行链，取代“只在设计文档中存在”的状态。

### 主要工作

1. 统一 Hook 枚举与扩展接线方式。
2. 在上下文构建前增加稳定插点。
3. 将 `context_compressor` 纳入默认扩展装配。
4. 通过日志确认压缩前后消息数量与 token 预算。

### 计划修改文件

- `learning_agent/agent/hook_system.py`
- `learning_agent/agent/react_engine.py`
- `learning_agent/learning_agent/extensions/context_compressor.py`
- `learning_agent/learning_agent/extensions/built_in.py`

### 验收标准

- 默认主链下可以观察到上下文压缩生效日志。
- 不再出现“扩展已实现但默认未接线”的状态。
- assistant/tool 协议配对不被破坏。

---

## 8.2 Phase 2：接入 memory recall 注入

### 目标

让 `MemoryManager` 从“领域能力”变成“默认可被对话主链消费的能力”。

### 主要工作

1. 在 Product 层根据 `TurnExecutionProfile` 判断是否 recall。
2. 调用 `relevant_recall(query)` 获取候选记忆。
3. 对候选记忆做排序、去重、冲突消解、预算裁剪。
4. 将结果注入 system supplement 或上下文前导块。

### 计划修改文件

- `learning_agent/learning_agent/mode_service.py`
- `learning_agent/agent/agent_loop.py`
- `learning_agent/memory/memory_manager.py`
- 视实现方式新增：
  - `learning_agent/learning_agent/memory_injection.py`

### 验收标准

- 打开 `memory_read` 时，prompt 中能稳定看到相关记忆块。
- 单轮注入条数与 token 预算受控。
- recall 不影响未开启 memory 的普通对话流程。

---

## 8.3 Phase 3：实现 session checkpoint / compaction

### 目标

让长会话具备可持续运行能力，而不是只靠工具截断和 emergency truncation。

### 主要工作

1. 为 `SessionManager.compact_session()` 提供真实实现。
2. 新增 `SessionCheckpoint` 数据结构。
3. provider 支持 compaction 时，封装官方 compaction 调用。
4. provider 不支持时，退化为本地 checkpoint summary。
5. 将 checkpoint 同步写入 session 与 episodic memory。

### 计划修改文件

- `learning_agent/learning_agent/session_manager.py`
- `learning_agent/agent/agent_loop.py`
- `learning_agent/agent/react_engine.py`
- provider 相关文件：
  - `learning_agent/providers/` 下对应实现
- 视实现方式新增：
  - `learning_agent/learning_agent/session_checkpoint.py`

### 验收标准

- 超长 session 可在 compaction 后继续运行。
- checkpoint 能保留 objective、关键事实、失败路径和下一步建议。
- compaction 失败时自动回退到本地 summary 或当前兜底逻辑。

---

## 8.4 Phase 4：实现 Goal-aware Code Pruning

### 目标

把现有“通用截断”升级为“目标感知的代码块裁剪”。

### 实施策略

先做规则版，再决定是否引入轻量模型。

规则版原则：

- 以函数/类/导入/异常处理块为单位
- 优先保留与 `focus_hint` 命中的块
- 对被删除块只保留结构提示，不做 token 级破坏

### 计划修改文件

- `learning_agent/learning_agent/extensions/code_tools.py`
- `learning_agent/learning_agent/extensions/grep_tools.py`
- `learning_agent/learning_agent/extensions/truncate_utils.py`
- 视实现方式新增：
  - `learning_agent/learning_agent/extensions/code_pruner.py`

### 验收标准

- 大文件阅读时上下文 token 明显下降。
- 保留下来的代码块结构仍可读、可编辑、可定位。
- 对 code review / bug fix 任务的成功率不低于当前基线。

---

## 8.5 Phase 5：升级检索与缓存

### 目标

将 recall 从轻语义匹配逐步升级为本地 hybrid retrieval，并接入 prompt caching 优化成本。

### 主要工作

1. 固定 system prompt、tools、schemas 的拼接顺序。
2. provider 请求透传 prompt caching 参数。
3. 将 memory / repo note 检索升级为 hybrid search。
4. 保持本地优先，不把核心能力直接绑定第三方云 memory 服务。

### 候选方向

- QMD 风格的本地 hybrid retrieval
- provider 原生 prompt caching

### 验收标准

- 重复会话前缀的 TTFT 与输入成本下降。
- recall 准确率优于当前纯关键词/轻语义方案。

---

## 9. 数据结构建议

### 9.1 SessionCheckpoint

```python
from dataclasses import dataclass, field
from typing import List


@dataclass
class SessionCheckpoint:
    session_id: str
    checkpoint_id: str
    objective: str
    confirmed_facts: List[str] = field(default_factory=list)
    failed_attempts: List[str] = field(default_factory=list)
    next_actions: List[str] = field(default_factory=list)
    related_memory_ids: List[str] = field(default_factory=list)
    created_at: float = 0.0
```

### 9.2 MemoryInjectionBundle

```python
from dataclasses import dataclass
from typing import List


@dataclass
class MemorySnippet:
    memory_id: str
    memory_type: str
    score: float
    content: str


@dataclass
class MemoryInjectionBundle:
    query: str
    snippets: List[MemorySnippet]
    total_estimated_tokens: int
```

---

## 10. 观测指标

### 10.1 必须新增的指标

- 每轮 prompt 估算 token
- compaction 触发次数
- checkpoint 生成次数
- recall 命中条数
- recall 注入 token 数
- ContextLengthError 次数
- emergency truncation 次数
- 工具输出裁剪命中率

### 10.2 结果指标

- 长会话中断率下降
- 平均 prompt token 下降
- 单任务总 token 消耗下降
- 任务成功率不下降

---

## 11. 风险与回退

### 11.1 风险

1. recall 注入过多导致噪声反增
2. compaction 总结漂移导致 session 状态失真
3. 代码裁剪过激导致关键实现被漏掉
4. Hook 插点调整影响现有扩展行为

### 11.2 回退策略

- recall 支持总开关，可快速关闭 memory 注入
- compaction 保留 provider / local / disabled 三档策略
- code pruning 以 feature flag 控制，默认先灰度
- 保留当前 emergency truncation 作为最后兜底

---

## 12. 建议实施顺序

必须按以下顺序推进：

1. `Phase 1` 打通上下文压缩主链
2. `Phase 2` 接入 memory recall 注入
3. `Phase 3` 实现 session checkpoint / compaction
4. `Phase 4` 上线 goal-aware code pruning
5. `Phase 5` 升级 hybrid retrieval 与 prompt caching

理由：

- 先补主链接线，才能验证后续能力是否真的生效。
- memory 若不先进入 prompt，就没有必要先上更复杂的 memory 基础设施。
- 会话级 checkpoint 在 recall 打通后才能真正形成“短期压缩 + 长期沉淀”的闭环。

---

## 13. 最终结论

当前项目最可行的落地方向不是单独引入某一个论文方案，而是采用以下组合：

- **现有 Search-first / Bounded IO 继续保留**
- **优先打通默认主链中的上下文压缩接线**
- **以 Product 层为中心接入小批量 relevant memory recall**
- **通过 session checkpoint / provider compaction 解决长会话**
- **再以 goal-aware code pruning 提升代码场景压缩质量**
- **最后用 prompt caching 和 hybrid retrieval 做成本与召回优化**

这一路线与当前仓库的真实代码状态、四层架构边界以及演进成本最匹配，能在最小破坏下建立“上下文压缩 + memory”闭环。
