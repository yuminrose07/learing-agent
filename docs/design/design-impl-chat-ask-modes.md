# 技术实现文档：Chat / Ask 双模式落地 + Study 预留接口

> **范围**：本期只落地 `Chat`（默认）与 `Ask`（显式开启），`Study` 仅做数据模型与接口占位。
> **原则**：模式属于 Product 层编排，不侵入 Runtime 核心执行链。
> **文档日期**：2026-05-15
> **依赖设计**：`design-chat-ask-study-modes.md`

---

## 一、需求澄清与本期边界

### 1.1 需求澄清

| 澄清点 | 结论 |
|--------|------|
| Chat 模式 | **默认且永久默认**。它就是现在输入框的默认行为，用户无需任何操作即可使用。 |
| Ask 模式 | **显式开启**。用户通过点击网页上的按钮进入 Ask 模式，本期不做任何自动建议或自动触发。 |
| Study 模式 | **仅预留接口**。本期不实现任何 Study 特有的 prompt、工具链或执行逻辑，只在数据模型和 API 中预留扩展位。 |

### 1.2 本期实现边界

- ✅ 引入 `AgentMode` 枚举（`chat` / `ask` / `study`）
- ✅ `Chat` 作为 `LearningSession` 的默认 `mode`
- ✅ `Ask` 显式切换 + 对齐确认闭环
- ✅ `Ask` 作为前置对齐流程；确认后默认收口回 `Chat`，仅在显式指定时切入 `Study`
- ✅ 前端 mode toolbar 从「Ask 布尔开关」升级为「三态模式选择器」（Chat 默认高亮，Ask 可点，Study 置灰/仅展示）
- ✅ 前端切换 session / 页面刷新后，可从服务端恢复当前 `session.mode`
- ✅ `ModeProfile` 与 `TurnExecutionProfile` 数据结构
- ✅ Prompt 从 `agent_loop.py` 硬编码迁移到 Product 层 profile
- ✅ 历史消息按条持久化 `mode` / `alignment` 元数据，支持 reload 后样式回放
- ✅ 观测系统同步记录 `mode`（trace、span tag、event payload、flow step）
- ⏸️ `Study` 仅保留：枚举值、profile 占位、API 接口、`session.mode` 可设为 `study`
- ❌ 不做自动模式判断
- ❌ 不做 Study 的 memory 深度接线
- ❌ 不改动 Runtime 核心状态机（`AgentState`）

---

## 二、总体架构变更一览

```text
┌─────────────┐      ┌──────────────┐      ┌─────────────────┐      ┌─────────────┐
│   Web UI    │─────▶│  Web Server  │─────▶│ LearningAgent   │─────▶│  AgentLoop  │
│ (mode btn)  │      │  (FastAPI)   │      │    System       │      │  (Runtime)  │
└─────────────┘      └──────────────┘      └─────────────────┘      └─────────────┘
                                                    │
                                                    ▼
                                            ┌──────────────┐
                                            │ ModeProfile  │
                                            │  Resolver    │
                                            └──────────────┘
```

变更要点：

1. **前端**：`isAskMode` 布尔值 → `currentMode` 字符串（`chat` / `ask` / `study`）
2. **API**：`ChatRequest.ask_mode: bool` → `ChatRequest.mode: AgentMode`
3. **产品层**：新增 `mode_profile.py`，负责根据 `AgentMode` 解析 `TurnExecutionProfile`
4. **Runtime**：`run_turn` 参数从 `ask_mode: bool` 改为 `profile: TurnExecutionProfile`

---

## 三、数据模型变更

### 3.1 新增 `AgentMode` 枚举

**文件**：`learning_agent/ai/models.py`

在现有枚举区域（`MessageRole` 之后）新增：

```python
class AgentMode(str, Enum):
    """Agent 产品模式枚举。"""
    CHAT = "chat"
    ASK = "ask"
    STUDY = "study"
```

### 3.2 扩展 `LearningSession`

**文件**：`learning_agent/ai/models.py`

将 `LearningSession` 扩展为：

```python
class LearningSession(BaseModel):
    id: str = Field(default_factory=lambda: f"sess-{uuid.uuid4().hex[:8]}")
    objective_id: Optional[str] = None
    title: Optional[str] = None
    root_entry_id: Optional[str] = None
    current_leaf_id: Optional[str] = None
    status: SessionStatus = SessionStatus.ACTIVE
    # === 模式相关 ===
    mode: AgentMode = AgentMode.CHAT
    mode_metadata: dict[str, Any] = Field(default_factory=dict)
    # =================
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extracted_knowledge_ids: list[str] = Field(default_factory=list)
    entries: list[SessionEntry] = Field(default_factory=list)
    ask_state: AskState = Field(default_factory=AskState)
```

说明：

- `mode` 默认为 `AgentMode.CHAT`，符合「Chat 是默认模式」的澄清。
- `mode_metadata` 是扩展桶，用于承载 `post_ask_target`、`last_mode_switch`、Study 策略等，避免持续新增顶级字段。
- `AskState` 保留不变，因为 Ask 的确认闭环逻辑仍需要它；但它只表示临时对齐态，不应替代 `session.mode` 这一产品层主状态。

### 3.2.1 `SessionEntry.metadata` 的模式元数据约定

本期需要补充一条实现约束：**模式不仅是 session 级状态，也需要按消息持久化**，否则前端 reload / 切换 session 后无法正确回放 Ask / Study 历史样式。

建议约定：

```python
class SessionEntry(BaseModel):
    ...
    metadata: dict[str, Any] = Field(default_factory=dict)
```

其中消息元数据至少支持以下键：

- `mode`: `"chat" | "ask" | "study"`，表示该条消息产生时的产品模式
- `alignment`: `bool`，仅 Ask 对齐轮消息为 `True`
- `source`: 可选，标记 `runtime` / `api` / `cli` 等来源

落地规则：

- 普通 ReACT 主链追加的 user / assistant message，写入 `metadata={"mode": profile.mode.value}`。
- Ask 对齐轮追加的 user / assistant message，写入 `metadata={"mode": "ask", "alignment": True}`。
- 前端历史消息渲染时，优先读取 `entry.metadata.mode` 和 `entry.metadata.alignment` 恢复样式，而不是依赖当前 toolbar 状态。

