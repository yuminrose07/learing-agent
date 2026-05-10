# Learning-Agent 可观测性与优化设计文档

> Agent 是一个黑盒系统——输入进去，输出出来，中间发生了什么你一无所知。没有观测，就没有优化。本文档定义如何将 Agent 从黑盒转变为白盒。
> 
> **核心原则：观测即基础设施。观测不是事后加的功能，而是架构的一等公民。**

---

## 目录

1. [Agent 为什么必须是白盒](#1-agent-为什么必须是白盒)
2. [可观测性的四个维度](#2-可观测性的四个维度)
3. [Agent 特有的观测对象](#3-agent-特有的观测对象)
4. [基于事件总线的观测架构](#4-基于事件总线的观测架构)
5. [链路追踪（Trace）设计](#5-链路追踪trace设计)
6. [指标（Metrics）设计](#6-指标metrics设计)
7. [日志与状态快照（Log / Snapshot）](#7-日志与状态快照log--snapshot)
8. [评估体系（Eval）设计](#8-评估体系eval设计)
9. [观测数据流与存储](#9-观测数据流与存储)
10. [人类可读性：观测的呈现方式](#10-人类可读性观测的呈现方式)
11. [优化闭环：从观测到改进](#11-优化闭环从观测到改进)
12. [当前版本实现优先级](#12-当前版本实现优先级)

---

## 1. Agent 为什么必须是白盒

### 1.1 传统软件 vs Agent

| 维度 | 传统软件 | Agent |
|------|---------|-------|
| 确定性 | 输入 A → 输出 B，100% 可预期 | 输入 A → 可能输出 B/C/D，概率性 |
| 错误定位 | 堆栈跟踪直接定位代码行 | LLM 输出"看起来对但实际错"，无堆栈 |
| 调试方式 | 断点、单步执行 | 无法断点在 LLM 脑子里 |
| 性能瓶颈 | 某段代码慢，profiler 可见 | 是 prompt 太长？是记忆召回不准？是模型不够强？ |

### 1.2 没有观测的 Agent = 盲人摸象

你会有这些问题，但无法回答：
- "为什么这次回答很差？" → 不知道 prompt 里组装了什么上下文
- "为什么工具没调用？" → 不知道 LLM 收到的 tool schema 是什么
- "为什么记忆没召回？" → 不知道召回阶段搜索了什么、返回了什么
- "为什么用户觉得 Agent 变笨了？" → 没有历史性能对比数据
- "扩展 A 和扩展 B 哪个更耗性能？" → 没有各阶段的耗时分布

### 1.3 白盒化的定义

**白盒化 = 在任意时刻，你能回答以下问题：**

1. **此刻 Agent 内部状态是什么？**（上下文、记忆、模式、扩展状态）
2. **从输入到输出，经过了哪些步骤？每一步的输入输出是什么？**
3. **每一步花了多少时间？消耗多少 token？**
4. **最终输出质量如何？哪里好？哪里差？**
5. **与历史相比，性能是在提升还是下降？**

---

## 2. 可观测性的四个维度

借用可观测性经典框架，Agent 需要四个支柱：

```
┌─────────────────────────────────────────────┐
│           Agent 可观测性体系                 │
├─────────────┬─────────────┬─────────────────┤
│   Trace     │   Metrics   │   Log/Snapshot  │
│   链路追踪   │   指标监控   │   日志与状态快照  │
│             │             │                 │
│  "经过了什么" │  "花了多少"  │   "当时是什么"   │
├─────────────┴─────────────┴─────────────────┤
│              Eval 评估体系                    │
│           "结果好不好"                        │
└─────────────────────────────────────────────┘
```

| 维度 | 回答的问题 | 使用场景 |
|------|-----------|---------|
| **Trace** | 一个请求从输入到输出的完整路径 | 调试某次对话为什么出错 |
| **Metrics** | 耗时、token、成功率、召回率 | 监控 Agent 整体健康度 |
| **Log/Snapshot** | 每个决策点的完整内部状态 | 事后复盘、离线分析 |
| **Eval** | 输出质量评分、用户满意度 | 版本对比、A/B 测试 |

---

## 3. Agent 特有的观测对象

传统后端观测的是 HTTP 请求、数据库查询。Agent 需要观测这些独有的东西：

### 3.1 LLM 调用层

```yaml
llm_call:
  traceId: "trace-001"
  spanId: "span-llm-001"
  parentSpanId: "span-agent-loop"
  
  model: "gpt-4o"
  messages: [...]           # 实际发送给 LLM 的完整消息列表
  tools: [...]              # 实际发送的 tool schema
  temperature: 0.7
  
  tokenUsage:
    prompt: 2048            # prompt token 数
    completion: 512         # completion token 数
    total: 2560
  
  latency:
    firstTokenMs: 320       # 首 token 延迟（流式）
    totalMs: 2800           # 总耗时
    interTokenMs: 12        # 平均 token 间隔
  
  response:
    content: "..."
    toolCalls: [...]
    finishReason: "stop"
  
  quality:
    relevance: 0.85         # 与输入的相关性（自动评分）
    hallucinationRisk: 0.1  # 幻觉风险（自动评分）
```

### 3.2 记忆召回层

```yaml
memory_recall:
  traceId: "trace-001"
  spanId: "span-recall-001"
  
  query: "Raft Leader 选举"  # 召回查询
  strategy: "tag+keyword"   # 使用的召回策略
  
  candidates:
    - nodeId: "kn-1"
      score: 0.92
      source: "L2-longterm"
      content: "..."
    - nodeId: "kn-2"
      score: 0.75
      source: "L2-longterm"
      content: "..."
  
  selected: ["kn-1", "kn-2"]  # 最终选入上下文的
  
  # 关键：召回质量评估
  relevance:
    precision@2: 0.5        # 选中的 2 个里几个真的相关
    recall: 0.3             # 应该召回的节点里召回了多少
```

### 3.3 意图解析层

```yaml
intent_parse:
  traceId: "trace-001"
  spanId: "span-intent-001"
  
  rawInput: "帮我看看这个"
  parsedIntent:
    type: "analyze_code"
    confidence: 0.65        # 置信度
    entities:
      - type: "code"
        value: "当前上下文代码"
  
  # 低置信度时的处理
  clarificationTriggered: true
  clarificationQuestion: "你想让我分析哪段代码？"
  
  # 用户确认后的修正
  userConfirmed: true
  correctedIntent:
    type: "analyze_code"
    confidence: 0.95
```

### 3.4 扩展系统层

```yaml
extension_execution:
  traceId: "trace-001"
  
  hooks:
    - hookPoint: "agent.beforeIntentParse"
      extensionId: "core-intent"
      durationMs: 45
      result: "modified"
      error: null
    
    - hookPoint: "agent.afterResponse"
      extensionId: "core-output-prompting"
      durationMs: 12
      result: "modified"
      error: null
    
    - hookPoint: "agent.onStreamChunk"
      extensionId: "custom-formatter"
      durationMs: 2
      result: "passed"
      error: null
  
  # 扩展性能分布
  totalHookTimeMs: 59
  slowestHook: "core-intent"
```

### 3.5 工具调用层

```yaml
tool_execution:
  traceId: "trace-001"
  
  toolCalls:
    - toolId: "read_material"
      extensionId: "core-material"
      params:
        materialId: "mat-1"
        page: 127
      result: "..."
      durationMs: 150
      success: true
    
    - toolId: "create_knowledge_node"
      extensionId: "core-memory"
      params:
        content: "..."
      result:
        nodeId: "kn-5"
      durationMs: 20
      success: true
```

### 3.6 上下文组装层（最容易出问题的环节）

```yaml
context_build:
  traceId: "trace-001"
  
  components:
    - type: "system_prompt"
      tokens: 200
      source: "mode:explore"
    - type: "session_history"
      tokens: 800
      entries: 12
    - type: "relevant_memories"
      tokens: 600
      nodes: ["kn-1", "kn-2", "kn-3"]
    - type: "review_reminders"
      tokens: 300
      nodes: ["kn-5"]
    - type: "user_input"
      tokens: 50
  
  totalTokens: 1950
  contextLimit: 8000
  utilization: 0.24         # 上下文利用率
  
  # 如果接近上限，记录了压缩策略
  compressionApplied: false
```

---

## 4. 基于事件总线的观测架构

我们已经在架构中设计了 **Event Bus**。观测体系应该完全基于事件总线构建——**观测不是侵入式的，而是订阅式的**。

### 4.1 观测作为扩展

```typescript
// observability-extension.ts
// 这是一个特殊的内置扩展，负责收集所有观测数据

const observabilityExtension: Extension = {
  id: 'core-observability',
  name: 'Observability Collector',
  type: 'builtin',
  
  activate(context) {
    // 订阅所有事件，转换为观测数据
    context.subscribeEvent('*', (event) => {
      // 将事件写入观测日志
      writeObservationLog(event);
    });
    
    // 注册关键 Hook，记录性能数据
    context.registerHook('agent.beforeLLMCall', async (params, hookCtx) => {
      hookCtx.trace.setMetric('llm.startTime', Date.now());
      hookCtx.trace.setTag('model', params.model);
    });
    
    context.registerHook('agent.afterResponse', async (response, hookCtx) => {
      const startTime = hookCtx.trace.getMetric('llm.startTime');
      hookCtx.trace.setMetric('llm.latency', Date.now() - startTime);
    });
  }
};
```

### 4.2 观测事件类型

所有系统事件都自动成为观测数据，无需额外埋点：

```yaml
# 系统自动发布的事件 = 观测数据源
event_types:
  # Agent 循环事件
  - agent.sessionStarted
  - agent.intentParsed          # 携带意图解析结果和置信度
  - agent.contextBuilt          # 携带上下文组成
  - agent.llmCalled             # 携带模型和参数
  - agent.responseChunk         # 流式 chunk（可用于计算首 token 延迟）
  - agent.responseDone          # 完整响应
  - agent.toolCalled            # 工具调用
  - agent.toolResult            # 工具结果
  - agent.sessionEnded
  
  # 记忆系统事件
  - memory.recallRequested      # 召回请求
  - memory.recallDone           # 召回结果
  - memory.nodeStored           # 节点写入
  - memory.nodeRetrieved        # 节点读取
  
  # 扩展系统事件
  - extension.hookExecuted      # Hook 执行完成（携带耗时）
  - extension.eventPublished    # 事件发布
  
  # Provider 事件
  - provider.requestSent        # 请求发出
  - provider.chunkReceived      # chunk 收到
  - provider.requestCompleted   # 请求完成
  - provider.error              # 错误
```

### 4.3 零侵入观测

**核心代码不需要为观测写任何额外代码。** 观测扩展通过订阅事件和注册 Hook 收集所有数据。

```typescript
// Agent Loop 核心代码——完全看不到观测逻辑
async function agentLoop(session, userInput) {
  // 1. 发布事件（这是业务逻辑本来就需要的）
  eventBus.publish({ type: 'agent.intentParse', payload: { input: userInput } });
  const intent = await parseIntent(userInput);
  
  // 2. 继续业务逻辑
  const context = await buildContext(session, intent);
  
  // 3. 调用 Provider（事件由 Provider 内部发布）
  const response = await provider.streamChat(context);
  
  // 4. 返回
  return response;
}

// 观测数据哪里来？
// → agent.intentParse 事件 = 意图解析观测
// → provider 内部发布的 provider.requestSent / chunkReceived = LLM 调用观测
// → 观测扩展注册的 Hook = 性能计时
```

---

## 5. 链路追踪（Trace）设计

### 5.1 Trace 结构

一个 Trace = 一次完整对话（从用户输入到最终响应）

```yaml
trace:
  traceId: "trace-20260510-001"
  sessionId: "sess-abc"
  objectiveId: "obj-xyz"
  timestamp: "2026-05-10T10:00:00Z"
  durationMs: 3500
  
  spans:
    - spanId: "span-root"
      name: "agent.loop"
      startTime: 0
      durationMs: 3500
      
    - spanId: "span-intent"
      parentId: "span-root"
      name: "intent.parse"
      startTime: 10
      durationMs: 45
      
    - spanId: "span-context"
      parentId: "span-root"
      name: "context.build"
      startTime: 60
      durationMs: 120
      tags:
        totalTokens: 1950
        memoryNodesRecalled: 3
        
    - spanId: "span-llm"
      parentId: "span-root"
      name: "llm.stream"
      startTime: 200
      durationMs: 2800
      tags:
        model: "gpt-4o"
        firstTokenMs: 320
        totalTokens: 2560
        
    - spanId: "span-tool-1"
      parentId: "span-llm"
      name: "tool.read_material"
      startTime: 1500
      durationMs: 150
      tags:
        toolId: "read_material"
        success: true
```

### 5.2 可视化

```
agent.loop [3500ms]
├── intent.parse [45ms]
├── context.build [120ms] (tokens: 1950, memories: 3)
├── llm.stream [2800ms] (model: gpt-4o, firstToken: 320ms)
│   ├── tool.read_material [150ms] ✓
│   └── tool.create_knowledge_node [20ms] ✓
└── result.process [120ms]
```

### 5.3 Trace 的传递机制

利用我们已有的 Hook Context：

```typescript
interface HookContext {
  trace: Trace;           // 当前 Trace 对象
  span: Span;             // 当前 Span
  session: Session;
  // ... 其他上下文
}

// 每个 Hook 点自动创建子 Span
function executeHook(hook, data, parentContext) {
  const span = parentContext.trace.startSpan(hook.name, parentContext.span);
  const hookContext = { ...parentContext, span };
  
  const start = Date.now();
  try {
    const result = hook.handler(data, hookContext);
    span.setTag('duration', Date.now() - start);
    span.setTag('result', 'success');
    return result;
  } catch (e) {
    span.setTag('error', e.message);
    throw e;
  } finally {
    span.end();
  }
}
```

---

## 6. 指标（Metrics）设计

### 6.1 实时仪表盘指标

| 指标 | 类型 | 说明 |
|------|------|------|
| `agent.request.rate` | Counter | 每秒请求数 |
| `agent.request.duration` | Histogram | 请求耗时分布 |
| `llm.first_token.latency` | Histogram | 首 token 延迟 |
| `llm.token.usage` | Counter | Token 消耗（按模型分维度）|
| `llm.token.throughput` | Gauge | Token 生成速度（tokens/s）|
| `memory.recall.precision` | Gauge | 记忆召回精确率 |
| `memory.recall.recall` | Gauge | 记忆召回复盖率 |
| `intent.confidence` | Histogram | 意图解析置信度分布 |
| `intent.clarification.rate` | Gauge | 需要澄清的比例 |
| `tool.call.success_rate` | Gauge | 工具调用成功率 |
| `extension.hook.duration` | Histogram | 各 Hook 执行耗时（按扩展分维度）|
| `session.tree.depth` | Histogram | 会话树深度 |
| `session.fork.count` | Counter | 分叉次数 |

### 6.2 业务指标

| 指标 | 说明 |
|------|------|
| `learning.objective.progress` | 各目标完成进度 |
| `learning.mastery.distribution` | 掌握度分布（estimated/familiar/understood/mastered）|
| `learning.review.due_count` | 待复习节点数 |
| `learning.knowledge.creation_rate` | 知识节点产生速度 |
| `output.prompting.completion_rate` | 输出倒逼的完成率 |

### 6.3 告警规则

```yaml
alerts:
  - name: "首 token 延迟过高"
    condition: llm.first_token.latency.p99 > 5000ms
    severity: warning
    
  - name: "工具调用失败率过高"
    condition: tool.call.success_rate < 0.9
    severity: critical
    
  - name: "意图解析置信度持续偏低"
    condition: intent.confidence.avg < 0.5 for 10m
    severity: warning
    
  - name: "扩展 Hook 执行过慢"
    condition: extension.hook.duration.p99 > 1000ms
    severity: warning
```

---

## 7. 日志与状态快照（Log / Snapshot）

### 7.1 结构化日志

所有观测数据都以结构化格式记录：

```json
{
  "timestamp": "2026-05-10T10:00:00.123Z",
  "level": "info",
  "traceId": "trace-001",
  "spanId": "span-llm-001",
  "type": "llm.request",
  "data": {
    "model": "gpt-4o",
    "messageCount": 8,
    "totalTokens": 1950,
    "tools": ["read_material", "create_knowledge_node"]
  }
}
```

### 7.2 状态快照

在关键节点自动保存 Agent 的完整内部状态：

```yaml
snapshot:
  timestamp: "2026-05-10T10:00:00.500Z"
  traceId: "trace-001"
  node: "agent.beforeLLMCall"   # 快照节点
  
  state:
    session:
      id: "sess-abc"
      currentLeafId: "entry-5"
      depth: 12
    
    context:
      messages: [...]            # 完整消息列表
      totalTokens: 1950
      components: [...]          # 各组成部分
    
    memory:
      workingMemory: [...]
      recalledNodes: ["kn-1", "kn-2"]
      recallStrategy: "tag+keyword"
    
    extensions:
      active: ["core-intent", "core-clarification", "core-review"]
      hookQueue: [...]
    
    provider:
      model: "gpt-4o"
      temperature: 0.7
```

**快照用途**：
- 事后复盘："这次回答为什么差？"→ 查看当时的上下文组装
- 调试：复现问题场景
- 测试：用真实快照做回归测试

### 7.3 快照触发条件

```typescript
const snapshotTriggers = [
  'agent.beforeLLMCall',       // 每次调用 LLM 前
  'agent.afterResponse',       // 每次响应后
  'memory.recallDone',         // 每次召回后
  'tool.callFailed',           // 工具调用失败时
  'user.ratedNegative',        // 用户给了负面反馈
  'intent.clarification',      // 进入澄清循环时
];
```

---

## 8. 评估体系（Eval）设计

观测告诉你"发生了什么"，Eval 告诉你"结果好不好"。

### 8.1 评估维度

```yaml
eval_dimensions:
  response_quality:
    - relevance:        # 回答是否与问题相关
        score: 1-5
        auto: true      # 可用 LLM 自动评分
    
    - accuracy:         # 事实准确性
        score: 1-5
        auto: false     # 需要人工或对比材料
    
    - completeness:     # 是否覆盖问题的所有方面
        score: 1-5
        auto: true
    
    - clarity:          # 表达是否清晰
        score: 1-5
        auto: true
  
  memory_quality:
    - recall_precision: # 召回的记忆是否相关
        score: 0-1
        auto: false     # 需要人工标注
    
    - recall_recall:    # 是否漏掉了应该召回的记忆
        score: 0-1
        auto: false
  
  intent_quality:
    - parse_accuracy:   # 意图解析是否正确
        score: 0-1
        auto: false
    
    - confidence_calibration:  # 置信度是否校准（0.9 的置信度是否真的 90% 准？）
        score: 0-1
        auto: true
  
  system_performance:
    - latency:          # 总耗时
        target: < 3000ms
    
    - first_token_latency:  # 首 token 延迟
        target: < 500ms
    
    - cost:             # 单次请求成本
        target: < $0.01
```

### 8.2 自动评估（Auto-Eval）

用 LLM 评估 LLM——成本低、速度快、可规模化：

```typescript
async function autoEvalResponse(request: string, response: string, context: any) {
  const evalPrompt = `
请评估以下 AI 助手回答的质量。

用户问题：${request}
AI 回答：${response}
相关上下文：${JSON.stringify(context)}

请从以下维度评分（1-5分）：
1. 相关性（relevance）：回答是否与问题相关？
2. 完整性（completeness）：是否覆盖了问题的关键方面？
3. 清晰度（clarity）：表达是否清晰易懂？

输出格式（JSON）：
{
  "relevance": {"score": 4, "reason": "..."},
  "completeness": {"score": 3, "reason": "..."},
  "clarity": {"score": 5, "reason": "..."}
}
`;
  
  return await provider.chat({ messages: [{ role: 'user', content: evalPrompt }] });
}
```

### 8.3 用户反馈

```yaml
user_feedback:
  explicit:                    # 显式反馈
    - type: thumbs_up/down     # 点赞/点踩
    - type: rating             # 1-5 星评分
    - type: correction         # 用户纠正 AI 的错误
    
  implicit:                    # 隐式反馈
    - type: copy_response      # 复制了回答
    - type: follow_up_question # 继续追问（说明回答激发了思考）
    - type: session_duration   # 会话时长
    - type: output_completion  # 用户完成了输出倒逼任务
```

### 8.4 Eval 数据集

积累一个"黄金数据集"用于回归测试：

```yaml
eval_dataset:
  - id: "eval-001"
    input: "Raft 中 Leader 选举需要满足什么条件？"
    expectedTopics: ["majority", "quorum", "timeout", "term"]
    expectedKnowledgeNodes: ["kn-raft-leader-election"]
    expectedTool: "query_knowledge_graph"
    
  - id: "eval-002"
    input: "帮我总结一下刚才的内容"
    expectedBehavior: "summarize_session"
    contextRequired: ["session_history"]
```

每次修改核心代码或扩展时，跑一遍 Eval 数据集，确保没有回归。

---

## 9. 观测数据流与存储

### 9.1 数据流架构

```
┌─────────────────────────────────────────────────────────────┐
│                        Agent 运行时                          │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Events  │  │  Traces  │  │ Metrics  │  │ Snapshots│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       │             │             │             │         │
│       └─────────────┴─────────────┴─────────────┘         │
│                         │                                   │
│                  ┌──────┴──────┐                          │
│                  │ 观测收集器   │  (core-observability)    │
│                  │ Collector   │                          │
│                  └──────┬──────┘                          │
└─────────────────────────┼─────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
          ▼               ▼               ▼
   ┌────────────┐  ┌────────────┐  ┌────────────┐
   │ 本地日志    │  │ 本地仪表   │  │ 评估系统   │
   │ 文件(JSONL)│  │ 面板(Web)  │  │ (回归测试) │
   └────────────┘  └────────────┘  └────────────┘
```

### 9.2 存储方案

| 数据类型 | 存储 | 保留策略 |
|---------|------|---------|
| Trace | JSONL 文件 | 30 天 |
| Metrics | 内存 + 定期聚合 | 实时 + 小时级聚合持久化 |
| Log | JSONL 文件 | 7 天 |
| Snapshot | JSON 文件（按 trace 组织）| 30 天 |
| Eval 结果 | SQLite / JSONL | 永久 |
| Eval 数据集 | JSON 文件 | 版本控制 |

### 9.3 本地仪表盘

一个轻量的 Web 仪表盘，实时展示：

```
┌─────────────────────────────────────────────────┐
│  Agent 观测仪表盘                                │
├─────────────────────────────────────────────────┤
│  实时指标                        │  最近 Trace   │
│  ┌─────────┐ ┌─────────┐        │  trace-001   │
│  │ 请求数   │ │ 平均耗时  │        │  3.2s ✓     │
│  │ 12/min  │ │ 2.8s    │        │  trace-002   │
│  └─────────┘ └─────────┘        │  4.1s ⚠️    │
│  ┌─────────┐ ┌─────────┐        │  trace-003   │
│  │ 首token  │ │ token   │        │  1.9s ✓     │
│  │ 320ms   │ │ 2560    │        │              │
│  └─────────┘ └─────────┘        │              │
├─────────────────────────────────────────────────┤
│  扩展性能分布                     │  记忆召回质量 │
│  core-intent: ████████ 45ms      │  precision:  │
│  core-review: ██ 12ms            │  0.75        │
│  core-output: █ 5ms              │  recall:     │
│                                  │  0.60        │
├─────────────────────────────────────────────────┤
│  最新告警                                        │
│  ⚠️ intent.confidence.avg < 0.5 (持续 10 分钟)   │
└─────────────────────────────────────────────────┘
```

---

## 10. 人类可读性：观测的呈现方式

> **观测数据的价值 = 数据质量 x 可读性。再完美的数据，如果人看不懂，就等同于不存在。**

机器喜欢的数据格式（JSON、CSV、指标曲线）对人类不友好。Agent 的观测必须被"翻译"成人类能直觉理解的形式。

### 10.1 核心原则：从"数据展示"到"故事讲述"

| 机器视角 | 人类视角 |
|---------|---------|
| 一堆 JSON 字段 | "Agent 花了 3 秒，先理解了你的意图，然后召回了 2 条记忆，最后让 GPT-4 生成回答" |
| token 数字 1950 | 上下文利用率 24%，还有大量空间 |
| Hook 耗时列表 | `core-intent` 花了 45ms 解析意图，是最慢的环节 |
| 召回节点 ID 列表 | 召回了"Raft Leader 选举"和"Quorum 机制"，但漏掉了"网络分区" |

**目标：任何开发者打开一次观测记录，30 秒内能理解发生了什么。**

### 10.2 叙事式 Trace 总结

不是给开发者看原始 Span 树，而是用自然语言讲述这次对话 Agent 的内部过程。

```markdown
# Trace 叙事总结: trace-20260510-001

## 概要
- **用户输入**: "Raft 的 Leader 选举在网络分区时会发生什么？"
- **总耗时**: 3.2s | **首 token**: 320ms | **模型**: GPT-4o
- **结果**: 成功生成回答

## 思考过程

### 1. 意图解析 (45ms)
Agent 识别到这是一个 **分析型问题**（置信度 92%），涉及概念：
- Raft 共识算法
- Leader 选举
- 网络分区

-> 无需澄清，直接进入上下文组装。

### 2. 记忆召回 (120ms)
Agent 从长期记忆中找到了 **3 条相关记忆**：

| 记忆 | 相关度 | 是否使用 |
|------|--------|---------|
| "Raft Leader 选举需要半数投票" | 95% | 已加入上下文 |
| "Quorum 机制" | 78% | 已加入上下文 |
| "网络分区下的脑裂问题" | 65% | 阈值不足，未使用 |

**注意**: 第三条"网络分区下的脑裂问题"被过滤掉了，但用户问题
    明确提到"网络分区"。这可能是召回策略的问题。

### 3. 上下文组装
上下文共 **1,950 tokens**（利用率 24%），组成如下：

```
系统提示       200 tokens  (探索模式)
会话历史       800 tokens  (12 条消息)
相关记忆       600 tokens  (2 个节点)
复习提醒       300 tokens  (1 个节点)
用户输入        50 tokens
```

### 4. LLM 生成 (2,800ms)
- **首 token 延迟**: 320ms（正常）
- **生成速度**: 180 tokens/s
- **总生成**: 512 tokens
- **工具调用**: 无

### 5. 输出处理
- 触发扩展 `core-output-prompting`：用户已连续 3 轮被动接收，
  追加了一句话："请用自己的话描述一下脑裂问题的解决方案。"

---

## 关键洞察

1. **召回可能漏了关键记忆**: "网络分区下的脑裂问题"相关度 65%，
   刚好低于 70% 阈值。建议检查召回阈值设置。

2. **上下文利用率低**: 仅用了 24%，说明可以加载更多记忆或历史，
   或当前模型上下文窗口足够大。

3. **输出倒逼触发**: 用户处于被动接收模式，已触发输出要求。
```

**这个叙事总结由系统自动生成**——基于 Trace 数据，用模板 + 轻量规则生成，不需要 LLM。

### 10.3 Prompt 的可视化呈现

开发者最需要看的是"Agent 到底给 LLM 看了什么"。原始 messages 数组不可读，必须格式化：

```
发送给 LLM 的消息 (共 8 条, 1,950 tokens)

[系统提示] (200 tokens)
  你是一个学习助手，当前处于探索模式...
  你的目标是帮助用户理解分布式系统...

[用户] (50 tokens)
  Raft 的 Leader 选举在网络分区时会发生什么？

[助手] 历史 (800 tokens, 已折叠 10 条)
  Raft 的 Leader 选举分为两个阶段...

[相关记忆] (600 tokens)
  [来自知识图谱] Raft Leader 选举 (相关度 95%)
    "需要获得半数以上节点的投票..."
  [来自知识图谱] Quorum 机制 (相关度 78%)
    "Quorum 是指分布式系统中达成一致的最低节点数..."

[复习提醒] (300 tokens)
  [间隔重复触发] 你昨天学过 "CAP 定理"，还记得吗？
  它在网络分区场景下与 Raft 有密切关联。

---
上下文分析:
- 会话历史占比 41%，记忆占比 31%，提示占比 10%
- 当前窗口利用率 24%，距离上限还有 6,050 tokens
- 历史消息 12 条，建议当超过 20 条时进行压缩
```

**关键设计**：
- 每条消息标注**角色、token 数**
- 记忆消息标注**来源和相关度**
- 长历史可以**折叠**
- 底部给出**上下文分析建议**

### 10.4 Diff 视图：Hook 介入前后的变化

扩展通过 Hook 修改数据，但开发者常常不知道"改了什么"。需要 Diff 视图：

```
Hook: core-output-prompting @ agent.afterResponse

修改前 (512 tokens):
  网络分区时，Raft 会通过心跳超时检测到 Leader 失联...
  ...因此网络分区不会导致数据不一致。

修改后 (548 tokens):
  网络分区时，Raft 会通过心跳超时检测到 Leader 失联...
  ...因此网络分区不会导致数据不一致。

  [新增] 请用自己的话描述一下脑裂问题的解决方案。

耗时: 12ms | 结果: modified
```

### 10.5 记忆召回的可视化

不是列出节点 ID，而是展示召回过程和结果：

```
记忆召回: "Raft 的 Leader 选举在网络分区时..."

查询策略: tag 匹配 + 关键词搜索

召回候选 (按相关度排序):

  [选中] kn-1 "Raft Leader 选举"     相关度 95%
    来源: 《DDIA》第9章 p.127
    掌握度: understood
    上次复习: 2 天前

  [选中] kn-3 "Quorum 机制"          相关度 78%
    来源: 会话 sess-20260508-001
    掌握度: familiar

  [过滤] kn-7 "网络分区下的脑裂"     相关度 65%
    原因: 低于阈值 70%
    建议: 用户问题明确提到"网络分区"，应降低阈值

  [过滤] kn-12 "Paxos 算法"          相关度 30%
    原因: 相关度过低

召回质量: precision=100% (选中的都相关), recall=40%
(漏掉了 kn-7，而 kn-12 本来就不该召回)
```

### 10.6 时间线视图：像 Chrome DevTools 一样

```
0ms     100ms   200ms   500ms   1s      2s      3s      3.5s
|       |       |       |       |       |       |       |
[=======]       [=======]       [===============]       |
 intent         context          LLM Stream
 parse          build            (2800ms)
 45ms           120ms            [===========]
                                 320ms
                                 首token
                                 ^
                                 chunk #1-42 (180 tok/s)

点击任意片段查看详情:
  [intent.parse] -> 查看解析结果和置信度
  [context.build] -> 查看完整上下文组成
  [LLM Stream] -> 查看逐 token 生成过程
```

### 10.7 错误诊断的引导式视图

当 Agent 出错时，不要给一堆日志，而是引导开发者定位问题：

```
本次请求异常: 意图解析置信度过低

诊断路径:

1. 用户输入
   "帮我看看这个"
   |
   问题: 输入过于模糊，缺少主语和上下文

2. 意图解析结果
   - analyze_code: 35%
   - summarize: 30%
   - explore: 20%
   - 其他: 15%
   |
   问题: 没有明显占优的意图，置信度分布太分散

3. 系统处理
   -> 触发澄清协议
   -> 向用户提问: "你想让我分析哪段代码？"
   -> 用户未在 30 秒内响应，请求超时

建议:
- 短期: 放宽澄清等待时间，或给用户提供快速选项
- 长期: 改进意图解析 prompt，加入更多上下文特征

[查看完整 Trace]  [查看 Prompt]  [查看快照]
```

### 10.8 记忆关联图谱视图

当观测知识节点时，人类更容易理解图谱而不是列表：

```
用户当前问题: "Raft 网络分区"
                    |
        +-----------+-----------+
        |           |           |
        v           v           v
   [Leader]    [Quorum]    [网络]
   [选举]      [机制]      [分区]
   (95%)       (78%)       (65%)
      |            |           |
      |            v           v
      |        [CAP]        [脑裂]
      |        [定理]       [问题]
      |        (关联)       (遗漏)
      +--------->

召回结果: [OK] kn-Leader选举, [OK] kn-Quorum机制
         [WARN] kn-网络分区 (被过滤, 建议降低阈值)
         [MISS] kn-脑裂问题 (未召回)
```

### 10.9 交互式设计：从"看"到"操作"

观测不只是静态查看，还要支持交互：

| 交互 | 作用 |
|------|------|
| 点击一个记忆节点 | 查看完整内容、来源、掌握度历史 |
| 拖动召回阈值滑块 | 实时看到哪些节点会被召回/过滤 |
| 编辑 Prompt 并重放 | 用修改后的 Prompt 重新运行同一次请求 |
| 对比两次 Trace | A/B 对比两个版本 Agent 的行为差异 |
| 标记错误召回 | 人工反馈"这个节点不该被召回"，用于改进算法 |

### 10.10 实现：观测的"呈现层"

观测数据是机器格式，呈现层负责翻译：

```
原始数据层                    呈现层
+--------------+            +--------------------------+
| Trace JSON   | -------->  | 叙事总结生成器           |
| Span 树      |            | (模板 + 规则 -> Markdown)|
+--------------+            +--------------------------+
                            | Prompt 格式化器          |
+--------------+            | (messages -> 可视化卡片) |  
| Snapshot     | -------->  +--------------------------+
| 状态快照     |            | Diff 引擎                |
+--------------+            | (before/after -> 高亮)   |
                            +--------------------------+
+--------------+            | 时间线渲染器             |
| Metrics      | -------->  | (Span -> Gantt 图)       |
| 指标数据     |            +--------------------------+
+--------------+            | 诊断引导引擎             |
                            | (规则 -> 建议列表)       |
                            +--------------------------+
                                         |
                                         v
                              +----------------------+
                              |  本地 Web 仪表盘      |
                              |  (人类可读的可视化)   |
                              +----------------------+
```

**关键设计**：
- 原始数据用 JSONL/SQLite 存储（机器高效）
- 呈现层按需渲染（人类可读）
- 呈现层本身也是 Agent 系统的一部分，可以用扩展实现（如 `core-observability-dashboard`）

---

## 11. 优化闭环：从观测到改进

### 11.1 闭环流程

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  运行    │───→│  观测    │───→│  分析    │───→│  假设    │───→│  实验    │
│          │    │          │    │          │    │          │    │          │
│ Agent    │    │ 收集     │    │ 发现     │    │ 提出     │    │ A/B      │
│ 运行中   │    │ Trace/   │    │ 瓶颈/    │    │ 优化     │    │ 测试/    │
│          │    │ Metrics  │    │ 问题     │    │ 方案     │    │ 回归测试 │
└──────────┘    └──────────┘    └──────────┘    └──────────┘    └─────┬────┘
                                                                       │
                                                                       ▼
                                                                ┌──────────┐
                                                                │  验证    │
                                                                │          │
                                                                │ Eval 数据│
                                                                │ 集验证   │
                                                                └─────┬────┘
                                                                      │
                                                                      ▼
                                                               ┌──────────┐
                                                               │  部署    │───┐
                                                               │          │   │
                                                               │ 合并到   │   │
                                                               │ 主分支   │   │
                                                               └──────────┘   │
                                                                               │
                                                                               └────→ 回到"运行"
```

### 11.2 典型优化案例

#### 案例 1：回答质量下降

```
观测：
  - auto-eval.relevance.avg 从 4.2 降到 3.1
  - memory.recall.precision 从 0.8 降到 0.5

分析：
  - 查看 Trace，发现召回的节点与问题不相关
  - 快照显示上下文组装时塞了太多无关记忆

假设：
  - Relevant Recall 策略太宽松，召回了很多低相关度节点

实验：
  - 调整召回阈值从 0.6 到 0.75
  - 在 Eval 数据集上测试

验证：
  - relevance.avg 回升到 4.0
  - recall.precision 回升到 0.78
  - 但 recall.recall 从 0.7 降到 0.55（漏召了一些）

决策：
  - 折中方案：阈值 0.7，同时增加一次重召回机制
```

#### 案例 2：首 token 延迟过高

```
观测：
  - llm.first_token.latency.p99 > 5000ms
  - context.totalTokens 经常在 6000+

分析：
  - 快照显示上下文组装塞了太多历史会话
  - 记忆召回也返回了大量内容

假设：
  - 上下文过长导致 LLM 处理变慢

实验：
  - 限制 session_history 最多 6 条
  - 记忆召回最多 3 个节点，每个节点摘要到 200 token

验证：
  - first_token.latency.p99 降到 2000ms
  - relevance.avg 无显著变化
```

#### 案例 3：扩展 Hook 拖慢系统

```
观测：
  - agent.request.duration.p99 突然升高到 8s
  - extension.hook.duration 显示 core-output-prompting 占 3s

分析：
  - 该 Hook 在 afterResponse 时调用了 LLM 做质量评估

假设：
  - Hook 里不应该同步调用 LLM

实验：
  - 将 LLM 评估改为异步（发事件，不阻塞响应）

验证：
  - request.duration.p99 降到 3s
  - 评估仍然完成，只是延迟几秒写入
```

---

## 12. 当前版本实现优先级

### P0（当前必须实现）

观测系统是基础设施，应该在写第一个业务功能前就搭好：

- [ ] **Event Bus 支持"*"订阅**（观测扩展需要订阅所有事件）
- [ ] **Trace/Span 基础结构**（Hook Context 携带 trace 和 span）
- [ ] **核心性能计时**（每个 Hook、每个 LLM 调用、每个工具调用计时）
- [ ] **结构化日志输出**（JSONL 格式到本地文件）
- [ ] **基础快照**（agent.beforeLLMCall 时保存上下文快照）

### P1（第一个 Milestone 后实现）

- [ ] **本地仪表盘**（Web 页面展示实时指标和最近 Trace）
- [ ] **自动 Eval**（用 LLM 评估回答质量）
- [ ] **Eval 数据集**（积累 20-50 个测试用例）
- [ ] **用户反馈收集**（点赞/点踩）

### P2（稳定后实现）

- [ ] **高级分析**（召回质量评估、意图解析校准）
- [ ] **告警系统**（关键指标异常时通知）
- [ ] **对比分析**（两个版本 Agent 的 A/B 对比）
- [ ] **导出到外部系统**（可选导出到 OpenTelemetry / Prometheus）

---

> **一句话总结：观测不是"锦上添花"，而是 Agent 系统的"神经系统"。没有观测，你只能猜测问题出在哪里；有了观测，你可以精确定位、量化影响、验证修复。**
