# 项目文档中心

本目录集中存放 Learning-Agent 项目的产品、设计、实现、验收、测试、审计与变更记录文档。

阅读原则：

- 先看“当前主线快速入口”，再按主题进入具体目录。
- 需要追溯某次改动时，优先看 `docs/changes/`。
- 旧文档不会随意删除；被后续方案取代的文档会在 README 中标注为“历史方案”或“部分沿用”。

---

## 当前主线快速入口

| 主题 | 优先阅读文档 | 说明 |
|------|--------------|------|
| 架构边界 | `docs/design/design-current-code-architecture-boundaries.md` | 当前四层边界与职责归属基线 |
| 四层收敛 | `docs/design/design-four-layer-convergence-plan.md` | Interface / Product / Runtime / Infrastructure 收敛方案 |
| 会话事实源 | `docs/design/design-session-event-log-single-source-refactor.md`、`docs/changes/2026-05-21-append-only-jsonl-rule.md` | append-only JSONL 是唯一事实源 |
| 压缩主线 | `docs/design/design-full-compact-slact-adaptation.md` | Full Compact / Slact / compaction summary 事件化 |
| 研学模式产品 | `docs/output/learning-mode-final-product-prd-2026-05-27.md` | 当前研学模式最终产品 PRD |
| 研学模式技术 | `docs/design/design-learning-unit-adaptive-alignment.md` | 当前学习卷默认直学、按需对齐主线 |
| 闲聊 / 研学分离 | `docs/changes/2026-05-27-chat-study-separation-interface-contract.md` | 双线开发共享接口契约 |
| Web Search | `docs/output/web-search-learning-agent-prd-2026-05-25.md`、`docs/design/design-web-search-learning-agent-implementation.md`、`docs/output/web-search-optimization-four-phase-checklist-2026-05-26.md` | Web Search 产品、技术与优化路线 |
| 工具调用可靠性 | `docs/output/tool-call-success-oriented-architecture-2026-05-26.md` | 工具调用成功导向架构 |
| 测试规范 | `docs/TESTING.md` | 单测、回放、真实数据集 E2E 规范 |

---

## 目录结构

```text
docs/
├── README.md              # 本文件：文档索引与导航
├── TESTING.md             # 测试策略与真实数据集 E2E 规范
├── changes/               # 每次实质改动的落盘记录，按日期命名
├── design/                # 技术设计文档、设计清单、接口契约
├── output/                # PRD、产品定义、实施总结、验收报告、任务归档
├── research/              # 外部系统调研、竞品与架构分析
├── audit/                 # 代码审计与项目诊断
├── bugfix/                # Bug 报告与修复记录
└── prompt/                # Agent 约束模块软链接入口
```

---

## 状态口径

| 状态 | 含义 |
|------|------|
| 基线文档 | 当前仍作为稳定约束使用 |
| 当前主线 | 后续同主题设计优先读取 |
| 已落地 | 对应实现已经进入代码，可作为实现说明参考 |
| 部分沿用 | 文档中仍有有效设计，但部分结论已被后续文档取代 |
| 历史方案 | 保留作背景，不再作为新需求或新实现的决策依据 |
| 产品主线 | 当前产品方向或 PRD 的优先事实来源 |
| 变更记录 | 某次改动的落盘说明，用于追溯上下文 |

---

## `changes/`：变更落盘索引

`docs/changes/` 是每次实质改动的追溯入口。新增文档、重构代码、修 Bug、新增需求功能，原则上都应在这里留下记录。