### 3.3 新增 `ModeProfile` 与 `TurnExecutionProfile`

**文件**：新建 `learning_agent/ai/mode_profile.py`

```python
"""
Mode Profile 定义：产品层模式策略配置。

Runtime 不直接消费 ModeProfile，而是消费由 Product 层解析后的
TurnExecutionProfile。这样 Runtime 保持最小通用执行器，不理解产品模式语义。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from learning_agent.ai.models import AgentMode


class ModeProfile(BaseModel):
    """模式策略配置（Product 层使用）。"""
    mode: AgentMode
    system_prompt: str
    tools_enabled: list[str] = Field(default_factory=list)
    memory_read: bool = False
    memory_write: bool = False
    ask_confirmation_required: bool = False
    context_budget: str = "normal"      # light | normal | heavy
    response_style: str = "direct"      # direct | align | tutor


class TurnExecutionProfile(BaseModel):
    """单轮执行配置（Product 层组装，Runtime 直接消费）。"""
    mode: AgentMode
    system_prompt: str
    visible_tools: list[str]
    memory_read: bool
    memory_write: bool
    response_style: str
    ask_confirmation_required: bool


# ───────────────────────────────
# 预置 Profile 注册表
# ───────────────────────────────

CHAT_PROFILE = ModeProfile(
    mode=AgentMode.CHAT,
    system_prompt=(
        "You are a helpful learning assistant.\n\n"
        "Your goal: answer the user's question directly and concisely.\n"
        "- Keep responses brief unless detail is clearly needed.\n"
        "- Use tools only when necessary.\n"
        "- Ask clarifying questions only when the request is genuinely ambiguous.\n"
        "- Encourage active recall when explaining concepts."
    ),
    tools_enabled=["read_file", "grep", "bash", "write_file", "edit_file"],
    memory_read=False,
    memory_write=False,
    ask_confirmation_required=False,
    context_budget="light",
    response_style="direct",
)

ASK_PROFILE = ModeProfile(
    mode=AgentMode.ASK,
    system_prompt=(
        "You are a learning assistant in **Alignment Mode**.\n\n"
        "The user has submitted a question or task. BEFORE you answer, you must:\n"
        "1. Restate the user's intent in one sentence.\n"
        "2. Briefly outline your planned approach to answer (1-2 sentences).\n"
        "3. Ask the user to confirm or clarify.\n\n"
        "Do NOT provide the detailed answer yet. "
        "Keep your response concise (under 150 words)."
    ),
    tools_enabled=["read_file", "grep"],
    memory_read=False,
    memory_write=False,
    ask_confirmation_required=True,
    context_budget="light",
    response_style="align",
)

STUDY_PROFILE = ModeProfile(
    mode=AgentMode.STUDY,
    system_prompt=(
        "You are a learning assistant in **Study Mode**.\n\n"
        "Your goal: maximize learning gain, not just give answers.\n"
        "- Explain concepts with structure: principle → example → summary.\n"
        "- Encourage comparison, contrast, and active recall.\n"
        "- Suggest next steps or follow-up questions.\n"
        "- Use available tools to read and analyze materials thoroughly."
    ),
    tools_enabled=["read_file", "grep", "bash", "write_file", "edit_file"],
    memory_read=True,
    memory_write=True,
    ask_confirmation_required=False,
    context_budget="heavy",
    response_style="tutor",
)

_PROFILE_REGISTRY: dict[AgentMode, ModeProfile] = {
    AgentMode.CHAT: CHAT_PROFILE,
    AgentMode.ASK: ASK_PROFILE,
    AgentMode.STUDY: STUDY_PROFILE,
}


def resolve_profile(mode: AgentMode) -> ModeProfile:
    """根据模式解析对应的 ModeProfile。"""
    if mode not in _PROFILE_REGISTRY:
        raise ValueError(f"Unknown agent mode: {mode}")
    return _PROFILE_REGISTRY[mode]


def build_turn_profile(
    mode: AgentMode,
    *,
    override_system_prompt: str | None = None,
    override_tools: list[str] | None = None,
) -> TurnExecutionProfile:
    """
    由 Product 层调用，将 ModeProfile 转换为 Runtime 可直接消费的
    TurnExecutionProfile。支持局部覆盖，用于未来动态调整。
    """
    profile = resolve_profile(mode)
    return TurnExecutionProfile(
        mode=profile.mode,
        system_prompt=override_system_prompt or profile.system_prompt,
        visible_tools=override_tools or profile.tools_enabled,
        memory_read=profile.memory_read,
        memory_write=profile.memory_write,
        response_style=profile.response_style,
        ask_confirmation_required=profile.ask_confirmation_required,
    )
```

### 3.4 更新 `ChatRequest`

**文件**：`learning_agent/web/web_server.py`

将：

```python
class ChatRequest(BaseModel):
    message: str
    stream: bool = True
    ask_mode: bool = False
```

改为：

```python
from learning_agent.ai.models import AgentMode


class ChatRequest(BaseModel):
    message: str
    stream: bool = True
    mode: AgentMode = AgentMode.CHAT
```

说明：

- 前端发送请求时携带 `mode` 字段。
- 后端应直接利用 Pydantic / FastAPI 对 `mode` 做枚举校验，避免 `mode: str` 进入业务层后才报错。
- 后端在校验 `session.mode` 与请求 `mode` 的一致性后，再执行后续逻辑。
- 如果请求 `mode` 与当前 `session.mode` 不同，视为用户意图切换模式。
- SSE payload 也应从 `ask_mode` 迁移为 `mode`，必要时可附带 `alignment: bool`。

---

## 四、后端实现步骤

### 4.1 `session_manager.py` — 模式切换与清理

**文件**：`learning_agent/learning_agent/session_manager.py`

新增/修改以下内容：

