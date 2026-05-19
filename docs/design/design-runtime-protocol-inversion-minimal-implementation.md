# 技术设计文档：Runtime 依赖倒置与多 Session 最小改动实现方案

> **目标**：消除 `AgentLoop` 对 Product 层 `SessionManager` / `MemoryManager` 具体实现类的反向依赖，同时保持现有 `AgentLoopSession` 主执行链基本不动。
> **设计原则**：Runtime 只依赖执行 turn 所必需的最小能力端口；Session 生命周期、全局观测拼装与跨 session 编排继续留在 Product 层。
> **文档日期**：2026-05-14
> **实现状态**：已落地 `SessionStore` / `MemoryService` / `ToolExecutionService` 三类端口，本文档反映当前真实代码状态。

---

## 一、背景与问题定义

### 1.1 当前真实依赖

当前系统装配链路如下：

```text
CLI / Web
    ->
LearningAgentSystem
    ->
AgentLoop(provider, memory_manager, session_manager, ...)
    ->
AgentLoopSession
```

其中，`AgentLoop` 直接依赖 Product 层的具体实现类：

- `learning_agent/agent/agent_loop.py`
  - 直接 import `SessionManager`
  - 直接 import `MemoryManager`
- `learning_agent/learning_agent/main.py`
  - 负责创建 `SessionManager`、`MemoryManager`
  - 再将具体对象注入 `AgentLoop`

这种结构在单 session 或 CLI 串行场景下可以工作，但在 Web 多 session 场景下会产生明显的边界问题：

- Runtime 作为执行器，却直接知道 Product 层的具体编排对象。
- Runtime 可以天然看到超出 turn 执行所需的能力，例如：
  - `list_sessions()`
  - `delete_session()`
  - `register_delete_callback()`
- 一旦后续继续往 `SessionManager` 上加 Product 语义方法，Runtime 很容易“顺手”调用，导致边界继续扩张。

### 1.2 当前代码中的具体症状

当前 `AgentLoop` 对 `SessionManager` 的真实使用可以分为两类：

**A. turn 执行必需能力**

- `append_message()`
- `get_message_history()`

**B. 非 turn 必需能力**

- `register_delete_callback()`
- `list_sessions()`

其中：

- A 类能力确实属于 Runtime 的最小依赖面。
- B 类能力本质上属于 Product 层的生命周期管理与聚合观测，不应继续由 Runtime 直接持有。

### 1.3 为什么这在 Web 多 Session 下更重要

CLI 的典型模式是“当前只有一个活跃 session，串行推进”，运行时和产品编排的边界较难暴露。

Web 的典型模式是“同一时刻有多个 session 并发请求”：

- Product 层负责判断“哪个 session 的请求被路由到 Runtime”
- Runtime 只负责“对这个 session 执行一轮 turn”

因此，多 session 场景要求 Runtime 的能力边界必须被明确收窄，否则 Runtime 会逐步从“执行层”退化成“半个编排层”。

---

## 二、设计目标与非目标

### 2.1 设计目标

| 目标 | 说明 |
|------|------|
| **依赖倒置** | Runtime 不再 import Product 层具体实现类，只依赖 Protocol |
| **最小改动** | 不重写 `AgentLoopSession` 的 1800+ 行 turn 执行链 |
| **能力收窄** | Runtime 只保留 `append_message()` 和 `get_message_history()` 这类执行必需能力 |
| **职责回正** | Session 生命周期、全局 session 统计、runtime 聚合观测由 Product 层负责 |
| **兼容现状** | 保持当前 Web/CLI 接口、流式执行、自愈策略、状态快照机制基本不变 |

### 2.2 非目标

本次重构**不做**以下事情：

- 不将 `AgentLoopSession.run_turn()` 重写为纯函数式执行器。
- 不移除 Runtime 对 session 数据存取的所有依赖。
- 不重构 Hook / EventBus / Observability 的接线方式。
- 不在本次重构中深度接入 `MemoryManager` 到主执行链。
- 不改变现有 session runtime 的并发策略：
  - 同 session 串行
  - 不同 session 并行

