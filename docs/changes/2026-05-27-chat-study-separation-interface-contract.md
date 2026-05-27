# 2026-05-27 闲聊 / 研学 模式分离 — 接口契约

> 双 AI 并行开发的共享边界文档。**先定接口、再并行**。
> 任何一方在动 §3 的共享面之前，必须先看这份文档；改动共享 schema 必须双方同意。

## 目标

把"闲聊模式"和"研学模式"真正分离，使两个 AI 能并行开发互不撞车：

- 一个 AI 完善 **闲聊**（本文档作者方）。
- 一个 AI 开发 **研学**（学习卷 / absorbing / outputting / 对齐门）。

分离的硬指标：**闲聊与研学不再共用同一个执行 profile**；改一边的提示词 / 工具 / 预算不会泄漏到另一边。

## 一、现状：为什么现在没分离（扎在真实代码上）

### 1.1 真实分叉不是按 UI mode，而是按"有没有学习卷"

[`main.py:_prepare_session_turn`](../../learning_agent/learning_agent/main.py)：

```
if session.learning_unit_id:
    → _prepare_learning_unit_turn(...)      # 研学领地
else:
    → CHAT（ASK 在这里被归一回 CHAT）         # 闲聊领地
```

### 1.2 `AgentMode.CHAT` 身兼两职（核心耦合）

研学的 absorbing 阶段**借用** `AgentMode.CHAT` 在跑（[`main.py` `_prepare_learning_unit_turn`](../../learning_agent/learning_agent/main.py)）：

```
absorbing  → CHAT（默认）/ ASK（对齐门临时覆写）
outputting → TEACH
consolidated → terminal（只读）
```

于是 `AgentMode.CHAT` 同时表示两件事：

1. 纯闲聊会话（无学习卷）。
2. 研学 absorbing 回合（有学习卷）。

两者都走同一个 `CHAT_PROFILE` + `CHAT_MODE_PROMPT`。**改 CHAT_PROFILE 就会同时改到研学 absorbing。** 这就是没分离的根。

### 1.3 `AgentMode.STUDY` 是孤儿

`STUDY_PROFILE` / `STUDY_MODE_PROMPT` 都定义好了（memory 读写 + heavy 预算 + tutor 风格 + "结论/原理/例子/小结"提示词），但 **main.py 路由里从来没人把 mode 设成 STUDY**。它是定义了却没接线的死模式——正好留给研学用。

### 1.4 当前 CHAT_PROFILE vs STUDY_PROFILE 的确切差异

| 字段 | CHAT_PROFILE | STUDY_PROFILE |
|---|---|---|
| `tools_enabled` | read_file, grep, web_search, web_fetch, write_file, edit_file | **同上 6 个** |
| `memory_read` | `False` | `True` |
| `memory_write` | `False` | `True` |
| `context_budget` | `"light"` | `"heavy"` |
| `response_style` | `"direct"` | `"tutor"` |
| `recent_token_budget` | `16000` | `24000` |
| `default_turn_kind` | `REACT` | `REACT` |
| mode 提示词 | CHAT：快速、低摩擦、精炼 | STUDY：深度讲解、结论/原理/例子/小结、鼓励回忆复盘 |

系统提示词组装：`NEUTRAL_GUARDRAILS + build_mode_prompt(mode) + persona.tone_prompt` (+ 可选 addendum)。

## 二、目标态：CHAT 只服务闲聊

- `AgentMode.CHAT` / `CHAT_PROFILE` / `CHAT_MODE_PROMPT` → **专属闲聊**（无学习卷的 `else` 分支）。
- 研学 absorbing → 改走 `AgentMode.STUDY`（复活孤儿）。
- 研学的所有 `effective_mode == AgentMode.CHAT` 判定 → 改成 `== AgentMode.STUDY`。

这样两张 profile 各属一方，可独立调。

## 三、接口契约（共享面 — 改动需双方同意）

这些是双方都依赖的"接口"，**不属于任何一方私有**，单方不得擅改语义：

1. **`ModeProfile` / `TurnExecutionProfile` schema**（[`mode_service.py`](../../learning_agent/learning_agent/mode_service.py)）
   字段集合是契约。新增字段需双方同意；任一方不得为自己方便删字段或改字段语义。
2. **`build_turn_profile(...)` 签名与装配逻辑**
   入参（persona_key / turn_kind / override_* / *_metadata）与"profile + persona + addendum"的组装顺序是契约。
3. **`_prepare_session_turn` 的分叉点本身**（`if session.learning_unit_id`）
   这是两个领地的物理边界。**由闲聊方做一次性收口**（见 §5），收口后冻结，研学方不动它。
