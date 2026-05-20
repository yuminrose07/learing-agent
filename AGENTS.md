# AGENTS.md — Learning Agent

本文档是项目的核心约束目录，只保留跨任务稳定有效的协作规则。更重、更专门的约束内容放在 `docs/prompt/` 下，按需通过软链接入口加载，避免主文档膨胀。

***

## 一、核心定位

> **目标：构建可长期运行、稳定、自愈、对用户友好的 Agent。**

- 长期运行：单轮失败不应拖垮整个会话。
- 稳定优先：优先重试、补偿、降级，不做剧烈失败。
- 用户友好：必要时告知结果，不暴露内部技术噪音。

***

## 二、核心规则

- 分层自治：本层能处理的问题不上抛；禁止随意 `abort`，只有权限拒绝、用户显式决策或不可恢复系统错误才能中断。
- 自愈优先：先重试、补偿、降级，再考虑失败退出；所有修复应有上限并可追踪。
- 可观测优先：先记录日志、事件、追踪，再执行修正或降级。
- 澄清优先：目标、范围、约束不清时，先提问再行动，避免猜测式实现。
- 文档对齐：实现、设计文档、测试结论必须同步更新，避免三者脱节。
- 事实源约束：会话持久化以 `append-only JSONL` 作为唯一事实源；`Agent/LLM/UI message` 只允许作为派生视图或投影，禁止成为并列事实源。
- 追加写约束：禁止原地修改、覆盖、删除或回写既有 `JSONL` 事件；任何新状态、修正、压缩结果或恢复信息都必须通过追加新事件表达，再基于事件流重建上下文与视图。
- 分支隔离：长期分支（如 `main`）禁止直接落代码。新需求或重构从长期分支切出 `feature/*` 分支；该 feature 自身引入的 bug 直接在 `feature/*` 分支修复，不额外开 fix 分支。只有线上（长期分支）发现的 bug，才从长期分支切出 `hotfix/*` 分支修复。若用户明确要求在当前分支操作，直接执行，不再额外询问。
- 变更留痕：仅在以下场景必须补充对应的变更文档，且统一落盘到 `docs/changes/`：新建文档、重构代码、修复 Bug、新增需求功能。普通文档修改或已有文档的小幅更新，不要求每次都额外新增一份变更记录；但涉及设计结论、约束或实现范围的实质变化时，仍应留痕。

***

## 三、严格分层

### 3.1 总原则

- 项目按四层收敛：`Interface`、`Product/Application`、`Agent Runtime`、`Infrastructure`。
- 每层只做本层决策，不替上层偷做业务编排，也不把下层实现细节向上泄漏。
- 新能力接入前，先判断“谁拥有状态、谁负责编排、谁只负责执行”，再落代码。
- 运行时只消费已经收口好的执行输入，不拥有产品语义。

### 3.2 各层职责

- `Interface`
  - 负责 CLI / Web / API / SSE / 前端交互适配。
  - 负责请求解析、参数校验、协议转换、输出格式化。
  - 不负责会话模式流转、不持有 runtime 私有状态、不实现核心业务决策。

- `Product/Application`
  - 负责产品级编排与领域状态，包括 session 生命周期、mode、mode metadata、ask 对齐流程、memory 子域编排、runtime 清理编排。
  - 负责把产品语义收口为 runtime 可执行输入，例如构造 turn profile、决定是否进入对齐轮或正常执行轮。
  - 负责持久化触发、跨子域协作、对外暴露稳定 facade。
  - 不实现 ReACT 主循环，不持有单 session 的 runtime 私有锁、流式状态机和工具执行细节。

- `Agent Runtime`
  - 负责单个 session / 单轮 turn 的执行，包括状态机、上下文组装、LLM 调用、工具调度、重试、降级、自愈、事件与追踪。
  - 只依赖最小协议或 ports，不直接绑定 `SessionManager`、`MemoryManager` 这类 Product 具体实现。
  - 可以持有 runtime 私有可变状态，例如锁、trace 关联、failure tracker、chat-only 降级状态。
  - 不负责 session 生命周期决策，不负责 mode 切换策略，不判断 Ask / Study 等产品语义，不维护确认词规则，不编排长期 memory 策略。

