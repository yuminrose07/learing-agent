# 2026-05-21 compaction summary pipeline 修复

## 变更内容

- 将 `CompactionCoordinator` 的 full compact 主链改为异步执行，允许在 Product/Application 层直接调用摘要执行器。
- 将结构化 compact prompt 真正接入摘要生成：`render_compact_prompt()` 的输出现在会送入 provider/summary executor，而不是仅构造后丢弃。
- 收口 compact summary 的事实源：`load_compact_summary()` 只读取 session event replay 得到的 latest compact summary，不再回退读取 `summary.txt`。
- 停止在 compact 成功主链中写入 `summary.txt` 路径语义；`summary_path` 不再进入 `compaction.summary_added` 事件。
- 修复 incremental/rebase 下 latest summary 的语义：没有结构化摘要执行器时，会把旧 summary 与新增 delta 合并成完整 canonical summary；有结构化执行器时，直接消费返回的 canonical summary。
- 调整测试为异步 compaction 调用，并补充结构化 prompt 接线、summary cache 非事实源、incremental canonical summary 回归测试。

## 设计结论

- 正式 compact 摘要必须消费结构化 prompt，而不是 fallback 文本拼接主导生产路径。
- `append-only JSONL` 事件流是 compact summary 的唯一可消费事实源；本地 summary/cache 文件不能作为回退事实源。
- 最新 `compaction.summary_added` 必须始终代表完整 canonical summary，而不是只包含新增 delta 的局部摘要。

## 验证结果

- 运行 `pytest -q tests/test_compaction_event_source.py tests/test_compaction_prompts.py tests/test_compaction.py tests/test_llm_input_view.py`
- 结果：`11 passed`
