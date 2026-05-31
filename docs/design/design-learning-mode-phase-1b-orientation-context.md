# Phase 1B 入局情境生成 技术设计

> 日期: 2026-05-30 | 类型: 子阶段技术设计 (Phase 1B / 5 个子阶段中第 2) | 范围: codex 主交付文档，自包含，可独立提交回滚 | 状态: 已落地 / 已验收（2026-05-31）
>
> 上游: `docs/design/design-learning-mode-phase-1b-1e-skeleton.md`（doc2）、`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`（doc1）、`AGENTS.md`、研习 dev-plan
>
> 同批: doc1（1A baseline 实跑核查与裂缝收口）、doc2（Phase 1B-1E 骨架）、doc4（`docs/output/learning-mode-phase-1b-plan-checklist-2026-05-30.md`）；本文档为 doc3，承接 doc2 §4 的 1B 子阶段约束。
>
> 真实数据集：`tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json`
>
> Baseline：`tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`（status=pass，run_id=`2026-05-31T073955Z_learning-mode-phase-1b-orientation-context-real`）

---

## 0. 实施前置门禁（硬约束，缺一不可开工）

本节登记 1B 开工的 4 件硬约束，与 doc4 §2 「前置准备（开工硬门）」一一对应。**任一未满足，禁止动 1B 任何代码**。

| 门禁 | 内容 | 事实源锚点 |
|---|---|---|
| (a) doc1 已合入 main | Phase 1A 硬化（`docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`）已合入 main；1A baseline `status` 已从 `not_yet_executed` 实跑回填为 `pass` 或带 `implementation_gap`；`LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 事件常量已落地 | doc1 §1.1 close 记录 commit hash |
| (b) doc2 已评审通过 | `docs/design/design-learning-mode-phase-1b-1e-skeleton.md` 评审通过，§4 1B 约束作为本文事实源；若本文与 doc2 §4.3/§4.4 冲突，以本文 doc3 为事实源（doc2 §0 已声明 design 为事实源） | doc2 §0、§4 |
| (c) doc3 已评审通过 | 本文档（doc3）已评审通过，§3 双事件 / §4.4 独立 provider 通道 / §2.1 source 字段三处对齐基准全部锁定 | 本文 §13 invariants 复核 |
| (d) doc4 §10 已回填 | doc4 §10 open_questions 4 条全部回填（含本文 §10.2 case_id 命名口径、`orientation_digest` 算法、LLM 硬超时秒数、1A baseline `baseline_version` 引用 commit hash 形式） | doc4 §10 |

**上述门禁与 doc4 §2 同语义、同顺序、同事实源**；任一项漂移以 doc4 §2 为统一规约。doc3 的实施步骤 §11 起点 = (a)(b)(c)(d) 全绿当天。

---

## 1. 产品目标

让用户在 collision 阶段不是「被动读到一段定义」，而是被一个**贴近目标的小情境**轻轻撞一下，产生「差一点就懂」的张力，从而愿意主动给出第一句猜测/复述/反应。

**入局情境（OrientationContext）的产品形态严格约束如下**：

- 文本主体 ≤ 120 中文字符（不含 markdown），单段，不允许列表/标题/代码块
- 必须以下面三种「引子」之一收尾，且不能混合：
  - `question`：一个用户能立刻用 1-2 句话回应的提问（非选择题、非「理解了吗」这种空问）
  - `scenario`：一个具体场景描写 + 一句「如果换成你会怎么做」
  - `counterintuitive`：一个反直觉断言 + 一句「你觉得这成立吗，为什么」
- 必须可被「立即回应」：禁止「请阅读以下材料」、「想想看」这类被动叙述
- 一个学习卷的一次 collision 阶段只生成 **1 次** orientation_context，重生成仅在 alignment 解除且目标已变更后允许（见 §7）

判定上述形态是否满足由 **prompt 工程 + Product 层后校验**完成，不引入新的 LLM judge（N=1 不加底座）。后校验只做最小检查：字符数、是否非空、是否含明令禁止的开头词；不达标即走静态降级模板（见 §4.4），由 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED` 事件标记降级。

---

## 2. 状态归属

复用 `LearningUnit`（`learning_agent/ai/learning_unit.py:190`），**只新增 1 个 Optional 字段**，不抽子模型、不在事件之外建任何「已生成历史」列表。

### 2.1 新增 schema

```python
# learning_agent/ai/learning_unit.py
class OrientationContext(BaseModel):
    """Phase 1B：collision 阶段一次性派生的入局情境。

    存在即代表本 unit 已经向用户投出过一次入局情境；不参与 replay，
    只用于 UI 渲染恢复 (页面刷新场景) 与「是否已生成过」的去重守卫。
    """
    prompt_text: str                                  # ≤120 字主体，含引子
    hook_kind: Literal["question", "scenario", "counterintuitive"]
    source: Literal["llm", "fallback"]                # 生成路径标识：llm=成功路径，fallback=静态降级
    source_seed_ref: Optional[str] = None             # objective.text 摘要或 concept_list[].id；纯追溯用
    orientation_digest: str                           # 结构体稳定摘要，事件 payload 与本字段一致
    generated_at: datetime = Field(default_factory=_now)
```

**`source` 字段约束（硬约束）**：

- 取值仅 `{"llm", "fallback"}` 两枚举值，**必填**，无默认值；
- 与事件类型一一对应：`source="llm"` → emit `LEARNING_UNIT_ORIENTATION_GENERATED`；`source="fallback"` → emit `LEARNING_UNIT_ORIENTATION_FALLBACK_USED`；
- 加载旧 1A unit 时若 `orientation_context is None` 直接放行（见 §8），不补构 `source` 字段。

```python
class LearningUnit(BaseModel):
    ...
    # Phase 1A 铸造状态骨架 (learning_unit.py:231-233 已有)
    forge_stage: ForgeStage = "entry"
    temperature_state: TemperatureState = "steady"
    # ── Phase 1B 入局情境 ─────────────────────────────────────
    orientation_context: Optional[OrientationContext] = None
```

### 2.2 不变项

- `_ALLOWED_TRANSITIONS`（`learning_unit.py:179-187`）**完全不动**：主链仍是 absorbing⇄outputting→consolidated
- `ForgeStage` Literal 不扩展，仍是 `entry/collision/forge/fixed/cooling`（`learning_unit.py:57`）
- `TemperatureState` 不扩展，仍为 `steady` 一档（1E 才动）
- 不抽 `LearningForgeState` 子模型；`orientation_context` 与 `forge_stage` 同辈挂在 LearningUnit 顶层（符合 1A 不抽子模型约束）
- Schema 兼容性：旧 unit（`orientation_context=None`）反序列化时安全，pydantic 默认值兜底；新 unit 在 entry 阶段进 collision 时走完整 1B 路径

