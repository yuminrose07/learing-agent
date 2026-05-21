# 2026-05-21 长任务压缩数据集与自动回放 runner

## 变更内容

- 新增长任务压缩数据集自动回放测试 `tests/test_compaction_dataset_runner.py`。
- 新增 `tests/fixtures/compaction_long_task_cases/`，首批包含 3 个“学习当前项目”的真实长任务脚本 JSON case。
- 数据集 runner 直接驱动 `AgentLoop + SessionManager + CompactionCoordinator + append-only JSONL event log`，而不是只做 summary 单元测试。
- 新增可脚本化断言：summary 内容、pending section、recent retained view、summary event scope 前缀、assistant 响应脚本一致性。
- 每次运行都会把 case 结果落到 `.test_artifacts/compaction_dataset_runs/latest/<case_name>/`，其中包含 `sessions/{session_id}.events.jsonl` 与 `report.json`。
- 保留 `tests/test_compaction_long_task.py` 作为更细粒度的长任务语义回归测试，数据集 runner 负责更贴近真实回放的场景。

## 设计结论

- 长任务压缩测试分为两层：
  - 数据集回放层：用 JSON 脚本驱动真实对话链，验证 summary/view/event 的整体行为。
  - 细粒度回归层：对 canonical summary、retained/raw 边界、非法摘要回退做更精确断言。
- CI 中优先使用受控 `summary_executor` 与 scripted provider，保证可重复性；真实 provider 评估应独立于稳定 CI。
- 自动回放测试的核心目标不是评估模型创作能力，而是验证“学习当前项目”的长任务在压缩后，目标、约束、纠正、完成项与待办项是否仍可被系统稳定恢复。
- 第一版数据集优先覆盖当前项目自己的学习场景，例如 `LearningAgentSystem`、`AgentLoop`、`SessionManager`、`session_projection`、`views`、`append-only JSONL` 与 compaction 主链。
- `report.json` 会按 turn 保存 user、assistant、compaction plan、summary event 数量、latest summary 与 view 重建结果，便于人工回看每一轮 LLM 回复与压缩演进。

## 验证结果

- 运行 `pytest -q tests/test_compaction_dataset_runner.py tests/test_compaction_long_task.py tests/test_compaction_event_source.py tests/test_compaction.py tests/test_llm_input_view.py`
- 结果：`15 passed`
