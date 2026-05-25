# 2026-05-25 L2 可复用断言模块 `tests/observability_asserts.py`

## 范围

设计文档 §3.5（设计中）→ 已落地。沉淀 4 个纯函数 helper 给所有读 L1 events.jsonl
的测试共用，对应设计文档 §10 步骤 10。

## 变更内容

### 新增 `tests/observability_asserts.py`

四个公共 helper（API 严格遵循 §3.5 spec）：

| Helper | 作用 | 失败行为 |
|---|---|---|
| `assert_event_present` | 在 events 里按 `type` / `payload_contains` / `visibility` / `parent_event_id` 找第一条匹配；返回命中事件 | 抛 `AssertionError` 并附「同 type 候选事件清单」单行摘要 |
| `assert_causal_chain_resolvable` | 校验每条带 `parent_event_id` 的事件：parent 存在 + 同 session_id + seq 严格小于 child | 抛错列出 orphan 与原因 |
| `assert_tool_exec_paired` | 校验 `tool.exec_started` ↔ `completed/failed`（同 `call_id`）配对 | 检出 unmatched / orphan close / duplicate close / duplicate started 四类违例 |
| `assert_visibility_isolation` | observability 事件不应泄漏到 projection messages | 强校验（`event_id` / `metadata.event_id` 字段）+ 弱校验（content 子串扫描）并行 |

### 新增 `tests/test_observability_asserts.py`（24 用例）

每个 helper 配 4–8 个用例，覆盖：
- 正路径（valid input passes）
- 各种违例分支（每种分支独立一个测试）
- 综合用例（在一份"真实形态"事件流上链式调用 4 个 helper）

### 文档

- `docs/design/design-observability-l1-l4-architecture.md` §3.5 由「设计中，待实现」改为「已落地」，附最终签名 + 测试位置 + 24 用例统计
- §7 总览表 L2 行去掉 "§3.5 可复用断言模块尚未沉淀" 的缺口标记
- §10 步骤 10 打勾，附后续整理思路（重构现有 `test_tool_exec_events.py` / `test_llm_exec_events.py` 调用新 helper）

## 设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 落地路径 | `tests/observability_asserts.py` | 用户明确指定；设计文档原写的 `tests/assertions/session_events.py` 只是"暂未创建"的占位 |
| 入参类型 | `Sequence[SessionEvent]` 而非 `list` | 允许传 tuple / 切片；只读语义 |
| `parent_event_id` 空串约定 | `""` 表示断言「无 parent」，`None` 表示「不约束」 | Pydantic 模型里 `parent_event_id: str \| None`，需要区分两种「无」的语义 |
| `assert_visibility_isolation` 校验强度 | 双层（强 + 弱）并行 | 强校验（event_id 字段）精确；弱校验（content 子串）抓 "payload 被 copy-paste 进 message" 这种粗暴泄漏。两个都过才放行 |
| `payload_contains` 匹配语义 | 子集匹配（k 必须存在且 v 必须严格等） | spec 写 "contains"，符合直觉；不做正则/类型宽松，避免误判 |
| 是否重构现有测试为调用新 helper | 不在本 PR 范围 | 控制 PR 大小；后续可见性收益不强（现有测试细粒度对应单一 emit helper） |
| 失败消息长度 | 最多列 5 条候选，超出加 `... and N more` | AI 友好但不刷屏 |

## 验证

| 验证 | 结果 |
|---|---|
| `pytest tests/test_observability_asserts.py -v` | 24/24 通过 |
| `pytest tests/test_session_projection.py tests/test_tool_exec_events.py`（回归） | 12/12 通过 |

## 关联

- 设计文档：`docs/design/design-observability-l1-l4-architecture.md` §3.5（API spec）、§7 / §7.1（状态总览）、§10 步骤 10
- 引用方（未来）：可替换 `test_tool_exec_events.py` / `test_llm_exec_events.py` / e2e scenarios 里的手写断言
- 分支：`feat/l2-observability-asserts`

## 不在本 PR 范围

- 重构现有 `test_tool_exec_events.py` / `test_llm_exec_events.py` 调用新 helper（后续整理）
- e2e scenarios 加 `assert_*` 调用（等 L2 模块在多个测试里被引用后看是否还需要做共享 fixture）
- §3.6 ratchet 字段（独立任务，需要 L2 输出聚合快照）