```python
from learning_agent.ai.models import AgentMode


class SessionManager:
    ...

    def switch_session_mode(
        self,
        session_id: str,
        mode: AgentMode,
        *,
        clear_ask_state: bool = True,
    ) -> LearningSession:
        """切换 session 的产品模式，并清理相关状态。"""
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        from_mode = session.mode
        if from_mode == mode:
            return session

        # 从 Ask 切出时，默认清理 ask_state；但 Ask 确认收口场景需要延后清理
        if clear_ask_state and from_mode == AgentMode.ASK and mode != AgentMode.ASK:
            session.ask_state = AskState()

        session.mode = mode
        session.mode_metadata["last_mode_switch"] = {
            "from": from_mode.value,
            "to": mode.value,
        }
        session.last_accessed_at = datetime.now(timezone.utc)
        self._persist(session)
        return session
```

### 4.2 `main.py` — `LearningAgentSystem` 接入 Mode Profile

**文件**：`learning_agent/learning_agent/main.py`

#### 4.2.1 导入新增模块

```python
from learning_agent.ai.mode_profile import (
    AgentMode,
    build_turn_profile,
    TurnExecutionProfile,
)
```

#### 4.2.2 改造 `stream_session_chat`

这里需要补一条**此前文档遗漏的核心规则**：

- `Ask` 是显式前置对齐流程，不是永久粘性模式。
- 当 session 正处于 Ask 对齐态，且用户本轮输入属于“确认/继续”语义时，本轮正式执行应按 `post_ask_target` 对应模式运行。
- 若未显式指定 `post_ask_target`，默认收口到 `Chat`。

为避免 Product 层直接依赖 Runtime 私有 helper，建议把“确认语义识别”提取为共享函数，例如：

```python
def is_confirmation_message(text: str) -> bool:
    ...
```

将：

```python
async def stream_session_chat(
    self,
    session_id: str,
    user_input: str,
    ask_mode: bool = False,
) -> AsyncGenerator[ChatChunk, None]:
    ...
    async for chunk in self.agent_loop.run(session, user_input, ask_mode=ask_mode):
        yield chunk
```

改为：

```python
async def stream_session_chat(
    self,
    session_id: str,
    user_input: str,
    mode: AgentMode = AgentMode.CHAT,
) -> AsyncGenerator[ChatChunk, None]:
    """将产品级聊天请求路由到指定 session runtime。

    参数 mode 为前端请求携带的目标模式。若与 session 当前模式不一致，
    则先切换 session 模式。
    """
    if self.agent_loop is None:
        raise RuntimeError("Agent loop is not initialized")

    session = self.get_session(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    requested_mode = mode
    effective_mode = requested_mode

    # Ask 确认完成后的模式收口：
    # 本轮正式回答按 post_ask_target 执行，默认回到 chat。
    if (
        session.mode == AgentMode.ASK
        and session.ask_state.status == "aligning"
        and requested_mode == AgentMode.ASK
        and is_confirmation_message(user_input)
    ):
        target_mode = AgentMode(session.mode_metadata.get("post_ask_target", "chat"))
        if target_mode != session.mode:
            session = self.session_manager.switch_session_mode(
                session_id,
                target_mode,
                clear_ask_state=False,
            )
        effective_mode = target_mode
    elif session.mode != requested_mode:
        session = self.session_manager.switch_session_mode(session_id, requested_mode)
        effective_mode = requested_mode

    # 组装本轮执行配置
    turn_profile = build_turn_profile(effective_mode)

    try:
        async for chunk in self.agent_loop.run(
            session, user_input, profile=turn_profile
        ):
            yield chunk
    finally:
        try:
            self.save_session(session_id)
        except Exception:
            logger.exception(f"[System] Failed to save session {session_id}")
```

#### 4.2.3 同步改造 `collect_session_chat`

```python
async def collect_session_chat(
    self,
    session_id: str,
    user_input: str,
    mode: AgentMode = AgentMode.CHAT,
) -> str:
    content_parts = []
    async for chunk in self.stream_session_chat(session_id, user_input, mode=mode):
        content_parts.append(chunk.content)
    return "".join(content_parts)
```

补充说明：

- `switch_session_mode(..., clear_ask_state=False)` 只用于 Ask 确认收口这一条路径，避免在 Runtime 消费 `confirmed_input` 之前把状态过早抹掉。
- 真正的 `ask_state` 清理由 Runtime 确认分支完成后执行，保证模式收口与确认输入消费不会互相覆盖。
- CLI 入口也应统一走 `mode=AgentMode.ASK`，保留 `/ask` 作为语法糖命令别名即可。

### 4.3 `agent_loop.py` — Runtime 消费 `TurnExecutionProfile`

**文件**：`learning_agent/agent/agent_loop.py`

#### 4.3.1 导入新增类型

```python
from learning_agent.ai.mode_profile import TurnExecutionProfile, AgentMode
```

#### 4.3.2 改造 `AgentLoop.run`

将：

```python
async def run(
    self,
    session: LearningSession,
    user_input: str,
    ask_mode: bool = False,
) -> AsyncIterable[ChatChunk]:
    ...
    async for chunk in runtime.run_turn(session, user_input, ask_mode=ask_mode):
        yield chunk
```

改为：

```python
async def run(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
) -> AsyncIterable[ChatChunk]:
    ...
    async for chunk in runtime.run_turn(session, user_input, profile=profile):
        yield chunk
```

#### 4.3.3 改造 `AgentLoopSession.run_turn`

将方法签名：

```python
async def run_turn(
    self,
    session: LearningSession,
    user_input: str,
    ask_mode: bool = False,
) -> AsyncIterable[ChatChunk]:
```

改为：

```python
async def run_turn(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
) -> AsyncIterable[ChatChunk]:
```

#### 4.3.4 Ask 对齐逻辑适配

将现有逻辑：

```python
is_aligning = session.ask_state.status == "aligning"
if ask_mode or is_aligning:
```

改为：

```python
is_aligning = session.ask_state.status == "aligning"
if profile.mode == AgentMode.ASK or is_aligning:
```

#### 4.3.5 `_run_alignment_turn` 使用 profile 的 system prompt

将：

