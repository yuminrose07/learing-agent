# 扩展接线审计表计划

## Summary

- 目标：产出一份 `docs/audit/extension-wiring-audit-checklist-2026-05-13.md`，系统审计仓库内 `Hook / Event / Extension / Trigger / Consumer / Test` 的真实接线状态。
- 结果形态：以 Markdown 审计文档为主，包含总览说明、状态定义、逐项接线表、发现的问题与缺口、建议补测项。
- 审计原则：只依据当前仓库可验证事实，不写推测性接线；所有条目都要能回链到真实文件。

## Current State Analysis

### 已确认的核心接线入口

- 扩展统一注册与激活位于 `learning_agent/main.py`：
  - `create_builtin_extensions()` 返回内置扩展列表。
  - `ExtensionManager.register()` 注册扩展。
  - `ExtensionManager.activate_all()` 激活扩展。
  - 系统级事件订阅包含：
    - `* -> observability.on_event`
    - `agent.stateSnapshot -> LearningAgentSystem._on_state_snapshot`
- 扩展模型与注册接口位于 `learning_agent/core/extension_manager.py`：
  - `ExtensionContext.register_hook()`
  - `ExtensionContext.subscribe_event()`
  - `ExtensionContext.register_tool()`
- Hook 定义与执行位于 `learning_agent/models/models.py`、`learning_agent/core/hook_system.py`：
  - 已定义 HookPoint 共 13 个。
  - 当前运行时明确触发的 HookPoint 主要集中在 `AgentLoop`。
- Event 定义与总线位于 `learning_agent/models/models.py`、`learning_agent/core/event_bus.py`。
- 运行时主要触发器位于 `learning_agent/agent/agent_loop.py`，已确认发布的事件包括：
  - `agent.stateChanged`
  - `agent.stateSnapshot`
  - `agent.llmCalled`
  - `agent.responseChunk`
  - `agent.streamInterrupted`
  - `agent.chatOnlyRecovered`
  - `agent.traceCompleted`
  - `agent.unhandledError`
  - `agent.turnRetry`
  - `agent.toolBanned`
  - `agent.toolValidationFailed`
  - `agent.toolPermissionAsk`
  - `agent.toolPermissionDenied`
  - `agent.toolCalled`
  - `agent.toolResult`
  - `agent.toolRetry`
  - `agent.orphanToolCallsCompensated`
- 非 `AgentLoop` 事件发布点还包括：
  - `session.created` in `learning_agent/main.py`
  - `knowledge.confirmed` in `learning_agent/main.py` 与 `learning_agent/web_server.py`
  - `extension.activated` / `extension.deactivated` in `learning_agent/core/extension_manager.py`
  - `agent.toolResultsValidated` in `learning_agent/extensions/built_in.py`

### 已确认的扩展清单

- `core-observability`
- `core-fulltrace`
- `core-output-prompting`
- `core-knowledge-extraction`
- `core-review`
- `core-material-text`
- `code-tools`
- `builtin-context-compressor`
- `core-tool-guard`
- `core-security-audit`
- `core-tool-results-validator`

### 已确认的扩展接线形态

- Hook 注册存在于：
  - `learning_agent/extensions/built_in.py`
  - `learning_agent/extensions/context_compressor.py`
  - `learning_agent/extensions/tool_guard.py`
- Event 订阅存在于：
  - `learning_agent/extensions/built_in.py`
  - `learning_agent/extensions/security_audit.py`
  - `learning_agent/main.py`
- Tool 注册存在于：
  - `learning_agent/extensions/built_in.py`
  - `learning_agent/extensions/code_tools.py`
- 当前仓库中没有独立的 `Trigger` 类或 `Consumer` 类；
  - `Trigger` 需要按“谁触发 Hook / 谁发布 Event”来审计。
  - `Consumer` 需要按“谁订阅 Event / 谁执行 Hook handler / 谁消费 Tool 结果”来审计。

### 已确认的测试现状

