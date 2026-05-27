# 2026-05-26 Tool Call Success-Oriented Architecture — Implementation

## 背景

承接 `2026-05-26-tool-call-success-oriented-architecture.md` 的架构方案。
该方案只输出了设计，没有落地。本轮把方案的 P0 收口在工具调用主链上落了下来，目标是：

- 用户感知不到工具失败
- `/chat` 任何模式（chat / learning / teach）都不会空返回
- 工具失败统一被收口、统一被记录，便于后续优化

实现层强制约束：修复必须发生在**与模式无关的底层通用层**（ToolExecutor / stream_session_chat 入口 / web_server SSE 边界），不在 chat / learning / teach 各自分支里复制。

## 本次实施

### 1. 结构化失败统一收口 — `learning_agent/agent/tool_executor.py`

- 新增 `_StructuredToolFailure` 内部异常类型，承载 `retryable / reason_code / message / payload` 四元组。
- 新增 `_detect_structured_failure(result)` 工具函数，统一识别两种历史 schema：
  - 嵌套：`{"error": {"code": ..., "message": ..., "retryable": ...}}`
  - 扁平：`{"error": "..."}`
- 在 `_execute_with_retry` 中，工具返回值落地后立即跑一次失败判定，命中则当作异常进入重试链，避免“返回值成功但语义失败”逃逸。
- 重试结束仍失败时：
  - 结构化失败保留 `dict` 原 payload，附带 reason_code，不再被 `f"[Error] ..."` 字符串吞掉。
  - 普通异常照旧字符串化，但同样落 `unresolved_failure_logger`。
- `execute_all` 的 `_execute_one` 套了内层 try/except + finally，确保 `obs.end_span` 不会因为异常路径漏调。

### 2. 失败账本 — `learning_agent/agent/unresolved_failure_logger.py`（新文件）

- JSONL sink，单文件 append。
- 写入走 `asyncio.to_thread`，锁保护，**绝不向主链抛异常**。
- 记录字段精简到：`ts / session_id / trace_id / tool / args(截断) / reason_code / message / retryable / where`。
- 主进程退出不需要清理；个人使用 N=1，文件直接落 `<data_dir>/unresolved_failures.jsonl`。

### 3. AgentLoop / SessionRuntime 透传 — `agent_loop.py` / `session_runtime.py`

- `AgentLoop.__init__` 新增可选 `unresolved_failure_logger` 参数，默认 `None`。
- `SessionRuntime` 在构造 `ToolExecutor` 时把 logger 透传下去。
- 没有 logger 时整个链条静默 no-op，不影响存量测试。

### 4. 最终回答保护 — `learning_agent/learning_agent/main.py`

- 引入 `SessionNotFoundError(LookupError)`，替代原来表达“session 不存在”的 `ValueError`，让 web 边界能用 404 区别于 500。
- 新增 `_SAFE_FALLBACK_ANSWER` 兜底文案。
- **重写 `stream_session_chat`**：把 teach_flow / _prepare_session_turn / agent_loop.run 三条原始分支封进内层 `_inner()` 异步生成器；外层统计 `visible_chars`。当 `_inner()` 结束后用户可见字符数为 0：
  - 调 `_build_rescue_answer(...)`：先尝试 LLM 改写（要求**禁止提及工具/错误/重试**），失败再退到静态兜底。
  - 把 rescue 文本作为正常 `ChatChunk` 推回前端，metadata 上挂 `rescue / rescue_reason / rescue_via`，供观测但不影响渲染。
  - 同步落 `unresolved_failure_logger`，标记 `where="stream_session_chat:empty"`。
- `_inner()` 内部 try/except 捕获所有异常，转换成空流 + rescue，不再向上抛。

### 5. SSE 边界守门 — `learning_agent/web/web_server.py`

- 导入 `SessionNotFoundError`，`_producer` 与非流式 `/chat` 都改成 `except SessionNotFoundError` 走 404 分支。
- `_producer` 的兜底 `except Exception`：
  - 不再吐 `{"error": ...}`。
  - 改成吐一个 rescue content payload：`{"content": "...", "rescue": True, "rescue_reason": ..., "rescue_via": "web_server_fallback"}`，前端拿到的仍是正常文本。
  - 同步写 `system.unresolved_failure_logger`。
- 非流式 `/chat` 的兜底分支同样改成 200 + rescue content，不再 500。
- 把 chunk metadata 里 main 层挂的 `rescue` 字段透传出去，便于前端 / 观测下游识别。

### 单工具 fallback（不在本次提交）

- `builtin_web_fetch.py` 的 archive.org 回退、`web_search_service.py` 的 query 降级也属于“成功导向”的一部分，但这两个文件与上一轮 web-search 工作深度混合（旧改动占绝大部分），无法干净切分。
- 为保持“一个 commit 一件事”，这两处单工具 fallback **不并入本次架构修复 commit**，随 web-search 那批改动一起提交。
- 本次架构修复只覆盖与模式无关的通用层（ToolExecutor / stream_session_chat / web_server SSE / 失败账本）。

## 验证

- `python3 -m pytest`：365 passed（与重构前一致，无回归）。
- 手写 5 项行为测试覆盖：
  - 结构化失败（nested + flat 两种 schema）能被识别并重试。
  - 重试穷尽后 payload 完整保留、reason_code 正确。
  - `stream_session_chat` 在 `_inner()` 全空时输出 rescue。
  - `_producer` 在最外层异常时输出 rescue content 而非 error。
  - `unresolved_failure_logger` 不向主链抛异常。

## 留心事项

- rescue 文案不要提工具 / 错误 / 重试，否则在前端会变成“穿帮”。
- `unresolved_failures.jsonl` 现在是 append-only 个人账本，不需要清理任务；体积异常时再单独处理。
- 本次只覆盖 P0；P1（provider fallback 模板化、可观测性指标）按方案文档继续推进。