| 日期 | 文档 | 主题 |
|------|------|------|
| 2026-05-19 | `2026-05-19-session-event-log-single-source-refactor.md` | 会话持久化切换为 JSONL 单事实源设计 |
| 2026-05-19 | `2026-05-19-full-compact-slact-design.md` | Full Compact 与 Slact 设计 |
| 2026-05-21 | `2026-05-21-append-only-jsonl-rule.md` | append-only JSONL 事实源约束进入核心规则 |
| 2026-05-21 | `2026-05-21-compaction-event-driven-revision.md` | 上下文压缩事件驱动修订 |
| 2026-05-21 | `2026-05-21-compaction-summary-pipeline-fix.md` | compaction summary pipeline 修复 |
| 2026-05-21 | `2026-05-21-full-compact-slact-code-implementation.md` | Full Compact / Slact 代码实现记录 |
| 2026-05-21 | `2026-05-21-compaction-dataset-runner.md` | 长任务压缩数据集与自动回放 runner |
| 2026-05-21 | `2026-05-21-git-workflow.md` | Git 工作流约束模块 |
| 2026-05-25 | `2026-05-25-learning-mode-product-diagnosis-report.md` | 学习模式产品诊断报告 |
| 2026-05-25 | `2026-05-25-learning-mode-mvp-definition.md` | 学习模式一阶段 MVP 定义 |
| 2026-05-25 | `2026-05-25-learning-unit-adaptive-alignment.md` | 学习卷自适应对齐方案 |
| 2026-05-25 | `2026-05-25-web-search-learning-agent-docs.md` | Web Search PRD 与技术文档落地 |
| 2026-05-25 | `2026-05-25-web-search-learning-agent-code-implementation.md` | Web Search 代码实现 |
| 2026-05-25 | `2026-05-25-web-search-chat-study-enable.md` | Web Search 接入 Chat / Study 模式 |
| 2026-05-25 | `2026-05-25-code-cleanup-hardcode-and-legacy-removal.md` | 硬编码与已退役空实现收缩 |
| 2026-05-25 | `2026-05-25-l2-observability-asserts.md` | L2 可复用断言模块 |
| 2026-05-26 | `2026-05-26-final-answer-guard-usable-answer-verdict.md` | Final Answer Guard 可用答案判定与回放测试 |
| 2026-05-26 | `2026-05-26-tool-call-success-oriented-architecture.md` | 工具调用成功导向架构方案 |
| 2026-05-26 | `2026-05-26-tool-call-success-oriented-architecture-implementation.md` | 工具调用成功导向架构实现 |
| 2026-05-26 | `2026-05-26-frontend-mode-loop-fix.md` | 前端 mode loop 修复 |
| 2026-05-26 | `2026-05-26-strict-chat-learning-separation.md` | 严格区分闲谈与研习 |
| 2026-05-26 | `2026-05-26-learning-mode-issues-before-memory-system.md` | 记忆系统 v1 前的研习模式问题清单 |
| 2026-05-26 | `2026-05-26-learning-mode-north-star-discussion.md` | 研学模式北极星讨论稿 |
| 2026-05-26 | `2026-05-26-learning-stop-modal-redesign.md` | 学习停止弹窗重设计 |
| 2026-05-26 | `2026-05-26-learning-unit-stop-and-status.md` | 学习卷停止能力与状态展示修复 |
| 2026-05-26 | `2026-05-26-learning-unit-session-link-backfill.md` | 学习卷 session 关联回填 |
| 2026-05-26 | `2026-05-26-learning-unit-review-fixes.md` | Learning Unit review fixes |
| 2026-05-26 | `2026-05-26-web-search-optimization-four-phase-checklist.md` | Web Search 四阶段优化清单落地 |
| 2026-05-26 | `2026-05-26-web-search-phase-1-quality-improvements.md` | Web Search 阶段一质量优化 |
| 2026-05-26 | `2026-05-26-web-search-phase-2-structured-fetch-and-section-pagination.md` | Web Search 阶段二结构化抓取与章节分页 |
| 2026-05-26 | `2026-05-26-web-search-phase-3-stop-strategy-and-budget-guards.md` | Web Search 阶段三停止策略与预算守卫 |
| 2026-05-26 | `2026-05-26-web-search-phase-3-followup-budget-and-guidance-fixes.md` | Web Search 阶段三后续预算与 guidance 修复 |
| 2026-05-26 | `2026-05-26-web-fetch-rss-fallback-for-official-blog-sites.md` | 官方博客 RSS fallback |
| 2026-05-26 | `2026-05-26-web-search-ssl-trust-chain-fix.md` | Web Search SSL 信任链修复 |
| 2026-05-27 | `2026-05-27-chat-study-separation-interface-contract.md` | 闲聊 / 研学模式分离接口契约 |
| 2026-05-27 | `2026-05-27-learning-chat-study-profile-separation.md` | 研学 absorbing 与闲聊 Chat profile 分离 |
| 2026-05-27 | `2026-05-27-learning-mode-final-product-prd.md` | 研学模式最终产品 PRD 变更记录 |
| 2026-05-27 | `2026-05-27-real-e2e-testing-spec.md` | 真实数据集 E2E 测试规范 |
| 2026-05-30 | `2026-05-30-learning-mode-phase-1b-kickoff.md` | 研学 Phase 1B kickoff 与顺序 gate |
| 2026-05-30 | `2026-05-30-phase-1a-hardening-absorbing-routes-to-study.md` | Phase 1A hardening：absorbing 路由到 STUDY |
| 2026-05-30 | `2026-05-30-phase-1a-hardening-rate-limited-event.md` | Phase 1A hardening：alignment rate-limited 事件 |
| 2026-05-30 | `2026-05-30-phase-1a-hardening-alignment-event-gap-rca.md` | Phase 1A hardening：对齐侧面事件 RCA |
| 2026-05-30 | `2026-05-30-phase-1a-hardening-real-runner-dataset.md` | Phase 1A hardening：真实 runner 与数据集补齐 |
| 2026-05-30 | `2026-05-30-phase-1a-hardening-baseline-rerun.md` | Phase 1A hardening：baseline 实跑回填 |
| 2026-05-31 | `2026-05-31-learning-mode-phase-1b-doc2-pregate-review.md` | Phase 1B doc2 预闸评审收口 |

