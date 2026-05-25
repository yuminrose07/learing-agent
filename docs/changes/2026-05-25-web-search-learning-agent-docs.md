# 2026-05-25 Learning-Agent Web Search 文档落地

## 范围

本次变更新增一套面向当前学习场景 Agent 的 Web Search 文档，不直接落代码实现。

目标是回答两个明确问题：

- Learning-Agent 的 `web_search` 产品到底要解决什么问题
- 该产品能力在当前项目分层下应该如何实现

## 变更内容

### 新增 PRD

新增 `docs/output/web-search-learning-agent-prd-2026-05-25.md`，系统化定义：

- `web_search` 的产品定位：外部知识发现能力，而不是通用搜索引擎
- 核心问题：当前系统缺少项目外知识发现、正文消费与引用闭环
- 目标用户：学习场景中的知识型学习者与执行检索的 Agent
- 产品闭环：`web_search -> web_fetch -> Agent 总结引用`
- 成功指标：带引用回答占比、一次回答解决率、官方来源命中率等
- 用户故事、验收标准、范围边界、风险与开放问题

### 新增技术设计文档

新增 `docs/design/design-web-search-learning-agent-implementation.md`，系统化定义：

- 为什么不能把 Web Search 做成简单的“返回链接列表”的工具函数
- 分层落点：Extension / Service / Provider / Config
- `web_search` 与 `web_fetch` 的职责边界
- 数据契约、错误码、来源分类、去重、排序策略
- 正文提取、截断与续读方案
- observability、评估集、实施阶段与回退策略

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 产品闭环 | `web_search + web_fetch + Agent 引用总结` | 单独做 search 或 fetch 都无法形成稳定的学习型外部知识消费链 |
| 输出形式 | 结构化结果优先 | 便于 Agent 消费、后续 UI 展示、评估与观测 |
| 排序原则 | 来源可信度优先于时效与结果数量 | 学习场景中“可信、可验证”比“搜得多”更重要 |
| 技术架构 | Provider 可替换，Service 承接质量策略 | 避免 Runtime 直接绑定外部搜索实现，符合现有分层约束 |
| 正文读取 | 搜索与抓取解耦 | 控制上下文成本，避免黑箱式一体工具难以调试和评估 |

## 设计结论

- 对 Learning-Agent 而言，Web Search 的价值不在“能上网”，而在“能为 Agent 提供可信、可引用、可继续消费的外部知识”。
- 初版应坚持克制边界：少而准、搜索与抓取分离、结构化输出、失败可降级。
- 实现上应遵守当前项目四层分层与稳定性优先原则，避免把第三方搜索 SDK 直接塞进 Runtime。 

## 关联文档

- PRD：`docs/output/web-search-learning-agent-prd-2026-05-25.md`
- 技术设计：`docs/design/design-web-search-learning-agent-implementation.md`
