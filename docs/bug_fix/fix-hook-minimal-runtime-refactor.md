# 修复：Hook 最小运行时收敛与 typed 契约重构

> 依据技术文档：`docs/design/design-hook-refactor-minimal-runtime.md`
> 日期：2026-05-13

---

## 一、功能概述

本次修复针对当前 Hook 机制“定义分散、接线不稳、契约泛化、旧点位残留过多”的问题，完成了最小运行时 Hook 重构，目标是让 Agent 主链在不依赖旧泛型 Hook 的前提下，仍能稳定完成以下能力：

1. **稳定流式输出**：Web/前端可以持续接收流式内容，`on_stream_chunk` 只做轻量修饰与观测，不再承载核心控制语义。
2. **稳定工具执行链路**：工具调用前后的权限、安全、审计和展示修正通过 typed Hook 接入，核心正确性仍由 `AgentLoop` 决定。
3. **清晰的 Core / Hook 边界**：Core 保留状态机、重试、持久化、事件发布、校验等裁决权；Hook 只提供标准切点和结构化返回。
4. **可测试的 typed 契约**：旧的 `data: Any + context: dict + HookResult` 被替换为明确的输入输出模型，便于静态分析和单元测试。

---

## 二、问题定义

### 2.1 旧问题

改造前的 Hook 体系存在以下结构性问题：

- Hook 点过多，但真正接入 `AgentLoop` 主链的点位有限，存在“已定义但未接通”的情况。
- `HookResult` 同时承载 `modified / abort / ask / data` 多种语义，不同阶段共享同一结果结构，边界不清晰。
- `AgentLoop` 仍依赖旧 `HookPoint` 与 `hooks.execute()` 泛型接口，主链语义分散在多个旧点位中。
- 工具调用前后、流式 chunk、响应收尾等阶段都依赖宽泛 `dict`/`Any` 传参，缺少 typed 契约。
- 某些扩展继续挂在旧 Hook 上，一旦主链切换，容易出现“扩展已注册但没有生效”。

### 2.2 直接风险

这些问题会带来三个直接风险：

- **运行时不稳定**：旧点位和新主链并存时，容易出现接线断裂或逻辑重复。
- **权限/审计失焦**：工具前后的策略判断和结果增强缺少固定契约，扩展行为难以推断。
- **测试困难**：泛型 `HookResult` 很难精确描述各阶段允许的返回值，导致测试粒度不清晰。

---

## 三、修复策略

### 3.1 收敛为 5 个稳定 Hook

本次运行时只保留以下 5 个 Hook：

1. `before_agent_run`
2. `before_tool_execute`
3. `after_tool_execute`
4. `on_stream_chunk`
5. `after_response`

### 3.2 引入 typed 输入输出模型

在 `learning_agent/models/models.py` 中新增以下模型：

- `HookName`
- `HookDecision`
- `HookWarning`
- `HookAuditRecord`
- `HookContext`
- `BeforeAgentRunInput / Result`
- `BeforeToolExecuteInput / Result`
- `AfterToolExecuteInput / Result`
- `OnStreamChunkInput / Result`
- `AfterResponseInput / Result`

同时移除运行时对以下旧模型的主链依赖：

- `HookPoint`
- `HookResult`
- `HookAbortError`

### 3.3 重构 `HookSystem`

`learning_agent/core/hook_system.py` 从统一的泛型执行器重构为 typed dispatcher：

- 删除旧 `execute(point, data, context)` 主接口
- 新增五个 typed `run_*` 方法
- 固定不同 Hook 的多 handler merge 规则
- handler 抛异常时默认记录日志并继续，不中断 Core 主链
- 不再允许通过抛异常表达 `ask / deny`

### 3.4 改造 `AgentLoop` 主链接线

`learning_agent/agent/agent_loop.py` 中完成以下替换：

- 在 `run_turn()` 开头接入 `before_agent_run`
- 流式处理时用 `run_on_stream_chunk()` 替换旧 `ON_STREAM_CHUNK`
- assistant 响应落库后接入 `run_after_response()`
- `_execute_tool_calls()` 中接入：
  - `before_tool_execute`
  - `after_tool_execute`
- 删除旧 `ON_TOOL_CALL`
- 删除旧 `AFTER_TOOL_RESULT`
- 删除旧 `BEFORE_TOOL_RESULTS_PERSIST`
- 删除主链对 `BEFORE_CONTEXT_BUILD` 的依赖

### 3.5 迁移关键扩展

- `core-tool-guard`：迁移到 `before_tool_execute`
- `core-fulltrace`：迁移到 `before_agent_run` / `on_stream_chunk` / `after_tool_execute` / `after_response`
- `core-observability`：保留事件消费者，并在允许的轻量 Hook 上做补充

默认最小运行时中，不再继续接入旧 Hook 依赖的扩展：

- `builtin-context-compressor`
- `core-tool-results-validator`
- 其他不属于本轮最小运行时目标的旧链路扩展

