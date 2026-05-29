# 研学模式最终产品 PRD 变更记录

日期：2026-05-27

## 背景

基于 `docs/changes/2026-05-26-learning-mode-north-star-discussion.md` 的北极星讨论，新增一份足够支持开发拆解的正式 PRD。

本次 PRD 不再从泛化市场用户或 MVP 验证出发，而是按当前共识处理：

- 首批用户就是当前真实长期使用者。
- 产品目标是最终形态。
- 文档需要支持开发理解产品目标、状态、事件、边界和验收标准。

## 新增文档

- `docs/output/learning-mode-final-product-prd-2026-05-27.md`

## 关键决策

1. 研学模式最终形态按三层体验组织：
   - 研学门厅
   - 理解铸造场
   - 理解博物馆

2. 学习人格镜子作为横切能力：
   - 被动观察用户学习行为。
   - 输出带证据的具体观察。
   - 不做粗糙人格标签。

3. 开发承接建议：
   - 现有 `LearningUnit` 可继续作为学习过程容器。
   - Product/Application 层拥有学习卷、理解展品、未完成分支和学习人格观察的权威状态。
   - Agent Runtime 只消费单轮执行配置，不承载产品语义。

4. 持久化原则：
   - 继续遵守 append-only JSONL 事实源约束。
   - 理解展品、未完成分支、学习人格观察等新状态应通过追加事件表达。

## 影响范围

- 文档层新增正式 PRD。
- 未修改运行时代码。
- 未修改现有北极星讨论稿。

## 后续建议

下一步应基于 PRD 继续拆技术设计文档，重点定义：

- `UnderstandingExhibit`
- `CuriosityBranch`
- `LearningProfileObservation`
- 事件 schema
- 前端视图
- 与现有 `LearningUnit` 的迁移或兼容关系