---

## 3. 新增事件

### 3.1 事件类型（双事件，禁止用 source 字段隐式分叉）

```python
# learning_agent/learning_agent/session_events.py
LEARNING_UNIT_ORIENTATION_GENERATED = "learning_unit.orientation_generated"
LEARNING_UNIT_ORIENTATION_FALLBACK_USED = "learning_unit.orientation_fallback_used"
```

**注册位置**：在 doc1 落地的 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 之后追加（符号锚点，不用行号；若 doc1 未合入，Step 1 不动）。事件常量的 `EventVisibility` 设为 `AGENT`（与 1A `FORGE_STAGE_CHANGED` 风格一致），不参与 replay。

**双事件的语义边界**：

| 事件类型 | 触发路径 | payload `source` | 与持久化的关系 |
|---|---|---|---|
| `learning_unit.orientation_generated` | LLM 成功路径，后校验通过 | `"llm"` | 与 `OrientationContext.source="llm"` 一一对应；事件先于持久化失败则不 emit |
| `learning_unit.orientation_fallback_used` | LLM 调用失败 / 超时 / 后校验拒绝 → 走静态降级模板 | `"fallback"` | 与 `OrientationContext.source="fallback"` 一一对应；同样在持久化成功后再 emit |

**不允许「仅一个事件」表述**：下游 projection / 指标必须把两者作为独立事件类型处理；不允许仅靠 payload `source` 字段隐式分叉。该规约与 doc2 §4.3 / doc4 §3.3 三方对齐。

### 3.2 Payload schema

两事件共享同形 payload schema（仅 `event` 字段与 `source` 字段不同）：

```python
# 实际调用: emit_unit_event(unit, <event_const>, extra={...})
extra = {
    "learning_unit_id": "lu-xxxxxxxx",          # 与 _emit_unit_event 基础 6 字段重复但显式登记
    "forge_stage": "collision",
    "temperature_state": "steady",
    "source": "llm",                            # 或 "fallback"
    "orientation_digest": "<稳定摘要>",
    "hook_kind": "question",                    # fallback 路径下取自静态模板，仍合法三枚举之一
    "prompt_text": "...≤120 字主体...",
    "source_seed_ref": "objective:首句摘要",
    "regenerated": False,
}
```

写入 JSONL 的完整字段（`_emit_unit_event` 自动叠加基础 6 字段 + `extra`）：

```json
{
  "session_id": "ses-xxxxxxxx",
  "ts": "2026-05-30T07:00:00.000Z",
  "learning_unit_id": "lu-xxxxxxxx",
  "phase": "absorbing",
  "alignment_state": "idle",
  "objective_status": "...",
  "alignment_reason": null,
  "clarification_count": 0,
  "event": "learning_unit.orientation_generated",
  "forge_stage": "collision",
  "temperature_state": "steady",
  "source": "llm",
  "orientation_digest": "<稳定摘要>",
  "hook_kind": "question",
  "prompt_text": "...≤120 字主体...",
  "source_seed_ref": "objective:首句摘要",
  "regenerated": false
}
```

**payload 共有强制字段**（与 doc4 §3.3 / doc2 §4.3 一致）：

- `learning_unit_id`
- `forge_stage`
- `temperature_state`
- `source`（枚举 `"llm" | "fallback"`）
- `orientation_digest`

字段语义：

- `regenerated`：仅当 `alignment_state in {resolved, skipped}` 且 objective 已变更后基于新目标重生成时为 `true`，首次生成为 `false`
- `prompt_text` 必须与持久化到 `orientation_context.prompt_text` **完全一致**；事件先于持久化失败时不发射，与 1A `maybe_advance_forge_stage` 持久化失败提前返回的模式一致（`forge_policy.py:101-107`）
- `orientation_digest` 算法：`sha256(f"{prompt_text}|{hook_kind}|{source_seed_ref or ''}")` 后取 hex 前 16 字符；事件 payload 与 `OrientationContext.orientation_digest` 完全一致。

### 3.3 一次性守卫

两类事件 emit 前共同必须满足：

```
unit.forge_stage == "collision"
AND unit.orientation_context is None         # 或 regenerated=True 路径已先清空
AND unit.alignment_state in {"idle", "skipped"}
AND 持久化已 commit (store.save 成功)
```

任一不满足 → no-op，与 1A `maybe_advance_forge_stage` 守卫风格一致（`forge_policy.py:85-98`）。

`LEARNING_UNIT_ORIENTATION_GENERATED` 与 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED` 在同一轮内**互斥**：成功路径 emit 前者、降级路径 emit 后者，不允许同轮双发。

---

## 4. 编排器变化

### 4.1 forge_policy 扩展：entry→collision 前置 gate

入口仍是 `maybe_advance_forge_stage`（`forge_policy.py:69`），**不新增第二个 forge_stage 推进入口**。Phase 1B 只在 1A 已有 `entry→collision` 判断前追加 `orientation_context is not None` 前置 gate（含降级路径已尝试过）；`collision→forge` 留给 Phase 1C。

```python
# learning_agent/learning_agent/forge_policy.py
def maybe_advance_forge_stage(...) -> None:
    # ── 1B 前置 gate（双路径已尝试 = orientation_context 非空） ──
    # 当前 1B 只约束 entry→collision 在 orientation 已落地（含 fallback）后才放行。
    # 现有 1A 守卫: response_text 非空 / STUDY / absorbing / unit 存在

    if unit.forge_stage == "entry":
        # Phase 1A 已有逻辑：推进到 collision
        # 1B 在此处现有判断前追加 orientation_context is not None 前置 gate
        if unit.orientation_context is None:
            return  # orientation 尚未落地（含 fallback 尝试），不推进
        _advance_entry_to_collision(unit, store, emit_unit_event)
        return

    # collision→forge 属于 Phase 1C，本阶段不推进。