---

## 四、修改文件清单

| # | 文件 | 改动说明 |
|---|------|---------|
| 1 | `learning_agent/models/models.py` | 新增 5 个 Hook 的 typed 契约模型与公共枚举/上下文模型 |
| 2 | `learning_agent/models/__init__.py` | 导出新的 Hook typed 模型 |
| 3 | `learning_agent/core/hook_system.py` | 重构为 typed dispatcher，新增五个 `run_*` 方法与固定 merge 规则 |
| 4 | `learning_agent/core/extension_manager.py` | `register_hook()` 改为接收 `HookName` |
| 5 | `learning_agent/core/__init__.py` | 删除对旧 `HookAbortError` 的导出 |
| 6 | `learning_agent/agent/agent_loop.py` | 接入 `before_agent_run` / `before_tool_execute` / `after_tool_execute` / `on_stream_chunk` / `after_response`，删除旧 Hook 主链接线 |
| 7 | `learning_agent/extensions/tool_guard.py` | 迁移到 `before_tool_execute`，统一返回 `BeforeToolExecuteResult` |
| 8 | `learning_agent/extensions/built_in.py` | 收敛默认内置扩展清单，迁移 `core-fulltrace` 和观测类扩展到新 Hook |
| 9 | `tests/test_tool_execution_reliability.py` | 更新集成测试到新 Hook 契约，并补充工具前后 Hook 场景 |
| 10 | `tests/test_hook_system.py` | 新增 HookSystem 单测，覆盖合并规则、优先级、异常容错和链式 override |

---

## 五、关键决策

### 决策 1：只保留 5 个 Hook

本次不是为了“让 Hook 更灵活”，而是为了让运行时最小集合稳定可控。将 Hook 数量收敛到 5 个，可以保证：

- 核心链路足够少，易于接线审计
- 扩展迁移目标明确
- 测试覆盖边界清楚

### 决策 2：Core 保留裁决权，Hook 不驱动状态机

Hook 结果只作为 `AgentLoop` 的输入，而不是直接改变状态机。以下能力仍保留在 Core：

- 工具存在性检查
- 参数校验
- retry 判定
- failure tracker 计数
- banned 判定
- session append / persistence
- event publish
- state transition

### 决策 3：阻断语义只允许出现在前置 Hook

只有：

- `before_agent_run`
- `before_tool_execute`

允许返回 `ASK / DENY`。

这样可以避免后置阶段误改主链结果，保证：

- `after_tool_execute` 不能改变 `success`
- `on_stream_chunk` 不能驱动工具执行
- `after_response` 不能反向改写当前轮状态机

### 决策 4：默认冻结非最小运行时扩展

像上下文压缩、知识提取、tool results validator 这类能力，不属于本轮最小运行时必须目标。默认从关键主链中弱化或移出，是为了先保证：

- 主链独立稳定运行
- 扩展接回时有明确的新契约基础

---

## 六、验证状态

### 6.1 单元与集成测试

已通过以下测试：

```bash
pytest tests/test_hook_system.py tests/test_tool_execution_reliability.py -q
```

结果：

```text
39 passed
```

### 6.2 覆盖场景

本次验证覆盖了以下关键场景：

- [x] Hook handler 按 priority 顺序执行
- [x] `before_tool_execute` 中 `DENY > ASK > CONTINUE` 合并规则
- [x] `patched_arguments` merge 规则
- [x] `warnings` / `audit_records` 聚合
- [x] handler 抛异常时不中断后续 handler
- [x] `on_stream_chunk` 的链式 `content_override`
- [x] `before_agent_run` 返回 `DENY`
- [x] `before_agent_run` 返回 `ASK`
- [x] `before_tool_execute` 参数 patch 后工具正常执行
- [x] `after_tool_execute` 修改展示文本
- [x] `after_response` 写入额外 metadata

---

## 七、已知限制与待办

1. **旧文件仍保留但未接入主链**：`context_compressor.py` 等旧扩展文件仍存在于仓库中，但已不属于默认最小运行时链路。
2. **旧模型残留待清理**：`ToolResultsBatch` 等历史模型文件级定义仍在，虽然主链已不再使用，后续可以继续做代码清理。
3. **文档与实现已对齐，但仓库还可继续收口**：如果后续继续推进，可删除更多旧 Hook 注释、旧扩展说明和无用兼容痕迹。

---

## 八、结论

本次修复完成后，运行时 Hook 系统已经从“旧泛型点位 + 宽泛结果结构”切换为“5 个稳定切点 + typed 契约 + Core 明确裁决边界”。

直接收益包括：

- `AgentLoop` 主链接线更清晰
- 工具执行前后策略更可控
- 流式输出更稳定
- Hook 系统更容易测试和扩展

这为后续重新接入上下文压缩、知识提取、记忆管理等上层能力，提供了更稳定的最小运行时底座。
