# 2026-05-26 Final Answer Guard — "可用答案"判定 + 回放测试

## 承接

承接上一轮 `2026-05-26-tool-call-success-oriented-architecture-implementation.md`。
那一轮把 P0 架构骨架搭起来了：ToolExecutor 结构化失败收口、unresolved_failure_logger、
SSE 边界守门、main 层 `_inner()` + rescue 兜底。

实际使用中前端仍然漏出过几条原始失败串。本轮把"成功"重新定义。

## 问题：守门的"成功"判定太宽

上一轮 `stream_session_chat` 守门只检查 `visible_chars == 0`。但真实泄漏多数不是空：

| 真实事故 | 用户看到的内容 | `visible_chars == 0`？ |
|---|---|---|
| sess-8f458ab2 turn 8（web_fetch 连续失败） | `让我尝试其他来源：` | 否 — 9 字残句 |
| provider 400/auth | `\n[Error] LLM stream failed: 400 ...\n` | 否 — content 里就是 `[Error]` |
| SINGLE_PASS 直答异常 | `\n[Error] Unable to continue: ...\n` | 否 — 同上 |
| SSL 中断（短可见 + 错误） | `Python 3.12 引入了 PEP 695\n[Error] SSL ...\n` | 否 — 前面有真内容 |

四类都被守门放行，最后原样投递给前端。根因是把"流里产生了文本"等同于"用户拿到可用答案"。

## 这一轮做的事

### 1. 源头标记：错误 chunk 全部加 metadata

把所有"系统自己产生的错误 chunk"加上 `metadata={"stream_error": True, "stream_error_reason": ...}`，
让上层能识别并丢弃。共 5 处：

- `learning_agent/agent/react_engine.py`
  - context_limit 分支
  - AuthError / InvalidRequestError 分支
- `learning_agent/agent/session_runtime.py`
  - finalize 失败分支（`finalize_failed`）
  - 未捕获异常分支（`unhandled_error`）
  - single-pass 失败分支（`single_pass_failed`）

每个错误 chunk 仍按原来 yield，但带上身份标签。**不在源头吞**——保留给观测/调试，
也保留给后续重试逻辑判断；**只在守门处丢**。

### 2. 三档判定：`_classify_incomplete_answer` + `_final_answer_verdict`

`learning_agent/learning_agent/main.py` 新增两个纯函数。

`_classify_incomplete_answer(visible_text)`：
- 空字符串 / 仅空白 → `"empty_stream"`
- 短（≤ `_INCOMPLETE_MAX_LEN = 48` 字符）且以悬挂标点收尾（`：:，,、；;…—－-`）
  → `"incomplete_answer"`
- 否则 → `None`（可用）

`_final_answer_verdict(visible_text, stream_error_reason, inner_reason)`：
按保守优先序判定本轮是否需要 rescue：
1. 完全无可见输出 → `stream_error_reason` 或 `inner_reason` 或 `"empty_stream"`
2. 有可见输出但是残句 → `"incomplete_answer"`
3. 有可见输出 + 本轮发生过系统级流错误 + 可见内容很短（≤ `_PARTIAL_AFTER_ERROR_MAX_LEN = 120`）
   → `stream_error_reason`
4. 其余 → `None`（可用，不 rescue）

第 3 条带长度上限是关键校准：完整长答案末尾才出现一次延迟流错误，**不该**被叠 rescue
（修复过程中验证过的 false-positive 守卫）。

### 3. 守门重写：`stream_session_chat` chunk 过滤 + verdict

把 `_inner()` 的输出循环改成：

```python
visible_parts: list[str] = []
stream_error_reason: Optional[str] = None
stream_error_detail: Optional[str] = None

async for chunk in _inner():
    md = chunk.metadata or {}
    if md.get("stream_error"):
        if stream_error_reason is None:
            stream_error_reason = str(md.get("stream_error_reason") or "stream_error")
            stream_error_detail = (chunk.content or "").strip() or None
        continue                               # 不转发给上游
    if chunk.content:
        visible_chars += len(chunk.content)
        visible_parts.append(chunk.content)
    yield chunk

verdict = _final_answer_verdict(
    visible_text="".join(visible_parts),
    stream_error_reason=stream_error_reason,
    inner_reason=rescue_reason,
)

if verdict is not None:
    # 走 rescue：_build_rescue_answer + 落账本 rescue_used=True
elif stream_error_reason is not None:
    # 答案可用，但本轮出过错：落账本 rescue_used=False，便于后续优化
```

关键差异：
- 系统错误 chunk **从此不再投递给 web 上游**，前端永远拿不到 `[Error] ...`。
- "出过错但答案可用"也写账本，区别 `rescue_used=False`，给优化提供线索。

### 4. 回放测试 — `tests/test_final_answer_guard.py`（新增）

26 个用例，分 5 段：

| Section | 用例数 | 锁住的内容 |
|---|---|---|
| `TestClassifyIncompleteAnswer` | 11 | 残句判定边界（含 9 个 dangling 标点 + 长答案不误判 + `是的。` `Done.` 不误判） |
| `TestFinalAnswerVerdictLeakCases` | 4 | 4 条真实泄漏案例返回正确 `reason_code` |
| `TestFinalAnswerVerdictFalsePositiveGuards` | 5 | 长答案 + 末尾延迟错误、`是的。`、`inner_reason` 透传、空 + 无线索退化为 `empty_stream` |
| `TestStreamSessionChatGuard` | 4 | 4 条泄漏案例端到端：`[Error]` 不外泄 + rescue chunk + 账本 `rescue_used=True` |
| `TestStreamSessionChatNoFalsePositive` | 2 | 完整长答案 + 延迟错误不触发 rescue（但账本仍写 `rescue_used=False`）；正常答案完全不写账本 |

集成测试用 `LearningAgentSystem.__new__` + `MagicMock` 桩，monkey-patch
`_prepare_session_turn` / `agent_loop.run` / `_build_rescue_answer` / `unresolved_failure_logger`，
直接驱动 `stream_session_chat`。不引入新 fixture。

## 验证

- `python3 -m pytest tests/test_final_answer_guard.py` → 26 passed
- `python3 -m pytest` 全量 → 393 passed（上一轮基线 365 + 新增 26 + 其它增量 2，零回归）

## 留心事项

- **不可放宽 `_INCOMPLETE_MAX_LEN`（当前 48）**：放宽会把长答案末尾的合法冒号（"参考链接："）误判成残句。
- **不可放宽 `_PARTIAL_AFTER_ERROR_MAX_LEN`（当前 120）**：放宽会把完整长答案 + 末尾延迟流错误误判成片段。
- **rescue 文案约束依旧**：不出现"错误 / 重试 / 工具"字样，否则前端穿帮。
- 不在本轮提交：`extensions/web_search_tools.py` / `services/web_search_service.py` /
  `providers/*` / `tests/test_web_search_tools.py` 是上一批 web-search 工作的未提交改动，
  和本次架构修复无关，分开提交。
