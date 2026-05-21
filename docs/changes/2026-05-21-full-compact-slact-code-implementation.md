# Full Compact / Slact 适配实现记录

日期：2026-05-21

关联设计：

- `docs/design/design-full-compact-slact-adaptation.md`

## 本次实现范围

- 扩展 compaction 数据模型，补齐 `compact_mode`、`compact_scope`、`compact_event_id`、`source_entry_ids`、`retained_entry_ids`、`source_snapshot_seq`、JSONL cursor、summary validation 等字段。
- 新增 `compact-summary-v1` prompt 模板与后处理模块，支持 no-tools preamble、固定输入块顺序、九章节摘要结构、`<analysis>` 剥离、summary hash 与基础校验。
- compact 成功后将 `summary_text`、hash、source/retained ids、cursor 与 validation status 写入 `compaction.summary_added` 事件；独立 summary 文件仅保留为兼容 artifact/cache。
- session replay 时从最新有效 `compaction.summary_added` 重建 compact metadata 与 latest summary。
- LLMInputView 在存在 compact summary 时，只保留 `retained_entry_ids` 与 compact 事件之后新增的消息，避免 summary 与已压缩原文同时进入 provider context。
- 修正 auto compact retained 边界，使 cut point 所在 safe unit 被保留，而 source unit 被 summary 替代。

## 测试覆盖

- compact summary 事件包含内联 summary、hash、source/retained ids。
- compact 后 provider messages 不再包含 source entry 原文。
- 无当前 turn plan 时，LLMInputView 可从 JSONL replay 的 latest summary 继续构建压缩上下文。
- prompt 渲染固定块顺序，且 auto prefix 使用 continuation 型第 8/9 节标题。
- `format_compact_summary()` 不持久化 `<analysis>`。

## 后续未完成

- Runtime `agent.*` 事件到 Product SessionEvent 的完整幂等转写尚未实现。
- `/slact full/from/up_to/rebase` 的 Interface 命令与 `SlactService` 尚未接入。
- SummaryExecutor 仍使用确定性 fallback，后续应替换为 provider no-tools single-pass port。
- 大型 tool result artifact slice 能力尚未在本次实现中完成。
