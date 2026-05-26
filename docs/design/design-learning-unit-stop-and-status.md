# 研习卷停止与模式可感知性技术方案

Date: 2026-05-26

## 背景

当前研习模式把“未完成”定义为 `phase != consolidated`。这能保护“一次只运行一卷”的产品约束，但缺少用户主动结束当前主题的能力。结果是用户只要没有走到 TEACH 验收闭环，就无法开始新主题。

同时，前端进入研习 session 后缺少稳定的状态信号。用户很难判断自己是在闲谈还是研习，也不知道当前卷处于讲解、复述检验、已收束还是被放下。

## 问题定义

### P0: Active unit 无退出门

- 代码现状：`LearningUnit.is_terminal()` 仅判断 `phase == "consolidated"`。
- 阻塞来源：`LearningUnitStore.find_active()` 会返回任何非 terminal unit。
- 用户后果：未走完验收的旧卷会永久阻止新卷。

### P1: 完成定义与产品文档脱节

产品文档已定义一卷完成条件之一是“用户显式确认先结束当前卷”，但代码只实现了“通过 TEACH 进入 consolidated”。

### P1: 研习模式不可感知

前端只有底部按钮 active 状态和消息区顶部研习卡。加载历史后页面滚到底部，用户容易看不到研习卡，因此无法稳定感知当前模式。

## 设计决策

### 1. 增加 `stopped` 终态

新增 `LearningUnitPhase = "stopped"`。

- `consolidated`：完成验收，计入学习卷完成率。
- `stopped`：用户主动“先学到这里”，释放 active unit，但不计入完成率。

`is_terminal()` 改为：

```text
phase in {"consolidated", "stopped"}
```

### 2. 保留单卷互斥，但允许用户释放

`find_active()` 继续返回非 terminal unit。由于 `stopped` 是 terminal，它不会再阻止用户创建新研习卷。

### 3. 新增 stop API

新增：

```text
POST /learning-units/{unit_id}/stop
```

行为：

- `absorbing -> stopped`
- `outputting -> stopped`
- `stopped` 幂等返回当前卷
- `consolidated` 幂等返回当前卷
- 记录 `stop_reason` 与 `stopped_at`
- 追加 `learning_unit.phase_changed` 与 `learning_unit.stopped` 事件

### 4. 前端增加常驻研习状态

研习卡继续展示详细目标与动作，但顶部栏必须常驻显示：

```text
研习中 · <目标>
复述检验 · <目标>
已收束 · <目标>
已停止 · <目标>
```

这样用户即使在消息流底部，也能知道自己当前是否在研习。

### 5. 前端增加“先学到这里”

研习卡在 `absorbing` 和 `outputting` 阶段显示次级动作：

```text
先学到这里
```

点击后调用 stop API，研习卷进入 `stopped`，用户可以开始新主题。

## 非目标

- 不允许多个 active 研习卷并行。
- 不把 `stopped` 算进完成率。
- 不删除旧学习卷和 session 历史。
- 不改长期学习计划、跨卷串联和记忆策略。

## 实施清单

- [x] 数据模型：增加 `stopped` phase、`stop_reason`、`stopped_at`。
- [x] 状态机：允许 `absorbing/outputting -> stopped`。
- [x] active 判定：`stopped` 不再阻塞新卷。
- [x] Product 层：新增 `stop_learning_unit()`。
- [x] 事件：新增 `learning_unit.stopped`。
- [x] Web API：新增 `POST /learning-units/{unit_id}/stop`。
- [x] 前端研习卡：增加“先学到这里”按钮和 stopped 展示。
- [x] 前端顶部栏：常驻展示研习状态和目标。
- [x] 冲突恢复：遇到已有 active unit 时回到旧卷，但不把新主题自动发进旧卷。
- [x] 测试：覆盖状态机、store active 判定、API、事件、前端静态行为。

## 验证路径

1. 创建研习卷 A。
2. 不进入 TEACH，点击“先学到这里”。
3. A 的 phase 变为 `stopped`。
4. 创建研习卷 B 成功。
5. 顶部栏显示 B 的研习状态。
6. completion metrics 不把 A 算作 consolidated。
