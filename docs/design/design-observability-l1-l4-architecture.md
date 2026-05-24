# 可观测系统 L1-L4 分层架构

> **状态**：设计阶段，已对齐核心框架，L1 改造待动手
> **作者**：与 Claude 协同设计
> **关联文档**：`docs/TESTING.md`、`docs/design/design-learning-agent-observability.md`、`docs/design/design-session-event-log-single-source-refactor.md`、`AGENTS.md`
> **最后更新**：2026-05-24

---

## 〇、为什么有这份文档

### 0.1 起因

用户的目标是：「**验证当前项目中的某个功能是否可用、是否达到预期；如果有问题，是哪一步出现了问题。**」

现状的痛点：
- `.observability/events.jsonl` 只存 `payload_keys`，丢失诊断价值
- `.observability/flow_sess-*.json` 100+ 文件无清理
- `.observability/audit.jsonl`、`errors.jsonl` 是并行流，破坏「单一事实源」约束
- 没有「失败时按时间轴看完整因果链」的能力
- 测试通过 ≠ 质量好（compaction dataset 测试全绿但 drift_score 才 16.7/100）

用户决定**整套观测系统重做**。L1-L4 框架就是这次重做的总图。

### 0.2 框架本质

**「一份事实源，三只眼。」** Three Eyes, One Truth。

同一份 append-only JSONL 事件流，**喂给三个不同读者**：
- 机器（断言）
- 个人（报告）
- 团队（看板）

每个读者只负责自己的视角，不污染上游。

---

## 一、四层定义（核心）

```
┌─────────────────────────────────────────────────────────┐
│  L4  CI / 看板         (绿灯红灯 + 趋势图)              │ ← 团队读
├─────────────────────────────────────────────────────────┤
│  L3  报告器             (HTML / Markdown / dashboard)   │ ← 个人读
│      - dataset report                                    │
│      - per-session timeline                              │
│      - quality dashboard                                 │
├─────────────────────────────────────────────────────────┤
│  L2  断言器             (pytest / invariants)            │ ← 机器读
│      - hard assertions                                   │
│      - ratchet thresholds                                │
├─────────────────────────────────────────────────────────┤
│  L1  事件流             (append-only JSONL)              │ ← 唯一事实源
│      - session events                                    │
│      - test artifacts                                    │
│      - production traces                                 │
└─────────────────────────────────────────────────────────┘
```

### 1.1 总览表

| 层 | 角色 | 读者 | 输入 | 输出形态 | 任务 |
|---|---|---|---|---|---|
| **L1** | 事件流 | —（只写） | — | append-only JSONL | 保证完整 payload；唯一写入入口；replay 幂等 |
| **L2** | 断言器 | 机器 | L1 | pass/fail 信号 | pytest / 不变量 / ratchet 阈值 → 红绿 |
| **L3** | 报告器 | 个人 | L1 | HTML / Markdown | 一份「这次发生了什么」的可读视图 |
| **L4** | 看板 | 团队 | L2/L3 多次结果 | 趋势图 + 红绿灯 | 跨次回归提示、质量趋势 |

### 1.2 三条不可妥协的原则

1. **L1 是唯一写入层**——L2/L3/L4 都不写 L1
2. **数据单向**——L1 → L2/L3 → L4，不反向
3. **L1 schema 不区分来源**——测试事件、生产事件、e2e 跑出来的事件，**同一种文件、同一套字段**

第 3 条是这个框架最有价值的发现：**生产失败时把那份 events.jsonl 拿走，灌进 L2 就能复现，喂给 L3 就能肉眼看**。这就是用户最想要的「哪一步出错」的可达路径。

---

## 二、L1：事件流（事实源）

### 2.1 物理位置

```
<base_dir>/sessions/<session_id>.events.jsonl
```

- `base_dir` 默认 `.learning_agent_data/`，可通过 `LearningAgentSystem.config.data_dir` 配置（见 `learning_agent/main.py:70`）
- **扁平文件**，不是子目录——每个 session 一个 `.events.jsonl` 躺在共享 `sessions/` 下
- 实例：`/Users/roseannk/my-agent/.learning_agent_data/sessions/sess-68b7bf8b.events.jsonl`

### 2.2 唯一写入入口

```python
FileStore.append_session_event(session_id, event)
  └─ self.append_jsonl(f"sessions/{session_id}.events.jsonl", event)
```

定义在 `learning_agent/ai/file_store.py:108`。**全项目只有这一处可以往 L1 写**——这是不变量。

### 2.3 Schema（现状 + 扩展）

#### 现有业务事件
```json
{
  "seq": 42,
  "event_id": "evt-17ad099eaeca",
  "session_id": "sess-68b7bf8b",
  "ts": "2026-05-19T16:44:29.944065Z",
  "type": "session.created",
  "payload": { /* 完整内容 */ },
  "visibility": "system"
}
```

