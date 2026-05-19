# 项目文档中心

本目录集中存放 Learning-Agent 项目的设计、调研、审计、交付与故障记录文档。

---

## 目录结构

```
docs/
├── README.md              # 本文件：文档索引与导航
├── design/                # 技术设计文档（Design Docs）
├── research/              # 调研报告与竞品分析
├── audit/                 # 代码审计与项目诊断
├── bugfix/                # Bug 报告与修复记录
├── output/                # 实施报告、验收报告与任务归档
└── prompt/                # Agent 约束模块软链接入口
```

---

## 各目录说明

### `design/` — 技术设计文档

所有技术方案、架构设计、接口约定的源头文档。

**命名规范**：统一使用 `design-<主题>.md` 前缀，便于快速检索。

| 文档 | 主题 | 状态 |
|------|------|------|
| `design-current-code-architecture-boundaries.md` | 当前架构骨架与职责边界 | 基线文档 |
| `design-four-layer-convergence-plan.md` | 四层架构收敛方案 | 基线文档 |
| `design-architecture-convergence-implementation.md` | 架构收敛技术实现 | 已落地 |
| `design-agent-loop-session-refactor.md` | AgentLoopSession 架构重构 | 已落地 |
| `design-runtime-protocol-inversion-minimal-implementation.md` | Runtime 依赖倒置与多 Session 实现 | 已落地 |
| `design-hook-refactor-minimal-runtime.md` | Hook 重构与最小运行时切点收敛 | 已落地 |
| `design-web-adaptation.md` | Web 前后端适配方案 | 已落地 |
| `design-observability-enhancement.md` | 观测页面增强方案 | 已落地 |
| `design-learning-agent-observability.md` | 可观测性整体设计 | 已落地 |
| `design-error-log-copy-for-ai.md` | 观测页面「智能复制错误日志」功能 | 已落地 |
| `design-api-compatibility-tool-results-fix.md` | API 兼容性与 Tool Results 完整性修复 | 已落地 |
| `design-tool-execution-reliability-fix.md` | 工具执行可靠性修复 | 已落地 |
| `design-resilience-and-validation-design.md` | 重试、兜底与工具输入校验 | 已落地 |
| `design-context-compression-memory-implementation.md` | 上下文压缩与 Memory 实施 | 已落地 |
| `design-context-usage-observability.md` | 上下文 usage 数据结构、SSE 暴露、持久化与前端展示约定 | 已落地 |
| `design-compaction-strategy-adaptation-from-cc.md` | 基于 Claude Code 的 Compaction 适配 | 已落地 |
| `design-large-file-handling.md` | 大文件处理技术方案 | 已落地 |
| `design-chat-ask-study-modes.md` | Chat / Ask / Study 三模式产品设计 | 设计中 |
| `design-impl-chat-ask-modes.md` | Chat / Ask 双模式落地 + Study 预留接口 | 已落地 |

### `research/` — 调研报告

外部系统调研、竞品分析与策略深度研究。

| 文档 | 主题 |
|------|------|
| `compaction-strategy-deep-dive.md` | Claude Code 上下文压缩策略调研 |
| `claude-code-event-hook-extension-architecture.md` | Claude Code 事件/Hook/扩展架构调研 |
| `pi-mono-architecture-analysis.md` | pi-mono 分层架构调研 |
| `pi-mono-event-hook-extension-architecture.md` | pi-mono 事件/Hook/扩展架构调研 |
| `pi-tool-execution-layer-analysis.md` | pi 工具调用分层架构调研 |
| `pi-vs-claude-code-comparison.md` | pi-mono 与 Claude Code 对比分析 |
| `research-pi-mono-session-state-isolation.md` | pi-mono Session 状态隔离机制调研 |

### `audit/` — 代码审计

| 文档 | 主题 | 日期 |
|------|------|------|
| `code-audit-report-agent-loop-2026-05-12.md` | Agent Loop 核心代码错误分析 | 2026-05-12 |
| `extension-wiring-audit-checklist-2026-05-13.md` | 扩展接线审计表 | 2026-05-13 |
| `technical-lead-diagnostic-checklist-2026-05-13.md` | 技术负责人项目诊断清单 | 2026-05-13 |

### `bugfix/` — Bug 报告与修复

按时间顺序记录关键故障及其修复方案。

| 文档 | 主题 | 日期 |
|------|------|------|
| `bug-report-context-length-silent-failure-and-oversized-tool-result.md` | ContextLengthError 静默失败 & 超大工具结果 | 2026-05-12 |
| `fix-context-truncation-tool-call-id.md` | 滑动窗口截断导致 tool_call_id 丢失 | 2026-05-12 |
| `fix-hook-minimal-runtime-refactor.md` | Hook 最小运行时收敛与 typed 契约重构 | 2026-05-13 |
| `fix-agent-loop-monolith-split.md` | AgentLoop 单体文件拆分与边界收敛 | 2026-05-16 |

### `output/` — 实施与验收报告

设计文档落地后的执行归档，含实施总结、验收报告与任务报告。

| 文档 | 对应设计 | 类型 |
|------|----------|------|
| `implementation-summary-agent-loop-session-refactor.md` | `design-agent-loop-session-refactor.md` | 实施总结 |
| `implementation-summary-api-compatibility-tool-results-fix.md` | `design-api-compatibility-tool-results-fix.md` | 实施总结 |
| `implementation-summary-observability-enhancement.md` | `design-observability-enhancement.md` | 实施总结 |
| `implementation-summary-resilience.md` | `design-resilience-and-validation-design.md` | 实施总结 |
| `implementation-summary-tool-execution-reliability.md` | `design-tool-execution-reliability-fix.md` | 实施总结 |
| `implementation-summary-web-adaptation.md` | `design-web-adaptation.md` | 实施总结 |
| `acceptance-report-strict-layered-mode-refactor.md` | `design-impl-chat-ask-modes.md` | 验收报告 |
| `acceptance-report-context-usage-observability.md` | `design-context-usage-observability.md` | 验收记录 |
| `task-report-toolguard-refactor.md` | — | 任务报告 |

### `prompt/` — Agent 约束模块入口

软链接入口，按需加载专项约束：

- `architecture` → `AGENTS.architecture.md`
- `audit` → `AGENTS.audit.md`
- `constraints` → `AGENTS.constraints.md`

---

## 使用规范

1. **新增设计文档**必须放入 `design/`，并遵循 `design-<主题>.md` 命名。
2. **设计落地后**应在 `output/` 补充对应的实施总结或验收报告。
3. **发现关键 Bug**应在 `bugfix/` 按 `bug-report-<主题>.md` 或 `fix-<主题>.md` 记录。
4. **引用其他文档**时，使用相对路径，例如：`docs/design/design-four-layer-convergence-plan.md`。
5. **本索引更新**：新增或移动文档后，同步更新本 README 中的表格与状态。
