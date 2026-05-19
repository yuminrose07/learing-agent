# 验收报告：严格分层模式重构

> **验收对象**：Chat / Ask / Study 模式实现的严格分层重构
> **验收依据**：
> 1. `AGENTS.md` 中“严格分层”约束
> 2. `docs/design/design-impl-chat-ask-modes.md`
> **验收日期**：2026-05-15

---

## 一、验收结论

本次重构 **通过验收**。

核心结论如下：

1. **Product/Application 与 Agent Runtime 的职责边界已重新收口。**
   - `Ask` 的确认判断、`ask_state` 推进、mode 切换已从 Runtime 移回 Product 层。
   - Runtime 不再直接理解 `Ask -> Chat` 这类产品流程。

2. **Runtime 已改为消费通用执行计划，而非消费产品语义。**
   - Runtime 现只根据 `turn_kind` 执行 `react` 或 `single_pass`。
   - `Ask` 不再作为 Runtime 内部的硬编码业务分支存在。

3. **Interface 层回到了系统 facade。**
   - Web 层不再直接调用 `session_manager.switch_session_mode()`。
   - SSE 不再用“请求 mode”猜测实际执行状态，而是读取 Product 层注入的响应元数据。

4. **兼容性和回归验证达到本次改动的验收要求。**
   - 定向测试通过。
   - 语法编译通过。
   - 关键分层回归测试已补齐。

---

## 二、验收范围

本次验收覆盖以下目标：

1. 将模式策略定义从 Runtime 侧语义收回到 Product/Application 层。
2. 将 Ask 对齐流的确认、推进、收尾逻辑从 `agent_loop.py` 移出。
3. 让 Runtime 仅执行通用 turn 计划，不再直接操作 `ask_state`。
4. 修正 Web API 与 SSE，使其遵循 `Interface -> Product/Application` 的依赖方向。
5. 补充能约束未来回归的分层测试。

本次验收 **不包括**：

- Study 模式的真实功能启用
- 更大范围的全量回归测试
- 前端交互细节的手工 E2E 浏览器验收

---

## 三、主要变更与验收判断

### 3.1 Product/Application 层新增模式编排中心

新增文件：

- `learning_agent/learning_agent/mode_service.py`

验收判断：

- 通过。该模块集中定义了：
  - `TurnExecutionKind`
  - `ModeProfile`
  - `TurnExecutionProfile`
  - `PreparedSessionTurn`
  - `build_turn_profile()`
  - `is_confirmation_message()`
- 这意味着模式策略与执行计划已明确落在 Product/Application 层，而不是继续散落在 Runtime 中。

### 3.2 `LearningAgentSystem` 承担产品级模式编排

关键改动文件：

- `learning_agent/learning_agent/main.py`

验收判断：

- 通过。系统层新增并承担了以下产品职责：
  - `update_session_mode()`：统一 mode 切换与持久化
  - `_prepare_session_turn()`：判断 Ask 确认、推进 `ask_state`、决定实际执行 plan
  - `_finalize_prepared_turn()`：在 Product 层回收对齐结果
  - `stream_session_chat()`：只负责把 prepared turn 交给 Runtime 执行

结论：

- “产品想怎么运行” 已主要回到 `LearningAgentSystem`。

### 3.3 Runtime 不再直接编排 Ask 产品流程

关键改动文件：

- `learning_agent/agent/agent_loop.py`

验收判断：

- 通过。Runtime 已完成以下收敛：
  - 删除 Ask 专属确认分支
  - 删除 Runtime 内对 `ask_state` 的推进与回收
  - 删除对确认词规则的直接依赖
  - 将原 Ask 对齐轮改造为通用 `_run_single_pass_turn()`
  - 用 `profile.turn_kind` 决定执行类型

当前 Runtime 关注点：

- 单轮执行
- 状态机
- 上下文组装
- LLM 调用
- 工具调度
- 自愈与降级
- 事件与观测

结论：

- “这一轮具体怎么执行” 与 “产品为什么这么执行” 已被分离。

### 3.4 Interface 层不再越过系统 facade

关键改动文件：

- `learning_agent/web/web_server.py`
- `learning_agent/ai/models.py`

验收判断：

- 通过。
- Web 层的 `PUT /sessions/{session_id}/mode` 已改为调用 `system.update_session_mode()`，不再直接调用 `session_manager`。
- `ChatChunk` 新增 `metadata`，SSE 改为输出实际执行结果附带的元数据，而不是简单回显请求入参。

结论：

- Interface 层重新回到“协议适配层”的定位。

### 3.5 兼容层处理可接受

关键改动文件：

- `learning_agent/ai/mode_profile.py`

验收判断：

