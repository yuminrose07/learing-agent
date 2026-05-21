# 会话 JSONL 单事实源重构实现清单（文件级）

> 关联设计：
> - `docs/design/design-session-event-log-single-source-refactor.md`
> - `docs/design/design-full-compact-slact-adaptation.md`
>
> 目标：
> - 将会话持久化切换为 `sessions/{session_id}.events.jsonl` append-only 单事实源。
> - 将 session 主消息模型改为纯线性消息序列。
> - 将 Agent / LLM / UI 消息收口为派生视图。
> - 为 full compact / slact 提供稳定 JSONL source、cursor 和消息边界。
>
> 非目标：
> - 不保留旧 `session.json + session.jsonl` 作为运行时路径。
> - 不保留 `compact_session()` snapshot/delta 合并能力。
> - 不引入子代理、fork agent 或后台 summarizer。
> - 不实现新的分支、回溯、多 leaf 能力。

---

## 1. 实施总顺序

建议严格按下面顺序推进：

1. `事件模型与 FileStore`
2. `旧数据迁移与线性投影`
3. `Product 视图入口`
4. `Runtime 输入切换`
5. `UI 恢复切换`
6. `流式 message_end 落盘`
7. `Full Compact / Slact 接线`
8. `删除旧路径与验收`

原因：

- 先建立事实源，再切派生视图，避免 Runtime/UI 继续依赖旧 `session.entries`。
- 先保证线性消息恢复正确，再引入 compact source cursor。
- 先把 `message_end` 作为最终消息边界，否则流式半成品会污染 JSONL 和 compact summary。

---

## 2. 新增文件清单

### 2.1 `learning_agent/learning_agent/session_events.py`

职责：

- 定义 session event 基础模型。
- 定义事件类型常量或枚举。
- 提供事件构造 helper。

建议模型：

```python
class SessionEvent(BaseModel):
    seq: int
    event_id: str
    session_id: str
    ts: datetime
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    visibility: str = "agent"
```

建议事件类型：

- `message.user_appended`
- `message.assistant_started`
- `message_end`
- `message.stream_failed`
- `message.interrupted`
- `message.patch`
- `session.mode_changed`
- `session.ask_state_updated`
- `session.title_updated`
- `session.status_changed`
- `tool.call_started`
- `tool.call_completed`
- `tool.call_failed`
- `compaction.summary_added`
- `compaction.anchor_moved`
- `compaction.rebase_completed`

完成标准：

- 事件模型不依赖 Runtime 实现。
- 每个事件都必须包含 `seq` 与 `event_id`。
- `message_end` 是 assistant 完整消息进入事实源的唯一正常结束边界。

### 2.2 `learning_agent/learning_agent/session_event_store.py`

职责：

- Product/Application 层使用的 session event log facade。
- 包装 `FileStore` 的 append/read 能力。
- 管理 per-session `seq`。

建议接口：

```python
class SessionEventStore:
    def append_event(self, session_id: str, type: str, payload: dict[str, Any], visibility: str = "agent") -> SessionEvent: ...
    def read_events(self, session_id: str, after_seq: int | None = None) -> list[SessionEvent]: ...
    def next_seq(self, session_id: str) -> int: ...
```

完成标准：

- 写入目标为 `sessions/{session_id}.events.jsonl`。
- 不写入旧 `sessions/{session_id}.json`。
- 不写入旧 `sessions/{session_id}.jsonl`。
- `event_id` 唯一，`seq` 单 session 单调递增。

### 2.3 `learning_agent/learning_agent/session_projection.py`

职责：

- 从 event log replay 出线性 `AgentSnapshot`。
- 从旧 `LearningSession.entries` 兼容投影出线性消息。
- 支撑 LLM/UI/compact 统一消费线性视图。

建议模型：

```python
@dataclass
class AgentSnapshot:
    session_id: str
    mode: AgentMode
    ask_state: AskState
    mode_metadata: dict[str, Any]
    messages: list[SessionEntry]
    compact_metadata: CompactMetadata | None = None
```

建议接口：

```python
def replay_events(events: list[SessionEvent]) -> AgentSnapshot: ...
def project_legacy_session(session: LearningSession) -> AgentSnapshot: ...
```

完成标准：