```python
def _build_alignment_messages(self, session, user_input):
    ...
    system_prompt = (
        "You are a learning assistant in **Alignment Mode**.\n\n"
        ...
    )
```

改为使用 `profile.system_prompt`：

```python
async def _run_alignment_turn(
    self,
    session: LearningSession,
    user_input: str,
    profile: TurnExecutionProfile,
    parent_span: Any = None,
) -> AsyncIterable[ChatChunk]:
    """执行 Ask 对齐轮（单轮 LLM，非 ReACT）。"""
    self._set_state(AgentState.ALIGNING)
    ...

    messages: list[ChatMessage] = []
    messages.append(ChatMessage(role=MessageRole.SYSTEM, content=profile.system_prompt))

    history = self.sessions.get_message_history(session.id)
    for entry in history:
        messages.append(ChatMessage(role=entry.role, content=entry.content))

    messages.append(ChatMessage(role=MessageRole.USER, content=user_input))
    ...
```

同时补充消息持久化约定：

```python
self.sessions.append_message(
    session.id,
    MessageRole.USER,
    user_input,
    metadata={"mode": AgentMode.ASK.value, "alignment": True},
)
...
self.sessions.append_message(
    session.id,
    MessageRole.ASSISTANT,
    full_content,
    metadata={"mode": AgentMode.ASK.value, "alignment": True},
)
```

#### 4.3.6 `_build_context_for_turn` 使用 profile 的 system prompt

将：

```python
async def _build_context_for_turn(self, session: LearningSession) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    system_prompt = self.agent_loop._get_system_prompt()
    messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    ...
```

改为接收 profile：

```python
async def _build_context_for_turn(
    self,
    session: LearningSession,
    profile: TurnExecutionProfile,
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    messages.append(ChatMessage(role=MessageRole.SYSTEM, content=profile.system_prompt))
    ...
```

同时，在 `run_turn` 中调用 `_build_context_for_turn` 的地方传入 `profile`。

另外，普通 ReACT 主链写入消息时，也应统一补 `metadata={"mode": profile.mode.value}`，避免历史回放丢失模式信息。

#### 4.3.7 工具可见性控制

在 `run_turn` 中，找到获取 tools for LLM 的代码（约 L481）：

```python
tools_for_llm = self.tools.list_tools()
```

改为按 `profile.visible_tools` 过滤：

```python
all_tools = self.tools.list_tools()
tools_for_llm = [
    t for t in all_tools
    if t.name in profile.visible_tools
]
```

> 如果 `profile.visible_tools` 为空列表，则传空列表给 LLM（即不开放任何工具）。

#### 4.3.8 清理 `_get_system_prompt` 硬编码（可选但推荐）

`_get_system_prompt` 方法在改造后仅作为兜底保留，或被完全移除。

推荐做法：

```python
def _get_system_prompt(self) -> str:
    """兜底系统提示词。正式 turn 应通过 TurnExecutionProfile 注入。"""
    return (
        "You are a learning assistant. Help the user understand concepts deeply, "
        "ask clarifying questions when needed, and encourage active recall."
    )
```

实际运行时，优先使用 `profile.system_prompt`，仅在异常恢复路径中 fallback 到此方法。

### 4.4 `web_server.py` — API 透传与模式切换接口

**文件**：`learning_agent/web/web_server.py`

#### 4.4.1 改造 chat 端点

将：

```python
@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest) -> Any:
    if req.stream:
        return StreamingResponse(
            _stream_chat_chunks(system, session_id, req.message, ask_mode=req.ask_mode),
            media_type="text/event-stream",
        )
```

改为：

```python
@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, req: ChatRequest) -> Any:
    if req.stream:
        return StreamingResponse(
            _stream_chat_chunks(system, session_id, req.message, mode=req.mode),
            media_type="text/event-stream",
        )
    ...
```

并同步改造 `_stream_chat_chunks` 和同步 chat 分支的调用。

`_stream_chat_chunks` 建议同步改为：

```python
async def _stream_chat_chunks(
    system: LearningAgentSystem,
    session_id: str,
    message: str,
    mode: AgentMode = AgentMode.CHAT,
) -> AsyncGenerator[str, None]:
    async for chunk in system.stream_session_chat(session_id, message, mode=mode):
        payload = {
            "content": chunk.content,
            "tool_call": chunk.tool_call,
            "finish_reason": chunk.finish_reason,
            "mode": mode.value,
        }
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
```

错误语义要求：

- `session not found` 返回 404 / SSE error payload
- `invalid mode` 不应复用 `ValueError("Session ... not found")` 路径，建议在请求模型层直接拦截
- 若兼容期需要接受旧字段 `ask_mode`，应显式标注为 deprecated 并在一个版本后移除

#### 4.4.2 新增模式切换端点

```python
from learning_agent.ai.models import AgentMode


class UpdateModeRequest(BaseModel):
    mode: AgentMode


@app.put("/sessions/{session_id}/mode")
async def update_session_mode(session_id: str, req: UpdateModeRequest) -> dict[str, Any]:
    """显式切换 session 的产品模式。"""
    if system.session_manager is None:
        raise HTTPException(status_code=503, detail="System not initialized")

    session = system.session_manager.switch_session_mode(session_id, req.mode)
    return {"session_id": session.id, "mode": session.mode.value}
```

说明：

- 前端点击模式按钮时，可先调用此接口切换模式，再发送消息。
- 也可在发送消息时携带 `mode`，由 `stream_session_chat` 自动处理切换。
- 两种方式可并存，前端自行选择。

#### 4.4.3 获取 session 模式的服务端同步来源

前端切换 session 或刷新页面后，必须能够恢复服务端真实的 `session.mode`。推荐优先使用已有的 `GET /sessions/{session_id}`，因为 `LearningSession` 模型扩展后会天然返回：

- `mode`
- `mode_metadata`
- `ask_state`

如果确实需要轻量查询，再补一个专门的 mode 端点：

```python
@app.get("/sessions/{session_id}/mode")
async def get_session_mode(session_id: str) -> dict[str, Any]:
    session = system.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session.id,
        "mode": session.mode.value,
        "ask_state": session.ask_state.status,
    }
```

