# Compaction 实现清单（文件级）

> 关联设计：
> - `docs/design/design-compaction-strategy-adaptation-from-cc.md`
>
> 目标：
> - 把“SM 作为记忆文件、Micro + Full 两层压缩、首次全量/后续增量”的设计，拆成可直接执行的文件级实现清单。
>
> 范围：
> - 只覆盖当前主链第一版落地所需最小集合
> - 不包含 recall / KG / study 记忆提升的深度实现

---

## 1. 实施总顺序

建议严格按下面顺序推进：

1. `Schema 与存储`
2. `Product 编排入口`
3. `Micro Compact`
4. `Full Compact`
5. `Runtime 接线`
6. `观测与测试`

原因：

- 先落存储和元数据，后续 Full Compact 才有边界可推进。
- 先把 Product 层的 compaction plan 打通，再让 runtime 消费，避免策略下沉。
- 先上 Micro Compact，再上 Full Compact，便于分阶段验证收益。

---

## 2. 新增文件清单

### 2.1 `learning_agent/learning_agent/compaction/models.py`

职责：

- 定义 compaction 相关数据结构

建议放入：

- `SessionMemoryState`
- `CompactMetadata`
- `CompactSourceUnit`
- `FullCompactInput`
- `CompactTraceSummary`
- `FullCompactResult`
- `CompactionPlan`

建议接口：

```python
@dataclass
class CompactionPlan:
    use_micro_compact: bool = True
    use_full_compact: bool = False
    full_compact_scope: str | None = None  # full | incremental | rebase
    compact_anchor_entry_id: str | None = None
    cut_point_entry_id: str | None = None
    recent_token_budget: int = 0
    summary_block: str | None = None
```

完成标准：

- 该文件不依赖 runtime 具体实现
- 只包含纯数据模型和轻量 helper

### 2.2 `learning_agent/learning_agent/compaction/micro_compact.py`

职责：

- 实现每轮 API 调用前的旧工具结果清理逻辑

建议放入：

- `is_micro_compactable_tool_entry(entry) -> bool`
- `build_micro_compacted_history(entries, keep_recent_groups, mutate_storage=False) -> list[SessionEntry]`
- `group_tool_result_units(entries) -> list[list[SessionEntry]]`

注意：

- 默认先做发送侧 compact，不要一上来直接改 session 存储
- 只处理高体积、已消费的 TOOL 结果

### 2.3 `learning_agent/learning_agent/compaction/full_compact.py`

职责：

- 实现 Full Compact 的 unit 分组、cut point 计算、转录转换和摘要输入构建

建议放入：

- `build_round_units(entries) -> list[CompactSourceUnit]`
- `find_cut_point(units, recent_token_budget) -> str`
- `select_compact_scope(metadata) -> str`
- `build_full_compact_input(...) -> FullCompactInput`
- `render_role_transcript(...) -> str`
- `merge_incremental_summary(...) -> str`

注意：

- 该文件先实现“输入构建”和“结果结构组装”
- 真正调用摘要模型可先留在 coordinator 或后续单独抽象

### 2.4 `learning_agent/learning_agent/compaction/coordinator.py`

职责：

- Product 层 compaction 总编排器

建议放入：

- `CompactionCoordinator`

建议方法：

```python
class CompactionCoordinator:
    def evaluate_turn(... ) -> CompactionPlan: ...
    def maybe_run_full_compact(... ) -> FullCompactResult | None: ...
    def persist_compact_success(... ) -> None: ...
```

注意：

- 这里负责阈值判断、anchor 推进、scope 决策
- 不在 runtime 里做这些产品判断

---

## 3. 修改文件清单

## 3.1 `learning_agent/ai/file_store.py`

目标：

- 为 `SM` 状态和 compact 元数据提供独立落盘入口

新增方法建议：

```python
def save_session_memory_state(self, session_id: str, data: dict[str, Any]) -> None: ...
def load_session_memory_state(self, session_id: str) -> Optional[dict[str, Any]]: ...

def save_compact_metadata(self, session_id: str, data: dict[str, Any]) -> None: ...
def load_compact_metadata(self, session_id: str) -> Optional[dict[str, Any]]: ...

def save_compact_summary(self, session_id: str, content: str) -> str: ...
def load_compact_summary(self, session_id: str) -> Optional[str]: ...
```

建议存储路径：

- `sessions/{session_id}.json`
- `sessions/{session_id}.jsonl`
- `memory/session_state/{session_id}.json`
- `memory/compact/{session_id}.meta.json`
- `memory/compact/{session_id}.summary.txt`

注意：

- `compact_session()` 继续保留 snapshot + delta 合并语义
- 不要把 compaction 业务策略塞进 `FileStore`