- 新模型输出纯线性消息序列。
- 不再通过 `get_path_to_leaf()` 作为主路径。
- 旧 `parent_id/current_leaf_id/fork_point` 只在 legacy projection 中处理。

### 2.4 `learning_agent/learning_agent/views.py`

职责：

- 构建 `LLMInputView` 与 `UIViewMessage`。
- 将产品过滤规则集中在 Product/Application 层。

建议模型：

```python
@dataclass
class LLMInputView:
    session_id: str
    messages: list[ChatMessage]
    source_event_range: tuple[int, int] | None = None

@dataclass
class UIViewMessage:
    id: str
    role: str
    content: str
    status: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)
```

建议接口：

```python
def build_llm_input_view(snapshot: AgentSnapshot, profile: TurnExecutionProfile, compaction_plan: CompactionPlan | None = None) -> LLMInputView: ...
def build_ui_messages(snapshot: AgentSnapshot) -> list[UIViewMessage]: ...
```

完成标准：

- LLM view 不包含 UI-only / observability-only 事件。
- UI view 不暴露 agent-only/system-only 内部细节。
- assistant tool_calls 与 tool result 成组保留或成组压缩。

### 2.5 `learning_agent/learning_agent/session_migration.py`

职责：

- 将旧 `session.json + session.jsonl` 一次性迁移为 `session.events.jsonl`。
- 迁移旧树形 session 为线性事件序列。

建议接口：

```python
def migrate_legacy_session(session_id: str, file_store: FileStore) -> bool: ...
def migrate_all_sessions(file_store: FileStore) -> MigrationReport: ...
```

完成标准：

- 迁移后生成 `sessions/{session_id}.events.jsonl`。
- 旧 `session.json` / `session.jsonl` 迁移完成后删除。
- 迁移报告记录成功、失败、跳过原因。
- 迁移失败不能删除旧文件。

---

## 3. 修改文件清单

### 3.1 `learning_agent/ai/file_store.py`

目标：

- 新增 event log 文件读写。
- 删除或停用旧 snapshot/delta 主路径。
- 删除或停用 `compact_session()`。

新增方法建议：

```python
def append_session_event(self, session_id: str, event: dict[str, Any]) -> None: ...
def read_session_events(self, session_id: str, after_seq: int | None = None) -> list[dict[str, Any]]: ...
def delete_session_events(self, session_id: str) -> bool: ...
```

路径：

- 新增：`sessions/{session_id}.events.jsonl`
- 迁移后删除：`sessions/{session_id}.json`
- 迁移后删除：`sessions/{session_id}.jsonl`

必须删除或停用：

- `save_session(...)`
- `load_session(...)`
- `append_session_delta(...)`
- `read_session_deltas(...)`
- `compact_session(...)`

如果为了迁移短期保留旧方法：

- 方法名必须带 `legacy_` 前缀。
- 只能被 `session_migration.py` 调用。
- 不允许运行时主路径调用。

完成标准：

- 新会话只产生 `.events.jsonl`。
- `compact_session()` 不再存在于主路径。
- 删除 session 时能删除 `.events.jsonl`。

### 3.2 `learning_agent/learning_agent/main.py`

目标：

- 运行时写入 session event log。
- 停止 snapshot/delta 恢复和定期 `compact_session()`。
- 接入 projection / view facade。

修改点：

- `_load_state(...)`
- `_load_session_with_deltas(...)`
- `_save_state(...)`
- `_on_state_snapshot(...)`
- `_on_entry_appended(...)`
- `_on_entry_patched(...)`
- `_on_scalar_changed(...)`
- `stream_session_chat(...)`
- `collect_session_chat(...)`
- `delete_session(...)`

改造要求：

1. `_load_state(...)` 改为读取 `.events.jsonl` 并 replay 为 `AgentSnapshot`。
2. `_load_session_with_deltas(...)` 下线或改为 legacy migration helper。
3. `_save_state(...)` 不再写 session snapshot，不再清空 delta。
4. `_on_state_snapshot(...)` 不再调用 `file_store.compact_session(...)`。
5. entry append / patch / scalar 改为写标准 `SessionEvent`。
6. 流式正常完成后写入 `message_end`。
7. 流式失败写入 `message.stream_failed` 或 `message.interrupted`。
8. 删除 session 时同步删除 event log、compact summary、metadata、observability 关联文件。