#### 新增 trace 事件（待落地）
```json
{
  "seq": 43,
  "event_id": "evt-...",
  "session_id": "sess-...",
  "ts": "...",
  "type": "tool_call.started",
  "payload": {
    "tool_name": "read_file",
    "args": { /* 完整 args，不存 keys */ },
    "call_id": "call-..."
  },
  "visibility": "trace",          // ← 新取值
  "parent_event_id": "evt-..."    // ← 新字段
}
```

### 2.4 字段语义

| 字段 | 类型 | 必填 | 语义 |
|---|---|---|---|
| `seq` | int | ✓ | 单调递增序号（per session） |
| `event_id` | str | ✓ | 全局唯一事件 id，格式 `evt-<hex>` |
| `session_id` | str | ✓ | 所属 session |
| `ts` | ISO8601 | ✓ | 事件时间戳 |
| `type` | str | ✓ | 事件类型，命名规范：`domain.action_pastTense` |
| `payload` | object | ✓ | 事件完整数据，**不存 keys 存 value** |
| `visibility` | enum | ✓ | `system` / `user` / `trace` |
| `parent_event_id` | str | trace 必填 | 指向触发本事件的上一事件 id，构成因果链 |

### 2.5 `visibility` 语义重定义

| 取值 | 语义 | 是否参与 replay |
|---|---|---|
| `system` | 业务事件，系统侧（如 `session.created`） | **是** |
| `user` | 业务事件，用户侧（如 `message.user_appended`） | **是** |
| `trace` | 诊断事件，纯观测用途 | **否** |

**关键不变量**：`replay_events` / `session_projection` / 各 `_apply_*` 路径**必须**显式过滤 `visibility ∈ {system, user}`，**绝对不能**让 trace 事件进入 state 计算。

### 2.6 第一批新增 trace 事件类型

```
tool_call.started      { tool_name, args, call_id }
tool_call.completed    { tool_name, call_id, latency_ms, result, result_size }
tool_call.failed       { tool_name, call_id, error_type, error_message, traceback }
```

第二批（待 L1 闭环跑通后再做）：
```
llm.request_sent       { model, message_count, prompt_tokens, params }
llm.response_received  { model, latency_ms, tokens_in, tokens_out, content, finish_reason }
llm.failed             { error }
hook.fired             { hook_name, target_event_type }
compaction.started     { reason, source_event_count, budget }
compaction.completed   { summary_event_id, reduction_ratio }
```

### 2.7 当前 base_dir 实际状态（2026-05-24）

```
.learning_agent_data/sessions/
├── sess-1577a3c3.events.jsonl    3.0 KB
├── sess-3638e1b4.events.jsonl    287 KB
├── sess-43e454a7.events.jsonl    424 KB
├── sess-5536264e.events.jsonl    424 KB
├── sess-682a9b22.events.jsonl    293 KB
├── sess-68b7bf8b.events.jsonl    4.2 KB
└── ...                           # 共 16 个文件，1.7 MB 总计
```

---

## 三、L2：断言器（机器读）

### 3.1 角色

把 L1 事件流变成 **pass/fail** 信号。**机器读，不输出给人**（产物只有 exit code + 错误消息）。

### 3.2 当前 L2 资产

| 资产 | 位置 | 类型 |
|---|---|---|
| compaction dataset hard assertions | `tests/test_compaction_dataset_runner.py` | 硬断言 |
| compaction dataset ratchet 棘轮 | 同上，`_assert_expectations()` | min_drift_score / max_warning_count |
| e2e scenario assertions | `tests/e2e/runner.py` (CHECKERS 字典) | `event_count_min` / `event_contains` / `no_error` |
| 共享 fixture | `tests/conftest.py` | `scripted_provider` 等 |
| pytest marker | `pytest.ini` | unit / integration / e2e / slow / live_llm 等 |

### 3.3 L2 设计原则

1. **输入只读 L1**：从 `file_store.read_session_events(session_id)` 拿事件
2. **不裸 mock**：外部依赖（LLM、文件系统）走 `tests/conftest.py` 共享 fixture
3. **失败要落证据**：失败时把事件流拷到 `.test_artifacts/<test>/events.jsonl`，方便离线回放
4. **棘轮模式**：质量阈值只能升不能降（已在 compaction dataset 用上）

### 3.4 不变量测试清单（已落地）

按「事实源保护」「因果链完整」「容错」三类汇总：

| 类别 | 测试 | 文件 | 锁定的不变量 |
|---|---|---|---|
| 事实源 | `test_replay_ignores_observability_events` | `tests/test_session_projection.py` | `visibility=observability` 事件占 seq 不进 messages |
| 事实源 | `test_replay_filters_business_event_types_marked_observability` | 同上 | 业务类型 + observability visibility 也被过滤 |
| 因果链 | `test_emit_exec_started_writes_observability_event_with_args` | `tests/test_tool_exec_events.py` | started payload/visibility 字段正确 |
| 因果链 | `test_emit_exec_completed_chains_to_started_event` | 同上 | completed.parent_event_id 指向 started.event_id |
| 因果链 | `test_emit_exec_failed_carries_error_type_and_message` | 同上 | failed 携带 error_type/error_message |
| 容错 | `test_emit_helpers_no_op_when_writer_is_none` | 同上 | 未注入 writer 时 helpers 静默 no-op |
| 容错 | `test_emit_helpers_swallow_writer_exception` | 同上 | writer 抛异常被吞掉，业务不受影响 |
| 容错 | `test_safe_result_repr_truncates_long_payload` | 同上 | tool result 巨大时 L1 文件不会膨胀 |

