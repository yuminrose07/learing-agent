# 修复：AgentLoop 单体文件拆分与边界收敛

> 日期：2026-05-16
> 涉及模块：`learning_agent/agent/agent_loop.py`、`learning_agent/agent/session_runtime.py`、`learning_agent/agent/react_engine.py`、`learning_agent/agent/tool_executor.py`

---

## 一、问题概述

`learning_agent/agent/agent_loop.py` 长期维持在 **1904 行**，同时承载了以下四类截然不同的职责：

1. **ReACT 双层循环**：LLM 上下文组装、流式调用、tool call 解析、while 循环流转
2. **工具执行流水线**：ban 检查、参数 validation、Hook 前后切面、失败重试、failure tracker 记录
3. **边界横切逻辑**：状态机、并发锁、Hook 插桩、EventBus 广播、Observability trace/span、降级策略
4. **运行时路由**：per-session 容器管理、TTL 清理、对外只读摘要

这导致：

- **代码难以阅读和维护**：定位一个 bug 需要在一千九百行中横向扫描
- **测试难以独立覆盖**：想测 LLM 重试逻辑必须同时构造 HookSystem + EventBus + ObservabilityCollector
- **职责边界模糊**：`AgentLoopSession` 既做"产品想怎么运行"的决策，又做"这一轮具体怎么执行"的实现，违反了 AGENTS.md 的分层原则
- **协作成本高**：任何对工具执行或 LLM 调用的修改都有极高概率触及其他三个领域

---

## 二、问题定义

### 2.1 直接表现

| 症状 | 影响 |
|------|------|
| `agent_loop.py` 1904 行 / 47 个定义 | 文件体积超过 IDE 舒适浏览阈值，代码审查困难 |
| `_execute_tool_calls` 单方法 370 行 | 工具执行逻辑全部内联，无法独立单元测试 |
| `AgentLoopSession` 依赖 13 个外部组件 | 构造一个最小测试环境需要 mock 大量无关依赖 |
| 7 个测试因 `profile` 参数缺失而持续失败 | `AgentLoop.run()` 签名变更后测试未同步更新 |

### 2.2 根因

- **设计时未做物理拆分**：架构文档（`docs/design/`）中早已定义了四层分层，但 Runtime 层的实现却把所有子域塞进同一个文件
- **增量开发累积**：每次新增功能（韧性、降级、Hook、观测）都直接在 `agent_loop.py` 中追加方法，缺乏"这该放哪"的收敛检查
- **测试与 API 不同步**：`TurnExecutionProfile` 参数引入后，`AgentLoop.run()` 的测试调用方未更新

---

## 三、修复策略

### 3.1 拆分方案

按"谁拥有状态、谁负责编排、谁只负责执行"原则，将 `agent_loop.py` 拆为三个新文件 + 一个精简后的原文件：

```
agent_loop.py        → AgentLoop 路由器（~200 行）
session_runtime.py   → AgentLoopSession 边界编排器（~1000 行）
react_engine.py      → ReActEngine 纯 LLM 循环核心（~300 行）
tool_executor.py     → ToolExecutor 工具执行流水线（~500 行）
```

### 3.2 各文件职责边界

#### `react_engine.py` — 只关心 LLM 说什么

- **做的事**：组装上下文、流式调用 LLM（含重试）、解析 tool call buffer、紧急截断过长 tool result
- **不做的事**：不调用 Hook、不发布 EventBus 事件、不操作状态机、不做降级决策、不执行工具
- **依赖**：`BaseProvider`、`SessionStore`、`ResilienceConfig`

#### `tool_executor.py` — 只关心工具怎么执行

- **做的事**：对单个 tool call 执行完整生命周期（ban 检查 → validation → `before_tool_execute` Hook → 裸执行 → 重试 → `after_tool_execute` Hook）
- **不做的事**：不持有 session 状态机、不决定 ReACT 循环流转
- **依赖**：`HookSystem`、`ToolInputValidator`、`ToolFailureTracker`、`ToolExecutionService`、`SessionStore`

#### `session_runtime.py` — 只关心边界怎么包

- **做的事**：`run_turn()` 外层 while 循环、状态机转换、Hook 插桩、EventBus 广播、Obs trace/span 生命周期、chat-only 降级与恢复、错误兜底
- **委托点**：LLM 调用 → `ReActEngine`；工具执行 → `ToolExecutor`
- **向后兼容**：保留 `AgentLoopSession` 的公开接口签名不变；新增 `_execute_tool_calls` / `_stream_chat_with_retry` 包装方法供既有测试调用