完成标准：

- 正常对话只追加 `.events.jsonl`。
- 流式半成品不会进入后续 LLMInputView。
- 旧 snapshot/delta 不再被主路径读写。

### 3.3 `learning_agent/learning_agent/session_manager.py`

目标：

- 降级为 Product 层 session 状态 facade。
- 不再维护树形分支主语义。
- 不再提供 `compact_session()`。

修改点：

- `create_session(...)`
- `append_message(...)`
- `fork_at(...)`
- `get_path_to_leaf(...)`
- `get_message_history(...)`
- `list_entries_after(...)`
- `compact_session(...)`
- `apply_full_compact_result(...)`

改造要求：

1. `create_session(...)` 写入线性 session 创建事件。
2. `append_message(...)` 不再依赖 `parent_id/current_leaf_id`。
3. `fork_at(...)` 删除或标记为不支持。
4. `get_path_to_leaf(...)` 下线为 legacy-only。
5. `get_message_history(...)` 改为从 `AgentSnapshot.messages` 返回线性历史。
6. `list_entries_after(...)` 按线性顺序查找。
7. `compact_session(...)` 删除。
8. `apply_full_compact_result(...)` 改为写 `compaction.summary_added` / metadata 事件，不直接修改历史消息。

完成标准：

- 新 session 不产生 `fork_point`。
- 新消息不依赖 `parent_id`。
- compact 不删除原始消息。

### 3.4 `learning_agent/agent/react_engine.py`

目标：

- Runtime 不再自行扫描 session history。
- Runtime 消费 Product 层构造好的 `LLMInputView`。

修改点：

- `build_context(...)`
- `build_single_pass_messages(...)`

改造要求：

1. 删除或降级 `self.session_store.get_message_history(session_id)` 直接调用。
2. `build_context(...)` 接收 `LLMInputView` 或已构造 `ChatMessage` 列表。
3. tool error history 压缩如保留，应移动到 Product view 构建阶段。
4. compact summary 注入由 `build_llm_input_view(...)` 完成。

完成标准：

- Runtime 不决定哪些历史消息给模型看。
- Runtime 不解释 full compact / slact / UI visibility。
- tool_call / tool result 协议完整性由 LLMInputView 保证。

### 3.5 `learning_agent/agent/session_runtime.py`

目标：

- 在流式完成边界发出 `message_end`。
- 在失败/中断时发出明确失败事件。
- 消费 `LLMInputView`。

修改点：

- 主 run loop 中构建 context 的位置。
- 流式结束与异常处理路径。
- usage / observability 上报。

改造要求：

1. 成功完成 assistant response 后，Product 层收到完整文本并写入 `message_end`。
2. 流式失败时写 `message.stream_failed`。
3. 用户中断时写 `message.interrupted`。
4. 半成品 assistant 内容只用于 UI 临时展示和 observability，不进入事实消息。

完成标准：

- 刷新页面后不会恢复半条 assistant 消息为完整消息。
- 失败消息不会污染下一轮 LLMInputView。

### 3.6 `learning_agent/web/web_server.py`

目标：

- Web API 返回 UI view，而不是原始 session entries。
- 删除 session 时清理关联持久化文件。

修改点：

- 获取 session 消息列表的接口。
- SSE 流式接口。
- 删除 session 接口。

改造要求：

1. 历史消息读取改为 `build_ui_messages(session_id)`。
2. SSE chunk 继续可实时展示，但最终持久化以 `message_end` 为准。
3. compact summary 默认展示为折叠提示，不作为 assistant 气泡。
4. 删除 session 时清理：
   - `sessions/{session_id}.events.jsonl`
   - `memory/compact/{session_id}.meta.json`
   - `memory/compact/{session_id}.summary.txt`
   - session 相关 observability 文件
   - 可能存在的临时迁移/缓存文件

完成标准：

- 前端刷新后从 UI view 恢复。
- UI 不直接暴露 agent-only/system-only 内容。

### 3.7 `learning_agent/learning_agent/compaction/coordinator.py`

目标：