---

## 三、目标架构

### 3.1 重构后的依赖方向

目标依赖关系如下：

```text
CLI / Web
    ->
LearningAgentSystem
    ->
SessionManager implements SessionStore
MemoryManager implements MemoryService
ToolExecutionServiceImpl implements ToolExecutionService
    ->
AgentLoop(provider, session_store, memory_service, tool_execution_service, ...)
    ->
AgentLoopSession
```

关键变化：

- Runtime 定义自己需要的接口。
- Product 层提供这些接口的具体实现。
- Runtime 不再依赖 Product 层具体类名，只依赖能力边界。

### 3.2 分层职责调整

重构后应明确以下边界：

**Runtime 层负责**

- per-session runtime 实例路由
- turn 执行
- 状态机、自愈、工具执行、事件发射
- 单个 runtime 的只读摘要输出

**Product 层负责**

- session 创建 / 删除 / 查找 / 列表 / 生命周期
- 请求路由到哪个 `LearningSession`
- runtime 清理时机
- 具体工具执行实现与工具定义查询
- 全局 overview 聚合，例如：
  - `total_session_count`
  - runtime 列表与 session 总数的组合响应

---

## 四、端口设计

### 4.1 `SessionStore` Protocol

新建 Runtime 端口文件，例如：

- `learning_agent/agent/runtime_ports.py`

建议定义：

```python
from __future__ import annotations

from typing import Optional, Protocol

from learning_agent.ai import MessageRole, SessionEntry


class SessionStore(Protocol):
    def append_message(
        self,
        session_id: str,
        role: MessageRole,
        content: str,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[SessionEntry]: ...

    def get_message_history(
        self,
        session_id: str,
        leaf_id: Optional[str] = None,
    ) -> list[SessionEntry]: ...
```

### 4.2 为什么协议只保留这两个方法

因为这是当前 `AgentLoopSession` 执行 turn 的最小必需能力：

- `append_message()`：执行过程中需要实时写入用户消息、assistant 消息、工具结果等。
- `get_message_history()`：构造上下文和保存状态快照时需要读取当前分支消息历史。

本次方案**不把以下方法放入 Protocol**：

- `get_session()`
- `list_sessions()`
- `delete_session()`
- `register_delete_callback()`
- `fork_session()`
- `archive_session()`

原因是这些都不是 Runtime 执行 turn 的最小必需能力，保留它们只会重新放大 Runtime 的边界。

### 4.3 `MemoryService` Protocol

当前 Memory 主链尚未深度接入，因此建议先定义极小接口：

```python
from __future__ import annotations

from typing import Optional, Protocol

from learning_agent.ai import ContextComponent, LearningSession


class MemoryService(Protocol):
    def relevant_recall(
        self,
        query: str,
        session: Optional[LearningSession] = None,
        limit: int = 5,
    ) -> list[ContextComponent]: ...
```

实现策略：

- 第一阶段允许 `memory_service` 仅作为可选依赖注入。
- 如果 Runtime 当前没有实际调用点，可以先保留参数但不使用。
- 等主链真正需要 recall 时，再通过该端口接入。

### 4.4 `ToolExecutionService` Protocol

工具执行边界现已按同样方式下沉到 Product 层。Runtime 只知道：

- 如何执行一个 `ToolCall`
- 如何读取某个工具定义
- 如何列出当前可用工具

建议定义：

```python
from __future__ import annotations

from typing import Any, Optional, Protocol

from learning_agent.ai import ToolCall, ToolDefinition


class ToolExecutionService(Protocol):
    async def execute_tool_call(
        self,
        tool_call: ToolCall,
        timeout: Optional[float] = None,
    ) -> Any: ...

    def get_tool_definition(self, tool_id: str) -> Optional[ToolDefinition]: ...

    def list_tools(self) -> list[ToolDefinition]: ...
```

边界约束：