```

**重要约束**：

- forge_stage 推进入口**全项目唯一**：`forge_policy.maybe_advance_forge_stage`；不允许在 `main.py` / `orientation_provider` / 其他模块私造第二个入口
- Phase 1B 不实现 `collision→forge`；该推进由 Phase 1C design 拍板信号源后接入同一唯一入口
- 旧 1A unit 兼容（见 §8）：`forge_stage in {collision, forge, fixed, cooling}` 且 `orientation_context is None` 时不触达本前置 gate（gate 只作用于 entry→collision），加载直接放行

### 4.2 collision → forge 不在本期实现

`collision→forge` 是 Phase 1C 的职责。Phase 1B 只让用户看到 orientation 并等待第一反应；用户回应如何被判定为「碰撞已发生」以及是否持久化直觉，均由 Phase 1C 独立 design 决定。

### 4.3 prepare_forge_stage_metadata 扩展

`prepare_forge_stage_metadata`（`forge_policy.py:49`）的 `LearningAction` 新增枚举值，`ForgeStagePlan` 新增字段：

```python
LearningAction = Literal["orient", "prepare_to_guess", "await_orientation_response"]

class ForgeStagePlan(BaseModel):
    forge_stage: ForgeStage
    temperature_state: TemperatureState
    learning_action: LearningAction
    hook_kind: Optional[Literal["question", "scenario", "counterintuitive"]] = None

_ACTION_MAP: dict[str, LearningAction] = {
    "entry": "orient",
    "collision": "prepare_to_guess",   # orientation_context 为空时
}

def prepare_forge_stage_metadata(...) -> ForgeStagePlan:
    ...
    if unit.forge_stage == "collision" and unit.orientation_context is not None:
        learning_action = "await_orientation_response"
        hook_kind = unit.orientation_context.hook_kind
    elif unit.forge_stage == "collision":
        learning_action = "prepare_to_guess"
        hook_kind = None
    else:
        learning_action = _ACTION_MAP.get(unit.forge_stage, "orient")
        hook_kind = None

    return ForgeStagePlan(
        forge_stage=unit.forge_stage,
        temperature_state=unit.temperature_state,
        learning_action=learning_action,
        hook_kind=hook_kind,
    )
```

### 4.4 入局情境生成通道（独立 provider，不走 system_prompt 装配链）

**通道方向硬约束（重写自 doc2 §4.4）**：

- orientation 生成走**独立 provider 调用**，产物落到 `unit.orientation_context` 字段
- **禁止**任何形式进入 `TurnExecutionProfile.system_prompt`、`system_prompt_addendum`、主回合 prompt 装配链
- Runtime / `build_turn_profile` / agent_loop / provider adapter 完全**不感知** orientation 概念
- 主 absorbing 回合的 STUDY profile 不携带 orientation 相关文本；orientation 是「场内态」，与主回合 prompt 正交

新增模块 `learning_agent/learning_agent/orientation_policy.py`（与 `forge_policy.py` 同层注入风格，依赖通过函数参数注入，**禁止 `import learning_agent.main`**），导出：

```python
# learning_agent/learning_agent/orientation_policy.py
def maybe_generate_orientation(
    *,
    store: LearningUnitStore,
    emit_unit_event: EmitUnitEventCallable,
    session: Session,
    unit: LearningUnit,
) -> Optional[OrientationContext]:
    """Phase 1B：collision 阶段独立 provider 通道生成 OrientationContext。

    返回值即为落地后的 OrientationContext（source 可能是 llm 或 fallback），
    或 None（守卫未通过、不进入生成）。

    通道方向硬约束：
    - 走独立 provider 调用，不进 TurnExecutionProfile.system_prompt / addendum
    - 产物就地落到 unit.orientation_context，由 store.save 持久化
    - 持久化成功后才 emit 对应事件（GENERATED 或 FALLBACK_USED）
    """
    # 三段守卫：collision 态 + orientation 未落 + alignment 非活跃
    if unit.forge_stage != "collision":
        return None
    if unit.orientation_context is not None:
        return None
    if unit.alignment_state not in {"idle", "skipped"}:
        return None

    # 独立 provider 调用（硬超时，秒数由 doc4 §10 #4 拍板）
    try:
        raw = orientation_provider.generate(unit=unit, session=session, timeout=ORIENTATION_TIMEOUT_SECONDS)
    except (ProviderTimeout, ProviderError):
        raw = None

    # 后校验 + hook_kind 推断
    if raw is not None:
        validated = _validate_orientation_text(raw)
        if validated is not None:
            ctx = OrientationContext(
                prompt_text=validated,
                hook_kind=_infer_hook_kind(validated),
                source="llm",
                source_seed_ref=unit.objective.text[:30] if unit.objective else None,
                orientation_digest=_compute_orientation_digest(validated, unit),
            )
        else:
            ctx = _build_fallback_orientation(unit)
    else:
        ctx = _build_fallback_orientation(unit)

    # 写 unit 并持久化（失败则不 emit）
    unit.orientation_context = ctx
    try:
        store.save(unit)
    except Exception:
        unit.orientation_context = None  # 回滚内存态
        return None

    # 持久化成功后再 emit 对应事件
    event_const = (
        LEARNING_UNIT_ORIENTATION_GENERATED
        if ctx.source == "llm"
        else LEARNING_UNIT_ORIENTATION_FALLBACK_USED
    )
    emit_unit_event(
        unit,
        event_const,
        extra={
            "learning_unit_id": unit.id,
            "forge_stage": unit.forge_stage,
            "temperature_state": unit.temperature_state,
            "source": ctx.source,
            "orientation_digest": ctx.orientation_digest,
            "hook_kind": ctx.hook_kind,
            "prompt_text": ctx.prompt_text,
            "source_seed_ref": ctx.source_seed_ref,
            "regenerated": False,
        },
    )
    return ctx


def _build_fallback_orientation(unit: LearningUnit) -> OrientationContext:
    """静态降级模板：LLM 调用失败 / 超时 / 后校验拒绝时使用。

    模板严格满足 §1 产品形态约束：≤120 字 + question/scenario/counterintuitive 三引子之一。
    具体文案由 Product 在实施时确认；本设计层只保证：
    - source = "fallback"
    - hook_kind ∈ 三枚举之一
    - prompt_text 不含禁用开头词
    """
    template_text = _pick_fallback_template(unit.objective)
    return OrientationContext(
        prompt_text=template_text,
        hook_kind=_infer_hook_kind(template_text),
        source="fallback",
        source_seed_ref=unit.objective.text[:30] if unit.objective else None,
        orientation_digest=_compute_orientation_digest(template_text, unit),
    )


