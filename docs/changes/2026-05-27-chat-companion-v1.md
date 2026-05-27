# Chat Companion V1

## 背景

闲聊模式需要从“低强度问答”升级为“减压陪伴”，用于缓冲研学模式带来的压力。V1 不新增主模式，而是在 `chat` 下增加 `companion` 子档案，保持 `Interface -> Product/Application -> Agent Runtime` 的分层边界。

## 变更

- 新增 `learning_agent.learning_agent.companion_policy`，负责陪伴风格、减压意图、自然语言触发和 prompt addendum 生成。
- 非学习卷 `chat` turn 在 Product/Application 层生成 companion turn plan；Runtime 只消费已收口的 `TurnExecutionProfile`。
- 纯减压陪伴轮默认关闭工具，避免闲聊被工具流程打断；用户准备回到学习时允许恢复普通能力。
- 新增 `companion.profile_changed`、`companion.signal_detected`、`companion.recovery_suggested` 事件类型。陪伴状态仍通过 `session.mode_metadata` 重建，事件只用于观测和评估。
- 新增 Web API：
  - `GET /companion-styles`
  - `GET /sessions/{session_id}/companion`
  - `PUT /sessions/{session_id}/companion`
- 前端闲谈页顶部新增“小月亮 / 陪伴”切换入口；闲谈显示陪伴选择，研习保留思路选择。
- SSE 与历史回放支持 companion metadata，助手气泡可显示陪伴徽章。

## 边界

- 不新增 `AgentMode.GIRLFRIEND`，避免把陪伴人格变成与 `chat/study/teach` 并列的产品主协议。
- 不把陪伴判断放入 Agent Runtime。
- 不伪装真人伴侣，不使用控制欲、占有欲、羞辱或 PUA 式表达。
- 严重心理危机表达应退出角色扮演，转入安全支持。

## 验证

- `pytest -q tests/test_companion_policy.py tests/test_chat_study_separation.py tests/test_mode_layering.py`
- `node --test tests/test_web_static_app.js`
