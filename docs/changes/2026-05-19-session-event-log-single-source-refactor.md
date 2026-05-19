# 2026-05-19 会话持久化切换为 JSONL 单事实源设计文档变更

## 变更内容

- 新增 `docs/design/design-session-event-log-single-source-refactor.md`。
- 明确会话持久化后续采用 append-only JSONL event log 作为唯一事实源。
- 明确新文件为 `sessions/{session_id}.events.jsonl`，旧 `session.json` / `session.jsonl` 迁移完成后删除。
- 明确 `Agent message`、`LLM message`、`UI message` 都是派生视图，而不是并列事实源。
- 明确前端消息需要可恢复，但恢复来源应为后端基于事件流重建的 UI 视图。
- 明确 full compact / slact 只改变消费视图，不删除原始 transcript。
- 明确后续 session 主模型改为纯线性消息序列，不再采用旧树形分支模型。

## 说明

初版仅新增技术设计文档和变更记录，未修改运行时代码、前端代码或持久化实现。

2026-05-20 已按实现清单落地第一阶段到第五阶段的主闭环：

- 新增 session event log 模型、store、projection、view 与 legacy migration。
- `FileStore` 新增 `sessions/{session_id}.events.jsonl` append/read/delete 主路径，旧 snapshot/delta 方法改为 `legacy_` 迁移专用。
- `SessionManager` 改为线性消息 facade，新消息不再依赖 `parent_id/current_leaf_id/fork_point`，`fork_at()` 标记为不支持。
- `LearningAgentSystem` 启动时迁移旧 `session.json + session.jsonl`，运行时从 `.events.jsonl` replay，会话保存不再执行 snapshot/delta compact。
- Runtime 上下文改为消费 Product 层构造的 `LLMInputView`；成功响应以 `message_end` 进入事实源，流式失败写 `message.stream_failed`。
- Web 会话详情返回 UI 派生视图 `messages`，前端历史恢复优先消费 `messages`，不再把原始 entries 作为聊天气泡真相。
- full compact 继续保留原始 transcript，并记录 source event range、event ids 和 JSONL cursor metadata。

## 设计收口

- Persistence 层只记录事件，不裁决消息可见性。
- Product/Application 层负责从事件流构建 `AgentSnapshot`，并生成 `LLMInputView` 与 `UIViewMessage[]`。
- Runtime 层只消费 `LLMInputView`，不承担前端展示与产品级消息过滤语义。
- Interface 层负责 UI 展示适配，但不持有服务端权威会话状态。
- 旧 `parent_id/current_leaf_id/fork_point` 只作为迁移兼容字段，新写入路径不再依赖分支语义。
- `compact_session()` 不再保留；项目只保留上下文语义层的 full compact / slact。

## 后续实现建议

- 第一阶段先引入 event log 语义、投影入口和 UI/LLM 视图构建入口。
- 旧 `session.json` / `session.jsonl` 仅作为一次性迁移输入，迁移完成后删除。
- 不保留 checkpoint/cache 作为第一版目标态。
- 第一阶段增加旧树形 session 到线性消息序列的兼容投影入口，LLM/UI/compact 均消费线性投影。
- 增加 `seq` / `event_id` 幂等 replay。
- 正常流式结束后以 `message_end` 作为最终消息写入边界；失败/中断消息不进入后续 LLMInputView。
- 敏感 tool output 不进入 session event log，只进入 observability；删除 session 时同步删除 event log、compact summary 和 observability 关联文件。

## 实现清单

- 新增 `docs/design/impl-session-event-log-single-source-checklist.md`。
- 将重构拆为事件源、线性投影、Product 视图、Runtime/UI 切换、流式边界、compact 接入、旧路径删除等阶段。
- 明确每阶段涉及文件、删除旧路径、测试清单和验收标准。

## 验证记录

- `pytest tests`：87 passed。
- `node --test tests/test_web_static_app.js`：4 passed。