def _validate_orientation_text(text: str) -> Optional[str]:
    """后校验 orientation 文本。失败返回 None。"""
    text = text.rstrip()
    if not text or len(text) > 120:
        return None
    forbidden_starts = ["请", "请读", "想想", "理解了吗"]
    if any(text.startswith(w) for w in forbidden_starts):
        return None
    return text


def _infer_hook_kind(text: str) -> Literal["question", "scenario", "counterintuitive"]:
    """从 orientation 文本启发推断 hook_kind。"""
    text = text.rstrip()
    if text.endswith(("?", "？")):
        return "question"
    if "如果换成你" in text or "你会怎么" in text:
        return "scenario"
    return "counterintuitive"
```

**`_prepare_learning_unit_turn`（`main.py:983`）的接入方式（彻底重写，不再用 addendum）**：

```python
# learning_agent/learning_agent/main.py
if effective_mode == STUDY and unit.phase == "absorbing":
    # ...既有的 alignment 判定逻辑...

    opening_addendum = self._build_absorbing_opening_addendum(...)
    # 注意：opening_addendum 走的是 1A 既有通道，与 orientation 通道完全独立

    # Phase 1B 新增：独立 provider 通道生成 orientation（不进 prompt 装配链）
    if (
        unit.forge_stage == "collision"
        and unit.orientation_context is None
        and unit.alignment_state in {"idle", "skipped"}
        and alignment_popup is None
    ):
        orientation_policy.maybe_generate_orientation(
            store=self.store,
            emit_unit_event=self._emit_unit_event,
            session=prepared_session,
            unit=unit,
        )
        # 注意：返回值不参与本回合 STUDY profile 构造；
        # 产物只通过 unit.orientation_context 落到 unit，下次刷新 / SSE snapshot 时由前端独立容器渲染

    if prepare_turn_profile == STUDY:
        profile = build_turn_profile(
            ...,
            system_prompt_addendum=opening_addendum,  # 仅 1A opening，不含 orientation
            ...,
        )
```

**关键约束（再次强调）**：

- orientation 生成与持久化在 `_prepare_learning_unit_turn` 内完成，但**不写回 system_prompt_addendum**
- Runtime 层（`build_turn_profile`，`main.py:1113`）完全**不感知** orientation 概念
- 不与 alignment_popup 短路同轮触发；alignment_popup 优先级更高
- 与 1A `_build_absorbing_opening_addendum` 走两条独立通道，互不替代、互不叠加

### 4.5 持久化与事件落地（按生成路径填 source 字段）

§4.4 的 `maybe_generate_orientation` 已内聚「生成 → 校验 → 持久化 → emit」四步；持久化阶段按生成路径填 `OrientationContext.source` 字段：

| 路径 | `source` 字段 | 发射事件 | payload `source` |
|---|---|---|---|
| LLM 成功（含后校验通过） | `"llm"` | `learning_unit.orientation_generated` | `"llm"` |
| LLM 失败 / 超时 / 后校验拒绝 → 静态降级 | `"fallback"` | `learning_unit.orientation_fallback_used` | `"fallback"` |

**持久化失败处理**：`store.save` 抛异常 → 回滚内存态 `unit.orientation_context = None`、不 emit 任何事件、不抛上层（主回复流不受阻塞）。

**Resume 路径**：跨会话恢复 unit 时若 `orientation_context is not None`，**不重生成、不重发事件**；前端按 §6 直接从 snapshot 渲染。

**重要不变性（与 1A 衔接）**：

- orientation 生成轮**不改** `first_value_delivered_at` 的 t0 语义（该时间戳只标记学习卷首次实质交付，不含 orientation）
- `maybe_generate_orientation` 与 `maybe_advance_forge_stage` 的调用顺序由 §11 Step 5/6 锁定，保证首次生成 orientation 后只允许 `entry→collision`，不会推进 `collision→forge`

---

## 5. Runtime 边界（硬约束）

**Runtime 不知道 orientation 是什么。**

- `build_turn_profile` / `agent_loop.run` / `TurnExecutionProfile` 不新增分支、不新增字段
- 入局情境**不通过** STUDY profile 的 `system_prompt` / `system_prompt_addendum` / 主对话 history / tool_result 任何一条通道传递
- `unit_metadata` 中的 `hook_kind` 是 Product 层私有元数据，Runtime 透传不解读
- provider adapter 不感知 orientation：orientation 走的是 `orientation_policy.maybe_generate_orientation` 内部直接调用的 `orientation_provider`，与主对话 provider 通道**物理隔离**
- orientation 调用失败时静态降级路径完全在 Product 层完成；Runtime 不接收 fallback 信号

**违反此边界即重写**（四层职责硬约束；与 doc2 §8 第 7 条对齐）。

---

## 6. SSE / Interface

### 6.1 SSE 字段

新增字段到 SSE 增量 metadata：

```python
# learning_agent/learning_agent/main.py:1062-1108 的 unit_metadata 中显式注入
unit_metadata["hook_kind"] = unit_metadata.get("hook_kind") or None
unit_metadata["orientation_context_present"] = unit.orientation_context is not None
```

关键说明：

- `/learning-units/{unit_id}` REST endpoint 返回的 unit JSON snapshot 自动包含 `orientation_context` 字段（因为它在 `LearningUnit` schema 里），无需额外改代码
- SSE 增量流只携带 `hook_kind` 与 `orientation_context_present` 存在标记；结构体本身走 REST 拉取一次，避免 SSE 帧膨胀（与 doc4 §3.4 一致）
- `orientation_context_present=true` 后前端主动拉一次 `/learning-units/{unit_id}` 刷新卡片，不轮询

### 6.2 SSE 字段白名单

```python
# learning_agent/web/web_server.py:221-249
ALLOWED_UNIT_METADATA_KEYS = {
    ...,
    "hook_kind",                       # Phase 1B 新增
    "orientation_context_present",     # Phase 1B 新增（仅存在标记）
}
```

### 6.3 前端渲染

`web/static/learning-unit-ui.js`：

- 在 `state.forgeStage` 旁加 `state.orientationContext` / `state.hookKind`（`learning-unit-ui.js:184` hydrateFromUnit、`:227` applyDelta 同步处补一份）
- 新增渲染容器 `#learning-unit-card` 内 `[data-testid="orientation-context"]`，仅当 `forgeStage==='collision' && orientationContext` 时显示
- 视觉上不与 alignment modal 同时存在（alignment modal 优先级更高，见 §7）
- `learningAction === 'await_orientation_response'` 时 placeholder 文案变成「按你的第一反应回我一句」
- 收到 `orientation_context_present=true` 的 SSE metadata 后主动拉 `/learning-units/{unit_id}` 一次刷新