前端约束：

- `selectSession()` 或首屏恢复 last session 时，应先拉取服务端 session，再设置本地 `currentMode`。
- toolbar 的高亮状态以服务端 `session.mode` 为准，而不是沿用上一个 session 的本地状态。

---

## 五、观测系统同步改造

模式落地后，观测系统必须能区分不同模式的执行轨迹，否则后续排查问题时会丢失「用户到底在哪个模式下运行」这一关键上下文。

### 5.1 改造原则

- **零侵入 Runtime 核心**：观测系统只在 `AgentLoopSession` 已有 hook 点中读取 `profile.mode`，不新增独立分支。
- **全链路透传**：从 trace 创建 → span tag → event payload → flow step，mode 信息应完整贯穿。
- **向后兼容**：旧 trace 无 `mode` 字段时，前端/后端均做兼容处理。

### 5.2 `models.py` — `Trace` 增加 `mode` 字段

**文件**：`learning_agent/ai/models.py`

在 `Trace` 模型中增加：

```python
class Trace(BaseModel):
    trace_id: str = Field(default_factory=lambda: f"trace-{uuid.uuid4().hex[:8]}")
    session_id: Optional[str] = None
    objective_id: Optional[str] = None
    mode: Optional[str] = None          # "chat" | "ask" | "study"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: Optional[int] = None
    spans: list[TraceSpan] = Field(default_factory=list)
    ...
```

### 5.3 `observability.py` — `start_trace` 接收 `mode`

**文件**：`learning_agent/agent/observability.py`

将 `start_trace` 签名：

```python
def start_trace(
    self,
    session_id: Optional[str] = None,
    objective_id: Optional[str] = None,
) -> Trace:
```

改为：

```python
def start_trace(
    self,
    session_id: Optional[str] = None,
    objective_id: Optional[str] = None,
    mode: Optional[str] = None,
) -> Trace:
    trace = Trace(session_id=session_id, objective_id=objective_id, mode=mode)
    ...
```

### 5.4 `agent_loop.py` — trace / span / event 注入 mode

**文件**：`learning_agent/agent/agent_loop.py`

#### 5.4.1 trace 创建时传入 mode

在 `run_turn` 中（约 L335）：

```python
if self.obs:
    trace = self.obs.start_trace(
        session_id=session.id,
        objective_id=session.objective_id,
        mode=profile.mode.value,
    )
    self._current_trace_id = trace.trace_id if trace else None
    root_span = self.obs.start_span("agent.loop", trace_id=self._current_trace_id)
    if root_span:
        root_span.tags["mode"] = profile.mode.value
```

#### 5.4.2 `_emit_agent_event` payload 中增加 mode

在 `run_turn` 的 `AGENT_START` 事件发射处（约 L347）：

```python
await self._emit_agent_event(
    AgentEventType.AGENT_START,
    {
        "session_id": session.id,
        "user_input": user_input,
        "mode": profile.mode.value,
    },
    session.id,
)
```

> 其余事件（`TURN_START`, `TOOL_EXECUTION_START` 等）如已有 `payload`，可酌情追加 `mode`，但最低要求是 `AGENT_START` 必须携带。

#### 5.4.3 HookContext metadata 中传递 mode

在 `before_agent_run` hook 调用处（约 L402）：

```python
context=self._build_hook_context(
    session,
    metadata={"mode": profile.mode.value},
),
```

> 这样 `core-fulltrace` 等扩展可以通过 `hook_input.context.metadata.get("mode")` 读取当前模式。

#### 5.4.4 Ask 对齐轮的 span tag

在 `_run_alignment_turn` 中（约 L924）：

```python
llm_span = self.obs.start_span("ask.alignment", parent=parent_span, trace_id=self._current_trace_id) if self.obs else None
if llm_span:
    llm_span.tags["mode"] = "ask"
```

### 5.5 `built_in.py` — `core-fulltrace` 扩展记录 mode

**文件**：`learning_agent/learning_agent/extensions/built_in.py`

在 `_fulltrace_hook_before_agent_run` 中：

```python
async def _fulltrace_hook_before_agent_run(
    hook_input: BeforeAgentRunInput,
) -> BeforeAgentRunResult:
    sid = hook_input.context.session_id
    mode = hook_input.context.metadata.get("mode", "chat")
    _fulltrace_add_step(
        sid,
        "before_agent_run",
        {
            "user_input": hook_input.user_input[:1000],
            "tool_count": len(hook_input.tools_summary),
            "provider": hook_input.provider_summary,
            "mode": mode,
        },
    )
    return BeforeAgentRunResult()
```

> 其余 flow step（`llm_stream_start`, `tool_result`, `final_response`）可不重复记录 mode，因为同一 flow 内的所有 step 共享 session，可通过首个 step 的 `mode` 字段推断。

### 5.6 `web/static/observability.js` — 可选展示增强

**文件**：`web/static/observability.js`

#### 5.6.1 Trace 列表摘要返回 mode

若前端 trace 列表直接调用 `/observability/traces`，后端摘要对象也应补 `mode`，避免为每张卡片额外请求详情：

```python
traces.append({
    "trace_id": data.get("trace_id"),
    "session_id": data.get("session_id"),
    "timestamp": data.get("timestamp"),
    "duration_ms": data.get("duration_ms"),
    "span_count": len(data.get("spans", [])),
    "mode": data.get("mode"),
})
```

#### 5.6.2 Trace 卡片中显示 mode

在渲染 trace card 时，若 `trace.mode` 存在，增加一个小 badge：

```javascript
const modeBadge = trace.mode
    ? `<span class="trace-mode-badge mode-${trace.mode}">${trace.mode.toUpperCase()}</span>`
    : '';
```

CSS 样式（追加到 `style.css` 或内联）：

```css
.trace-mode-badge {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
    margin-left: 8px;
}
.trace-mode-badge.mode-chat { background: rgba(59,130,246,0.15); color: #3b82f6; }
.trace-mode-badge.mode-ask  { background: rgba(234,179,8,0.15); color: #eab308; }
.trace-mode-badge.mode-study{ background: rgba(139,92,246,0.15); color: #8b5cf6; }
```

