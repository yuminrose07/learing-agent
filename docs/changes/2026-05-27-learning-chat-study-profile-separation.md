# 2026-05-27 研学 absorbing 与闲聊 Chat profile 分离

## 背景

并行开发中，闲聊模式和研学模式仍共享 `CHAT_PROFILE`：

- 无学习卷的纯闲聊走 `AgentMode.CHAT`。
- 学习卷 `absorbing` 阶段也默认走 `AgentMode.CHAT`。

这会导致闲聊方调整 `CHAT_PROFILE` 或 `CHAT_MODE_PROMPT` 时，直接影响研学 absorbing 阶段；研学侧也无法独立启用 Study 的 memory、heavy context 和 tutor 风格。

## 本次改动

### 1. 研学 absorbing 默认改走 Study

更新：

- `learning_agent/learning_agent/main.py`
- `learning_agent/ai/learning_unit.py`

关键变化：

- 学习卷 `absorbing` 默认 `effective_mode = AgentMode.STUDY`。
- 只有对齐策略判定为 `active` 时，本轮临时覆写为 `AgentMode.ASK`。
- `outputting` 仍走 `AgentMode.TEACH`。
- 非学习卷会话仍走 `AgentMode.CHAT`。

### 2. 下游守卫迁移到 Study

以下 absorbing 下游守卫从 `CHAT` 迁到 `STUDY`：

- 首轮 opening addendum 注入。
- `allow_full_compact` 判断。
- 概念抽取尾任务。
- `learning_unit.first_value_delivered` 事件。

决策：absorbing 仍允许 full compact，因为研学场景更可能进入长上下文学习链路。

### 3. Study prompt 改成研学语义

更新 `STUDY_MODE_PROMPT`，从通用“深度讲解”改为研学 absorbing 的体验协议：

- 入局
- 碰撞
- 铸造
- 定型
- 火候控制
- 少量定型
- 下一块发光的地方

### 4. 测试同步

更新学习卷相关测试：

- absorbing 清晰输入应产出 `STUDY`。
- 对齐门仍产出 `ASK`。
- 无学习卷会话仍产出 `CHAT`。
- first-value 事件与学习卷验收 helper 改用 `STUDY` prepared turn。

## 文档同步

更新 `docs/design/design-learning-unit-adaptive-alignment.md`：

- 当前主逻辑从 `absorbing -> CHAT` 改为 `absorbing -> STUDY`。
- 明确 `ASK` 只作为对齐门临时覆写。

## 结果

闲聊和研学的 profile 已在 Product/Application 层真正分离：

- 闲聊方可以独立调整 `CHAT_PROFILE` / `CHAT_MODE_PROMPT`。
- 研学方可以独立调整 `STUDY_PROFILE` / `STUDY_MODE_PROMPT`。
- Runtime 主链无需新增模式分支，仍只消费 Product 层准备好的 `TurnExecutionProfile`。

## 验证

- `python3 -m pytest tests/test_mode_layering.py tests/test_learning_unit_events.py tests/test_learning_unit_acceptance.py tests/test_alignment_policy.py`
- 结果：`96 passed`
- `python3 -m pytest tests/test_learning_unit_api.py tests/test_learning_unit_store.py tests/test_learning_unit_metrics.py`
- 结果：`79 passed`
- `node --test tests/test_web_static_app.js`
- 结果：`10 passed`