---

## `design/`：技术设计文档

所有技术方案、架构设计、接口约定的源头文档。命名规范：`design-<主题>.md`。

| 文档 | 主题 | 状态 |
|------|------|------|
| `design-current-code-architecture-boundaries.md` | 当前架构骨架与职责边界 | 基线文档 |
| `design-four-layer-convergence-plan.md` | 四层架构收敛方案 | 基线文档 |
| `design-architecture-convergence-implementation.md` | 架构收敛技术实现 | 已落地 |
| `design-agent-loop-session-refactor.md` | AgentLoopSession 架构重构 | 已落地 |
| `design-runtime-protocol-inversion-minimal-implementation.md` | Runtime 依赖倒置与多 Session 最小实现 | 已落地 |
| `design-hook-refactor-minimal-runtime.md` | Hook 重构与最小运行时切点收敛 | 已落地 |
| `design-session-event-log-single-source-refactor.md` | 会话持久化切换为 JSONL 单事实源 | 当前主线 / 已落地 |
| `impl-session-event-log-single-source-checklist.md` | 会话 JSONL 单事实源重构实现清单 | 已落地参考 |
| `design-full-compact-slact-adaptation.md` | Full Compact 与 Slact 压缩模板适配 | 当前主线 / 部分已落地 |
| `impl-compaction-strategy-file-level-checklist.md` | Compaction 实现文件级清单 | 已落地参考 |
| `design-context-compression-memory-implementation.md` | 上下文压缩与 Memory 实施 | 部分沿用 |
| `design-compaction-strategy-adaptation-from-cc.md` | 基于 Claude Code 的 Compaction 适配 | 部分沿用 |
| `design-context-usage-observability.md` | 上下文 usage 数据结构、SSE 暴露、持久化与前端展示 | 已落地 |
| `design-learning-agent-observability.md` | 可观测性整体设计 | 已落地参考 |
| `design-observability-l1-l4-architecture.md` | 可观测系统 L1-L4 分层架构 | 当前主线参考 |
| `design-observability-enhancement.md` | 观测页面增强方案 | 已落地 |
| `design-error-log-copy-for-ai.md` | 观测页面智能复制错误日志 | 已落地 |
| `design-api-compatibility-tool-results-fix.md` | API 兼容性与 Tool Results 完整性修复 | 已落地 |
| `design-tool-execution-reliability-fix.md` | 工具执行可靠性修复 | 已落地 |
| `design-resilience-and-validation-design.md` | 重试、兜底与工具输入校验 | 已落地 |
| `design-web-adaptation.md` | Web 前后端适配方案 | 已落地 |
| `design-large-file-handling.md` | 大文件处理技术方案 | 已落地 |
| `design-chat-ask-study-modes.md` | Chat / Ask / Study 三模式产品设计 | 历史方案：三模式顶层口径已被学习卷方案取代 |
| `design-impl-chat-ask-modes.md` | Chat / Ask 双模式落地 + Study 预留接口 | 已落地：Chat/Ask 基础实现参考；`Study` 顶层口径已被学习卷取代 |
| `design-modes-refactor-chat-vs-learning-unit.md` | Chat 与学习卷重构，Learning Unit / TEACH / 持久化契约 | 部分沿用：学习卷结构仍有效，强制 `aligning -> ASK` 已被取代 |
| `design-learning-unit-adaptive-alignment.md` | 学习卷自适应对齐 | 当前主线 / 已落地 |
| `design-learning-mode-phase-1b-1e-skeleton.md` | 研学 Phase 1B-1E 骨架边界 | 已预闸收口 / 待正式评审 |
| `design-learning-mode-phase-1b-orientation-context.md` | Phase 1B OrientationContext 详设 | 待评审 / 后续实施依据 |
| `design-learning-unit-stop-and-status.md` | 学习卷停止能力与前端常驻研习状态 | 已落地 |
| `design-web-search-learning-agent-implementation.md` | Learning-Agent Web Search 外部知识检索能力 | 当前主线 / 已落地迭代中 |

