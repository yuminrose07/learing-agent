# 全局工具调用成功导向架构方案

日期：2026-05-26

## 1. 目标

本方案只服务一个最终目标：

- 对用户面，工具调用必须表现为成功
- 用户不能感知到中间失败
- 系统不能出现空返回、半截回答、原始工具错误暴露
- 所有失败都必须在系统内部完成重试、换源、补偿和恢复

这不是单个工具的优化问题，而是整个工具调用主链的正确性问题。

## 2. 当前问题

当前系统已经有基础的工具执行骨架，但整体仍然偏“执行导向”，不是“成功导向”。主要问题有五类：

### 2.1 失败语义不统一

- 有的工具通过抛异常表示失败
- 有的工具通过返回 `{"error": ...}` 结构表示失败
- 有的错误带 `retryable` 信息，但执行器并不统一消费

结果是：

- 只有“异常型失败”才能进入现有重试链
- “结果型失败”会被当成普通工具结果写回主链

### 2.2 重试机制只覆盖异常，不覆盖失败结果

- 当前重试判断主要依赖异常类型或错误字符串关键字
- 如果工具返回结构化错误结果，执行器不会自动进入重试和补偿

结果是：

- 真实失败没有被统一识别
- 系统缺少“失败结果驱动恢复”的能力

### 2.3 恢复策略分散在各工具内部

- 某些 fallback 是工具里临时写的
- 某些恢复靠模型自己决定要不要再搜、再抓、再换源
- 某些工具没有任何补偿路径

结果是：

- 不同工具可靠性不一致
- 系统层没有稳定的“失败后怎么恢复”的统一策略

### 2.4 结果提交前没有硬护栏

- 工具结果写入 session 主链前，缺少正式的结果校验/修正层
- 空结果、失败结果、结构化错误结果都可能直接流入对话链

结果是：

- 运行时可能已经失败，但主链仍然继续
- 最终可能出现用户看到空回答或不完整回答

### 2.5 用户可见层没有“成功结果保证”

- 同步 `/chat` 目前只是收集 assistant chunk 拼接文本
- 如果工具失败后模型没有继续生成回答，就会返回空内容

结果是：

- 用户感知到失败
- 用户承担了系统内部错误的成本

## 3. 北极星原则

后续所有工具调用相关改造，都必须满足以下四条原则：

### 3.1 成功优先

- 工具调用的目标不是“执行完成”
- 而是“最终拿到正确结果并返回给用户”

### 3.2 失败内化

- 工具错误只能存在于系统内部
- 不允许直接暴露到用户回答

### 3.3 结果导向恢复

- 恢复链不应只对异常生效
- 只要最终结果不可用，就必须继续恢复

### 3.4 提交前收口

- 只有可用结果才能进入用户可见主链
- 所有失败结果都必须在提交前被消费、修正或替换

## 4. 目标架构

建议把整个工具调用主链改造成五层：

### 4.1 Tool Adapter

职责：

- 与外部系统交互
- 只负责真实调用
- 返回标准化原始结果

不负责：

- 重试
- 换源
- 用户体验兜底

### 4.2 Tool Outcome Normalizer

职责：

- 统一把所有工具执行结果收口成标准 `ToolOutcome`
- 无论原始表现是异常、错误对象还是空结果，都转成统一语义

建议标准字段：

- `status`: `success | retryable_failure | recoverable_failure | fatal_failure`
- `reason_code`
- `message`
- `data`
- `retryable`
- `recoverable`
- `provider_hint`
- `recovery_hint`

这一层是全局改造的核心入口。

### 4.3 Tool Recovery Policy

职责：

- 在工具失败时统一编排内部恢复链

建议标准恢复顺序：

1. 同参数重试
2. 缩小参数范围重试
3. 改 query 重试
4. 换同域候选 URL
5. 换同类官方来源
6. 换 fallback provider
7. 生成内部 synthetic 成功结果或补偿结果

说明：

- 恢复策略属于 Runtime 的公共能力
- 不应散落在各个工具内部

### 4.4 Tool Result Committer

职责：

- 工具结果写入 session 主链前做统一校验
- 拦截无效结果
- 替换失败结果
- 保证主链只看到“可继续推进回答”的结果

建议校验项：

- `tool_call_id` 是否完整
- result 是否为空
- 是否仍是失败态
- 是否已有可用补偿结果
- 是否允许提交为用户可见证据

### 4.5 Final Answer Guarantee

职责：

- 在 assistant 最终回答提交前做最终保证
- 如果工具链路经历过失败但已经恢复成功，正常回答
- 如果所有恢复都失败，也必须继续内部补偿，不能直接给用户空内容

最终对用户的要求只有一个：

- 返回可用答案

## 5. 核心对象设计

### 5.1 ToolOutcome

建议新增统一对象：

```python
@dataclass
class ToolOutcome:
    tool_id: str
    call_id: str
    status: Literal[
        "success",
        "retryable_failure",
        "recoverable_failure",
        "fatal_failure",
    ]
    reason_code: str
    message: str
    data: Any
    retryable: bool
    recoverable: bool
    user_visible: bool
    recovery_hint: dict[str, Any]
    internal_attempts: int
```

关键约束：

- `user_visible=False` 应作为默认值
- 工具层失败不应天然进入用户可见链路

### 5.2 RecoveryPlan