第一批护栏遵循「先红再绿」原则——OBSERVABILITY 过滤器在 `session_projection.replay_events` 中显式 `continue`，
就靠 `test_replay_filters_business_event_types_marked_observability` 锁住「过滤器真的在工作」。

### 3.5 可复用断言模块（设计中，待实现）

L2 层将沉淀一组**纯函数**断言 helper，给所有读 L1 events.jsonl 的测试共用。
**只规定 API，不在本次落地实现**：

```python
# 期望路径：tests/assertions/session_events.py（暂未创建）

def assert_event_present(
    events: list[SessionEvent],
    *,
    type: str,
    payload_contains: dict[str, Any] | None = None,
    visibility: str | None = None,
) -> SessionEvent: ...
    """在 events 里找第一条满足条件的事件；找不到 → AssertionError 并附上候选清单。"""

def assert_causal_chain_resolvable(events: list[SessionEvent]) -> None: ...
    """对每条带 parent_event_id 的事件，校验 parent 在同 session 内可解析。"""

def assert_tool_exec_paired(events: list[SessionEvent]) -> None: ...
    """每个 tool.exec_started 必须跟着一个 tool.exec_completed 或 tool.exec_failed（同 call_id）。"""

def assert_visibility_isolation(events: list[SessionEvent], projected_messages: list[Any]) -> None: ...
    """observability 事件不应出现在 replay 重建的 messages 里。"""
```

设计要点：
- 输入只接 `list[SessionEvent]`，不带 IO；测试自己负责加载
- 失败消息要够 AI 友好——附上「最接近的几条候选事件」摘要，方便复制给模型诊断
- 不强制返回值，但 `assert_event_present` 返回命中事件以便链式断言
- 这些 helper 共享 `tests/render_session_timeline.py` 的 visibility / type 语义，**保证 CLI / Web / 测试三处对「事件长什么样」的理解一致**

### 3.6 棘轮字段（设计中，待实现）

L4 的趋势图需要 L2 输出**棘轮快照**才能画。ratchet 字段（仅记录字段名，不实现）：

| 字段 | 单位 | 来源 | 棘轮方向 |
|---|---|---|---|
| `min_drift_score` | int 0-100 | `tests/test_compaction_dataset_runner.py` | 单调升（不允许跌） |
| `max_warning_count` | int | 同上 | 单调降（不允许涨） |
| `tool_exec_failed_rate` | float 0-1 | 聚合 `tool.exec_failed` / `tool.exec_started` 比 | 单调降 |
| `p99_latency_ms` | int | 聚合 `tool.exec_completed.payload.latency_ms` | 单调降 |

落地策略与 §5.5 的 `ratchet.json` 一一对应。

---

## 四、L3：报告器（个人读）

### 4.1 角色

把 L1 事件流变成 **人能读懂**的视图：HTML / Markdown / dashboard。给**单个工程师**看「这次发生了什么」。

### 4.2 当前 L3 资产

| 资产 | 位置 | 状态 |
|---|---|---|
| compaction dataset HTML 报告 | `tests/render_compaction_dataset_report.py` | ✓ 已有，手动跑 |
| per-session timeline viewer | — | ✗ **最大缺口** |
| LLM 调用详情视图 | — | ✗ 缺 |
| tool_call 因果树视图 | — | ✗ 缺 |

### 4.3 L3 设计原则

1. **永远从 L1 取数据**，不维护独立状态
2. **测试和生产共用**：同一个 timeline viewer 既能看 e2e 测试跑出的 events.jsonl，也能看生产 session 的 events.jsonl
3. **可链接到 L1 事件 id**：报告里每个项都能 hover 出对应的 raw event
4. **可离线**：报告生成后，没有运行时依赖就能打开看

### 4.4 per-session timeline viewer（已落地）

**单一渲染逻辑、两个加载源**——CLI 离线渲染器与 Web UI 共用同一份 events.jsonl，
区别只在于「从文件读」还是「从 HTTP 拉」。

#### 离线 CLI 渲染器

`tests/render_session_timeline.py`：

```bash
python3 tests/render_session_timeline.py <events.jsonl> --out <out.html>
```

输出独立 HTML（CSS 内联、无运行时依赖），包含：
- 顶部：session_id、事件总数、起止时间、visibility 分布
- 主体：按 `seq` 升序排列的事件列表，每行 `#seq · ts · type · visibility-chip · [↳ parent #N]`
- `visibility` 颜色区分：agent=蓝、system=灰、observability=橙、ui=青
- 失败事件（`tool.exec_failed` / payload 含 `error_type`）整行红色高亮
- 点击事件展开完整 payload（CSS `<details>`，无 JS 依赖）
- **parent 解析**：先按 `event_id` 索引一遍，遇到带 `parent_event_id` 的行画一根
  `↳ parent #N` 角标，**flat list**（不是树、不折叠）

