# 2026-05-25 学习卷自适应对齐方案

## 范围

本次变更只调整学习卷入口阶段的产品设计，不涉及 Runtime 内核重写，也不直接落代码实现。

目标是回答一个明确的产品问题：

- 学习模式下，`Ask` 是否还应该作为默认强制前置阶段？

结论是：

- 保留 `Ask` 能力
- 取消学习卷默认强制 `ASK` 起步
- 改为 `adaptive alignment`：默认直学，必要时再收窄目标

## 变更内容

### 新增设计文档

新增 `docs/design/design-learning-unit-adaptive-alignment.md`，系统化定义：

- 为什么“强制 `aligning -> ASK`”会伤害学习体验
- 为什么 `Ask` 该保留为能力，而不是继续保留为学习卷第一阶段
- 学习卷新的主链：`absorbing -> outputting -> consolidated`
- 独立的 `alignment_state` 模型：`idle | suggested | active | resolved | skipped`
- 三档启动策略：直接开学、非阻塞建议、短暂阻塞澄清
- `LearningUnit` 的字段调整、Product/Application 落点、前端控件、观测指标与实施计划

### 更新现有主设计文档

更新 `docs/design/design-modes-refactor-chat-vs-learning-unit.md` 顶部说明：

- 明确“学习卷是否必须先进入 `aligning -> ASK`”这一点以后续的 adaptive alignment 方案为准
- 保留原文其余关于 Learning Unit、TEACH、概念抽取、持久化与前后端契约的主参考地位

## 设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 学习卷入口策略 | 默认直接进入 `absorbing` | 用户进入学习模式时，应该优先拿到学习内容，而不是先被拦住 |
| `Ask` 的定位 | 保留为按需协议能力 | 学习中仍需要收窄目标和纠偏，但不该固定前置 |
| 对齐状态建模 | 从 `phase` 中拆出独立 `alignment_state` | 避免把旁路能力误建模为主阶段，降低前后端语义混乱 |
| 阻塞澄清触发 | 只在“无法开学”时启用 | 控制摩擦，避免系统重新退化成多轮反问 |
| 文档落地方式 | 新增独立方案文档 + 在主文档加指针 | 既保留历史设计上下文，也避免两份文档对入口策略互相冲突 |

## 设计结论

- 学习型 Agent 的价值交付顺序应是：先提供学习增益，再按需纠偏，最后做输出验收。
- `Ask` 继续存在，但从“学习卷第一阶段”降级为“学习卷内部的自适应对齐动作”。
- Product/Application 层继续拥有对齐判断与模式编排；Runtime 只消费当前轮的协议配置。<mccoremem id="01KRJKA544XWME3RFFKH6QKYKQ|01KRH3PSBX67H36PNR3Q1GBAAX" />
- 状态与持久化设计继续遵守 append-only JSONL 单事实源约束；若后续实现数据迁移，应通过追加事件或投影转换表达，不得原地回写。<mccoremem id="03g5n3yk063e7m76t9gjg5dis" />

## 后续实现建议

1. 先改 `LearningUnit.phase` 与 Product 调度，让学习卷默认直达 `absorbing`
2. 再补 `alignment_state`、目标卡片和 suggestion bar 的前端交互
3. 最后增加 alignment 相关观测事件和评测指标，对比新旧方案的真实体验差异

## 关联文档

- 主方案：`docs/design/design-learning-unit-adaptive-alignment.md`
- 现有学习卷总设计：`docs/design/design-modes-refactor-chat-vs-learning-unit.md`