- Runtime 不直接持有工具 handler。
- Runtime 不直接管理工具注册/注销。
- Runtime 不依赖 `ToolRegistry` 具体类。
- Product 层可以继续用 `ToolRegistry` 作为内部实现细节，但该细节不再向 Runtime 暴露。

---

## 五、逐文件改动方案

### 5.1 新增文件：`learning_agent/agent/runtime_ports.py`

新增内容：

- `SessionStore`
- `MemoryService`
- `ToolExecutionService`

约束：

- 该文件只定义 Protocol，不放实现逻辑。
- 不依赖 Product 层模块。
- 尽量只 import `learning_agent.ai` 中已经稳定存在的类型。

### 5.2 修改：`learning_agent/agent/agent_loop.py`

#### 5.2.1 import 调整

当前：

```python
from learning_agent.memory.memory_manager import MemoryManager
from learning_agent.learning_agent.session_manager import SessionManager
```

改为：

```python
from learning_agent.agent.runtime_ports import (
    MemoryService,
    SessionStore,
    ToolExecutionService,
)
```

目的：

- 消除 Runtime 文件对 Product 具体类的 import。

#### 5.2.2 构造函数签名调整

当前：

```python
def __init__(
    self,
    provider: BaseProvider,
    memory_manager: MemoryManager,
    session_manager: SessionManager,
    ...
):
```

建议改为：

```python
def __init__(
    self,
    provider: BaseProvider,
    session_store: SessionStore,
    memory_service: MemoryService | None,
    tool_execution_service: ToolExecutionService,
    ...
):
```

内部字段同步调整：

```python
self.memory_service = memory_service
self.sessions = session_store
self.tool_execution_service = tool_execution_service
```

这里保留 `self.sessions` 这个字段名也可以，优点是：

- 改动面更小
- `AgentLoopSession` 里现有调用点几乎不用动

如果后续希望语义更清晰，也可以统一改成 `self.session_store`，但那会带来更多替换，不属于“最小改动”。

#### 5.2.3 删除回调注册逻辑移除

当前：

```python
if hasattr(self.sessions, "register_delete_callback"):
    self.sessions.register_delete_callback(self.clear_session_runtime)
```

建议直接删除。

原因：

- 这是 Runtime 对 Product 生命周期对象的反向订阅。
- 多 session 架构下，session 删除时机应由 Product 层显式驱动。
- 保留这个回调会让 Runtime 继续知道“SessionManager 还有别的额外能力”。

#### 5.2.4 `clear_session_runtime()` 的定位

该方法可以保留，但语义要调整为：

- 这是 Product 层可主动调用的 Runtime 清理接口。
- 不再声明“由 `SessionManager` 删除回调触发”。

建议文档注释改成：

```python
"""
清理指定 session 的运行时状态。
供 Product 层在 session 删除、reset 或生命周期收缩时显式调用。
"""
```

#### 5.2.5 `get_runtime_overview()` 收窄

当前实现：

```python
return {
    "active_runtime_count": len(runtimes),
    "total_session_count": len(self.sessions.list_sessions()),
    "runtimes": runtimes,
}
```

建议改为只返回 Runtime 自身数据：

```python
return {
    "active_runtime_count": len(runtimes),
    "runtimes": runtimes,
}
```

或者更进一步，直接删除 `get_runtime_overview()`，只保留：

- `list_runtime_summaries()`
- `get_runtime_summary(session_id)`

在“最小改动”前提下，更推荐保留方法但收窄返回值，因为对现有调用方影响更小。

#### 5.2.6 工具执行通过 Protocol 下沉到 Product 层

Runtime 当前不再直接依赖 `ToolRegistry.execute()`，而是依赖：

```python
result = await self.tools.execute_tool_call(tc, timeout=timeout)
```

同时：

- 工具 schema 构造改为依赖 `list_tools()`
- 参数校验改为依赖 `get_tool_definition()`

这保证了 `AgentLoopSession` 只保留“工具调度”和“重试控制”，不再展开具体 handler 调用细节。

