# 2026-05-21 上下文压缩事件驱动修订

## 变更内容

- 更新 `docs/design/design-full-compact-slact-adaptation.md`。
- 明确压缩设计文档是可迭代草案，2026-05-21 讨论结论优先于初版中“summary 文件落盘后再引用”的表述。
- 明确 compact summary 首先作为 `compaction.summary_added` 事件写回 `sessions/{session_id}.events.jsonl`，独立 summary 文件最多作为 artifact/cache，不作为事实源。
- 明确 `CompactMetadata` 是从会话 JSONL replay 得到的派生索引或加速缓存，冲突时必须以 JSONL 为准。
- 明确 Agent Runtime 不直接 append session 事实；Runtime 发布 `agent.*` 运行事件，由 Product/Application 层确认并转写为 session event。
- 明确事件分为 ProductCommand、AgentRuntimeEvent、SessionEvent、ProductDerivedEvent 四类，避免 `agent.message_end` 与 `message_end` 混淆。
- 明确事件必须通过 `turn_id`、`run_id`、`attempt_id`、`user_event_id` 关联，支撑重试、幂等、中断和并发隔离。
- 补充 SessionEvent 信封字段、at-least-once 投递语义和 Product 层幂等转写要求。
- 新增 `CompactionSourceView -> Safe Compact Units` 的压缩边界模型，避免直接按 raw JSONL 行或普通消息类型切分。
- 明确当前 turn 的 user event 可以先写入 JSONL，但不进入本轮 compact source，必须进入 retained context。
- 补充大型 tool result 的 preview + `artifact_ref` 策略：完整结果保存为本地 artifact，JSONL 和 LLM message 只保留 preview、引用和必要元数据。
- 补充 compact 提交事务顺序：summary 候选校验成功后，以 `compaction.summary_added` 写入 JSONL 作为提交点，metadata/cache 最后更新。
- 补充 `source_snapshot_seq`，避免 compact source 被并发 append、取消或迟到 runtime event 污染。
- 补充 summary validation / grounding 要求，校验失败不能写入可消费 compact summary。
- 补齐结构化 compact prompt 文档：定义 `compact-summary-v1`、PromptSpec 输入块、六类模式差异、required output contract 和 prompt/事件字段绑定。
- 补充多次 compact 后 latest valid summary 选择规则，旧 summary 保留为审计事实但默认不进入 LLMInputView。
- 补充 UI 折叠提示和 checkpoint/cache 的边界：二者都是派生视图或可丢弃缓存，不能成为会话事实源。
- 补充 `/slact` 与 UI 压缩操作的命令语义，不默认写成 LLM 可读用户消息。
- 补充 compact summary 与长期 memory 的边界，summary 不能自动提升为 memory fact。

## 设计结论

- 会话级 JSONL 是唯一事实源。
- Product/Application 层负责从 JSONL 构建 `CompactionSourceView`，并决定 source / retained / cut point。
- cut point 和 slact pivot 只能落在 safe compact unit 边界；tool call 与 tool result 必须成组保留或成组压缩。
- compact 成功提交以 `compaction.summary_added` 事件为准；metadata 与 summary artifact 都不能替代该事件。
- Runtime 事件可重复或迟到，Product 层负责幂等转写；SessionEvent append 才是事实提交点。
- compact source 必须绑定固定 `source_snapshot_seq`，不覆盖 snapshot 后的事件。
- Runtime 只消费 Product 层构建好的 `LLMInputView`，不拥有压缩策略。
- 当前 turn 的原始用户输入和 Product 解析后的 runtime input 需要区分，后者不能冒充用户原话。
- `/slact` 控制命令和自然语言用户消息需要区分，避免命令文本污染学习上下文。
- summary candidate 必须通过结构、hash、source ids、用户原话锚点和 token budget 校验后才能提交。
- 正式压缩提示词必须由 Product 层根据 `CompactionSourceView.safe_units` 组装，SummaryExecutor 只消费结构化 prompt，不自行读取上下文。
- compact summary 不自动进入长期 memory；memory fact 必须引用原始 SessionEvent。
- 大型 artifact、summary artifact、敏感 observability 文件必须跟随 session 删除统一清理。

## 后续实现建议

- 先收口 Runtime -> Product 的 `agent.*` 事件边界，再修正 auto compact 主链。
- 先实现 SessionEvent 信封、`seq` 分配和幂等 dedupe，再接入 compact summary 事件。
- 按 `compact-summary-v1` 实现 `learning_agent/learning_agent/compaction/prompts.py`，包括 PromptSpec、六类 mode instruction、九章节 skeleton、analysis 剥离和 validation。
- 为 `CompactionPlan` 补充 `compact_event_id`、`source_entry_ids`、`retained_entry_ids`。
- 为 `CompactionSourceView`、safe compact units、当前 turn user retained、`source_snapshot_seq` 规则补单元测试。
- 为大型工具结果补 artifact 保存和 slice 读取策略，逐步替代会改写事实源的 emergency truncation。
- 为 Runtime 事件幂等转写、latest valid summary 选择、UI 折叠提示不反写事实源、checkpoint/cache 冲突丢弃补测试。
- 为 slact 命令不进入 LLM 可读 user message、summary validation 失败、compact summary 不自动写 memory 补测试。
