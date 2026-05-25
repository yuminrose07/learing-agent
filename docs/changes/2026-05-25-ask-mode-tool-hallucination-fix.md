# 2026-05-25 修复 Ask 模式工具幻觉与 post_ask_target 丢失

## 背景

Session `sess-a4283e64` 复现了一组互相耦合的 bug：用户在 Ask 模式下让 Agent "看一下当前项目"，Agent 反复输出 ```` ```bash\nls -la\n``` ```` 形式的 markdown 文本，从未真正发起任何工具调用（事件流里 `tool_calls`、`tool_results` 全程为空）。

定位根因有三条：

1. **P0 — system prompt 与运行时配置打架**：`NEUTRAL_GUARDRAILS` 给所有模式都拼了一段"工具使用优先级（grep / read_file / edit_file / bash）"，但 `ASK_PROFILE.tools_enabled=[]` 且 `SINGLE_PASS` 路径根本不向 LLM 透传 `tools` 字段。模型相信自己有工具，于是把"想做的事"用 bash 代码块描述出来，进入幻觉式工具调用。
2. **P1 — 设计文档与实现脱节**：`design-chat-ask-study-modes.md` 与 `design-impl-chat-ask-modes.md` 都写着 `tools_enabled=["read_file", "grep"]`，但代码已经是 `[]`，且 `SINGLE_PASS` 架构上不支持工具调用。文档容易把读者带回"该给 Ask 加工具"的错误结论。
3. **P2 — `post_ask_target` 只读不写**：`main.py:607` 从 `mode_metadata.get("post_ask_target", AgentMode.CHAT.value)` 读取确认后的目标模式，但代码库里**没有任何地方写入** `post_ask_target`。结果用户从 Study 进 Ask 再确认时，会被错误地踢回 Chat。

## 改动

### P0：拆分 guardrails，按 `tools_enabled` 选择拼接

`learning_agent/learning_agent/mode_service.py`：
- 把原 `NEUTRAL_GUARDRAILS` 拆为 `_BASE_GUARDRAILS`（角色与语言约束） + `_TOOL_USAGE_GUARDRAILS`（工具优先级章节）。
- 新增 `_NO_TOOL_GUARDRAILS`：明确告诉模型"当前模式没有工具，不要输出工具调用格式的内容"，并引导用户切到 Chat / Study。
- `build_system_prompt` 根据 `resolve_profile(mode).tools_enabled` 选择拼 `_TOOL_USAGE_GUARDRAILS` 还是 `_NO_TOOL_GUARDRAILS`。
- `NEUTRAL_GUARDRAILS` 作为旧名保留为 `_BASE_GUARDRAILS + _TOOL_USAGE_GUARDRAILS`，兼容已有引用。

副作用范围：`ASK` 与 `TEACH` 现在拿到的是 `_NO_TOOL_GUARDRAILS`；`CHAT` / `STUDY` 行为不变。

### P1：对齐文档与代码

- `docs/design/design-chat-ask-study-modes.md` §8.2 Ask Profile：`tools_enabled` 从 `["read_file", "grep"]` 改为 `[]`，说明改为"Ask 是对齐阶段，需查代码请在确认后切到 Chat 或 Study"。
- 同文 §11.1 Tools 策略表 Ask 行同步更新。
- `docs/design/design-impl-chat-ask-modes.md`：`ASK_PROFILE.tools_enabled` 同步改为 `[]`。

### P2：在 `switch_session_mode` 处写入 / 清理 `post_ask_target`

`learning_agent/learning_agent/session_manager.py`：在写入 `last_mode_switch` 之后追加两行：
- `mode == ASK and from_mode != ASK` → 把 `from_mode.value` 写入 `mode_metadata["post_ask_target"]`，确认后能回到正确的来源模式。
- `mode != ASK` → 清掉 `post_ask_target`，避免陈旧值污染下一次进入 Ask。

选址理由：`switch_session_mode` 是模式切换的单一入口，与已有的 `last_mode_switch` 写入位置一致；不在 `_prepare_session_turn` 内处理是为了避免把"模式切换副作用"分散在多处。

### 测试

`tests/test_mode_layering.py` 新增两个测试类、共 6 个用例：

- `TestSystemPromptToolGuardrails`：
  - Ask 模式的 system prompt 不应含"工具使用优先级"，应含"工具限制"
  - Chat 模式应保留"工具使用优先级 / grep / read_file"
  - Teach 模式与 Ask 一致（无工具）
- `TestPostAskTargetMetadata`：
  - 从 Study 切入 Ask 应在 `mode_metadata` 写入 `post_ask_target=study`
  - 从 Ask 切出到 Chat 应清掉 `post_ask_target`
  - 从 Chat 切入 Ask 应记录 `post_ask_target=chat`

全量测试 187 通过（181 原有 + 6 新增），无回归。

## 未决项

- 用户在 Ask 模式下输入自然语言确认（如 "你可以用 grep 工具"）不会被 `is_confirmation_message` 识别为确认，会一直循环在 aligning 状态。本次未改 —— 这是产品交互设计问题，建议另起一个 PR 讨论是否扩大确认词典或加"识别动作型意图自动切到 Chat / Study"的产品逻辑。
- `_run_single_pass_turn` 目前不透传 `tools` 字段；如未来要让 Ask / Teach 支持轻量工具，需在该路径补齐 `ChatParams(tools=...)`，并同步评估是否破坏"单回合收口"语义。