### 5.3 新增文件：`learning_agent/learning_agent/tool_execution_service.py`

新增 `ToolExecutionServiceImpl`，职责如下：

- 实现 Runtime 侧 `ToolExecutionService` Protocol
- 组合 `ToolRegistry`
- 将 `execute_tool_call()`、`get_tool_definition()`、`list_tools()` 统一委托给 Product 层内部的工具基础设施

这一步的重点不是移除 `ToolRegistry`，而是把它从 Runtime 可见依赖收缩成 Product 内部实现细节。

### 5.4 修改：`learning_agent/learning_agent/main.py`

#### 5.4.1 AgentLoop 装配改为注入 Protocol 实现

当前：

```python
self.agent_loop = AgentLoop(
    provider=self.provider,
    memory_manager=self.memory_manager,
    session_manager=self.session_manager,
    ...
)
```

改为：

```python
self.agent_loop = AgentLoop(
    provider=self.provider,
    memory_service=self.memory_manager,
    session_store=self.session_manager,
    tool_execution_service=tool_execution_service,
    ...
)
```

这里不需要改 `SessionManager` / `MemoryManager` 的继承结构，只要它们满足结构化类型即可。

同时在 Product 层初始化：

```python
tool_execution_service = ToolExecutionServiceImpl(self.tool_registry)
```

#### 5.4.2 `get_runtime_overview()` 改由 Product facade 聚合

当前 `LearningAgentSystem.get_runtime_overview()` 直接透传 Runtime 的结果。

建议改为 Product 层自行组装：

```python
def get_runtime_overview(self) -> dict[str, Any]:
    runtimes = []
    if self.agent_loop is not None:
        runtimes = self.agent_loop.list_runtime_summaries()

    return {
        "active_runtime_count": len(runtimes),
        "total_session_count": len(self.list_sessions()),
        "runtimes": runtimes,
    }
```

这样：

- Runtime 只报 runtime 数据。
- Product 层决定是否拼接 `total_session_count`。
- Web/CLI 的外部接口保持兼容。

#### 5.4.3 session 删除链路保持显式清理

当前删除逻辑：

```python
self.reset_session_runtime(session_id)
deleted = self.session_manager.delete_session(session_id)
```

这个顺序可以继续保留，不需要依赖回调。

解释：

- Product 层明确知道自己正在删除某个 session。
- 它也最适合决定是否同步清理 runtime。
- 这种显式调用比 Runtime 反向注册 Product 内部回调更符合分层。

### 5.5 `learning_agent/learning_agent/session_manager.py`

本次原则上**不必改动业务实现**。

可选改进：

- 显式写成 `class SessionManager(SessionStore):`

但这不是必要条件，因为：

- Python Protocol 支持结构化子类型
- 显式继承只是增强意图表达

如果做这一步，注意不要因此把 Runtime 端口文件 import 进过多 Product 模块中，避免形成新的命名耦合扩散。

### 5.6 `learning_agent/memory/memory_manager.py`

本次原则上**不要求深度改动**。

可选改进：

- 补一个 `relevant_recall()` 方法，作为 `MemoryService` 的最小实现。
- 如果当前仓库里已有近似方法，可以通过轻量适配器方式对齐签名。

如果当前没有任何 Runtime 调用点，则可以先让 `memory_service` 为可选，并暂时不实际使用。

---

### 5.7 修改：`learning_agent/agent/tool_validator.py`

`ToolInputValidator` 当前也不再依赖 `ToolRegistry.get()`，而是改为依赖：

```python
tool_def = self._registry.get_tool_definition(tool_call.tool_id)
```

这样参数校验仍然保留在 Runtime 内，但它依赖的是“工具定义查询能力”，不是具体注册表实现。

## 六、实施状态与后续收口

当前已落地：

- `SessionStore`
- `MemoryService`
- `ToolExecutionService`
- `LearningAgentSystem -> ToolExecutionServiceImpl -> ToolRegistry` 的 Product 装配链路
- Runtime 对 session 删除回调和全局 session 总数的直接依赖移除
- `ToolRegistry` 已物理迁移到 `learning_agent/learning_agent/`，不再位于 `agent/`