#### 5.6.3 Runtime 表格中显示 mode

在 `renderRuntimes` 函数中，读取 `runtime.mode` 并展示：

```javascript
const mode = runtime.mode || 'chat';
// 在表格行中增加一列
`<td><span class="trace-mode-badge mode-${mode}">${mode}</span></td>`
```

> 后端 `/observability/runtimes` 端点如未返回 `mode`，需先在 `web_server.py` 中补充。

### 5.7 `web_server.py` — Runtimes 端点补充 mode

在 `/observability/runtimes` 的实现中，为每个 active session 补充 `mode`：

```python
{
    "session_id": sid,
    "state": runtime.state.value,
    "mode": session.mode.value if session else "chat",
    "chat_only_mode": runtime.chat_only_mode,
    ...
}
```

---

## 六、前端实现步骤

### 6.1 `index.html` — 模式工具栏改造

**文件**：`web/index.html`

将现有的：

```html
<!-- 模式工具栏（预留扩展位） -->
<div class="mode-toolbar" id="mode-toolbar">
    <button id="btn-ask-mode" class="btn-mode" title="Ask 模式：先对齐意图再回答" aria-label="切换 Ask 模式">
        <span class="mode-icon">❓</span>
        <span class="mode-label">Ask</span>
    </button>
    <!-- 后续可在此添加更多模式按钮 -->
</div>
```

改为显式三态选择器：

```html
<!-- 模式工具栏 -->
<div class="mode-toolbar" id="mode-toolbar">
    <button id="btn-mode-chat" class="btn-mode active" title="Chat 模式：快速对话" aria-label="Chat 模式">
        <span class="mode-icon">💬</span>
        <span class="mode-label">Chat</span>
    </button>
    <button id="btn-mode-ask" class="btn-mode" title="Ask 模式：先对齐意图再回答" aria-label="Ask 模式">
        <span class="mode-icon">❓</span>
        <span class="mode-label">Ask</span>
    </button>
    <button id="btn-mode-study" class="btn-mode disabled" title="Study 模式：深度学习（即将上线）" aria-label="Study 模式" disabled>
        <span class="mode-icon">📚</span>
        <span class="mode-label">Study</span>
    </button>
</div>
```

### 6.2 `style.css` — 新增 Study 与禁用态样式

**文件**：`web/static/style.css`

在 `.btn-mode` 样式区域补充：

```css
.btn-mode.disabled {
    opacity: 0.4;
    cursor: not-allowed;
    pointer-events: none;
}

/* Chat 模式激活态 */
.btn-mode.active[data-mode="chat"] {
    background: rgba(59, 130, 246, 0.15);
    color: #3b82f6;
    border-color: rgba(59, 130, 246, 0.3);
}

/* Study 占位样式 */
.message.assistant.study-mode {
    border-left: 3px solid #8b5cf6;
    background: rgba(139, 92, 246, 0.05);
}
```

### 6.3 `app.js` — 模式状态管理改造

**文件**：`web/static/app.js`

#### 6.3.1 状态变量替换

将：

```javascript
let isAskMode = false;  // Ask 对齐模式开关
```

改为：

```javascript
let currentMode = 'chat';  // 'chat' | 'ask' | 'study'
```

#### 6.3.2 DOM 引用更新

将：

```javascript
btnAskMode: document.getElementById('btn-ask-mode'),
```

改为：

```javascript
btnModeChat: document.getElementById('btn-mode-chat'),
btnModeAsk: document.getElementById('btn-mode-ask'),
btnModeStudy: document.getElementById('btn-mode-study'),
```

#### 6.3.3 初始化与事件绑定

在初始化逻辑中，替换原有的 Ask 按钮事件：

```javascript
// 模式按钮事件
els.btnModeChat.addEventListener('click', () => switchMode('chat'));
els.btnModeAsk.addEventListener('click', () => switchMode('ask'));
// Study 按钮已 disabled，无需绑定

async function switchMode(mode) {
    if (currentMode === mode) return;
    const previousMode = currentMode;
    currentMode = mode;
    updateModeToolbar();

    // 若当前已有 session，优先同步到服务端，失败则回滚本地状态
    if (currentSessionId) {
        try {
            await api('PUT', `/sessions/${currentSessionId}/mode`, { mode });
        } catch (err) {
            currentMode = previousMode;
            updateModeToolbar();
            throw err;
        }
    }
}

function updateModeToolbar() {
    [els.btnModeChat, els.btnModeAsk, els.btnModeStudy].forEach(btn => {
        btn.classList.remove('active');
    });
    const activeBtn = {
        chat: els.btnModeChat,
        ask: els.btnModeAsk,
        study: els.btnModeStudy,
    }[currentMode];
    if (activeBtn) activeBtn.classList.add('active');
}
```

#### 6.3.3A 切换 session / 页面刷新时恢复服务端 mode

在 `selectSession()` 或 `loadSessionHistory()` 过程中，补充：

```javascript
async function selectSession(id, title) {
    currentSessionId = id;
    ...

    const session = await api('GET', `/sessions/${id}`);
    currentMode = session.mode || 'chat';
    updateModeToolbar();

    await loadSessionHistory(id, session);
    updateAskPlaceholder(session.ask_state?.status === 'aligning');
}
```

说明：

- 这里的 `currentMode` 以服务端 session 为准。
- 这样才能避免“上一个 session 处于 Ask，本次切到另一个 session 仍高亮 Ask”的前端漂移问题。
- 若保留 `GET /sessions/{id}/mode` 轻量端点，也可在这里调用该端点恢复模式。

#### 6.3.4 发送消息时携带 mode

将：

```javascript
addMessage('user', text.trim(), { askMode: isAskMode });
...
body: JSON.stringify({ message: text.trim(), stream: true, ask_mode: isAskMode }),
```

改为：

```javascript
addMessage('user', text.trim(), { mode: currentMode });
...
body: JSON.stringify({ message: text.trim(), stream: true, mode: currentMode }),
```