```javascript
// web/static/learning-unit-ui.js
function hydrateFromUnit(unit) {
    state.forgeStage = unit.forge_stage;
    state.orientationContext = unit.orientation_context;  // 新增
    state.hookKind = unit.orientation_context ? unit.orientation_context.hook_kind : null;
    ...
}

function applyDelta(delta) {
    if ("forge_stage" in delta) state.forgeStage = delta.forge_stage;
    if ("hook_kind" in delta) state.hookKind = delta.hook_kind;
    if (delta.orientation_context_present === true && !state.orientationContext) {
        fetchUnitSnapshot(state.unitId);  // 主动拉取一次
    }
    ...
}

function renderLearningUnit() {
    // 既有的 alignment modal / forge stage indicator...

    const elem = document.querySelector("#learning-unit-card [data-testid='orientation-context']");
    if (!elem) return;
    if (state.forgeStage === "collision" && state.orientationContext) {
        elem.innerHTML = `<div class="orientation-prompt">${escapeHtml(state.orientationContext.prompt_text)}</div>`;
        elem.hidden = false;
    } else {
        elem.hidden = true;
    }
}
```

---

## 7. 与 alignment 旁路的协同

**互斥规则（硬约束）**：

| `alignment_state` | orientation 生成 | orientation 展示 | entry→collision 推进 |
|---|---|---|---|
| `idle` | 允许 | 展示 | 允许（若 orientation_context ≠ None 且当前为 entry） |
| `suggested` | 跳过本轮 | 展示既有 | 不推进（alignment 优先） |
| `active` | 短路，不调 LLM | 被 alignment modal 遮盖 | 不推进 |
| `resolved` | （下一轮再判，到达 orientation 注入点前已被重写） | 展示既有 | 允许（若 orientation_context ≠ None 且当前为 entry） |
| `skipped` | 允许（与 idle 同） | 展示既有 | 允许（若 orientation_context ≠ None 且当前为 entry） |

注：`resolved` 在到达 orientation 注入点前已被下一轮 `apply_alignment_decision` 重写为 `idle/suggested/active` 之一，故实际只可能看到 `idle/skipped`；上表保留 `resolved` 仅为阐述意图。

**实现关键点**：

1. `_prepare_learning_unit_turn` 中「是否调用 `maybe_generate_orientation`」的守卫（§4.4）已含 `alignment_state in {idle, skipped}` 与 `alignment_popup is None` 双重检查
2. `maybe_generate_orientation` 内部三段守卫再次校验 `alignment_state`，避免并发 `/align` 端点改 state 后还落 stale orientation
3. Phase 1B 不判断 orientation 回应是否构成碰撞；alignment 解除后的回应不在本期触发 `collision→forge`

**active alignment 短路 → 下一轮补生成**：

active alignment 当轮不调用 `maybe_generate_orientation`，下一 STUDY 轮自然进入 §4.4 路径补生成（覆盖在 §10.2 case `lm-p1b-orientation-defer-after-active-alignment`）。

**重生成（regenerated=true）触发条件**：

仅当以下全部满足时，下一个 absorbing 轮允许重生成：

```
unit.forge_stage == "collision"
AND unit.orientation_context is not None
AND unit.alignment_state in {"idle", "resolved", "skipped"}
AND last_alignment_at > orientation_context.generated_at  (上轮发生过对齐)
AND unit.objective.text 与 orientation_context.source_seed_ref 已不匹配  (目标已更新)
```

判定放在 `_prepare_learning_unit_turn` 中、调用 `maybe_generate_orientation` 之前；触发时先 `unit.orientation_context = None`，让 §4.4 的入口守卫自然走「首次生成」路径，只是 `maybe_generate_orientation` 内部在 emit 时把 payload `regenerated` 字段置为 `True`（实现上需要外部提示传入 flag 或在 unit 上挂临时标记，由 §11 Step 8 锁定具体形式）。

---

## 8. 与旧 1A unit 的兼容性

加载时若 `forge_stage in {collision, forge, fixed, cooling}` 且 `orientation_context is None`：

- **直接放行**（不触发生成、不阻塞、不发事件）
- 仅当新建 unit（`forge_stage=entry, orientation_context=None`）走完整 1B 路径（含 §4.1 前置 gate 与 §4.4 独立 provider 通道）
- Resume 跨会话时不重生成、不重发事件
- §4.1 的「entry→collision 前置 gate」只作用于 entry 态，不触达 collision/forge/fixed/cooling 旧 unit

该兼容性条款必须在加载入口处显式落地（断言或注释指明放行分支），并由 §10.1 单元测试覆盖。

---

## 9. 不做事项（边界硬约束）

| 不做 | 为什么 |
|---|---|
| 不实现 1C 碰撞捕获（用户初始直觉持久化） | 1B 仅生成「已入局」的情境提示；用户的回应不在 1B 中被判定或入库 |
| 不实现 1D 用户自己的话持久化 | 同上，避免 N=1 提前建底座 |
| 不实现 1E 火候策略 | `temperature_state` 1B 期保持单档 steady；判定 hook_kind 不依赖温度 |
| 不引入 UnderstandingExhibit / 任何博物馆/镜子字段 | Phase 2/4 范围，与 N=1 顺序冲突 |
| 不修改 ASK 对齐流程 / 不动 alignment_state 状态机 | alignment 与 orientation 互斥，靠守卫不靠耦合 |
| 不动 stop modal / consolidated feedback_card 生成 | 与 collision 无关 |
| 不新增 Runtime 分支 / 不抽 LearningForgeState 子模型 | 1A 已立的边界 |
| 不让 ORIENTATION_GENERATED / ORIENTATION_FALLBACK_USED 参与 replay | 事实源唯一 + 减少 replay 表面积 |
| 不引入 LLM judge 做 hook_kind 后校验 | 用最小启发（§4.4 `_infer_hook_kind`）+ prompt 工程即可 |
| 不让用户看到 `[HOOK:xxx]` 标记 | §4.4 `_validate_orientation_text` 不依赖此标记 |
| 不为 hook_kind 增加 stop/skip 用户显式接口 | `/skip` 复用现有 alignment skip 路径；orientation 回应处理留给 1C |
| 不让 orientation 通道与 STUDY profile system_prompt 链共用 | 四层职责硬约束（§5）；orientation 走独立 provider |
| 不用 `source` 字段隐式分叉单事件 | doc2 §4.3 / doc4 §3.3 / 本文 §3.1 三方对齐双事件 |
| 不允许 `OrientationContext.source` 留空或带默认值 | §2.1 硬约束：必填、仅两枚举值 |
| 不重新审 close 章节的 docs/changes/ 留痕规约 | 按 §11 Step 10 三类留痕规约登记即可 |