当前仍保留但已降级为 Product 内部实现细节：

- `ExtensionManager` 仍直接对 `ToolRegistry` 注册工具

后续如果继续收口，可再做两步：

1. 为 `ExtensionManager` 增加更明确的 Product 级工具注册门面，而不是直接暴露整个 `ToolRegistry`。
2. 继续清理 README/设计文档中把 `ToolRegistry` 视作 Runtime 直连依赖的旧表述。

## 七、实施顺序

建议按下面顺序落地，避免一次性改动过大：

### 阶段 1：引入端口，不改执行逻辑

改动：

- 新增 `runtime_ports.py`
- `AgentLoop` 构造参数类型切换为 `Protocol`
- `main.py` 改为注入 `session_store` / `memory_service`

验收标准：

- 现有功能行为不变
- `AgentLoopSession` 主执行链无行为回归

### 阶段 2：移除越界依赖

改动：

- 删除 `register_delete_callback()` 接线
- 收窄 `get_runtime_overview()`
- 由 `LearningAgentSystem` 负责 `total_session_count` 聚合
- 引入 `ToolExecutionService`，把具体工具执行移到 Product 层

验收标准：

- session 删除后 runtime 仍能正确清理
- `/observability/runtimes` 响应结构保持兼容

### 阶段 3：补齐文档与测试

改动：

- 更新架构设计文档
- 增加 / 调整针对 Protocol 边界的测试
- 明确 Runtime 多 session 安全保证
- 清理 Runtime 中的旧注释、重复执行实现与无用依赖

验收标准：

- 文档与实现对齐
- 测试覆盖新的边界约束

---

## 八、测试方案

### 8.1 必测回归点

1. 单 session 正常对话

- 用户输入后，消息仍能写入 session 树
- assistant 输出仍能落库
- 状态快照仍能从 `get_message_history()` 构建

2. 多 session 并发隔离

- session A 和 session B 可以并发请求
- 同一 session 仍通过 `asyncio.Lock` 串行
- runtime 摘要仍按 `session_id` 隔离

3. session 删除与 runtime 清理

- 删除 session 前显式调用 `reset_session_runtime()`
- runtime 被移除
- 不再依赖 `register_delete_callback()`

4. runtime overview

- Product 层返回：
  - `active_runtime_count`
  - `total_session_count`
  - `runtimes`
- Runtime 层内部不再需要访问 `list_sessions()`

5. 工具执行协议化

- `AgentLoop` 不直接依赖 `ToolRegistry`
- `ToolExecutionServiceImpl` 可以透明代理现有 `ToolRegistry`
- 参数校验、tool schema 构造、工具执行重试链仍保持成立

### 8.2 建议新增测试

#### 测试 1：`AgentLoop` 只依赖最小 `SessionStore`

思路：

- 构造一个 stub，只实现：
  - `append_message()`
  - `get_message_history()`
- 用它初始化 `AgentLoop`
- 验证 `AgentLoop` 不要求 `list_sessions()` / `delete_session()` / `register_delete_callback()`

价值：

- 这是依赖倒置是否真正成立的核心验证。

#### 测试 2：`LearningAgentSystem.get_runtime_overview()` 负责聚合

思路：

- mock `agent_loop.list_runtime_summaries()`
- mock `list_sessions()`
- 验证最终返回值由 Product 层统一组装

价值：

- 防止后续有人再把 `total_session_count` 搬回 Runtime。

#### 测试 3：session 删除时无回调也能清理 runtime

思路：

- 创建 session
- 运行一次 turn，让 runtime 建立
- 调用 `delete_session()`
- 验证 runtime 已被清理

价值：

- 证明“显式 Product 编排”已经替代“Runtime 反向订阅回调”。

#### 测试 4：`AgentLoop` 只依赖最小 `ToolExecutionService`

思路：