#### Web UI

`web/observability.html` + `web/static/observability.js`：

- 左侧 session 列表来自 `GET /sessions`
- 选中后调 `GET /sessions/{id}/events?limit=5000` 一次性拉全量（v1 不分页）
- visibility 三个 chip 默认全开，点击 toggle
- type 文本框：子串匹配，前端过滤
- 事件行点击展开 payload（与离线渲染器同款 CSS）
- 失败事件红色高亮
- **过滤后 parent 不在视图内**：渲染 `↳ parent #N (hidden by filter)` 灰色 stub，
  避免用户以为因果链断了
- **不引入**：React / 构建工具 / SSE。纯 vanilla JS。

### 4.5 输出落点

```
# 测试产物 / 失败证据
.test_artifacts/<test_name>/
├── events.jsonl          # ← L1（落证据）
└── timeline.html         # ← 用 tests/render_session_timeline.py 渲染

# 离线分析（手动跑、临时分析用）
/tmp/<session_id>.timeline.html
```

生产环境的 timeline 不落盘——由 Web UI 即时渲染 `<base_dir>/sessions/<id>.events.jsonl`。
**不再需要 `.observability/reports/` 这种独立目录**（旧设计的产物，已下线）。

| L3 资产 | 路径 |
|---|---|
| compaction dataset 报告 | `tests/render_compaction_dataset_report.py` |
| per-session timeline 离线渲染器 | `tests/render_session_timeline.py` |
| Web UI 入口 | `web/observability.html` |
| Web UI 逻辑 | `web/static/observability.js` |
| 后端 API | `GET /sessions/{id}/events`（见 `learning_agent/web/web_server.py`） |
| 共享过滤函数 | `learning_agent/learning_agent/session_event_store.py::filter_events` |

### 4.6 CLI 与 Web 的「同源」保证

两条路径都读 L1 同一份 events.jsonl，**用同一套字段语义**（visibility、type、
parent_event_id、payload），渲染层用相同的 CSS 类名（`event-row` / `vis-chip` /
`parent-link` / `is-failure`）。意义在于：

- 一份失败的 `.test_artifacts/.../events.jsonl` 拿到本地，开 CLI 渲染就能离线复盘
- 同一份 events.jsonl 灌进生产数据目录，Web UI 就能看
- 未来如果想给「测试失败时自动生成 timeline 链接发到 Slack」这种功能，CLI 直出 HTML 就够用

**唯一允许的差异**：Web UI 加 visibility/type 过滤（交互性需求），CLI 全量渲染（静态产物）。
其它任何字段、颜色、布局差异都属于 bug。

---

## 五、L4：看板（团队读）

### 5.1 角色

把**多次执行**的 L2/L3 结果聚合，给团队看：CI 红绿灯、跨次趋势、质量回归提示。

### 5.2 当前 L4 资产

**几乎为零**。这是最不急但中期收益最大的一层。

### 5.3 L4 设计原则

1. **不读 L1 raw**——读 L2 的 pass/fail 记录 + L3 的报告元数据
2. **历史可追**：保留 N 次跑的结果，画趋势
3. **回归立刻可见**：drift_score 跌、新 warning 出现，看板第一时间红

### 5.4 候选视图（设计中）

每个视图都列「数据来源」「核心字段」「频率」，便于实现时直接对照。

| 视图 | 数据来源 | 核心字段 | 频率 |
|---|---|---|---|
| pytest 红绿灯 | CI 每次跑的 exit code | `passed`/`failed`/`skipped` 计数、提交 SHA、耗时 | 每次 PR |
| drift_score 趋势 | 历次 `tests/.../compaction_dataset_runs/<mode>/<utc_ts>/index.json` | `average_drift_score`, `severity_counts`, `top_recurring_warnings` | 每次 PR |
| tool 失败率 | 聚合 `.learning_agent_data/sessions/*.events.jsonl` 中的 `tool.exec_started` vs `tool.exec_failed` | `started_count`, `failed_count`, `fail_rate`, top `error_type` 分布 | 日级 |
| 慢调用 Top N | 聚合 `tool.exec_completed.payload.latency_ms` | `tool_name`, `latency_ms`, `session_id`, `seq` | 日级 |
| OBSERVABILITY 覆盖率 | 检查每次 `_execute_with_retry` 是否都伴随 `tool.exec_*` 三事件之一 | `coverage_ratio`（防止哪天 helper 被吞掉无人察觉） | 每次 PR |

### 5.5 文件布局（设计中）

L4 是「多次跑结果的聚合」，每次 CI / 每个分支都写一份独立目录，绝不就地覆盖：