- full compact / slact source 从 `.events.jsonl` 读取。
- 使用 JSONL cursor / event seq 推进 anchor。
- 不扫描内存 `session.entries` 作为事实源。

修改点：

- `evaluate_turn(...)`
- `maybe_run_full_compact(...)`
- `persist_compact_success(...)`

改造要求：

1. source selection 读取 event log replay 后的线性消息。
2. 只选择 `message_end` 后的完整 assistant 消息。
3. 敏感 tool output 不进入 compact source。
4. compact success 写入 `compaction.summary_added` 事件。
5. compact failure 不推进 anchor。
6. metadata 记录：
   - `source_event_start_seq`
   - `source_event_end_seq`
   - `next_jsonl_cursor`
   - `summary_hash`
   - `template_version`

完成标准：

- incremental 只处理 cursor 之后的新事件。
- rebase 不重读全部内存历史。
- compact source 可审计。

### 3.8 `learning_agent/learning_agent/compaction/models.py`

目标：

- 补齐 event log / cursor / slact 所需字段。

建议新增：

- `CompactMode`
- `CompactScope`
- `JsonlCursor`
- `source_event_start_seq`
- `source_event_end_seq`
- `source_event_ids`
- `retained_event_ids`
- `template_version`

完成标准：

- metadata 能定位 compact source range。
- metadata 能支持 incremental / rebase。
- metadata 不依赖 branch/current_leaf。

### 3.9 `learning_agent/learning_agent/compaction/full_compact.py`

目标：

- 基于线性消息和 event range 构建 compact input。

改造要求：

1. `build_round_units(...)` 输入是线性消息序列。
2. cut point 以 event seq / entry id 表达。
3. safe boundary 不拆 assistant tool_calls / tool result。
4. role transcript 使用安全 tool 摘要，不使用敏感原始 output。

完成标准：

- source / retained 划分稳定。
- cut point 不会落在工具配对中间。

### 3.10 `learning_agent/learning_agent/compaction/prompts.py`

目标：

- 按 `docs/design/design-full-compact-slact-adaptation.md` 第 6 章新增正式 compact prompt 模板。
- 第一版模板版本固定为 `compact-summary-v1`。

建议包含：

- `CompactPromptSpec` 或等价结构，包含 mode、scope、summary_position、source_event_range、source_snapshot_seq、current_user_event_id、existing_summary、session_memory_state、source_units、source_transcript。
- no-tools preamble。
- auto prefix / slact full / slact from / slact up_to / incremental / rebase 的 mode-specific instruction。
- required output contract：一个 `<analysis>` 加一个 `<summary>`。
- 九章节 summary skeleton。
- few-shot examples。
- `format_compact_summary(...)`，只提取 `<summary>`。
- `validate_compact_summary(...)` 或等价校验逻辑。

完成标准：

- prompt 输入来自 `CompactionSourceView.safe_units`，不直接读取 raw JSONL。
- analysis 被剥离，不能进入 `compaction.summary_added`。
- summary 九章节完整，且第 8/9 节标题与 compact mode 匹配。
- few-shot 不进入最终 summary。
- 用户原话锚点可从 `source_event_ids` 追溯。
- retained-only 消息不会被 summary 发明或重复总结。
- artifact_ref 被保留但不扩写 artifact 全文。
- incremental / rebase 输出 canonical summary，不追加 `[Incremental Update]`。

---

## 4. 旧路径删除清单

必须删除或停用：

- `FileStore.compact_session(...)`
- `FileStore.save_session(...)` 主路径
- `FileStore.load_session(...)` 主路径
- `FileStore.append_session_delta(...)` 主路径
- `FileStore.read_session_deltas(...)` 主路径
- `LearningAgentSystem._save_state(...)` 中的 session snapshot 保存
- `LearningAgentSystem._on_state_snapshot(...)` 中的 session compact
- `SessionManager.fork_at(...)` 新写入能力
- `SessionManager.get_path_to_leaf(...)` 主路径使用
- Runtime 直接扫描 `session_store.get_message_history(...)`

允许短期保留但必须标记 legacy：

- 旧文件读取辅助
- 旧树形 session 到线性序列投影
- 旧数据迁移脚本

---

## 5. 敏感数据与删除清单

### 5.1 Tool Output

规则：