- 构造一个 stub，只实现：
  - `execute_tool_call()`
  - `get_tool_definition()`
  - `list_tools()`
- 用它初始化 `AgentLoop`
- 验证 Runtime 可以正常创建，并保持 overview 稳定

价值：

- 证明具体工具执行逻辑已经离开 Runtime 的类型边界。

---

## 九、兼容性与风险

### 9.1 兼容性收益

- Web 和 CLI 外部接口可以保持不变。
- `AgentLoopSession` 主执行逻辑不需要重写。
- 自愈、流式输出、工具补偿、状态快照机制都可以原样保留。
- 现有 `ToolRegistry` 及扩展注册逻辑可以继续工作。

### 9.2 风险点

#### 风险 1：隐藏调用点仍依赖 `SessionManager` 额外方法

说明：

- 如果 `agent_loop.py` 中还有未识别的 `list_sessions()` / `get_session()` 调用，Protocol 收窄后会暴露出来。

缓解：

- 先全量 grep Runtime 文件中的 `self.sessions.` 调用点
- 只保留执行 turn 所需最小方法

#### 风险 2：overview 接口响应结构变化引起前端回归

说明：

- 如果直接删掉 Runtime 的 `get_runtime_overview()`，调用方可能受影响。

缓解：

- 第一阶段保留方法名，只把聚合逻辑迁回 Product 层
- Web 层继续走 `system.get_runtime_overview()`

#### 风险 3：Memory Protocol 先定义但短期未使用

说明：

- 会出现“接口存在，但主链尚未实际调用”的状态。

缓解：

- 明确将其标注为预留端口
- 不在本次文档中夸大 Memory 已接线程度

#### 风险 4：工具定义读取与工具执行接口不同步

说明：

- 如果 `ToolExecutionService` 只暴露执行能力，不暴露工具定义查询和工具列表，Runtime 中的参数校验与工具 schema 构造会失配。

缓解：

- 将 `get_tool_definition()` 与 `list_tools()` 一并纳入端口定义。
- 让 Product 层实现统一代理这三类能力，而不是只代理 `execute()`。

---

## 十、为什么不采用更激进方案

一个更彻底的方向是：

- Runtime 不再持有任何 session 存取能力
- `run_turn()` 接收完整上下文快照
- 执行结果返回给 Product 层
- Product 层决定如何持久化消息

这个方向理论上更纯净，但当前不适合本项目现状，原因有三：

1. `AgentLoopSession` 已经较重，且稳定性策略深嵌在执行过程中。
2. 执行过程中需要实时 `append_message()`，不是只在末尾统一提交。
3. 一次性改成纯函数式会同时触发执行链、观测链、补偿链的大规模重构。

因此，本次采取的策略是：

> **先用最小 Protocol 收紧边界，纠正依赖方向；保留 Runtime 当前的执行内核。**

这符合项目当前“稳定优先、先收边界再逐步演进”的策略。

---

## 十一、最终建议

本次重构的推荐落地结论如下：

1. 在 Runtime 层新增 `SessionStore` / `MemoryService` / `ToolExecutionService` Protocol。
2. 将 `AgentLoop` 对 `SessionManager` / `MemoryManager` / 工具执行实现 的具体类依赖改为 Protocol 依赖。
3. `SessionStore` 只保留：
   - `append_message()`
   - `get_message_history()`
4. `ToolExecutionService` 只保留：
   - `execute_tool_call()`
   - `get_tool_definition()`
   - `list_tools()`
5. 删除 Runtime 对 `register_delete_callback()` 和 `list_sessions()` 的直接依赖。
6. 由 `LearningAgentSystem` 继续承担：
   - session 生命周期管理
   - runtime 清理编排
   - runtime overview 聚合
   - Product 层工具执行服务装配
7. `MemoryService` 先作为预留端口存在，不要求本次深度接线。

一句话概括：

> **Runtime 只保留 ReACT 调度能力，Product 继续掌握编排权与具体工具执行；用最小 Protocol 完成依赖倒置，而不是重写整个 Runtime。**
