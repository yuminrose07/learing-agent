# 扩展接线审计表

## 1. 目标与适用范围

- 目标：审计当前仓库中 `Hook / Event / Extension / Trigger / Consumer / Test` 的实际接线状态。
- 范围：仅基于当前代码仓库可验证事实，不纳入未来规划或口头设计。
- 适用对象：扩展系统、事件总线、主循环触发点、观测与审计链路、现有测试覆盖。

## 2. 状态判定标准

### 2.1 功能状态

- `已接线`：定义、注册或发布、运行时触发、消费者闭环成立。
- `部分接线`：存在定义或注册，但缺触发、缺消费者、或只连到局部窄路径。
- `未接线`：已有定义，但当前仓库没有发现运行时接入。
- `未定义`：仓库中没有独立抽象，只能按语义映射审计。

### 2.2 测试状态

- `已覆盖`：存在直接断言，明确验证该对象或事件。
- `间接覆盖`：相关主路径会经过该对象，但没有逐项断言。
- `未覆盖`：未发现对应测试。

### 2.3 语义映射说明

- `Trigger`：谁在代码里主动触发 Hook 或发布 Event。
- `Consumer`：谁消费 Hook、订阅 Event、或显式处理对应运行时结果。
- 当前仓库没有独立的 `Trigger` 类或 `Consumer` 类，因此本表按运行时语义盘点。

## 3. 审计范围与证据来源

### 3.1 主链路文件

- `learning_agent/main.py`
- `learning_agent/agent/agent_loop.py`
- `learning_agent/web_server.py`

### 3.2 核心系统文件

- `learning_agent/models/models.py`
- `learning_agent/core/extension_manager.py`
- `learning_agent/core/hook_system.py`
- `learning_agent/core/event_bus.py`
- `learning_agent/core/observability.py`

### 3.3 扩展实现文件

- `learning_agent/extensions/built_in.py`
- `learning_agent/extensions/code_tools.py`
- `learning_agent/extensions/context_compressor.py`
- `learning_agent/extensions/tool_guard.py`
- `learning_agent/extensions/security_audit.py`

### 3.4 测试文件

- `tests/test_tool_execution_reliability.py`
- `tests/test_web_adaptation.py`

## 4. 总览结论

- 当前主注册链中共有 11 个扩展，实际进入系统初始化流程。
- 13 个 `HookPoint` 中，当前只有 7 个在运行时被显式触发，另外 6 个仍停留在预留定义层。
- 事件总线已形成主闭环，但存在“消费者已注册、事件未发布”的断链点。
- 最明显的断链是 `core-knowledge-extraction` 订阅 `agent.responseDone`，而当前主循环并未发布该事件。
- 观测系统内还存在对 `agent.contextCompressed`、`agent.traceError`、`agent.responseDone` 的处理分支，但当前仓库未看到对应发布点。
- 测试重点集中在 `AgentLoop` 的韧性与运行时清理；扩展级接线表、事件-消费者矩阵、扩展激活后注册结果目前没有专门测试。

## 5. Extension 总表

