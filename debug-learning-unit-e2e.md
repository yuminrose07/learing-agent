# Debug Session: learning-unit-e2e

- Status: OPEN
- Started: 2026-05-26
- Goal: 端到端复现并定位“无法开启新学习卷 / 找不到旧学习卷 / 进入旧卷后看不到关闭按钮”问题

## Hypotheses

- H1: 前端在 active learning unit 冲突后虽然回到了旧卷，但没有正确切到对应 session，导致用户看不到该卷的完整研习卡。
- H2: 旧学习卷实际 phase 已经是 terminal，但前端本地状态或后端查询仍把它当成 active，阻塞新卷创建。
- H3: 研习卡宿主或状态事件在某些页面流转后没有正确 hydrate，导致 stop 按钮应该存在但没有渲染出来。
- H4: active unit 冲突恢复链路只恢复了 unit id，没有恢复/高亮对应 session，导致用户不知道哪一个会话是旧卷。
- H5: 前端在欢迎页、历史会话加载、learning-unit metadata 增量更新三条路径上存在状态不同步，导致新卷创建与停止按钮展示出现分叉行为。

## Evidence Log

- 运行时证据 1：`GET /learning-units` 返回唯一 active 学习卷 `lu-a401db44`，`session_id = sess-a4283e64`，`phase = absorbing`，目标是“怎么学agent”。
- 运行时证据 2：浏览器打开首页后，尝试新建/恢复研习链路前，左侧历史可见“怎么学agent”。
- 运行时证据 3：点击左侧“怎么学agent”后，页面进入普通 chat 态，输入框占位符为“继续闲谈...”，顶部未显示研习状态，页面未渲染“先学到这里”按钮。
- 运行时证据 4：`GET /sessions/sess-a4283e64` 返回 `{learning_unit_id: null, mode: "chat"}`，与 `GET /learning-units/lu-a401db44` 中 `session_id = sess-a4283e64` 直接矛盾。
- 运行时证据 5：`GET /sessions` 全量结果中没有任何 session 带 `learning_unit_id`，说明前端无法从会话列表识别任何研习会话。
- 代码证据 1：`create_learning_unit()` 运行时会设置 `session.learning_unit_id = unit.id`，说明关联在内存里被赋值过。[main.py:L504-L524](file:///Users/roseannk/my-agent/learning_agent/learning_agent/main.py#L504-L524)
- 代码证据 2：`LearningSession` 模型本身存在 `learning_unit_id` 字段，不是模型缺字段。[models.py:L193-L207](file:///Users/roseannk/my-agent/learning_agent/ai/models.py#L193-L207)
- 代码证据 3：会话投影 `AgentSnapshot -> LearningSession` 时没有携带 `learning_unit_id`。[session_projection.py:L24-L63](file:///Users/roseannk/my-agent/learning_agent/learning_agent/session_projection.py#L24-L63)
- 代码证据 4：事件回放 `_apply_event()` 处理 `session.created` / `session.mode_changed` 等事件时，没有任何分支会恢复 `learning_unit_id`。[session_projection.py:L144-L201](file:///Users/roseannk/my-agent/learning_agent/learning_agent/session_projection.py#L144-L201)
- 代码证据 5：`SessionManager.create_session()` 追加的 `session.created` 事件 payload 也没有 `learning_unit_id`，且 `create_learning_unit()` 给 session 赋值后没有补发一条会话关联更新事件。[session_manager.py:L85-L122](file:///Users/roseannk/my-agent/learning_agent/learning_agent/session_manager.py#L85-L122)

## Fix Log

- Pending

## Verification

- 结论：H1/H4/H5 成立，H2 被否定，H3 更像上层表象而非根因。
- 当前最小根因：学习卷与会话的关联没有进入 append-only 会话事件投影链，导致重载/读取后 `session.learning_unit_id` 丢失，前端无法识别研习会话。
