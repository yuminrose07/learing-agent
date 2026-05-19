# 2026-05-19 Full Compact 与 Slact 设计文档变更

## 变更内容

- 新增 `docs/design/design-full-compact-slact-adaptation.md`。
- 明确 auto compact 只采用“压缩旧消息 + 保留最近消息”的路径。
- 明确后续 slact 功能承接 Claude Code 的 full / from / up_to 三类用户触发模板。
- 将 Claude Code 九章节摘要结构适配为学习 Agent 场景。
- 补充 analysis -> summary、反漂移原文锚点、incremental 与 rebase 的实现要求。

## 说明

本次仅新增技术设计文档和变更记录，未修改运行时代码。

## 追加澄清

- 现阶段不引入子代理、fork agent 或后台 summarizer。
- auto compact 与 slact 的摘要生成由主代理主链路执行，并复用已有重试、熔断和降级机制。

## Few-Shot 规则补充

- 压缩模板必须内置学习场景 few-shot 示例，用于提高摘要生成质量。
- 示例需要展示 `<analysis>` / `<summary>` 结构、九章节输出、用户原话锚点，以及 auto/up_to 与 full/from 的第 8、9 节差异。
- incremental / rebase 示例需要展示如何合并新旧信息，而不是机械追加 delta。

## JSONL 事实源补充

- 明确 full compact 的压缩输入以每个 session 的持久化 JSONL transcript 为事实源，而不是内存 message 数组。
- 增加 JSONL cursor / entry id anchor 设计，用于支撑增量压缩和 rebase。
- 区分持久化层 snapshot/delta 合并与会话上下文 full compact，避免上下文压缩误清空原始 transcript。
- 根据 session 线性化方向，slact full 的 source 范围改为当前线性 session 序列，不再按 branch path 解释。
- 更新为读取 `sessions/{session_id}.events.jsonl`；旧 `compact_session()` 不再保留。
- compact source 只消费 `message_end` 后的完整 assistant 消息，失败/中断流式消息默认不进入摘要。
- 敏感 tool output 不进入 compact source，summary 只能基于安全摘要、状态、引用和必要元数据生成。