---

## 10. 测试要求

### 10.1 单测

`tests/test_forge_policy.py` 扩展 entry→collision gate case：

| Case ID | 输入 | 期望 |
|---|---|---|
| 10.1.1 entry_stays_without_orientation | unit.forge_stage=entry, orientation_context=None | forge_stage 不变，不 emit |
| 10.1.2 entry_advances_after_orientation_attempt | unit.forge_stage=entry, orientation_context 非 None | forge_stage→collision，emit FORGE_STAGE_CHANGED |
| 10.1.3 collision_does_not_advance_in_phase_1b | unit.forge_stage=collision, orientation_context 非 None, user_input 任意 | forge_stage 不变，不 emit；`collision→forge` 留给 1C |

`tests/test_learning_unit_events.py` 扩展 ORIENTATION_GENERATED / FALLBACK_USED 契约 case：

- 10.1.5a emit `learning_unit.orientation_generated` payload 字段完整、`source="llm"`、`hook_kind` 在三枚举内、`prompt_text` 与 unit 字段一致、`orientation_digest` 与 unit 字段一致；事件名断言用全限定字符串 `"learning_unit.orientation_generated"`（**不允许裁切前缀**）
- 10.1.5b emit `learning_unit.orientation_fallback_used` payload 字段完整、`source="fallback"`、`hook_kind` 在三枚举内；事件名断言用全限定字符串 `"learning_unit.orientation_fallback_used"`（**不允许裁切前缀**）
- 10.1.5c **source 字段正确性双路径校验**：构造 LLM 成功路径 → 断言 `OrientationContext.source=="llm"` 与事件 `source=="llm"` 同时成立；构造 LLM 失败路径 → 断言 `OrientationContext.source=="fallback"` 与事件 `source=="fallback"` 同时成立；两路径互斥不同轮共发
- 10.1.6 持久化失败时不 emit 任一事件（用 monkeypatch `store.save` 抛异常；同时断言内存态已回滚为 `orientation_context=None`）
- 10.1.7 `alignment_state=suggested/active` 时 emit 守卫拒绝（两事件均不发）
- 10.1.8 `regenerated=true` 路径的 `alignment_state` 检查（`last_alignment_at > orientation_context.generated_at` 且 objective 已变更）

`tests/test_orientation_policy.py` 新增（覆盖独立 provider 通道 + 校验 + 降级）：

- 10.1.9 `maybe_generate_orientation` 返回值满足 ≤120 字 + 三引子格式之一；source 字段与生成路径对应
- 10.1.10 `_validate_orientation_text` 校验：字符数上限、禁用开头词
- 10.1.11 `_infer_hook_kind` 启发判定：问号→question、「如果换成」→scenario、其他→counterintuitive
- 10.1.12 **依赖注入边界**：`orientation_policy` 模块顶层 import 集合不含 `learning_agent.main`（防循环依赖回归；与 doc4 §3.2 cross_recs #5 对齐）
- 10.1.13 **Runtime 边界**：构造一次 `_prepare_learning_unit_turn` collision 轮调用，断言 `TurnExecutionProfile.system_prompt` 与 `system_prompt_addendum` 不出现任何 orientation 文本

### 10.2 真实数据集

`tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json` 新增 5 个 case（与 doc4 §5 一一对应；命名前缀强制 `lm-p1b-`，禁止保留 `1b-case-*` 风格）：

| Case ID | 场景 | 关键验收 |
|---|---|---|
| `lm-p1b-orientation-generated-on-first-turn` | 正常生成：STUDY absorbing collision 首轮，期望 orientation 落档 + `learning_action=await_orientation_response`、emit `learning_unit.orientation_generated`、`source=llm` | learning_unit payload 含 `orientation_context`；事件流含 1 条 `learning_unit.orientation_generated`，payload `source=llm`、`orientation_digest` 与 unit 字段一致 |
| `lm-p1b-no-attempt-blocks-stage-advance` | orientation 尝试未发生时 `forge_stage` 不推进 | 在 orientation 事件出现前，无 `learning_unit.forge_stage_changed(entry→collision)`；orientation 落地（含 fallback）后才允许推进 |
| `lm-p1b-orientation-fallback-on-llm-failure` | LLM 失败/超时走静态降级且不阻塞回复 | 事件类型为 `learning_unit.orientation_fallback_used`（**不是** generated 同名事件靠 source 分叉），回复成功 finalize，TTFV_p50 ≤ 1A baseline TTFV_p50 × 1.5 |
| `lm-p1b-ask-alignment-does-not-trigger-orientation` | ASK 对齐轮不消费也不生成 orientation | alignment_popup 短路路径无 orientation 相关事件（GENERATED / FALLBACK_USED 均无） |
| `lm-p1b-orientation-defer-after-active-alignment` | active alignment 短路后由下一 STUDY 轮补生成 orientation | active alignment 轮无 orientation 事件；紧接的 STUDY 轮 emit `learning_unit.orientation_generated`（或 `learning_unit.orientation_fallback_used`），事件次序正确 |

baseline 必须**实跑**（`status=pass` 或带具体 `implementation_gap`，**禁止 `not_yet_executed`**），文件名：`tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json`，由 `tests/e2e/real_runner.py` 经统一路由产出（参考 doc1 中 1A baseline 实跑修复的同一通道）。

baseline 结构包含（参考 1A）：

```json
{
  "must_pass_cases": [
    "lm-p1b-orientation-generated-on-first-turn",
    "lm-p1b-no-attempt-blocks-stage-advance",
    "lm-p1b-orientation-fallback-on-llm-failure",
    "lm-p1b-ask-alignment-does-not-trigger-orientation",
    "lm-p1b-orientation-defer-after-active-alignment"
  ],
  "hard_targets": [
    "event_emission_integrity: 双事件分别 emit，禁止 source 字段隐式分叉",
    "stage_gate_integrity: Phase 1B 不推进 collision→forge",
    "alignment_override: active 状态下不生成 orientation",
    "runtime_isolation: TurnExecutionProfile.system_prompt 不含 orientation 文本",
    "ttfv_regression: TTFV_p50 ≤ 1A baseline TTFV_p50 * 1.5"
  ],
  "failure_budgets": {
    "tolerable_event_latency_ms": 500,
    "max_skipped_regenerations": 1
  },
  "case_results": { "...": "..." },
  "status": "executed"
}
```