| Extension | 定义文件 | Hook | Event 订阅 | Tool 注册 | Trigger | Consumer | Test 状态 | 总体状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `core-tool-results-validator` | `learning_agent/extensions/built_in.py` | `agent.beforeToolResultsPersist` | 无 | 无 | `AgentLoop` 在写入 tool results 前触发 | `_hook_validate_tool_results` | `未覆盖` | `已接线` | 只在 tool batch 不一致时发 `agent.toolResultsValidated` |
| `core-observability` | `learning_agent/extensions/built_in.py` | `agent.beforeLLMCall`、`agent.afterResponse` | `*` | 无 | `AgentLoop` Hook 触发 + EventBus 全量广播 | `_hook_before_llm`、`_hook_after_response`、`_on_any_event` | `间接覆盖` | `已接线` | 主要是 debug 级消费，核心观测仍在 `ObservabilityCollector` |
| `core-output-prompting` | `learning_agent/extensions/built_in.py` | `agent.afterResponse` | 无 | 无 | `AgentLoop` 响应后触发 | `_hook_output_prompting` | `未覆盖` | `已接线` | 对输出内容做追加提示 |
| `core-knowledge-extraction` | `learning_agent/extensions/built_in.py` | 无 | `agent.responseDone` | `create_knowledge_node` | Tool 由 LLM 触发；事件订阅目前无发布源 | `_on_response_done`、`_tool_create_knowledge_node` | `未覆盖` | `部分接线` | 订阅事件名与当前主循环不一致，是本次最明显断链 |
| `core-review` | `learning_agent/extensions/built_in.py` | 无 | `knowledge.confirmed` | `schedule_review` | `main.py` 与 `web_server.py` 发布 `knowledge.confirmed`；tool 由 LLM 触发 | `_on_knowledge_confirmed`、`_tool_schedule_review` | `未覆盖` | `已接线` | 事件链路成立，但缺专门测试 |
| `core-material-text` | `learning_agent/extensions/built_in.py` | 无 | 无 | `read_material` | LLM tool call | `_tool_read_material` | `未覆盖` | `已接线` | 纯工具型扩展 |
| `core-fulltrace` | `learning_agent/extensions/built_in.py` | `agent.beforeLLMCall`、`agent.onStreamChunk`、`agent.afterResponse`、`agent.onToolCall`、`agent.afterToolResult` | 无 | 无 | `AgentLoop` 主循环与工具执行链 | `_fulltrace_*` hooks | `未覆盖` | `已接线` | 覆盖主链路最完整，但缺直接测试 |
| `core-code-tools` | `learning_agent/extensions/code_tools.py` | 无 | 无 | `read_file`、`write_file`、`edit_file`、`bash` | LLM tool call | 各 `_tool_*` handler | `间接覆盖` | `已接线` | 扩展实际 id 为 `core-code-tools`，不是 `code-tools` |
| `builtin-context-compressor` | `learning_agent/extensions/context_compressor.py` | `agent.beforeContextBuild` | 无 | 无 | `AgentLoop._build_context_for_turn()` | `_hook_compress_context` | `间接覆盖` | `已接线` | 主链已接入，但无“压缩生效”断言 |
| `core-tool-guard` | `learning_agent/extensions/tool_guard.py` | `agent.onToolCall` | 无 | 无 | `AgentLoop._execute_tool_calls()` | `_on_tool_call` 包装器 | `间接覆盖` | `已接线` | Hook 位置关键，但当前测试主要覆盖通用 abort 路径，不是扩展本身 |
| `core-security-audit` | `learning_agent/extensions/security_audit.py` | 无 | `agent.toolCalled`、`agent.toolResult` | 无 | `AgentLoop` 在工具前后发布事件 | `_on_tool_called`、`_on_tool_result` | `未覆盖` | `已接线` | 事件闭环成立，但无 audit.jsonl 专测 |

## 6. Hook 接线表

| Hook | 定义位置 | 触发位置 | 注册扩展 | 主要 Consumer | Test 状态 | 接线状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `agent.beforeIntentParse` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | 预留枚举值 |
| `agent.afterIntentParse` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | 预留枚举值 |
| `agent.beforeContextBuild` | `learning_agent/models/models.py` | `AgentLoop._build_context_for_turn()` | `builtin-context-compressor` | `_hook_compress_context` | `间接覆盖` | `已接线` | 当前唯一接入的上下文裁剪入口 |
| `agent.beforeLLMCall` | `learning_agent/models/models.py` | `AgentLoop.run_turn()` | `core-observability`、`core-fulltrace` | `_hook_before_llm`、`_fulltrace_hook_before_llm` | `间接覆盖` | `已接线` | 主循环明确触发 |
| `agent.onStreamChunk` | `learning_agent/models/models.py` | `AgentLoop` 流式读取 chunk 时 | `core-fulltrace` | `_fulltrace_hook_stream_chunk` | `间接覆盖` | `已接线` | 用于流式轨迹采集 |
| `agent.afterResponse` | `learning_agent/models/models.py` | `AgentLoop` 写入 assistant 响应后 | `core-observability`、`core-output-prompting`、`core-fulltrace` | 对应三个 hook handler | `间接覆盖` | `已接线` | 扩展最密集的响应后切点 |
| `agent.onToolCall` | `learning_agent/models/models.py` | `AgentLoop._execute_tool_calls()` | `core-tool-guard`、`core-fulltrace` | `_on_tool_call`、`_fulltrace_hook_tool_call` | `间接覆盖` | `已接线` | 权限控制主入口 |
| `agent.afterToolResult` | `learning_agent/models/models.py` | `AgentLoop._execute_tool_calls()` | `core-fulltrace` | `_fulltrace_hook_tool_result` | `间接覆盖` | `已接线` | 仅 fulltrace 注册 |
| `agent.beforeToolResultsPersist` | `learning_agent/models/models.py` | `AgentLoop` 写入 tool results 前 | `core-tool-results-validator` | `_hook_validate_tool_results` | `未覆盖` | `已接线` | 校验与补偿逻辑存在，但无回归测试 |
| `memory.beforeStore` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | 记忆扩展点尚未进入主链 |
| `memory.afterRecall` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | 记忆扩展点尚未进入主链 |
| `session.onFork` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | Session fork 存在能力，但未走 Hook |
| `session.onEnd` | `learning_agent/models/models.py` | 未发现 | 无 | 无 | `未覆盖` | `未接线` | Session end 未通过 Hook 暴露 |

## 7. Event 接线表

