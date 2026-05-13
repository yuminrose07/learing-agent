# 实施记录：AgentLoopSession 架构重构

> **依据文档**：`docs/design/design-agent-loop-session-refactor.md`
> **实施日期**：2026-05-12

---

## 一、功能概述

本次重构解决了 `AgentLoop` 作为全局单例时，per-session 运行时状态（`ToolFailureTracker`、chat-only mode、恢复计数等）被所有 session 共享导致的三大问题：

1. **实例级状态污染**：Session A 的工具连续失败 → `_chat_only_mode = True` → Session B 也无法使用工具
2. **并发控制缺失**：同 session 的多次调用无串行化机制，可能导致状态竞争
3. **Span 泄漏**：session 删除后，其运行时状态无精确回收机制

重构方案参考 pi-mono 的"per-session 实例隔离"哲学，将 ReACT 循环逻辑和 per-session 状态下沉到新的 `AgentLoopSession` 类，`AgentLoop` 退化为工厂 + 路由 + 共享依赖持有者。

---

## 二、修改文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `learning_agent/agent/agent_loop.py` | 重构 | 新增 `AgentLoopSession` 类；`AgentLoop` 变为工厂/路由模式；原有方法迁移至 `AgentLoopSession` |
| `learning_agent/agent/__init__.py` | 修改 | 导出 `AgentLoopSession` |
| `learning_agent/session/session_manager.py` | 修改 | 新增 `delete_session()`、`register_delete_callback()`；删除时回调清理运行时 |
| `tests/test_tool_execution_reliability.py` | 修改 + 新增 | 适配新 API（`runtime._execute_tool_calls` 等）；新增 7 个状态隔离/并发/生命周期测试 |
| `docs/implementation-summary-agent-loop-session-refactor.md` | 新增 | 本文档 |

---

## 三、关键决策

### 3.1 Property 快捷访问 vs 机械替换

`AgentLoopSession` 需要访问大量共享依赖（`provider`, `events`, `tools`, `_resilience_config` 等）。

**决策**：在 `AgentLoopSession` 中定义 property 作为共享依赖的快捷访问，而非在 1000+ 行代码中机械替换 `self.xxx` → `self.agent_loop.xxx`。

- **收益**：减少代码改动量约 80%，极大降低迁移出错概率
- **代价**：增加一层间接调用（property getter），性能影响可忽略

### 3.2 `asyncio.Lock` 而非 `activeRun` Promise 守卫

pi-mono 使用 `activeRun`（Promise 未完成时抛错拒绝并发调用）。

**决策**：采用 `asyncio.Lock` 实现同 session 串行化、不同 session 并行化。

- **原因**：Web 场景下用户快速双击应该排队而非报错；锁模式允许未来扩展为消息队列

### 3.3 TTL 过期机制

**决策**：新增 `_session_runtime_ttl`（默认 3600 秒），每次 `_get_or_create_runtime` 时自动清理过期实例。

- **原因**：防止长时间运行的 Web 服务中，用户创建 session 后不再使用但运行时一直驻留内存
- **行为**：被清理的运行时下次聊天时会自动重建，状态重置（这是预期行为）

### 3.4 SessionManager 回调解耦

`SessionManager` 原本不持有 `AgentLoop` 引用。为了在不引入循环依赖的前提下实现删除回调：

**决策**：给 `SessionManager` 添加 `_on_delete_callbacks: list[Callable[[str], None]]` 和 `register_delete_callback()` 方法。`AgentLoop` 在 `__init__` 中自动注册 `self.clear_session_runtime` 为回调。

---

## 四、验证状态

### 4.1 单元测试

全部 **28 个测试通过**，覆盖：

| 测试类 | 测试数 | 覆盖内容 |
|--------|--------|---------|
| `TestToolFailureTracker` | 6 | 失败记录/成功清零、仅 execution_error 计入 ban、窗口过期、获取全部失败类型 |
| `TestToolRegistry` | 4 | 异步 handler、同步 handler 包装、超时抛错 |
| `TestAgentLoopRetryableError` | 3 | 可重试异常类型、关键字匹配、非可重试错误 |
| `TestAgentLoopExecuteToolCalls` | 6 | 场景 A/B/C/C'/F：重试成功、validation 不计 ban、hook_abort 不计 ban、execution_error 触发 ban、重试耗尽 |
| `TestAgentLoopChatOnlyRecovery` | 2 | 场景 D：3 轮无工具自动恢复、有工具请求不恢复 |
| `TestAgentLoopSessionIsolation` | 7 | **新增**：状态隔离、同 session 并发串行化、不同 session 并发并行化、delete 回调清理、手动清理、清理全部、TTL 过期清理 |

### 4.2 状态隔离验证

```python
# Session A 进入 chat-only mode，Session B 不受影响
runtime_a = agent_loop._get_or_create_runtime("sess-a")
runtime_a._chat_only_mode = True
runtime_b = agent_loop._get_or_create_runtime("sess-b")
assert runtime_b._chat_only_mode is False  # ✓ 通过
```

### 4.3 并发安全验证

```python
# 同 session 并发：串行化（结果不交错）
await asyncio.gather(run1(), run2())  # ✓ 通过

# 不同 session 并发：并行化（总耗时 < 0.55s）
await asyncio.gather(loop.run(sess_a, "msgA"), loop.run(sess_b, "msgB"))  # ✓ 通过
```

### 4.4 生命周期验证

```python
# 删除 session → 运行时自动清理
sm.delete_session(session.id)
assert session.id not in agent_loop._session_runtimes  # ✓ 通过
```

---

## 五、已知限制与待办事项

| 事项 | 说明 | 优先级 |
|------|------|--------|
| `web_server.py` 集成 | 设计文档提到需在 FastAPI lifespan 中调用 `agent_loop.clear_all_runtimes()`，但当前项目中未发现 `web_server.py` | P1 |
| `DELETE /sessions` 端点 | 需确认 Web 层是否已实现并调用 `SessionManager.delete_session()` | P1 |
| 纯函数循环抽离 | 设计文档提到未来可进一步将 `run_turn()` 核心逻辑抽离为纯函数（类似 pi-mono `agent-loop.ts`），当前未实施 | P2 |
| 消息队列扩展 | 当前暂无 steering/followUp 消息注入需求，未来可扩展 `PendingMessageQueue` | P2 |