## 3.2 `learning_agent/learning_agent/session_manager.py`

目标：

- 挂 session 级 compaction 元数据和 SM 状态读写
- 提供 session transcript 视角下的 compact 辅助接口

新增属性建议：

- `_session_memory_states: dict[str, SessionMemoryState]`
- `_compact_metadata: dict[str, CompactMetadata]`

新增方法建议：

```python
def get_session_memory_state(self, session_id: str) -> SessionMemoryState | None: ...
def set_session_memory_state(self, session_id: str, state: SessionMemoryState) -> None: ...

def get_compact_metadata(self, session_id: str) -> CompactMetadata | None: ...
def set_compact_metadata(self, session_id: str, metadata: CompactMetadata) -> None: ...

def iter_message_entries(self, session_id: str, leaf_id: str | None = None) -> list[SessionEntry]: ...
def list_entries_after(self, session_id: str, entry_id: str | None) -> list[SessionEntry]: ...
```

对现有 `compact_session()` 的修改建议：

- 不再只是 `status = COMPACTED`
- 改为接收明确结果对象，例如：

```python
def compact_session(
    self,
    session_id: str,
    result: FullCompactResult,
    *,
    preserve_recent_entries: bool = True,
) -> bool: ...
```

但如果你想降低改动风险，第一版也可以：

- 保留旧签名
- 新增 `apply_full_compact_result(...)`

注意：

- `SessionManager` 负责“应用 compaction 结果到 session 视图”
- 不负责摘要模型调用

## 3.3 `learning_agent/learning_agent/main.py`

目标：

- 在 Product 层 turn 准备阶段注入 compaction 评估

修改点：

- `_prepare_session_turn(...)`

建议改造：

1. 先保持 Ask 确认语义逻辑不变
2. 在构造 `PreparedSessionTurn` 前调用 `CompactionCoordinator.evaluate_turn(...)`
3. 把 `CompactionPlan` 挂到 `PreparedSessionTurn`

建议新增私有方法：

```python
def _build_compaction_plan(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
) -> CompactionPlan: ...
```

注意：

- Ask `aligning` 阶段默认只允许 Micro Compact
- 不要在 runtime 再次重复判断 mode 特殊规则

## 3.4 `learning_agent/learning_agent/mode_service.py`

目标：

- 让 compaction policy 成为 mode 配置的一部分

修改点：

- `ModeProfile`
- `TurnExecutionProfile`
- `build_turn_profile(...)`

新增字段建议：

```python
class ModeProfile(BaseModel):
    ...
    micro_compact_enabled: bool = True
    full_compact_enabled: bool = True
    full_compact_threshold: float = 0.85
    recent_token_budget: int = 16000
```

```python
class TurnExecutionProfile(BaseModel):
    ...
    micro_compact_enabled: bool = True
    full_compact_enabled: bool = True
    full_compact_threshold: float = 0.85
    recent_token_budget: int = 16000
```

模式建议：

- `CHAT`
  - Micro: on
  - Full: on
- `ASK`
  - Micro: on
  - Full: off 或受限
- `STUDY`
  - Micro: on
  - Full: on
  - `recent_token_budget` 可更高

## 3.5 `learning_agent/agent/react_engine.py`

目标：

- 在上下文构建链路消费 compaction plan

修改重点：

- `build_context(...)`

现状：

- 当前只读取 `session_store.get_message_history(session_id)`
- 再做 `_compress_tool_error_history(...)`

建议改造签名：

```python
def build_context(
    self,
    session_id: str,
    profile: TurnExecutionProfile,
    compaction_plan: CompactionPlan | None = None,
) -> list[ChatMessage]:
```

内部顺序建议：

1. 读取 history
2. 做 `_compress_tool_error_history`
3. 如果 `compaction_plan.use_micro_compact`，执行 `build_micro_compacted_history(...)`
4. 如果 `compaction_plan.summary_block` 存在，则先注入 compact summary block
5. 再拼接 recent messages

建议新增 helper：

- `_build_summary_chat_message(summary_block: str) -> ChatMessage`
- `_history_to_chat_messages(entries: list[SessionEntry]) -> list[ChatMessage]`

注意：

- Full Compact 真正生成 summary 的动作不要塞进 `ReActEngine`
- `ReActEngine` 只负责消费结果

## 3.6 `learning_agent/agent/session_runtime.py`

目标：

- 把 `PreparedSessionTurn` 中的 compaction plan 传递给 `ReActEngine`

修改点：

- `run_turn(...)` 中调用 `self._engine.build_context(session.id, profile)`

改为：

```python
context_messages = self._engine.build_context(
    session.id,
    profile,
    compaction_plan=getattr(profile, "compaction_plan", None),
)
```

更好的做法：