---

## `output/`：产品、实施与验收归档

| 文档 | 类型 | 状态 |
|------|------|------|
| `learning-mode-final-product-prd-2026-05-27.md` | 正式 PRD | 产品主线 |
| `learning-mode-phase-1a-hardening-2026-05-30.md` | Phase 1A 硬化清单 | 当前实施 / 已回填 baseline |
| `learning-mode-phase-1b-plan-checklist-2026-05-30.md` | Phase 1B 开工 checklist | 待 Phase 1A / 1B design gate 关闭后执行 |
| `report-learning-mode-product-diagnosis-2026-05-25.md` | 产品诊断 | 背景参考 |
| `learning-mode-mvp-definition-2026-05-25.md` | MVP 范围稿 | 历史 MVP 参考；最终口径以 2026-05-27 PRD 为准 |
| `learning-mode-mvp-task-breakdown-2026-05-25.md` | 任务拆解 | 历史任务拆解；实现细节需按当前代码与 adaptive alignment 校验 |
| `web-search-learning-agent-prd-2026-05-25.md` | 正式 PRD | Web Search 产品主线 |
| `web-search-optimization-four-phase-checklist-2026-05-26.md` | 优化路线 | Web Search 当前优化主线 |
| `tool-call-success-oriented-architecture-2026-05-26.md` | 架构方案 | 工具调用可靠性主线 |
| `acceptance-report-strict-layered-mode-refactor.md` | 验收报告 | 已完成 |
| `acceptance-report-context-usage-observability.md` | 验收记录 | 已完成 |
| `implementation-summary-agent-loop-session-refactor.md` | 实施总结 | 已完成 |
| `implementation-summary-api-compatibility-tool-results-fix.md` | 实施总结 | 已完成 |
| `implementation-summary-observability-enhancement.md` | 实施总结 | 已完成 |
| `implementation-summary-resilience.md` | 实施总结 | 已完成 |
| `implementation-summary-tool-execution-reliability.md` | 实施总结 | 已完成 |
| `implementation-summary-web-adaptation.md` | 实施总结 | 已完成 |
| `task-report-toolguard-refactor.md` | 任务报告 | 已完成 |

---

## 学习 / 研学模式读取顺序

研学模式相关文档按以下顺序读取：

1. `docs/output/learning-mode-final-product-prd-2026-05-27.md`
2. `docs/changes/2026-05-26-learning-mode-north-star-discussion.md`
3. `docs/changes/2026-05-27-chat-study-separation-interface-contract.md`
4. `docs/changes/2026-05-27-learning-chat-study-profile-separation.md`
5. `docs/design/design-learning-unit-adaptive-alignment.md`
6. `docs/output/learning-mode-phase-1a-hardening-2026-05-30.md`
7. `docs/design/design-learning-mode-phase-1b-1e-skeleton.md`
8. `docs/design/design-learning-mode-phase-1b-orientation-context.md`
9. `docs/output/learning-mode-phase-1b-plan-checklist-2026-05-30.md`
10. `docs/design/design-modes-refactor-chat-vs-learning-unit.md`
11. `docs/output/learning-mode-mvp-definition-2026-05-25.md`
12. `docs/design/design-chat-ask-study-modes.md`

当前结论：