建议新增恢复计划对象：

```python
@dataclass
class RecoveryPlan:
    strategy: Literal[
        "retry_same_call",
        "retry_with_adjusted_args",
        "retry_with_new_query",
        "retry_with_alternate_url",
        "retry_with_alternate_source",
        "retry_with_fallback_provider",
        "synthetic_compensation",
    ]
    next_arguments: dict[str, Any]
    max_attempts: int
    stop_after_success: bool = True
```

### 5.3 CommitDecision

建议结果提交器输出：

```python
@dataclass
class CommitDecision:
    commit: bool
    replace_with: ToolOutcome | None
    append_internal_event: bool
    reason: str
```

## 6. 主链改造方案

### 6.1 统一失败判定

要做的事：

- 废弃“仅凭异常判断失败”的模式
- 所有工具执行结束后统一走 `ToolOutcomeNormalizer`
- 把异常、结构化错误结果、空结果全部收口成标准失败语义

目标：

- 系统只认 `ToolOutcome`
- 不再依赖工具作者各自决定怎么表达失败

### 6.2 统一恢复编排

要做的事：

- `ToolExecutor` 不再只负责调用和简单重试
- 增加 `ToolRecoveryPolicy`，由它统一决定下一步怎么补救

恢复策略优先级：

- 优先最小代价恢复
- 优先权威来源恢复
- 优先同域补偿
- 最后才走 provider/fallback 切换

### 6.3 统一提交收口

要做的事：

- 工具结果不再直接 append 到 session
- 先过 `ToolResultCommitter`

提交规则：

- 成功结果可以提交
- 可恢复失败不能直接提交
- 不可恢复失败也不能直接暴露给用户
- 必须先走补偿和恢复

### 6.4 最终回答保证

要做的事：

- `/chat` 同步链路增加最终回答保护
- 不允许 `content=""`
- 不允许只有工具失败记录但没有 assistant 结果

保证方式：

- 回答提交前检查本轮是否有有效证据
- 若没有，则继续走内部恢复
- 直到得到可交付答案

## 7. 分层落点

建议按以下边界落代码：

### 7.1 Agent Runtime

负责：

- `ToolOutcomeNormalizer`
- `ToolRecoveryPolicy`
- `ToolExecutor`
- `Final Answer Guarantee`

原因：

- 这些都属于“这一轮具体怎么执行和恢复”

### 7.2 Product/Application

负责：

- `ToolResultCommitter`
- session 主链提交收口
- 结果投影前的一致性保护

原因：

- 这里已经进入事实写入与用户可见结果层

### 7.3 Infrastructure

负责：

- 各类 provider/adapters
- 外部调用和 fallback 接入能力

原因：

- 这里提供能力，不负责恢复策略

## 8. P0 改造清单

第一阶段只做必须收口的 P0：

### P0-1 统一 ToolOutcome

- 新增标准结果对象
- 所有工具执行后统一归一

### P0-2 修失败判定主链

- 修 `turn_count` 失败记录问题
- 让失败窗口统计真实可信

### P0-3 结果级恢复链

- 让结构化失败结果也能触发恢复
- 不再只有异常才重试

### P0-4 结果提交器

- 工具结果写入 session 前统一校验
- 禁止空结果和失败结果直接进入主链

### P0-5 最终回答保护

- `/chat` 保证不返回空内容
- 一轮回答结束前必须确认存在可交付结果

## 9. P1 改造清单

在 P0 稳定后推进：

### P1-1 Provider fallback 标准化

- 把 RSS fallback、浏览器 fallback、镜像源 fallback 变成统一能力

### P1-2 恢复策略模板化

- 为搜索类、抓取类、代码类、写入类工具分别定义恢复模板

### P1-3 可观测性增强

- 清晰记录每次恢复采用了哪种补偿策略
- 但这些信息只用于内部调试，不暴露给用户

### P1-4 成功率指标体系

建议增加：

- `tool_final_success_rate`
- `tool_internal_recovery_rate`
- `user_visible_failure_rate`
- `empty_response_rate`
- `recovery_steps_per_turn`

核心指标：

- `user_visible_failure_rate` 必须逼近 0
- `empty_response_rate` 必须为 0

## 10. 验收标准

### 10.1 用户侧标准

- 用户看不到工具错误
- 用户看不到空回答
- 用户看不到“请重试”类文案

### 10.2 运行时标准

- 所有失败都能被统一归类
- 所有失败都能进入恢复链
- 所有主链结果都经过提交前校验

### 10.3 测试标准

至少覆盖以下场景：

- 工具抛异常
- 工具返回结构化失败结果
- 工具返回空结果
- 同域 fallback 成功
- provider fallback 成功
- 恢复后正常回答
- 同步 `/chat` 不再空返回

## 11. 建议实施顺序

建议按四步推进：

1. 先统一失败语义和 `ToolOutcome`
2. 再补结果级恢复链
3. 再加提交器和最终回答保护
4. 最后统一 provider fallback 与指标体系

原因：

- 如果先补 fallback，不先统一失败语义，后面还会继续散
- 如果不先加提交器，恢复链即使存在，也可能让失败结果继续流进主链

## 12. 一句话方案

把“工具调用”从当前的“执行一次函数”升级成“一个必须成功交付结果的内部事务”。

用户只应该看到成功结果，不应该看到失败过程。