#### 6.3.5 消息样式适配

修改 `addMessage` 或相关样式逻辑，根据 `mode` 而不是 `askMode` 添加 CSS class：

```javascript
function addMessage(role, content, options = {}) {
    const div = document.createElement('div');
    div.className = `message ${role}`;
    if (options.mode === 'ask') div.classList.add('ask-mode');
    if (options.mode === 'study') div.classList.add('study-mode');
    if (options.alignment) div.classList.add('alignment');
    ...
}
```

历史消息回放也要走同一套规则：

```javascript
function renderHistoryMessage(role, text, metadata = {}) {
    addMessage(role, text, {
        mode: metadata.mode || 'chat',
        alignment: Boolean(metadata.alignment),
    });
}
```

#### 6.3.6 对齐轮样式发送逻辑

将：

```javascript
addMessage('assistant', '', { alignment: isAskMode });
```

改为：

```javascript
addMessage('assistant', '', { alignment: currentMode === 'ask' });
```

#### 6.3.7 保留 Ask 确认态 placeholder 逻辑

`updateAskPlaceholder` 函数保留不变，因为它检查的是 `session.ask_state.status`，与本期改造不冲突。

---

## 七、Study 模式预留接口

本期 Study 不做任何实际能力，但必须保证以下接口可用，为后续无缝扩展做准备。

### 7.1 后端预留

1. `AgentMode.STUDY` 枚举值已定义。
2. `STUDY_PROFILE` 已注册在 profile 注册表中。
3. `session.mode` 可设为 `AgentMode.STUDY`。
4. `stream_session_chat` 传入 `mode="study"` 时，能正常组装 `TurnExecutionProfile` 并进入 Runtime。
5. `AgentLoop.run_turn` 对 `profile.mode == AgentMode.STUDY` 走与 Chat 相同的 ReACT 主链（不特殊处理）。

### 7.2 前端预留

1. `btn-mode-study` 按钮已存在于 toolbar，但 `disabled`。
2. `currentMode` 变量可接受 `'study'`。
3. 消息样式 `.study-mode` 已预留。

### 7.3 后续扩展 Study 时只需

- 移除 `btn-mode-study` 的 `disabled` 属性。
- 为 `STUDY_PROFILE` 接入 memory read/write（当 memory 主链就绪后）。
- 优化 `STUDY_PROFILE.system_prompt`。
- 如需，增加 Study 特有的工具调用策略或输出模板。

---

## 八、Prompt 策略

### 8.1 迁移原则

- **不再在 `agent_loop.py` 中写死 prompt**。所有模式级 system prompt 统一收进 `mode_profile.py`。
- `agent_loop.py` 中的 `_get_system_prompt()` 降级为兜底方法。
- 未来如需支持用户自定义 prompt，可在 `build_turn_profile()` 中增加 `override_system_prompt` 参数。

### 8.2 本期 Prompt 清单

| 模式 | 存放位置 | 说明 |
|------|----------|------|
| Chat | `mode_profile.py` → `CHAT_PROFILE.system_prompt` | 直接、简洁、按需澄清、轻量工具 |
| Ask | `mode_profile.py` → `ASK_PROFILE.system_prompt` | 重述意图、给出计划、询问确认、不直接给答案 |
| Study | `mode_profile.py` → `STUDY_PROFILE.system_prompt` | 学习导向、结构化解释、例子、回顾（本期仅占位） |
| 兜底 | `agent_loop.py` → `_get_system_prompt()` | 异常恢复时的 fallback |

---

## 九、API 变更清单

### 9.1 变更接口

| 方法 | 端点 | 变更内容 |
|------|------|----------|
| POST | `/sessions/{id}/chat` | 请求体 `ask_mode: bool` → `mode: AgentMode` |

### 9.2 新增接口

| 方法 | 端点 | 说明 |
|------|------|------|
| PUT | `/sessions/{id}/mode` | 显式切换 session 模式 |
| GET | `/sessions/{id}/mode` | 获取 session 当前模式与 ask_state（轻量接口，可选） |

### 9.3 SSE Payload 变更

建议同步调整 SSE 事件体：

```json
{
  "content": "...",
  "tool_call": null,
  "finish_reason": null,
  "mode": "ask"
}
```

如需区分 Ask 对齐轮，可额外增加：

```json
{
  "alignment": true
}
```

### 9.4 请求/响应示例

**切换模式：**

```http
PUT /sessions/sess-abc123/mode
Content-Type: application/json

{"mode": "ask"}
```

```json
{"session_id": "sess-abc123", "mode": "ask"}
```

**发送消息（Ask 模式）：**

```http
POST /sessions/sess-abc123/chat
Content-Type: application/json

{"message": "帮我写一个爬虫", "stream": true, "mode": "ask"}
```

---

## 十、文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `learning_agent/ai/models.py` | 修改 | 新增 `AgentMode` 枚举；`LearningSession` 增加 `mode` 与 `mode_metadata` |
| `learning_agent/ai/mode_profile.py` | 新增 | `ModeProfile`、`TurnExecutionProfile`、预置 profile 注册表、解析函数 |
| `learning_agent/learning_agent/session_manager.py` | 修改 | 新增 `switch_session_mode` 方法 |
| `learning_agent/learning_agent/main.py` | 修改 | `stream_session_chat` / `collect_session_chat` 接入 mode profile；Ask 确认收口到目标模式；CLI `/ask` 走 `mode="ask"` |
| `learning_agent/agent/agent_loop.py` | 修改 | `run_turn` 消费 `TurnExecutionProfile`；`_build_context_for_turn` 注入 profile system_prompt；工具按 `visible_tools` 过滤 |
| `learning_agent/web/web_server.py` | 修改 | `ChatRequest` 字段变更；SSE payload 使用 `mode`；新增 `PUT /sessions/{id}/mode`；可选新增 `GET /sessions/{id}/mode` |
| `web/index.html` | 修改 | mode toolbar 从单按钮改为三态选择器 |
| `web/static/style.css` | 修改 | 新增 `.disabled`、Chat 激活态、Study 消息样式 |
| `web/static/app.js` | 修改 | `isAskMode` → `currentMode`；模式切换函数；切换 session 时从服务端恢复 mode；历史消息按 metadata 回放 |
| `learning_agent/agent/observability.py` | 修改 | `start_trace` 增加 `mode` 参数，Trace 持久化时记录模式 |
| `learning_agent/learning_agent/extensions/built_in.py` | 修改 | `core-fulltrace` 扩展的 `before_agent_run` flow step 增加 `mode` 字段 |
| `web/static/observability.js` | 修改（可选） | Trace 卡片与 Runtime 表格增加 mode badge 展示 |
| `tests/test_web_adaptation.py` | 修改 | 请求模型、SSE payload、系统调用参数从 `ask_mode` 改为 `mode` |
| `docs/design/design-impl-chat-ask-modes.md` | 新增 | 本文档 |