- `Study` 不再作为顶层产品主线；深度学习场景由学习卷 / 研学模式承载。
- 学习卷不再默认强制 `aligning -> ASK`；当前口径是默认直学、必要时按需对齐。
- 学习卷 absorbing 不再借用 `CHAT_PROFILE`；当前口径是默认 `STUDY`，对齐门临时 `ASK`。
- 一阶段 MVP 文档仍可用于理解旧闭环，但不能覆盖最终产品 PRD 的三层体验：研学门厅、理解铸造场、理解博物馆。
- 闲聊与研学 profile 已完成基础分离，后续调整 `CHAT_PROFILE` 不应影响研学 absorbing。

---

## Web Search 读取顺序

1. `docs/output/web-search-learning-agent-prd-2026-05-25.md`
2. `docs/design/design-web-search-learning-agent-implementation.md`
3. `docs/output/web-search-optimization-four-phase-checklist-2026-05-26.md`
4. `docs/changes/2026-05-26-web-search-phase-1-quality-improvements.md`
5. `docs/changes/2026-05-26-web-search-phase-2-structured-fetch-and-section-pagination.md`
6. `docs/changes/2026-05-26-web-search-phase-3-stop-strategy-and-budget-guards.md`
7. `docs/changes/2026-05-26-web-search-phase-3-followup-budget-and-guidance-fixes.md`
8. `docs/changes/2026-05-26-web-fetch-rss-fallback-for-official-blog-sites.md`
9. `docs/changes/2026-05-26-web-search-ssl-trust-chain-fix.md`

当前结论：

- Web Search 主线不是扩工具数量，而是提高证据质量、结构化消费和停止策略。
- 阶段一到三已分别覆盖来源分类与主内容抽取、章节级分页、预算与 guidance。
- SSL 信任链修复和 RSS fallback 是当前真实环境可用性的关键补丁。

---

## 其他目录

### `research/`

| 文档 | 主题 |
|------|------|
| `compaction-strategy-deep-dive.md` | Claude Code 上下文压缩策略调研 |
| `claude-code-event-hook-extension-architecture.md` | Claude Code 事件 / Hook / 扩展架构调研 |
| `pi-mono-architecture-analysis.md` | pi-mono 分层架构调研 |
| `pi-mono-event-hook-extension-architecture.md` | pi-mono 事件 / Hook / 扩展架构调研 |
| `pi-tool-execution-layer-analysis.md` | pi 工具执行层分析 |
| `pi-vs-claude-code-comparison.md` | pi-mono 与 Claude Code 对比 |
| `research-pi-mono-session-state-isolation.md` | pi-mono Session 状态隔离机制调研 |

### `audit/`

| 文档 | 主题 |
|------|------|
| `code-audit-report-agent-loop-2026-05-12.md` | Agent Loop 核心代码错误分析 |
| `extension-wiring-audit-checklist-2026-05-13.md` | 扩展接线审计表 |
| `technical-lead-diagnostic-checklist-2026-05-13.md` | 技术负责人项目诊断清单 |

### `bugfix/`

| 文档 | 主题 |
|------|------|
| `bug-report-context-length-silent-failure-and-oversized-tool-result.md` | ContextLengthError 静默失败与超大工具结果 |
| `fix-context-truncation-tool-call-id.md` | 滑动窗口截断导致 tool_call_id 丢失 |
| `fix-hook-minimal-runtime-refactor.md` | Hook 最小运行时收敛与 typed 契约重构 |
| `fix-agent-loop-monolith-split.md` | AgentLoop 单体文件拆分与边界收敛 |

### `prompt/`

软链接入口，按需加载专项约束：

- `architecture` -> `AGENTS.architecture.md`
- `audit` -> `AGENTS.audit.md`
- `constraints` -> `AGENTS.constraints.md`

---

## 使用规范

1. 新增设计文档放入 `docs/design/`，并遵循 `design-<主题>.md` 命名。
2. PRD、产品定义、验收报告、实施总结放入 `docs/output/`。
3. 每次新建文档、重构代码、修复 Bug、新增需求功能，都优先在 `docs/changes/` 补变更记录。
4. 普通文档小修不必额外新增变更记录；但涉及设计结论、约束或实现范围变化时，应留痕。
5. 引用其他文档时，使用相对路径，例如：`docs/design/design-four-layer-convergence-plan.md`。
6. 更新或新增重要文档后，同步更新本 README 的当前主线、目录索引或读取顺序。