- `Infrastructure`
  - 负责文件存储、数据库、外部 Provider、网络 IO、第三方 SDK 适配等基础设施实现。
  - 只提供能力，不拥有产品策略，不决定流程流转。
  - 不直接编排 session、mode、runtime 状态。

### 3.3 状态归属

- 会话持久化状态属于 `Product/Application`，例如：`session.mode`、`mode_metadata`、`ask_state`、objective 关联。
- 单轮执行态和运行时私有状态属于 `Agent Runtime`，例如：锁、当前状态机节点、trace/span 关联、失败计数、降级状态。
- Provider 连接、文件落盘、外部资源句柄属于 `Infrastructure`。
- UI 临时展示态属于 `Interface`，不得替代服务端真实会话状态。

### 3.4 依赖方向

- 允许依赖方向：`Interface -> Product/Application -> Agent Runtime -> Infrastructure ports/adapters`。
- `Product/Application` 可以编排 `Runtime` 与领域服务，但不应反向依赖 `Interface`。
- `Agent Runtime` 只能依赖抽象协议、ports、通用数据结构，禁止反向依赖 Product 具体编排器。
- `Infrastructure` 不反向依赖上层业务语义；若需要回调，必须通过显式接口或事件。

### 3.5 禁止越界

- 禁止在 `Agent Runtime` 中直接实现产品流程编排。
  - 例如：根据确认词决定 Ask 是否结束、决定 `Ask -> Chat` / `Ask -> Study`、切换 session mode。
- 禁止在 `Interface` 中维护服务端真实状态。
  - 例如：前端本地 mode 成为权威状态，覆盖服务端 session.mode。
- 禁止把未来属于领域层的能力硬塞进 runtime。
  - 若能力尚未完成，先在正确层定义边界与 No-op / Stub，再逐步补实现。
- 禁止 `Infrastructure` 携带业务分支。
  - 例如：Provider / FileStore 根据产品 mode 改写业务策略。

### 3.6 判定规则

- 如果某段代码回答的是“产品想怎么运行”，它属于 `Product/Application`。
- 如果某段代码回答的是“这一轮具体怎么执行”，它属于 `Agent Runtime`。
- 如果某段代码回答的是“协议怎么进出系统”，它属于 `Interface`。
- 如果某段代码回答的是“能力如何与外部系统交互”，它属于 `Infrastructure`。

### 3.7 文档使用

- `AGENTS.md` 作为默认入口，定义稳定的分层职责与边界。
- 需要了解 Core / Extensions 的职责边界、判断标准和 Hook 规则时，加载 `AGENTS.architecture.md`。

***

## 四、高级约束目录

- `AGENTS.md` 只保留核心规则；高级约束统一外置到 `docs/prompt/`，按需通过软链接入口加载。
- 需要了解约束框架本身时，加载 `AGENTS.constraints.md`。
- 需要做项目诊断、迭代前审视、修 Bug 前排查时，加载 `AGENTS.audit.md`。
- 后续模块沿用同一入口约定：`AGENTS.<module>.md`，例如 `AGENTS.memory.md`、`AGENTS.performance.md`、`AGENTS.security.md`。
- 高级约束只做补充和收紧，不覆盖本文件中的核心规则。

***

## 五、维护规范

- 只有“跨任务复用、篇幅较长、属于专项流程模板”的内容，才外置为约束模块。
- 临时任务说明和一次性上下文不进入 `AGENTS.md`，也不注册为模块。
- 核心规则变化时，同步检查相关扩展模块，避免冲突。
- 每次新增模块时，至少确认三件事：入口是否有效、用途是否清晰、是否真的值得常驻维护。
- 优先保证 `AGENTS.md` 在 1 分钟内可读完，重点约束可快速定位。