---

## 十一、实现顺序建议

建议按以下顺序分步实现，每步可独立验证：

### Step 1：数据模型与 Profile（无风险）

1. 新建 `mode_profile.py`
2. 在 `models.py` 新增 `AgentMode` 和 `LearningSession` 扩展
3. 运行现有测试，确认无 break

### Step 2：Runtime 层适配（中等风险）

1. 改造 `agent_loop.py`：
   - `run_turn` 签名改为接收 `profile: TurnExecutionProfile`
   - `_build_context_for_turn` 使用 `profile.system_prompt`
   - `_run_alignment_turn` 使用 `profile.system_prompt`
   - 工具列表按 `profile.visible_tools` 过滤
2. 此时 `AgentLoop.run` 的新签名与 `main.py` 旧调用不兼容，需同步改 `main.py`

### Step 3：产品层适配（中等风险）

1. 改造 `main.py`：`stream_session_chat` / `collect_session_chat` 接入 mode
2. 改造 `session_manager.py`：新增 `switch_session_mode`
3. 实现 Ask 确认后的模式收口：默认 `Ask -> Chat`，可按 `post_ask_target` 切到 `Study`
4. 跑通后端端到端（CLI 或 curl）

### Step 4：API 层适配（低风险）

1. 改造 `web_server.py`：
   - `ChatRequest` 字段
   - chat 端点透传 `mode`
   - SSE payload `ask_mode` → `mode`
   - 新增 `PUT /sessions/{id}/mode`
2. 用 curl 验证 API

### Step 5：前端适配（低风险）

1. 改造 `index.html` toolbar
2. 改造 `style.css`
3. 改造 `app.js`
   - session 切换时从服务端恢复 `mode`
   - 历史消息按 `entry.metadata` 恢复 Ask / Study 样式
4. 浏览器端到端验证

### Step 6：观测系统同步（低风险）

1. 改造 `observability.py`：`start_trace` 接收 `mode`
2. 改造 `agent_loop.py`：trace / span tag / event payload / HookContext metadata 注入 `mode`
3. 改造 `built_in.py`：`core-fulltrace` 的 flow step 记录 `mode`
4. 可选：改造 `observability.js` + `web_server.py` runtimes 端点，展示 mode badge

### Step 7：回归测试

1. 运行全部测试
2. 验证：
   - Chat 模式默认行为与改造前一致
   - Ask 模式显式开启、对齐、确认闭环正常
   - Ask 确认后，session.mode 默认回到 `chat`
   - Study 模式可设为 `study`，行为与 Chat 相同（占位验证）
   - 模式切换时 ask_state 正确清理
   - session 切换 / 刷新页面后，toolbar 能恢复服务端 mode
   - 历史消息 reload 后仍保留 Ask / Study 样式
   - Web SSE payload 与非流式接口均返回正确 `mode`
   - CLI `/ask` 与普通输入都走新的 `mode` 语义
   - 观测系统 trace / flow / event 中均能读取到 `mode` 字段

---

## 十二、兼容性说明

### 12.1 向后兼容

- `LearningSession` 新增字段均有默认值（`mode=CHAT`, `mode_metadata={}`），旧 session 数据反序列化后自动使用默认值。
- HTTP 请求体从 `ask_mode` 改为 `mode`，这对旧 Web/CLI 调用方是**显式接口变更**。
- 若需要平滑迁移，建议在过渡期同时接受 `mode` 与 `ask_mode`，并将 `ask_mode` 标记为 deprecated；本期若不做兼容层，则必须同步更新调用方。

### 12.2 需要同步更新的调用方

- `web/static/app.js`：必须同步更新，否则请求字段不匹配。
- `interactive_cli()` / `LearningAgentSystem.chat()`：如直接调用 `stream_session_chat(..., ask_mode=...)`，需改为 `mode=...`。
- `tests/test_web_adaptation.py`：断言参数与请求模型均需更新。

---

## 十三、风险与规避

| 风险 | 缓解方式 |
|------|----------|
| `agent_loop.py` 改动面大，易引入 regression | 每改一个方法即运行测试；保持 `_get_system_prompt` 兜底 |
| 前端 `isAskMode` 全局变量散落多处 | 用 IDE 全局搜索 `isAskMode`，逐一替换 |
| Ask 确认后 mode 未收口，导致后续每轮继续对齐 | 在 Product 层补 `post_ask_target` 收口规则，并加入回归测试 |
| 前端 toolbar 状态与服务端 `session.mode` 漂移 | session 切换 / 刷新时始终以服务端返回的 `mode` 恢复本地状态 |
| 历史消息 reload 后丢失 Ask / Study 样式 | 在 `SessionEntry.metadata` 中持久化 `mode` / `alignment`，前端按 metadata 回放 |
| `invalid mode` 与 `session not found` 错误语义混淆 | 请求模型改用 `AgentMode` 枚举；避免业务层统一抛 `ValueError` |
| Study 占位接口后续扩展时 breaking | Study 本期走与 Chat 相同的 ReACT 主链，不新增特殊分支，后续扩展只需叠加 |
| 旧 session 无 `mode` 字段 | Pydantic 默认值自动处理，无需迁移脚本 |

---

*文档完成。下一步：按「实现顺序建议」分步编码，每步验证后推进。*