- 不把 `compaction_plan` 塞进 `profile`
- 而是让 `AgentLoop.run(...)` / `run_turn(...)` 显式多传一个参数

建议最终方向：

```python
async def run_turn(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
    compaction_plan: CompactionPlan | None = None,
) -> ...
```

注意：

- 这里只做参数透传
- 不在 runtime 里决定 scope 或阈值

## 3.7 `learning_agent/agent/agent_loop.py`

目标：

- 作为 runtime 路由层，把 compaction_plan 从 Product 传到 `AgentLoopSession`

修改点：

- `run(...)`

建议改造签名：

```python
async def run(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
    *,
    compaction_plan: CompactionPlan | None = None,
) -> ...
```

职责：

- 不做 compaction 决策
- 只负责路由到对应 session runtime

## 3.8 `learning_agent/ai/models.py`

目标：

- 如果你希望 `PreparedSessionTurn` 保持为 Pydantic 模型，需要把 compaction plan 类型纳入模型层

修改建议：

- 给 `PreparedSessionTurn` 新增字段：

```python
compaction_plan: dict[str, Any] | None = None
```

更优方案：

- 若不想引入跨层耦合，可先用 `dict[str, Any]`
- 等 compaction 模块稳定后，再改成明确模型

---

## 4. 可选调整文件

## 4.1 `learning_agent/learning_agent/extensions/context_compressor.py`

建议：

- 第一版不再作为主入口继续增强
- 可以把其中：
  - token 估算
  - turn unit 保序
  - orphan tool message 过滤
  抽到 `compaction/` 新模块中复用

处理方式：

- `deprecated but kept`
- 或仅保留底层 helper

## 4.2 `learning_agent/learning_agent/extensions/built_in.py`

可选：

- 增加轻量 observability hook
- 记录本轮是否命中 Micro / Full Compact

但注意：

- 不要把 compaction 核心逻辑再做成扩展

---

## 5. 文件级任务单

### 5.1 第一批提交

- `learning_agent/learning_agent/compaction/models.py`
- `learning_agent/ai/file_store.py`
- `learning_agent/learning_agent/session_manager.py`

目标：

- 把 schema 和存储先打通

### 5.2 第二批提交

- `learning_agent/learning_agent/compaction/micro_compact.py`
- `learning_agent/learning_agent/mode_service.py`
- `learning_agent/learning_agent/main.py`

目标：

- 让 Product 层能产出 `CompactionPlan`
- 先只打开 Micro Compact

### 5.3 第三批提交

- `learning_agent/learning_agent/compaction/full_compact.py`
- `learning_agent/learning_agent/compaction/coordinator.py`
- `learning_agent/agent/agent_loop.py`
- `learning_agent/agent/session_runtime.py`
- `learning_agent/agent/react_engine.py`

目标：

- 打通 Full Compact 主链

### 5.4 第四批提交

- 测试文件
- 观测埋点
- rebase 与 circuit-breaker

---

## 6. 测试文件建议

建议新增：

- `tests/test_compaction_models.py`
- `tests/test_micro_compact.py`
- `tests/test_full_compact_units.py`
- `tests/test_full_compact_incremental.py`
- `tests/test_compaction_coordinator.py`

建议补充现有测试：

- `tests/test_large_file_handling.py`
- `tests/test_hook_system.py`
- 若存在 session / runtime 相关测试，补充 Full Compact 后继续对话场景

关键用例：

1. 首次 full compact 从第一条消息开始压缩
2. 第二次 full compact 只压 `anchor` 之后新增区间
3. `cut_point` 不落在 tool 配对中间
4. Ask `aligning` 阶段不会自动 full compact
5. micro compact 不破坏原始 recent tool messages
6. compact 失败不会推进 `compact_anchor`
7. 连续失败达到阈值后触发 circuit-breaker

---

## 7. 验收标准

达到以下标准，才算第一版落地完成：

- `SM` 文件可独立读写，且不参与 compact summary 主链
- 每轮 API 前都可命中 Micro Compact
- 超阈值时可自动触发 Full Compact
- 首次 Full Compact 为全量
- 第二次及后续 Full Compact 为增量
- compact summary 保留 `reads / writes / searches / failures`
- compact 后 session 能继续正常运行
- 失败不会污染 transcript 和 anchor

---

## 8. 风险提醒

实现时最容易踩的坑：

1. 把 compaction 决策塞进 runtime
2. 在 Micro Compact 阶段过早修改原始 transcript
3. `cut_point` 落在 tool 配对中间
4. 增量 compact 成功后忘记推进 `compact_anchor`
5. 增量失败却错误覆盖旧 summary
6. Ask 对齐轮被 Full Compact 干扰

建议实现时始终遵守：

- Product 决策
- Runtime 消费
- FileStore 只存储