```
.test_artifacts/runs/<utc_ts>/         # 一次 CI 运行的全部产物
├── summary.json                       # pytest 红绿汇总 + git SHA + 时长
├── ratchet.json                       # 各项阈值快照（drift_score / fail_rate / p99_latency）
├── tool_exec_stats.json               # 按 tool_name 聚合的成功/失败/p50/p99
├── slow_calls.json                    # latency_ms top N
└── dashboard.html                     # 自包含的静态可分享报告

.test_artifacts/runs/_index.json       # 跨次索引：[<utc_ts>] → ratchet 快照，给趋势图用
```

读者：`dashboard.html` 是给团队看的最终产物；`_index.json` 是给未来 L4 趋势渲染器用的。
**L4 永远不读 L1 raw**——上面 5 个 JSON 都是 L2 聚合产出。

### 5.6 L4 启动门槛

**L1 + L3 闭环跑顺之后才动 L4**。原因：
- 没数据之前画看板等于自欺欺人
- L1 schema 还在改，L4 现在做都是重做
- L3 现已落地（§4.4、§4.5），但「多次跑」的样本量还不足以画趋势；至少要积累
  一周以上的 daily run + 5 次以上的 PR run 再启动 L4 工作

---

## 六、贯穿四层的硬约束

### 6.1 不变量清单

1. **L1 唯一写入**：只有 `FileStore.append_session_event` 可以写 events.jsonl
2. **L1 不区分来源**：test / e2e / 生产事件，schema 完全相同
3. **数据单向**：L1 → L2/L3 → L4
4. **L4 不读 L1 raw**：L4 读 L2/L3 聚合产物
5. **append-only**：events.jsonl 只追加，绝不修改/删除（AGENTS.md 已有约束）
6. **replay 幂等**：从 L1 完全重建任何 L2/L3 状态
7. **trace 不入 state**：`visibility=trace` 事件不参与 replay/projection（必须有不变量测试锁住）

### 6.2 这些约束怎么落实

- 写代码护栏：单一写入入口 + 显式 visibility 过滤
- 写测试护栏：`test_replay_ignores_trace_events` 是第一个，后面应该有更多
- 写文档护栏：本文件 + `AGENTS.md` 的「事实源约束」
- code review 时把这份不变量清单当 checklist

---

## 七、当前状态总览（2026-05-24）

| 层 | 现状 | 缺口 |
|---|---|---|
| **L1** | events.jsonl 已有，完整 payload，per-session；**`visibility=observability` + `parent_event_id` 已启用**；**第一批 trace 事件 `tool.exec_started/completed/failed` 已接入**；**旧 `.observability/` 并行流（events/errors/audit/flow/trace/snap）已全部下线**；**新 API `GET /sessions/{id}/events` 已上线（支持 `visibility` / `type` / `after_seq` / `limit` 过滤，5000 上限，返回 `next_after_seq` 作为轮询游标）** | 第二批 trace 事件（`llm.*`、`hook.*`、`compaction.*`）批量接入待办 |
| **L2** | compaction hard assertions + ratchet ✓；e2e scenarios ✓；ScriptedProvider ✓；**`test_session_projection.py` 含 OBSERVABILITY 过滤护栏 ✓**；**`test_tool_exec_events.py` 含 tool.exec_* 因果链护栏 ✓** | §3.5 可复用断言模块（`assert_event_present` / `assert_causal_chain_resolvable` / `assert_tool_exec_paired` / `assert_visibility_isolation`）尚未沉淀；§3.6 ratchet 字段（drift_score、tool_exec_failed_rate、p99_latency_ms 等）尚未实现 |
| **L3** | compaction dataset HTML 报告 ✓；**per-session timeline 离线渲染器 `tests/render_session_timeline.py` 已上线 ✓**；**Web UI `/ui/observability.html` 重写完成（vanilla JS、无构建、无 SSE）✓**；**CLI 与 Web 共用同一份 events.jsonl 与 `filter_events` 纯函数 ✓** | 多次跑对比视图、SSE 实时尾随、跨 session 聚合（v2 再说） |
| **L4** | 几乎为零 | §5.4 候选视图、§5.5 文件布局 `.test_artifacts/runs/<utc_ts>/` 都还是设计；至少积累一周 daily run 数据再动手 |

### 7.1 L1 已落地的实际改动（2026-05-24）