| Event | 发布 Trigger | 发布文件 | Consumer | Test 状态 | 接线状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `agent.agent_start` | `AgentLoop.run_turn()` | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 通过 `_emit_agent_event()` 动态发布 |
| `agent.agent_end` | `AgentLoop.run_turn()` | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 正常结束与 alignment 结束都会触发 |
| `agent.turn_start` | `AgentLoop` 每轮开始 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | ReACT 内层循环事件 |
| `agent.turn_end` | `AgentLoop` 每轮结束 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 正常路径与中断收尾路径都会触发 |
| `agent.message_start` | assistant 开始输出前 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 动态事件 |
| `agent.message_update` | 流式 chunk 到来时 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 与 `agent.responseChunk` 并行存在 |
| `agent.message_end` | user/assistant 消息完成时 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | user 与 assistant 都会发 |
| `agent.tool_execution_start` | 每次工具执行前 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 动态事件 |
| `agent.tool_execution_end` | 每次工具执行结束后 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 成功、ask、deny、validation、execution_error 都走这里 |
| `agent.stateChanged` | runtime 状态迁移 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 由 `_set_state()` 发射 |
| `agent.stateSnapshot` | 每轮快照保存 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`LearningAgentSystem._on_state_snapshot`、`core-observability(*)` | `间接覆盖` | `已接线` | 唯一存在系统级专属订阅者 |
| `agent.llmCalled` | 调 Provider 前 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 记录模型与消息数 |
| `agent.responseChunk` | 每个响应 chunk | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 观测层有专门 metrics 更新分支 |
| `agent.streamInterrupted` | LLM 流中断补偿后 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 没有专项回归测试 |
| `agent.chatOnlyRecovered` | chat-only 自动恢复 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `已覆盖` | `已接线` | `test_chat_only_auto_recovery` 有直接断言 |
| `agent.traceCompleted` | trace 收尾 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | trace 完结事件无专项测试 |
| `agent.unhandledError` | 兜底异常路径 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 触发路径存在，未见专项断言 |
| `agent.turnRetry` | Provider 级 turn 重试 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 当前测试更偏工具重试，不是 turn retry |
| `agent.toolBanned` | 工具被 failure tracker 禁用 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `已覆盖` | `已接线` | `test_execution_error_triggers_ban` 有直接断言 |
| `agent.toolValidationFailed` | 工具参数校验失败 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | 走了路径，但未断言事件本身 |
| `agent.toolPermissionAsk` | Hook 返回 ask | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 当前未见 ask 分支测试 |
| `agent.toolPermissionDenied` | Hook 返回 abort | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `间接覆盖` | `已接线` | `test_hook_abort_no_ban` 经过路径，但未断言事件 |
| `agent.toolCalled` | 工具真正开始执行 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)`、`core-security-audit` | `间接覆盖` | `已接线` | 安全审计依赖此事件 |
| `agent.toolResult` | 工具成功或失败后 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)`、`core-security-audit` | `间接覆盖` | `已接线` | 安全审计依赖此事件 |
| `agent.toolRetry` | 工具执行重试 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `已覆盖` | `已接线` | `test_retry_success_on_transient_error`、`test_retry_exhausted_records_failure` 有断言 |
| `agent.orphanToolCallsCompensated` | 孤儿 tool call 补偿写回 | `learning_agent/agent/agent_loop.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 重要补偿链路，无专项测试 |
| `session.created` | 新 session 创建 | `LearningAgentSystem.start_session()` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 无专门订阅者 |
| `knowledge.confirmed` | 手动确认知识节点 | `learning_agent/main.py`、`learning_agent/web_server.py` | `ObservabilityCollector.on_event`、`core-observability(*)`、`core-review` | `未覆盖` | `已接线` | review 扩展依赖此事件 |
| `extension.activated` | 扩展激活后 | `learning_agent/core/extension_manager.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 生命周期事件存在，但无回归测试 |
| `extension.deactivated` | 扩展停用后 | `learning_agent/core/extension_manager.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 生命周期事件存在，但无回归测试 |
| `agent.toolResultsValidated` | tool results 校验修正后 | `learning_agent/extensions/built_in.py` | `ObservabilityCollector.on_event`、`core-observability(*)` | `未覆盖` | `已接线` | 仅在检测到不一致时发布 |

## 8. Test 映射表

| 对象 | 对象类型 | 对应测试文件 | 覆盖方式 | 覆盖状态 | 缺口说明 |
| --- | --- | --- | --- | --- | --- |
| `HookSystem.execute()` | Hook 执行基础设施 | `tests/test_tool_execution_reliability.py` | 通过 `hook_abort_no_ban` 等路径覆盖执行与返回结果 | `间接覆盖` | 没有逐个 `HookPoint` 的表驱动测试 |
| `EventBus.publish()` | Event 基础设施 | `tests/test_tool_execution_reliability.py` | 通过事件历史断言验证 `toolRetry`、`toolBanned`、`chatOnlyRecovered` | `间接覆盖` | 没有事件矩阵级别测试 |
| `ToolRegistry.execute()` | Tool 执行基础设施 | `tests/test_tool_execution_reliability.py` | 成功、同步包装、超时均有断言 | `已覆盖` | 覆盖较完整 |
| `AgentLoop._execute_tool_calls()` | 工具执行主链 | `tests/test_tool_execution_reliability.py` | retry、validation、abort、ban 均覆盖 | `已覆盖` | AFTER_TOOL_RESULT 与 tool guard 扩展本体无专项断言 |
| `agent.toolRetry` | Event | `tests/test_tool_execution_reliability.py` | 直接检查 event history | `已覆盖` | 无 |
| `agent.toolBanned` | Event | `tests/test_tool_execution_reliability.py` | 直接检查 event history | `已覆盖` | 无 |
| `agent.chatOnlyRecovered` | Event | `tests/test_tool_execution_reliability.py` | 直接检查 event history | `已覆盖` | 无 |
| `core-tool-guard` | Extension / Hook consumer | `tests/test_tool_execution_reliability.py` | 仅通过通用 abort hook 路径间接覆盖 | `间接覆盖` | 未验证真实 guard 策略与 ask/deny 分支 |
| `builtin-context-compressor` | Extension / Hook consumer | 未发现 | 无 | `未覆盖` | 未验证压缩启用、关闭、异常回退 |
| `core-security-audit` | Extension / Event consumer | 未发现 | 无 | `未覆盖` | 未验证 `.observability/audit.jsonl` 写入 |
| `core-tool-results-validator` | Extension / Hook consumer | 未发现 | 无 | `未覆盖` | 未验证错位结果修正与 `agent.toolResultsValidated` 事件 |
| `core-knowledge-extraction` | Extension / Event consumer | 未发现 | 无 | `未覆盖` | 关键问题是当前订阅事件本身未发布 |
| `web runtime cleanup` | Runtime 生命周期 | `tests/test_web_adaptation.py`、`tests/test_tool_execution_reliability.py` | 删除 session、reset runtime、lifespan 清理都有断言 | `已覆盖` | 与扩展接线无直接关联 |

## 9. 缺口与风险

### P0

- `core-knowledge-extraction` 订阅 `agent.responseDone`，但当前主循环发布的是 `agent.message_end` 与 `agent.responseChunk`，未见 `agent.responseDone` 发布点。
- 这意味着该扩展的事件消费链默认不会被触发，属于“系统声明存在扩展，但实际主循环未接通”的断链。

### P1

- 6 个 HookPoint 仍是预留态：`beforeIntentParse`、`afterIntentParse`、`beforeStore`、`afterRecall`、`onFork`、`onEnd`。
- `core-security-audit`、`core-fulltrace`、`core-tool-results-validator` 都在关键链路上，但没有专门回归测试。
- `agent.toolPermissionAsk`、`agent.streamInterrupted`、`agent.orphanToolCallsCompensated` 等异常或补偿事件已发布，但测试不足。
- 观测系统识别 `agent.contextCompressed`、`agent.traceError`、`agent.responseDone`，但未见仓库内发布点，存在“观测预期先于真实事件”的漂移。

### P2

- 扩展命名存在认知偏差风险：实际扩展 id 是 `core-code-tools`，不是 `code-tools`。
- 目前没有一份单独文档把“扩展注册链、Hook 触发点、Event 消费者、测试覆盖”放在同一视图里，本文件补上了这块空缺。

## 10. 建议补测项

- 为 `core-tool-results-validator` 补一个错位 `tool_call_id` 的回归测试，断言修正结果与 `agent.toolResultsValidated` 事件。
- 为 `core-security-audit` 补事件消费测试，断言 `agent.toolCalled` / `agent.toolResult` 会写入 `.observability/audit.jsonl`。
- 为 `builtin-context-compressor` 补压缩生效测试，覆盖启用、禁用、异常回退三条路径。
- 为 `core-tool-guard` 补真实策略测试，分别覆盖 `allow / ask / deny`。
- 决定 `core-knowledge-extraction` 的事件接线方案：
  - 要么把订阅改为当前真实发布事件；
  - 要么在主循环补发 `agent.responseDone`；
  - 但无论采用哪条路线，都应补一条闭环测试。

## 11. 最终判断

- 主循环、事件总线、工具执行链已形成可运行闭环，扩展系统不是“名义存在”。
- 但“扩展已注册”不等于“扩展已接通”，当前最典型的失配是 `core-knowledge-extraction`。
- 如果下一步要做治理，优先级建议是：
  - 先修复事件断链；
  - 再补关键扩展的接线测试；
  - 最后处理预留 Hook 与观测命名漂移。