---

## 11. 实施步骤（codex 可直接执行）

每步独立可提交、可独立回滚，**禁止跨步合并**。

### Step 0: 前置门禁核对

- 操作: 核对 §0 四件硬约束 (a)(b)(c)(d) 全绿；任一未绿，本步停在此处不向后推进
- 验收: doc4 §2「前置准备」勾选完整；§0 表格四行均可引用 commit hash / 评审通过日期
- 提交: 无（仅核对，不动代码）

### Step 1: schema 与事件类型（无行为变化）

- 文件: `learning_agent/ai/learning_unit.py` 在 `LearningUnit` 类内新增 `OrientationContext` 模型、`LearningUnit` 加 `orientation_context: Optional[OrientationContext] = None`；`OrientationContext.source: Literal["llm", "fallback"]` 必填、`orientation_digest: str` 必填
- 文件: `learning_agent/learning_agent/session_events.py` 在 **`LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 之后追加**（符号锚点，不用行号）：
  - `LEARNING_UNIT_ORIENTATION_GENERATED = "learning_unit.orientation_generated"`
  - `LEARNING_UNIT_ORIENTATION_FALLBACK_USED = "learning_unit.orientation_fallback_used"`
- **前置依赖**：本步硬依赖 doc1 已合入 main 且 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED` 常量已落地；**若 doc1 未合入则不动 Step 1**
- 验收: `pytest tests/test_learning_unit_store.py` 通过；`grep -r orientation_context learning_agent/` 无残留 import 错误；事件常量在 `session_events.py` 中紧邻 `LEARNING_UNIT_ALIGNMENT_RATE_LIMITED`
- 提交: `feat(learning-unit/§3.1): 新增 OrientationContext schema 与双事件常量`

### Step 2: 重跑 1A baseline（确保兼容性）

- 操作: 删除或重跑 `tests/e2e/real_baselines/learning-mode-phase-1a-forge-state-real.baseline.json` 确保 `orientation_context=None` 新字段纳入 baseline
- 或: 在 diff 工具上把 `orientation_context` 列入忽略键
- 验收: 1A baseline / 回归通过（status=pass）
- 提交: `test(baseline/§3.1): 1A baseline 重跑纳入 orientation_context 新字段`

### Step 3: forge_policy entry→collision 前置 gate + ForgeStagePlan 扩展

- 文件: `learning_agent/learning_agent/forge_policy.py:32-57` 扩展 `LearningAction` 枚举、`ForgeStagePlan` 加 `hook_kind`、`prepare_forge_stage_metadata` 派生 `await_orientation_response`
- 文件: 同上 `:69-117` 抽出 `_advance_entry_to_collision`（1A 原逻辑原样搬迁，**前置加 `orientation_context is not None` gate**）；`collision→forge` 不在本阶段实现
- 验收: `pytest tests/test_forge_policy.py` 含 entry gate 与 collision no-op case 全绿；1A 原 case 不回归
- 提交: `feat(forge-policy/§3.2): entry→collision 增加 orientation 前置 gate`

### Step 4: orientation_policy 模块（独立 provider 通道）

- 文件: 新建 `learning_agent/learning_agent/orientation_policy.py`，导出 `maybe_generate_orientation` / `_build_fallback_orientation` / `_validate_orientation_text` / `_infer_hook_kind` / `_compute_orientation_digest`
- 依赖注入边界：模块顶层**禁止** `import learning_agent.main`；依赖通过函数参数注入
- 文件: 新建（或复用 provider adapter 注册中心）`orientation_provider` 接口，支持硬超时（秒数由 doc4 §10 #4 拍板）
- 验收: `pytest tests/test_orientation_policy.py` 新增 10.1.9/10/11/12 通过；`grep "import.*learning_agent.main" learning_agent/learning_agent/orientation_policy.py` 无输出
- 提交: `feat(orientation-policy/§3.2): 独立 provider 通道 + 后校验 + 降级模板`

### Step 5: _prepare_learning_unit_turn 接入 orientation_policy（独立通道，不走 addendum）

- 文件: `learning_agent/learning_agent/main.py:1109-1113` 之间，在 `opening_addendum = self._build_absorbing_opening_addendum(...)` 之**外**（与 opening 通道并列）追加 §4.4 描述的 `maybe_generate_orientation` 调用
- 文件: 在 unit_metadata `:1062-1108` 块内补 `unit_metadata["hook_kind"] = None` 初始化、`unit_metadata["orientation_context_present"] = False` 初始化
- **硬禁止**：本步不允许把 orientation 写回 `opening_addendum` / `system_prompt_addendum` / `TurnExecutionProfile.system_prompt`
- 验收: STUDY absorbing collision 阶段能投出 orientation；单测 10.1.13 通过（profile 不含 orientation 文本）
- 提交: `feat(main/§3.2): collision 阶段独立通道接入 orientation_policy`

### Step 6: Product 层后处理 + 双事件落地

- 文件: `learning_agent/learning_agent/orientation_policy.py` 内 `maybe_generate_orientation` 末尾按 §4.5 表格区分 `source` 字段、emit 对应事件常量
- 文件: `tests/test_learning_unit_events.py` 新增 10.1.5a/5b/5c/6/7/8
- 验收: 全套 events 测试通过；双路径（LLM 成功 / 降级）`OrientationContext.source` 与事件 payload `source` 一一对应；持久化失败时内存态回滚且不 emit
- 提交: `feat(learning-unit/§3.3): 双事件落地 + source 字段双路径校验`

### Step 7: SSE / 前端集成

- 文件: `learning_agent/learning_agent/main.py:1062-1108` unit_metadata 块内补 `unit_metadata["orientation_context_present"]` 赋值、`unit_metadata["hook_kind"]` 赋值
- 文件: `learning_agent/web/web_server.py:221-249` SSE 字段白名单追加 `"hook_kind"` 与 `"orientation_context_present"`
- 文件: `web/static/learning-unit-ui.js:184,227` hydrateFromUnit / applyDelta 补 `state.orientationContext` 与 `state.hookKind` 映射；新增 `[data-testid="orientation-context"]` 容器渲染；收到 `orientation_context_present=true` 时主动拉 `/learning-units/{unit_id}`
- 文件: `tests/test_observability_static.js` 断言 `[data-testid="orientation-context"]` 容器三态可见性正确
- 验收: web 页面 collision 阶段卡片出现 orientation；刷新后信息保留；SSE 帧不含完整 OrientationContext 结构体
- 提交: `feat(web/§3.4-§3.5): orientation SSE 透传与前端渲染`