- `tests/test_tool_execution_reliability.py` 已覆盖：
  - `HookSystem`
  - `EventBus`
  - `ToolRegistry`
  - `AgentLoop._execute_tool_calls()`
  - Chat-only 恢复
  - Session 运行时隔离
- `tests/test_web_adaptation.py` 已覆盖：
  - Web 侧 session/runtime 生命周期
  - runtime observability API
- 目前尚未看到“按扩展逐项验证注册成功”或“按事件-消费者矩阵逐项验证”的专门审计测试。

## Assumptions & Decisions

- 本次交付物以文档审计为主，不修改运行时代码。
- 审计文档目标路径采用现有建议命名：`docs/audit/extension-wiring-audit-checklist-2026-05-13.md`。
- 文档语言使用中文，与当前请求一致。
- 审计对象按“实际存在且当前仓库可验证”收口，不引入未来规划项。
- `Trigger` 与 `Consumer` 采用语义映射，而非要求代码中必须存在同名抽象：
  - `Trigger`：实际触发 Hook 或发布 Event 的代码点。
  - `Consumer`：Hook handler、Event subscriber、或显式消费某类运行时结果的处理方。
- 状态字段统一采用四档，避免模糊表达：
  - `已接线`：定义、注册/触发、消费三者至少在当前路径上闭环成立。
  - `部分接线`：存在定义或注册，但缺少明显触发、消费者、或测试覆盖。
  - `未接线`：有定义但当前仓库未发现运行时接入。
  - `未定义`：该维度在仓库中不存在独立概念，只能通过语义映射表示。
- `Test 状态` 单独判定，不与功能接线状态混淆：
  - `已覆盖`：存在直接或高相关测试。
  - `间接覆盖`：路径被更高层测试覆盖，但无逐项断言。
  - `未覆盖`：未发现对应测试。

## Proposed Changes

### 1. 新建审计文档

- 新建 `docs/audit/extension-wiring-audit-checklist-2026-05-13.md`
- 内容结构固定如下：
  - 文档目标与适用范围
  - 状态判定标准
  - 审计范围与证据来源
  - 总览结论
  - 逐项审计表
  - 缺口与风险
  - 建议补测项

### 2. 在文档中建立 4 张主表

- 表 1：`Extension 总表`
  - 列：`Extension` | `定义文件` | `Hook` | `Event 订阅` | `Tool 注册` | `Trigger` | `Consumer` | `Test 状态` | `总体状态` | `备注`
  - 行：逐个扩展列出 11 个已确认 extension id。
  - 目的：从扩展视角做总览，回答“每个扩展到底接了什么”。

- 表 2：`Hook 接线表`
  - 列：`Hook` | `定义位置` | `触发位置` | `注册扩展` | `主要 Consumer` | `Test 状态` | `接线状态` | `备注`
  - 行：
    - 所有 `HookPoint` 枚举项都要列出。
    - 对未被 `AgentLoop` 或其他运行时触发的 Hook 标记为 `未接线` 或 `部分接线`。
  - 目的：回答“哪些 Hook 只是定义了，哪些真的进主流程”。

- 表 3：`Event 接线表`
  - 列：`Event` | `发布 Trigger` | `发布文件` | `Consumer` | `Test 状态` | `接线状态` | `备注`
  - 行：
    - 列出当前已确认的所有运行时事件类型。
    - 至少覆盖 `AgentLoop`、`main.py`、`web_server.py`、`extension_manager.py`、`built_in.py` 中的发布事件。
  - 目的：回答“事件从哪发、谁在收、是否有孤儿事件”。

- 表 4：`Test 映射表`
  - 列：`对象` | `对象类型` | `对应测试文件` | `覆盖方式` | `覆盖状态` | `缺口说明`
  - 行：
    - 核心对象包括关键扩展、关键 Hook、关键 Event、关键 runtime 行为。
  - 目的：把功能接线和测试接线分离，清楚标注缺口。

### 3. 文档内明确状态判定方法