| 资产 | 路径 | 作用 |
|---|---|---|
| 新事件类型常量 | `learning_agent/learning_agent/session_events.py` `SessionEventType.TOOL_EXEC_STARTED/COMPLETED/FAILED` | 与业务 `TOOL_CALL_*` 区分：前者是 OBSERVABILITY，后者是 AGENT |
| `parent_event_id` 字段 | `SessionEvent.parent_event_id: str \| None` + `make_session_event(parent_event_id=...)` | 因果链字段，trace 事件必填，业务事件留 None |
| OBSERVABILITY 取值 | `EventVisibility.OBSERVABILITY = "observability"`（文档中也称 trace） | replay/projection 必须过滤 |
| 过滤护栏 | `session_projection.replay_events` 在 seq 校验之后、`_apply_event` 之前显式 `if event.visibility == OBSERVABILITY: continue` | L1 事实源不被诊断事件污染 |
| L1 写入扩展 | `SessionEventStore.append_event(..., parent_event_id=None)` | 唯一写入入口扩展支持因果字段 |
| Runtime 端口 | `runtime_ports.SessionEventWriter` Protocol | Runtime 依赖抽象，Product 层 `SessionEventStore` 自动满足 |
| Tool trace 发射 | `ToolExecutor._emit_exec_started/completed/failed` | 在 `_execute_with_retry` 实际工具调用前后落 L1，每次重试都有独立 started→completed/failed 对，completed/failed.parent = 对应 started.event_id |
| 端到端串联 | `LearningAgentSystem` → `AgentLoop(event_writer=session_event_store)` → `AgentLoopSession` → `ToolExecutor(event_writer=...)` | 生产路径自动启用；event_writer 默认 None 时整条链路 no-op |
| 容错性 | helpers 内部 try/except，writer 抛异常只 warning 不影响业务 | L1 写入失败不能影响工具执行 |

### 7.2 已写入的护栏测试

| 测试 | 文件 | 锁定的不变量 |
|---|---|---|
| `test_replay_ignores_observability_events` | `tests/test_session_projection.py` | OBSERVABILITY 事件占据 seq 但不进 messages，state 与无 trace 版本完全等价 |
| `test_replay_filters_business_event_types_marked_observability` | 同上 | **业务类型 + OBSERVABILITY visibility 也必须被过滤**（真正验证过滤器有效） |
| `test_emit_exec_started_writes_observability_event_with_args` | `tests/test_tool_exec_events.py` | started 事件 payload/visibility 字段正确 |
| `test_emit_exec_completed_chains_to_started_event` | 同上 | completed.parent_event_id = started.event_id（因果链） |
| `test_emit_exec_failed_carries_error_type_and_message` | 同上 | failed 携带 error_type/error_message，挂在 started 之下 |
| `test_emit_helpers_no_op_when_writer_is_none` | 同上 | 未注入 writer 时 helpers 静默 no-op |
| `test_emit_helpers_swallow_writer_exception` | 同上 | writer 抛异常被吞掉，业务不受影响 |
| `test_safe_result_repr_truncates_long_payload` | 同上 | L1 文件不会因 tool result 巨大而膨胀 |

### 7.3 真实 LLM + 工具的端到端验证（2026-05-24）

驱动脚本：[`scripts/drive_l1_real_turn.py`](../../scripts/drive_l1_real_turn.py)（一次 Moonshot Kimi 真实调用，读 read_file 工具）。

实际跑出的单 session events.jsonl 时间线：

```
seq=1   session.created          (system)
seq=2   session.mode_changed     (agent)
seq=3   message.user_appended    (agent)         ← 用户问题
seq=4   message_end              (agent)         ← LLM 第一轮：决定调工具
seq=5   tool.exec_started        (observability) parent=—
seq=6   tool.exec_completed      (observability) parent=evt-5635f5e9c88a ← 因果链回指 seq=5
seq=7   tool.call_completed      (agent)         ← 业务侧工具完成
seq=8   message_end              (agent)         ← LLM 第二轮：用工具结果生成回答
```

证实的不变量：
- **「哪一步出错」可达**：从 seq=3 到 seq=8 一份文件讲清楚「用户问 → LLM 决定调工具 → 工具执行 → LLM 出最终答」整条链路
- **因果链工作**：completed.parent_event_id 正确指回 started.event_id
- **OBSERVABILITY 过滤生效**：replay 重建 4 条 messages，0 corrupt event，0 observability 泄漏进 messages
- **业务/诊断双轨并存**：业务 `tool.call_completed`（agent）和诊断 `tool.exec_started/completed`（observability）共存于同一时间轴，互不污染

另一个值得记录的发现：当 `tool_guard` 在 hook 层拦截工具（HookDecision.DENY，路径越界）时，执行没有进入 `_execute_with_retry`，所以 `tool.exec_*` 事件**不会**发射，只有业务侧的 `tool.call_failed` 出现。这是预期行为——`tool.exec_*` 描述「实际工具执行的时序」，hook 拦截属于「执行前决策」，应该由未来的 `hook.fired` / `hook.denied` 这类事件覆盖（第二批 trace 事件，见第十节）。

### 待砍掉的旧资产 — **2026-05-24 已全部下线**

| 旧资产 | 状态 |
|---|---|
| `.observability/events.jsonl` | ✓ 已删除（写入路径同步去除） |
| `.observability/errors.jsonl` | ✓ 已删除 |
| `.observability/audit.jsonl` + `core-security-audit` 扩展 | ✓ 扩展文件 `security_audit.py` 已删除 |
| `.observability/flow_sess-*.json` + `core-fulltrace` 扩展 | ✓ 已从 `built_in.py` 移除 |
| `.observability/trace_*.json` / `snap_*.json` | ✓ `ObservabilityCollector._persist_*` 已删 |
| `Config.observability_dir` / `LA_OBS_DIR` 环境变量 | ✓ 已下线 |
| `web_server.py` 的 9 个 `/observability/*` 端点 | ✓ 整组删除（前端不再依赖） |
| `LearningAgentSystem._delete_session_observability_files` | ✓ 已删除 |
| 全局 EventBus `subscribe("*", observability.on_event)` 持久化订阅 | ✓ 已解除 |
| `.observability/` 物理目录 | ✓ 已 `rm -rf` |