### Step 8: 重生成路径 + alignment 协同

- 文件: `learning_agent/learning_agent/main.py` 在 `_prepare_learning_unit_turn` alignment 决策后、`maybe_generate_orientation` 调用前加 §7 描述的重生成判定（满足条件时先 `unit.orientation_context = None`，并通过参数或临时标记传 `regenerated=True` 给下游 emit）
- 验收: 10.1.8 + `lm-p1b-orientation-defer-after-active-alignment` 单测/数据集 case 全绿
- 提交: `feat(orientation/§3.2): alignment 解除后重生成路径`

### Step 9: 真实数据集 + baseline 实跑

- 文件: `tests/e2e/real_datasets/learning-mode-phase-1b-orientation-context-real.json` 新增 5 case（与 §10.2 表一一对应）
- 文件: `tests/e2e/real_baselines/learning-mode-phase-1b-orientation-context-real.baseline.json` 由 `real_runner.py` 真实跑出（**status 必须是 executed/pass，不允许 `not_yet_executed`**）
- 验收: 数据集 case 全绿；baseline status=pass；diff 与设计 §3.2 事件 payload 字段对齐；5 个 must_pass_cases 都达成。
- 提交: `test(e2e/§4.3-§4.4): Phase 1B orientation real dataset + baseline 实跑`

### Step 10: 文档/CHANGELOG 同步（三类 docs/changes/ 留痕）

`docs/changes/` 留痕扩为三类（与 doc4 §6 / doc2 §11 / doc1 §1.1 对齐），命名规约：`docs/changes/<date>-learning-mode-phase-1b-{kickoff|close|rollback}.md`。

- 必出：`docs/changes/<kickoff-date>-learning-mode-phase-1b-kickoff.md`（开工记录，写在 doc1 合入 + doc3 评审通过当天；记录 Step 0 门禁四件硬约束的 commit hash / 评审通过日期）
- 全绿后出：`docs/changes/<close-date>-learning-mode-phase-1b-close.md`（关闭记录，§7 全绿当天的本地日期，含 baseline 摘要 / 真实 JSONL 摘要；前端截图项已按 2026-05-31 用户指示免除，并在验收报告中登记替代证据）
- 触发回滚才出：`docs/changes/<rollback-date>-learning-mode-phase-1b-rollback.md`（任一 doc4 §8 回滚条件触发时；按 §3 子节逆序 revert 后留痕）
- 更新: `docs/README.md` 研学读取顺序表新增「研学 Phase 1B」一行，指向本文与 doc4
- 验收: 文档/代码/测试三同步检查通过；AGENTS.md 对齐基准无新增要求；三类 docs/changes/ 留痕文件命名遵守规约
- 提交: `docs(learning-mode/§6): Phase 1B 三类留痕规约登记`

---

## 12. open_questions（需用户拍板，与 doc4 §10 一致）

1. **已对齐：`orientation_digest` 摘要算法**。实现为 `sha256(f"{prompt_text}|{hook_kind}|{source_seed_ref or ''}")` 后取 hex 前 16 字符；事件 payload 与 `OrientationContext.orientation_digest` 完全一致。
2. **已对齐：LLM 调用硬超时秒数**。实现常量为 `ORIENTATION_TIMEOUT_SECONDS = 6`，低于 doc4 建议上限 8 秒；失败/超时走静态 fallback。
3. **已删除：clarification 启发置信度**。本期不实现 `collision→forge`，因此不需要 clarification 判定；相关逻辑留给 Phase 1C。
4. **已对齐：重生成冷却**。本期不引入重生成冷却和额外数据底座；`orientation_context is not None` 时 resume/后续轮复用，不重发事件。
5. **已对齐：`prompt_text` 120 字上限**。模型字段与后校验均按 120 字约束执行；5 个 1B real cases 已通过。

---

## 13. invariants 复核

- **事实源唯一**: `orientation_context` 落 LearningUnit；事件 JSONL 仅追踪 ✓
- **四层职责**: schema 在 ai 层、policy 在 learning_agent 层、Runtime 透传不解读、UI 只渲染派生态 ✓
- **N=1 不做未来化**: 不为 1C/1D 建字段；不抽子模型；hook_kind 启发判定不加 LLM judge ✓
- **文档/代码/测试三同步**: 实施步骤 Step 0-10 每步配单测+数据集+baseline ✓
- **学习卷主链不变**: `_ALLOWED_TRANSITIONS` 完全不动 ✓
- **子阶段独立可回滚**: Step 1-10 每步可独立 revert（Step 1/2 即使单独保留也只是冗余字段+枚举，无运行时影响）✓
- **与 1A 衔接**: entry→collision 推进在 `orientation_context is not None` 前置 gate 之后；1B 不推进 `collision→forge`；ASK 对齐轮短路时 orientation 路径不触达 ✓
- **alignment 状态机一致**: orientation 判定只涉及 {idle, skipped} 两档真实可达状态 ✓
- **事件 EventVisibility**: `LEARNING_UNIT_ORIENTATION_GENERATED` / `LEARNING_UNIT_ORIENTATION_FALLBACK_USED` = AGENT，不参与 replay ✓
- **双事件登记**: `LEARNING_UNIT_ORIENTATION_GENERATED` 与 `LEARNING_UNIT_ORIENTATION_FALLBACK_USED` 在 `session_events.py` 与 projection 中**分别**登记；下游 projection / 指标作为独立事件类型计算，禁止仅靠 `source` 字段隐式分叉 ✓
- **独立 provider 通道**: orientation 走独立 provider 调用，产物落 `unit.orientation_context`；**禁止**进 `TurnExecutionProfile.system_prompt` / `system_prompt_addendum` / 主回合 prompt 装配链；Runtime / `build_turn_profile` 完全不感知 orientation ✓
- **source 字段必填**: `OrientationContext.source: Literal["llm", "fallback"]` 必填、无默认值；与生成路径一一对应；与事件类型与 payload `source` 三方一致 ✓
- **三类 docs/changes/ 留痕**: `kickoff` / `close` / `rollback` 三类文件按 §11 Step 10 规约登记；与 doc4 §6 / doc2 §11 / doc1 §1.1 对齐 ✓