- 对每个维度写清楚“如何判断状态”：
  - Hook：看 `HookPoint` 定义、`register_hook()`、`HookSystem.execute()` 触发点、测试覆盖。
  - Event：看 `Event(type=...)` 发布点、`subscribe()`/`subscribe_event()` 订阅点、测试覆盖。
  - Extension：看 `Extension(...)` 定义、`create_builtin_extensions()` 是否纳入主注册链、是否在激活时注册能力。
  - Trigger：看 `AgentLoop`、`main.py`、`web_server.py`、`built_in.py`、`extension_manager.py` 中的主动触发点。
  - Consumer：看 Hook handler、Event subscriber、状态持久化回调、审计/观测处理器。
  - Test：看 `tests/` 中是否存在直接断言或高相关路径覆盖。

### 4. 逐项盘点时必须覆盖的具体文件

- 运行时主链路：
  - `learning_agent/main.py`
  - `learning_agent/agent/agent_loop.py`
  - `learning_agent/web_server.py`
- 核心系统：
  - `learning_agent/models/models.py`
  - `learning_agent/core/extension_manager.py`
  - `learning_agent/core/hook_system.py`
  - `learning_agent/core/event_bus.py`
  - `learning_agent/core/observability.py`
- 扩展实现：
  - `learning_agent/extensions/built_in.py`
  - `learning_agent/extensions/code_tools.py`
  - `learning_agent/extensions/context_compressor.py`
  - `learning_agent/extensions/tool_guard.py`
  - `learning_agent/extensions/security_audit.py`
- 测试：
  - `tests/test_tool_execution_reliability.py`
  - `tests/test_web_adaptation.py`

### 5. 文档里要输出的关键审计结论

- 至少明确以下判断：
  - 哪些 HookPoint 已定义但未进入主循环。
  - 哪些 Event 已发布但消费者单薄或缺失。
  - 哪些 Extension 已在主注册链中，但只有工具注册、没有 Hook/Event 接线。
  - 哪些 Consumer 只是日志型消费，哪些会影响状态或流程。
  - 哪些关键路径只有间接测试，没有逐项断言。
  - 是否存在“文档宣称的能力”和“真实接线状态”不一致之处。

### 6. 如有必要，顺带更新现有诊断文档的引用

- 评估是否需要在 `docs/audit/technical-lead-diagnostic-checklist-2026-05-13.md` 中：
  - 保留现有“建议补文档”条目不动；
  - 或补一行“已产出文档路径”。
- 默认策略：除非执行时发现确有必要，否则不改该文件，避免扩大改动面。

## Implementation Steps

1. 基于已确认文件，先整理扩展清单、HookPoint 清单、事件清单。
2. 为每个对象补齐三类证据：
   - 定义位置
   - 触发/注册/订阅位置
   - 测试位置
3. 先写文档头部：
   - 目标
   - 范围
   - 状态定义
   - 证据来源
4. 再写 4 张主表，先全量列项，再逐行填状态。
5. 补充“缺口与风险”段落，按 `P0 / P1 / P2` 归类：
   - `P0`：主流程声明与接线不一致
   - `P1`：接线存在但消费者或测试不足
   - `P2`：命名、文档、审计可读性问题
6. 最后补“建议补测项”，只写高价值补测，不机械铺满。

## Verification

- 文档自检
  - 确认所有表中引用的对象都能在仓库中找到对应代码。
  - 确认未把“推测关系”写成“已接线事实”。
  - 确认 `Trigger`、`Consumer` 的语义映射有说明，不会误导为框架内建类型。

- 覆盖性检查
  - Extension 表应覆盖 11 个当前已注册内置扩展。
  - Hook 表应覆盖 `HookPoint` 枚举中的全部项。
  - Event 表应覆盖当前确认的运行时发布事件。
  - Test 表应至少覆盖两份测试文件里的关键对象映射。

- 一致性检查
  - 文档结论要与 `learning_agent/main.py` 的注册链保持一致。
  - 文档中“已接线/部分接线/未接线”的判定要与证据一致。
  - 文档中“建议下一步”要与现有 `docs/audit/technical-lead-diagnostic-checklist-2026-05-13.md` 的治理方向一致。