保留下来的可观测性能力：

- `ObservabilityCollector` 类本身（纯内存版本）—— `MetricsStore` 计数 / Gauge / Histogram、按 session 隔离的 Trace/Span 栈，仍由 `ToolExecutor` / `SessionRuntime` 通过 `if self.obs:` 调用
- `TraceSpan` / `Trace` / `Snapshot` 类型定义在 `learning_agent/ai/models.py`
- `MEM-METRICS` 通过 `LearningAgentSystem.observability.get_metrics_summary()` 仍可被 CLI `/metrics` 命令读取（CLI 路径未受影响）

砍掉策略的执行路径与第九节的「三段式」一致，已直接走完阶段 3，因为前端已不再读 `.observability/`。

---

## 八、决策记录（DR）

### DR-1：单文件 vs 兄弟文件（合并 vs 分离）

**结论**：合并到单文件 `sessions/<id>.events.jsonl`，用 `visibility` 字段分层。**不**新建 `trace.jsonl`。

**理由**：
1. `visibility` 字段本来就在 schema 里，是为分层可见性设计的
2. 单时间轴对调试无可替代——「哪一步出错」需要同一份文件能 grep 出全部
3. 物理隔离的风险可用代码护栏替代（不变量测试 + 单一写入入口）
4. L3 timeline 本来就要 merge 多源数据，物理在一起 vs 分开不影响 L3 复杂度

**讨论过的备选**：
- 兄弟文件 `sessions/<id>.trace.jsonl` —— 物理隔离最安全，但代价是 L3 始终要 join 两份
- 同文件用事件类型前缀 `trace.*` —— 没有 `visibility` 字段干净

### DR-2：观测系统重做范围

**结论**：`.observability/` 整套删除，重做。前端切到新流。

**理由**：用户明确表态「整个观测系统全部删了都没问题」。唯一约束是前端连贯性，用阶段式过渡解决。

### DR-3：第一批 trace 事件选 tool_call

**结论**：只先接 `tool_call.started` / `completed` / `failed`，不一次性加完。

**理由**：
1. 用户痛点「哪一步出错」最常发生在工具调用
2. 三个事件够验证整套模式（schema + parent_event_id + 不变量）
3. 模式验证完再批量加 llm.* / hook.* / compaction.* 是低风险扩展

### DR-4：L1-L4 按读者分层，不按数据流分层

**结论**：L1=事件源，L2=机器读（断言），L3=人读（报告），L4=团队读（看板）。

**理由**：之前曾提议过「L2=派生视图」的分法，但派生视图其实是每层自己的内部计算，不是独立一层。按读者切分每层有明确 output 形态，更可执行。

---

## 九、迁移策略（前端连贯性）

### 9.0 执行状态（2026-05-24 更新）

**三阶段一次性走完**。原计划的「双写过渡 → 灰度切换 → 删除旧流」简化为单次切除，因为用户确认前端已不再依赖任何 `/observability/*` 端点，没有兼容性负担。具体见 §7「待砍掉的旧资产」表。

### 9.1 三段式（保留作为历史参考）

| 阶段 | 后端动作 | 前端状态 | 风险 |
|---|---|---|---|
| **阶段 1** | 新事件写入 `sessions/*.events.jsonl`；`.observability/` **保留双写** | 仍读旧路径 | 低（双写冗余但前端零影响） |
| **阶段 2** | `web_server` 加新 API：`/api/sessions/{id}/events`（支持 visibility 过滤）；前端切到新 API | 灰度新旧并存 | 中（前端需配合改造） |
| **阶段 3** | 删除 `.observability/` 所有写入代码 + 旧文件 + 旧 API | 完全切到新流 | 低（前端已不依赖旧路径） |

### 9.2 阶段 1 的「双写」不可省

**理由**：前端不停服。阶段 1 的代价是后端短期容忍冗余写入，换前端零中断。

### 9.3 阶段 3 触发条件

- 所有前端页面已切到新 API
- 至少一周观察期无前端报错
- 新 API 的 L2 监控（错误率、延迟）正常

---

## 十、下一步行动顺序

### 短期（L1 落地，1-2 次工作）—— **已完成**

1. ✅ **读代码摸底**：`learning_agent/agent/observability.py`、`learning_agent/web/web_server.py`、全项目 `visibility=` grep —— 完成
2. ✅ **写护栏测试**：`test_replay_ignores_observability_events` 已写入 `tests/test_session_projection.py`
3. ✅ **加 schema 字段**：`SessionEvent.parent_event_id: str | None` + `make_session_event(parent_event_id=...)` 已上线
4. ✅ **接入第一批事件**：`ToolExecutor._emit_exec_started/completed/failed` 在 `_execute_with_retry` 前后落 L1
5. ✅ **跑 e2e scenario 验证**：`scripts/drive_l1_real_turn.py` 能从 user.appended 一路看到 tool.exec_completed/failed 的因果链