4. **`stream_session_chat` / `collect_session_chat` 主链**
   工具调用成功导向架构 / Final Answer Guard 在这里（见 `2026-05-26-final-answer-guard-usable-answer-verdict.md`）。这是模式无关的通用层，**双方都不得在这里塞模式分支**。
5. **`PreparedSessionTurn.effective_mode` 的含义**
   它表示"本轮真正执行的模式"。闲聊方保证 `else` 分支永远产出 `CHAT`；研学方保证学习卷分支产出 `STUDY/TEACH`（不再是 CHAT）。

## 四、所有权划分

### 闲聊方（本文档作者）独占

- `_prepare_session_turn` 的 `else` 分支（无学习卷路径）。
- `CHAT_PROFILE`、`CHAT_MODE_PROMPT`。
- 闲聊的 persona 行为（NEUTRAL 默认；persona overlay 是共享 UI 能力，调用方式不改）。

### 研学方独占

- `_prepare_learning_unit_turn` 全函数及其下游。
- `STUDY_PROFILE`、`TEACH_PROFILE`、`STUDY_MODE_PROMPT`、`TEACH_MODE_PROMPT`、`ASK_MODE_PROMPT`。
- `alignment_policy.py`、学习卷 store / 概念抽取 / teach 出题评分 / 反馈卡。
- §1.2 的 phase→mode 映射；§5 的"absorbing 改走 STUDY"由研学方落地。
- 下游 absorbing guard：`_maybe_fire_concept_extraction`（main.py:1338）、`_maybe_emit_first_value`（main.py:1376）、`_build_absorbing_opening_addendum`（main.py:1152）、`allow_full_compact`（main.py:1130）里的 `== CHAT` 判定。

### 共享（§3，改动需同意）

`ModeProfile` / `TurnExecutionProfile` / `build_turn_profile` / `_PROFILE_REGISTRY` / `_prepare_session_turn` 分叉点 / `stream_session_chat` 主链 / `NEUTRAL_GUARDRAILS`。

## 五、解耦关键动作（一次性，分两方各自做）

> 顺序无强依赖，但研学方的 5.2 完成前，闲聊方对 CHAT_PROFILE 的改动会泄漏到 absorbing。建议研学方先做 5.2。

### 5.1 闲聊方

- 把 `else` 分支整理成不含任何学习卷假设的自洽路径。
- 收口 `_prepare_session_turn` 分叉点后冻结其结构。
- 之后在 `CHAT_PROFILE` / `CHAT_MODE_PROMPT` 上做闲聊优化，不碰研学常量。

### 5.2 研学方

- `_prepare_learning_unit_turn`：`absorbing → AgentMode.CHAT` 改为 `→ AgentMode.STUDY`。
- 把上述 4 处下游 `effective_mode == AgentMode.CHAT` 判定改成 `== AgentMode.STUDY`。
- 确认 `STUDY_PROFILE` 的 memory 读写 / heavy 预算 / tutor 风格符合研学预期（这正是 absorbing 想要的）。
- 视需要决定 absorbing 是否仍要 `allow_full_compact`（原 CHAT 才允许；STUDY 默认不在白名单，需显式决定）。

## 六、并行规则（防撞车）

- **mode_service.py**：按常量分区改。`CHAT_*` 归闲聊，`STUDY_* / TEACH_* / ASK_*` 归研学。`ModeProfile` / `build_turn_profile` / registry / `NEUTRAL_GUARDRAILS` 是共享脚手架，change-by-agreement。
- **main.py**：`_prepare_session_turn` 的 `else` 分支与 `_prepare_learning_unit_turn` 物理分离；分叉点本身由闲聊方一次性收口后冻结。
- **绝不**在 `stream_session_chat` 主链塞模式分支（§3.4）。
- 每方改完先 `python3 -m pytest`，确认对方测试不红。

## 七、验证（锁住边界的测试）

- 断言：无学习卷会话 → `effective_mode == CHAT`；有学习卷 absorbing → `effective_mode == STUDY`（不再是 CHAT）。
- 断言：`CHAT_PROFILE` 不被任何学习卷路径取用（grep 级 + 行为级）。
- 回归：现有 `test_mode_layering.py` / `test_alignment_policy.py` / `test_learning_unit_*` 全绿。
- 研学方 5.2 改完后，absorbing 的 first-value / 概念抽取仍按 STUDY 正常触发（原 CHAT 守卫迁移正确）。

## 留心事项

- 这是 N=1 个人项目：不为"未来可能的第 5 个模式"预留扩展点，只解决当前 CHAT 一职两用的真实耦合。
- `AgentMode.ASK` 仍只作为对齐门的临时覆写存在（上一轮已砍掉独立 ASK 模式），本次不复活为独立模式。
- persona overlay 是跨模式的 UI 能力，分离不触碰它的调用契约。