- 敏感 tool output 不进入 session event log。
- session event log 只保留安全摘要、状态、引用和必要元数据。
- 完整敏感 output 只进入 observability 关联文件。

### 5.2 删除 Session

删除 session 时必须清理：

- `sessions/{session_id}.events.jsonl`
- `memory/compact/{session_id}.meta.json`
- `memory/compact/{session_id}.summary.txt`
- `memory/session_state/{session_id}.json`
- session 相关 observability trace / flow / event 文件
- 迁移残留的旧 `sessions/{session_id}.json`
- 迁移残留的旧 `sessions/{session_id}.jsonl`
- 临时 checkpoint/cache 文件，如果未来出现

完成标准：

- 删除后不可从后端恢复该 session 的正文、summary 或敏感工具输出。

---

## 6. 测试清单

### 6.1 单元测试

新增或更新：

- `tests/test_session_event_store.py`
- `tests/test_session_projection.py`
- `tests/test_event_log_migration.py`
- `tests/test_llm_input_view.py`
- `tests/test_ui_message_view.py`
- `tests/test_compaction_event_source.py`

覆盖：

- append event 自动分配 seq。
- event_id 重复 replay 幂等。
- seq 缺口或乱序进入 corrupt event 处理。
- 旧树形 session 投影为线性消息。
- 新写入不产生 fork_point。
- patch event 正确 replay。
- scalar event 正确 replay mode / ask_state。
- message_end 后 assistant 消息进入 snapshot。
- stream_failed / interrupted 不进入 LLMInputView。
- sensitive tool output 不进入 session event log。
- delete session 清理 event log / compact / observability 关联文件。

### 6.2 集成测试

覆盖：

- 新建 session 后只生成 `.events.jsonl`。
- 发送一轮消息后，刷新/重启能从 event log 恢复 UI messages。
- Runtime 使用 LLMInputView，不直接扫 session.entries。
- full compact source 来自 `.events.jsonl`。
- incremental compact 只读取 cursor 后新增事件。
- slact full / from / up_to 使用线性 sequence。
- compact summary 不作为普通 assistant 气泡展示。
- 删除 session 后关联文件全部消失。

### 6.3 回归测试

至少跑：

```bash
pytest tests/test_mode_layering.py
pytest tests/test_compaction.py
pytest tests/test_turn_usage.py
pytest tests/test_web_adaptation.py
pytest tests/test_tool_execution_reliability.py
```

如果测试改名或新增：

```bash
pytest tests/test_session_event_store.py
pytest tests/test_session_projection.py
pytest tests/test_compaction_event_source.py
```

---

## 7. 分阶段验收标准

### 阶段 1：事件源可用

- 新 session 写入 `sessions/{session_id}.events.jsonl`。
- event 有 `seq` 和 `event_id`。
- replay 幂等。
- 旧 snapshot/delta 不再由主路径写入。

### 阶段 2：线性投影可用

- AgentSnapshot 可从 event log 重建。
- 旧树形 session 可兼容投影为线性序列。
- 新写入不产生 fork_point。

### 阶段 3：视图切换完成

- Runtime 消费 LLMInputView。
- Web 历史消费 UIViewMessage。
- UI-only / agent-only / observability-only 不污染 LLMInputView。

### 阶段 4：流式边界完成

- 成功流式结束写 `message_end`。
- 失败/中断写失败事件。
- 半成品消息不进入下一轮 LLMInputView。

### 阶段 5：Compact 接入完成

- full compact / slact source 来自 `.events.jsonl`。
- compact anchor 使用 event seq / cursor。
- 敏感 tool output 不进入 summary source。
- compact 失败不推进 anchor。

### 阶段 6：旧路径删除完成

- `compact_session()` 不存在或不被任何主路径引用。
- 旧 `session.json` / `session.jsonl` 迁移完成后删除。
- 删除 session 能清理所有关联文件。

---

## 8. 实施前检查

开工前确认：

- 当前分支干净或已明确哪些改动属于本任务。
- 旧 session 数据是否需要迁移样本。
- observability 文件命名规则是否能按 session_id 定位。
- 前端是否能接受 compact summary 折叠提示而不是 assistant 气泡。
- mode / ask_state 当前事件字段是否足够 replay。