#### `agent_loop.py` — 只负责路由

- **做的事**：`AgentLoop` 管理 per-session `AgentLoopSession` 实例、TTL 清理、对外只读摘要
- **向后兼容**：重新导出 `AgentLoopSession` 和 `AgentState`，保证 `from learning_agent.agent.agent_loop import ...` 的既有代码零改动

### 3.3 修复原有测试失败

`AgentLoop.run()` 的 `profile` 参数在新代码中设置为可选（默认 `None`）。当传入 `None` 时，自动构造默认 `TurnExecutionProfile`：

```python
profile = TurnExecutionProfile(
    mode=AgentMode.CHAT,
    turn_kind=TurnExecutionKind.REACT,
    system_prompt=self._get_system_prompt(),
)
```

这修复了 7 个因 `profile` 参数缺失而持续失败的测试，且不影响任何调用方传入自定义 `profile` 的行为。

---

## 四、关键变更清单

### 4.1 新增文件

| 文件 | 来源 | 说明 |
|------|------|------|
| `learning_agent/agent/react_engine.py` | 新增 | 从 `agent_loop.py` 提取的 LLM 纯逻辑 |
| `learning_agent/agent/tool_executor.py` | 新增 | 从 `agent_loop.py` 提取的工具执行流水线 |
| `learning_agent/agent/session_runtime.py` | 新增 | 从 `agent_loop.py` 提取的 `AgentLoopSession` |

### 4.2 修改文件

| 文件 | 变更 |
|------|------|
| `learning_agent/agent/agent_loop.py` | 从 1904 行精简为 ~219 行；保留 `AgentLoop` + 向后兼容导出；`run()` 的 `profile` 参数设为可选 |
| `learning_agent/agent/__init__.py` | `AgentLoopSession` 和 `AgentState` 的 `__getattr__` 映射指向 `session_runtime` 模块 |

### 4.3 向后兼容措施

1. `agent_loop.py` 重新导出 `AgentLoopSession` 和 `AgentState`，测试代码的 `from learning_agent.agent.agent_loop import AgentLoop, AgentLoopSession, AgentState` 无需修改
2. `AgentLoopSession` 新增 `_execute_tool_calls()` 和 `_stream_chat_with_retry()` 包装方法，供既有测试直接调用
3. `AgentLoop.run()` 的 `profile` 参数添加默认值，修复旧测试回归

---

## 五、验证结果

### 5.1 测试覆盖

```bash
pytest tests/ -v
```

**结果：67 passed, 0 failed**

拆分前状态：60 passed, 7 failed（`profile` 参数缺失导致的回归）
拆分后状态：67 passed, 0 failed

### 5.2 文件体积

| 文件 | 拆分前 | 拆分后 |
|------|--------|--------|
| `agent_loop.py` | 1904 行 | 219 行 |
| `session_runtime.py` | — | 1064 行 |
| `react_engine.py` | — | 327 行 |
| `tool_executor.py` | — | 495 行 |
| **合计** | **1904** | **2105** |

行数略有增加（+~200 行），主要来源于：
- `ToolExecutor` 提取后需要独立的构造函数和辅助方法
- `ReActEngine` 添加了 `attempt_emergency_truncation` 和事件发射（兼容测试所需）
- `AgentLoopSession` 添加了向后兼容包装方法

**可维护性提升远大于行数增加的代价**。

---

## 六、后续建议

1. **进一步收敛 `session_runtime.py`**：虽然已从 1904 行降到 1064 行，`AgentLoopSession` 仍然是边界层的大头。未来可考虑把事件发射、状态转换、Obs 打点提取为独立的 `BoundaryDecorator` 或 `TurnOrchestrator`
2. **消除 `ReActEngine` 对 `EventBus` 的依赖**：当前为了兼容 `test_large_file_handling.py` 中的事件断言，`ReActEngine` 保留了可选的 `events` 参数。后续可把 `attempt_emergency_truncation` 的截断信号通过返回值暴露，由 `AgentLoopSession` 统一发射事件
3. **为 `react_engine.py` 和 `tool_executor.py` 补充独立单元测试**：目前它们通过 `AgentLoopSession` 的兼容包装被间接测试，未来应直接 mock `BaseProvider` / `ToolExecutionService` 进行快速单元测试
4. **删除兼容包装**：当确认没有外部代码直接调用 `_execute_tool_calls` / `_stream_chat_with_retry` 后，可从 `AgentLoopSession` 中移除这两个向后兼容方法