### 中期（L3 落地，3-5 次工作）—— **本轮完成**

6. ✅ **per-session timeline viewer**：`tests/render_session_timeline.py` 已上线，输入 events.jsonl 输出独立 HTML，flat list + 因果链 `↳ parent #N` + 失败红高亮
7. ✅ **测试和生产共用**：同一份 events.jsonl 文件，CLI（`render_session_timeline.py`）与 Web（`/ui/observability.html` → `GET /sessions/{id}/events`）共用 `filter_events` 纯函数与同名 CSS 类，渲染语义一致
8. ✅ **前端切到新 API**：`web/observability.html` + `web/static/observability.js` 已整文件重写，调 `GET /sessions/{id}/events`；FastAPI 通过 `/ui` mount `web/` 目录，`/` 自动跳转 `/ui/index.html`

### 长期（L4 落地 + 旧系统清理）—— **未启动**

9. **第二批 trace 事件**：`llm.*`（call_started/completed/failed）、`hook.*`（pre_run/post_run）、`compaction.*`（compact_started/completed）批量接入
10. **L2 可复用断言模块**：按 §3.5 spec 沉淀 `assert_event_present` / `assert_causal_chain_resolvable` / `assert_tool_exec_paired` / `assert_visibility_isolation` 为 `tests/observability_asserts.py` 模块
11. **L4 看板**：按 §5.4 / §5.5 在 `.test_artifacts/runs/<utc_ts>/dashboard.html` 落地 drift_score 趋势、tool 失败率、p99 延迟、慢调用 Top N
12. **删除 `.observability/`**：仓库内残余目录与代码已经在 L1 落地阶段下线，本步骤事实上已合并入步骤 5（可在长期门槛达成后从架构文档中正式移除"`.observability/` 并行流"小节）

---

## 十一、本文件该如何使用

### 11.1 PR 评审时

把这份文档当 checklist：
- 新增功能时，明确它在哪一层
- 改 L1 时，把不变量清单（第六节）过一遍
- 改 L2/L3/L4 时，确认没有违反单向数据流

### 11.2 新人 onboarding

按顺序读：
1. `AGENTS.md`（项目总约束）
2. `docs/TESTING.md`（测试方法论）
3. **本文档**（可观测系统架构）
4. `docs/design/design-session-event-log-single-source-refactor.md`（L1 历史演进）

### 11.3 失败排查时

按 L4 → L3 → L2 → L1 反向追：
1. L4 看板：哪个指标红了？
2. L3 报告：哪次跑出问题？打开那次的 timeline 看
3. L2 断言：哪个断言失败了？看具体消息
4. L1 事件流：直接 grep `.test_artifacts/<test>/events.jsonl` 看原始事件

**「哪一步出错」=「在 timeline 上往左找到第一个红色节点」**。

---

## 附录 A：关键文件路径速查

| 资产 | 路径 |
|---|---|
| L1 事件流（per session） | `<base_dir>/sessions/<session_id>.events.jsonl` |
| L1 默认 base_dir | `.learning_agent_data/` |
| L1 写入入口 | `learning_agent/ai/file_store.py:108` `append_session_event` |
| L1 读取入口 | `learning_agent/ai/file_store.py:111` `read_session_events` |
| FileStore 配置 | `learning_agent/learning_agent/main.py:70` `FileStore(self.config.data_dir)` |
| L2 共享 fixture | `tests/conftest.py` |
| L2 e2e runner | `tests/e2e/runner.py` |
| L2 compaction 数据集 | `tests/test_compaction_dataset_runner.py` |
| L2 棘轮配置 | `tests/fixtures/compaction_long_task_cases/*.json` |
| L3 compaction HTML 报告生成器 | `tests/render_compaction_dataset_report.py` |
| L3 测试产物 | `.test_artifacts/<test_name>/` |
| 旧观测系统（已删） | ~~`.observability/`~~（2026-05-24 全部下线，目录已 `rm -rf`） |

## 附录 B：术语对照

| 术语 | 含义 |
|---|---|
| **事实源**（fact source） | L1，即唯一可信的事件流 |
| **派生视图**（derived view） | L2/L3 从 L1 算出来的产物（pass/fail、HTML），不是独立层 |
| **棘轮**（ratchet） | 质量阈值只升不降的模式（min_drift_score 等） |
| **trace 事件** | `visibility=trace` 的事件，纯诊断，不入 state |
| **business 事件** | `visibility ∈ {system, user}` 的事件，参与 replay |
| **因果链** | 通过 `parent_event_id` 串起来的事件序列 |
| **Three Eyes, One Truth** | L2/L3/L4 三个读者共享 L1 一份事实源 |
| **DR**（Decision Record） | 第八节的决策记录 |