- 通过。
- 当前该文件已退化为兼容导出层，把旧引用桥接到 Product 层的 `mode_service`。
- 这有助于避免一次性改动过大导致其它模块联动失效。

限制：

- 这是过渡性方案，不是最终收口形态。
- 后续可继续把外部引用统一迁移到 `learning_agent.learning_agent.mode_service`。

---

## 四、修改文件清单

| 文件 | 改动类型 | 验收关注点 |
|------|---------|-----------|
| `learning_agent/learning_agent/mode_service.py` | 新增 | Product 层模式策略与执行计划中心 |
| `learning_agent/learning_agent/main.py` | 重构 | Ask 编排、mode 切换、prepared turn 收口 |
| `learning_agent/agent/agent_loop.py` | 重构 | Runtime 改为 `turn_kind` 驱动，不再编排 Ask 流程 |
| `learning_agent/web/web_server.py` | 修改 | Web 层改走系统 facade，SSE 改用实际元数据 |
| `learning_agent/ai/models.py` | 修改 | `ChatChunk` 增加 `metadata` |
| `learning_agent/ai/mode_profile.py` | 修改 | 兼容导出 shim |
| `tests/test_web_adaptation.py` | 修改 | 验证 Web 不直连内部 manager |
| `tests/test_mode_layering.py` | 新增 | 验证 Product 层负责 Ask 编排与结果回收 |

---

## 五、验收证据

### 5.1 分层行为证据

以下行为现在位于 Product/Application 层：

1. Ask 确认词判断
2. `ask_state.status` 推进
3. `ask_state.confirmed_input` 回收
4. `Ask -> Chat` 模式切换
5. `PreparedSessionTurn` 构造

以下行为现在位于 Runtime：

1. `react` 执行
2. `single_pass` 执行
3. 状态机推进
4. LLM / Tool / Hook / Event 执行
5. 失败自愈与收尾

### 5.2 测试证据

执行命令：

```bash
python3 -m pytest tests/test_web_adaptation.py tests/test_mode_layering.py
```

结果：

```bash
============================= test session starts ==============================
collected 22 items

tests/test_web_adaptation.py ....................                        [ 90%]
tests/test_mode_layering.py ..                                           [100%]

============================== 22 passed in 0.38s ==============================
```

重点覆盖：

- `test_update_mode_endpoint_uses_system_api`
  - 验证 Web 端点走 `LearningAgentSystem` facade，而不是直接操作 `session_manager`
- `test_prepare_session_turn_keeps_ask_confirmation_in_product_layer`
  - 验证 Ask 确认逻辑由 Product 层处理
- `test_stream_session_chat_captures_alignment_output_in_product_layer`
  - 验证对齐结果在 Product 层写回 `ask_state.confirmed_input`

### 5.3 编译证据

执行命令：

```bash
python3 -m compileall learning_agent tests
```

结果：

- 编译通过，无语法错误

---

## 六、验收标准逐项判断

| 验收项 | 结果 | 说明 |
|------|------|------|
| Ask 产品语义不再驻留 Runtime | 通过 | Runtime 改为 `turn_kind` 执行 |
| `ask_state` 由 Product/Application 持有并推进 | 通过 | 逻辑已收口到 `LearningAgentSystem` |
| Web 不绕过系统 facade | 通过 | mode 更新接口已走 `system.update_session_mode()` |
| SSE 输出实际执行元数据 | 通过 | 使用 `chunk.metadata` 而不是请求参数回显 |
| 旧引用具备兼容过渡方案 | 通过 | `learning_agent.ai.mode_profile` 已转为 shim |
| 针对本次分层边界的回归测试存在 | 通过 | 新增 `tests/test_mode_layering.py` |

---

## 七、已知限制

1. `learning_agent/ai/mode_profile.py` 仍作为兼容入口存在。
   - 当前可接受，但不是最终结构。

2. 本次仅完成针对性测试。
   - 尚未运行全量测试集，也未完成完整 Web 手工端到端验收。

3. Study 模式仍为占位态。
   - 本次验收重点是分层与模式编排收口，不是 Study 能力落地。

---

## 八、后续建议

1. 将外部引用逐步统一到 `learning_agent.learning_agent.mode_service`，最终移除兼容 shim。
2. 继续补一轮全量回归测试，确认本次分层重构未影响其它运行时能力。
3. 在前端与 observability 侧继续统一“mode 真源来自 Product 层”的原则，减少界面状态漂移风险。

---

## 九、最终结论

从“职责归属、依赖方向、状态归属、接口调用路径、回归测试”五个维度看，本次改动已经满足“严格按照分层思想重构”的验收要求。

可以将本次改动判定为：

- **架构方向正确**
- **边界收口到位**
- **验证证据充分**
- **允许进入后续增量优化阶段**
