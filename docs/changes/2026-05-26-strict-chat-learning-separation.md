# 严格区分闲谈与研习

## 背景

用户体验中需要明确区分两种入口：

- 闲谈：轻量对话，不中途升格为研习。
- 研习：通过独立研习卷开始，围绕一个主题推进。

现阶段不允许已有闲谈会话转成研习，也不允许通过旧 API 表达“从闲聊升格”。

## 改动

- 后端移除 `POST /chat-sessions/{session_id}/promote-to-learning-unit` 公开路由。
- 后端 `CreateLearningUnitRequest.source` 不再接受 `promoted_from_chat`。
- 保留底层 `promoted_from_chat` 枚举读取能力，仅用于历史实验数据兼容。
- `create_learning_unit` 返回前同步 `session.learning_unit_id`，确保上层立刻能按服务端绑定状态识别研习会话。
- 前端历史会话列表基于 `session.learning_unit_id` 显示 `闲谈` / `研习` 类型标识。
- 欢迎页文案明确闲谈和研习彼此独立。
- 更新 `design-modes-refactor-chat-vs-learning-unit.md`，对齐不可互转边界。

## 验证

- API 测试覆盖 `promoted_from_chat` public source 被拒绝。
- API 测试覆盖闲聊升格路由不再暴露。
- 前端静态测试覆盖历史会话列表类型标识。
