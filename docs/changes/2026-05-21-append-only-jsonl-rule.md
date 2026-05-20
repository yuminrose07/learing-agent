# 2026-05-21 append-only JSONL 事实源约束

## 变更内容

- 更新 `AGENTS.md` 核心规则。
- 明确会话持久化以 `append-only JSONL` 作为唯一事实源。
- 明确 `Agent/LLM/UI message` 只允许作为派生视图或投影，禁止成为并列事实源。
- 明确禁止原地修改、覆盖、删除或回写既有 `JSONL` 事件。
- 明确任何新状态、修正、压缩结果或恢复信息，都必须通过追加新事件表达，再基于事件流重建上下文与视图。

## 设计结论

- `sessions/{session_id}.events.jsonl` 是会话事实提交点。
- `summary.txt`、`meta.json`、UI 折叠提示、checkpoint/cache 都只能作为缓存、索引或派生视图，不能替代会话事实源。
- 上下文压缩、状态修正、恢复流程都必须遵守 append-only 语义，不能通过改写旧事件实现。

## 后续实现建议

- 收口 `load_compact_summary()`、`CompactMetadata` 和相关缓存逻辑，冲突时始终以 `JSONL replay` 为准。
- 为 compaction、session replay、UI projection 增加测试，防止再次引入第二事实源。
