# Web Search For Learning-Agent PRD

> 项目：Learning-Agent
>
> 文档类型：正式 PRD
>
> 日期：2026-05-25
>
> 状态：方案定稿，待评审，未开始实现
>
> 目标：为当前学习场景 Agent 定义一套正式的外部知识检索产品能力，帮助 Agent 在回答项目外、依赖最新信息或权威来源的问题时，能搜索、筛选、抓取并引用可信网页来源。

---

## 1. Executive Summary

当前 Learning-Agent 已具备本地文件读取与代码检索能力，但当用户询问官方文档、最新 API 变化、社区实践、项目外知识或多来源观点时，Agent 仍主要依赖参数知识或用户手工提供链接，导致回答容易过时、缺少来源、难以验证。

本项目计划新增 `web_search` 外部知识检索产品能力，并与 `web_fetch` 正文抓取能力形成闭环：先搜索候选来源，再抓取少量高质量正文，最后由 Agent 基于来源生成带引用的回答。该能力优先服务当前学习场景的 Agent，不追求通用浏览器体验，也不追求全网最强搜索，而是追求“少而准、可信、可引用、可继续消费”。[assumption: 当前主要目标是让 Agent 自动消费外部知识，而不是为终端用户构建手动网页搜索界面。]

---

## 2. Problem Statement

### 2.1 谁在面对这个问题

- 正在使用 Learning-Agent 学习概念、框架、项目外资料的用户
- 代表用户执行检索与总结的 Agent

### 2.2 问题是什么

当前系统缺少稳定的外部知识检索产品能力。对于以下类型的问题，Agent 无法自洽完成：

- 找官方文档
- 找最新版本变化
- 找社区常见做法
- 找项目外背景知识
- 对比多个公开来源的观点

系统可以在本地仓库里用 `grep` 找内容，但无法稳定地在互联网中发现、筛选和消费外部来源。

### 2.3 为什么痛

- 只依赖模型参数知识，回答容易过时
- 只依赖用户提供 URL，交互成本高
- 没有来源链，用户无法验证答案
- 直接抓网页全文会带来大量噪音和上下文膨胀
- 外部知识型问题一旦答错，用户对整个学习场景的信任会下降

### 2.4 证据与背景

- 当前项目已具备本地 `grep` 与大文件“先搜再读”机制，说明本地导航不是空白，但项目外知识发现仍是空白
- 当前项目核心定位是学习型 Agent，强调学习与理解，而不是纯粹代码生成或修复
- 学习场景用户高频会问“最新官方说法是什么”“这件事社区一般怎么做”“有没有更权威来源支持这个结论”
- [assumption: 在当前对话任务分布中，至少有一部分高价值问题依赖外部知识而不是仓库内资料]

---

## 3. Target Users & Personas

### 3.1 Primary Persona

**知识型学习者**

- 在使用 Learning-Agent 学习概念、框架、API、外部资料
- 不一定知道官方资料在哪，也不想自己跳出对话去手动检索
- 希望 Agent 不只是“回答”，而是“基于可信来源解释”

**核心 JTBD**

- 当我问一个依赖外部世界的问题时，我希望 Agent 能自己找到可信来源并解释给我，而不是只靠记忆回答。

### 3.2 Secondary Persona

**执行外部知识消费的 Agent**

- 需要在有限上下文里决定先搜什么、读什么、丢弃什么
- 需要来源结构化、正文可分页、错误可恢复
- 需要为后续回答保留引用链

**核心 JTBD**

- 当我需要使用互联网信息完成回答时，我希望先得到少量高质量候选来源，再读取最相关的正文，而不是面对一堆杂乱网页。

### 3.3 非目标用户

- 想把产品当通用浏览器或人工搜索引擎使用的用户
- 只关心搜到多少链接，不关心质量与来源的用户

---

## 4. Strategic Context

### 4.1 产品目标

- 补齐 Learning-Agent 在项目外知识发现上的明显空白
- 提升学习型问答的时效性、可信度、可验证性
- 让 Agent 形成“搜索 -> 抓取 -> 归纳 -> 引用”的稳定外部知识工作流

### 4.2 为什么现在做

- 本地代码搜索已有 `grep`，外部搜索仍是空白，优先级更高
- 学习场景价值不只来自“解释能力”，还来自“能解释最新、真实、可引用的材料”
- 如果继续只靠参数知识，学习型 Agent 对最新信息的处理能力会成为明显短板

### 4.3 竞争与替代

当前可替代方案主要有三种：

- 用户自己去搜索引擎查，再贴链接给 Agent
- Agent 只依赖参数知识回答
- 外接通用网页搜索但只返回链接列表

这些方案都无法稳定满足当前目标：

- 手工搜索成本高
- 参数知识过时风险高
- 仅返回链接缺少后续正文消费与引用闭环

### 4.4 战略取舍

本项目不追求做一个“像搜索引擎一样强”的通用网页检索产品，而是追求：

- 对 Agent 更可消费
- 对学习型任务更可信
- 对上下文成本更友好

这意味着我们愿意牺牲一部分召回广度，换取更高的结果质量与更低的噪音。

---

## 5. Solution Overview

### 5.1 一句话方案

新增 `web_search` 作为外部知识发现入口，返回少量结构化候选来源；新增 `web_fetch` 读取指定结果正文；由 Agent 基于来源做总结与引用。

### 5.2 产品闭环

```text
用户提问
  ->
Agent 判断需要外部知识
  ->
web_search 返回候选来源
  ->
Agent 选择 1-3 个高质量来源
  ->
web_fetch 抓取正文
  ->
Agent 总结并附来源
```

### 5.3 核心产品设计原则

1. **搜索与抓取分离**
   - 搜索负责发现来源
   - 抓取负责读取正文
   - 不做“搜 + 抓 + 总结”一体黑箱

2. **少而准**
   - 默认返回少量高质量候选，避免 10 到 20 条低价值链接淹没上下文

3. **结构化优先**
   - 结果必须带标题、URL、域名、摘要、来源类型、排序分数

4. **引用链优先**
   - 所有后续回答都应可回指到来源

5. **可恢复**
   - 结果过长可截断
   - 正文过长可分页
   - 搜索失败可降级

### 5.4 核心功能

#### 功能 A：结构化 Web Search

输入一条查询，返回少量候选来源，每条候选至少包含：

- `title`
- `url`
- `snippet`
- `domain`
- `source_type`
- `published_at`（若可得）
- `score`

#### 功能 B：来源可信度分层

系统对来源做显式分类，至少支持：

- `official_docs`
- `official_blog`
- `github_repo`
- `github_issue`
- `community_forum`
- `blog`
- `paper`
- `news`
- `aggregator`

#### 功能 C：结果去重与结果数控制

- 同域重复或内容高度相似结果应做去重或降权
- 默认返回 Top-K，避免结果膨胀

#### 功能 D：正文抓取

对单个来源进行正文提取，返回适合 Agent 消费的清洗后文本，而不是原始 HTML。

#### 功能 E：正文截断与续读

当正文过长时：

- 返回截断内容
- 标记 `truncated`
- 给出 `next_offset`

#### 功能 F：失败降级

即使搜索或正文抓取部分失败，也应保留：

- 已成功的搜索结果
- URL
- snippet
- 失败原因摘要

### 5.5 代表性用户场景

#### 场景 1：找官方文档

用户说：“帮我看一下 FastAPI 最新文档里对 StreamingResponse 的推荐写法。”

理想行为：

- Agent 调用 `web_search`
- 排序优先 `official_docs`
- 再抓取官方文档正文
- 输出总结并说明来源

#### 场景 2：找社区经验

用户说：“这个报错社区一般怎么处理？”

理想行为：

- Agent 调用 `web_search`
- 结果优先 GitHub issue、高质量社区、官方讨论
- 选少量来源抓取
- 输出归纳与差异点

#### 场景 3：找最新变化

用户说：“这个库 2026 年最近有什么重要变化？”

理想行为：

- 搜索时优先新内容
- 抓取官方 changelog / release note / 官方博客
- 明确时间与来源

---

## 6. Success Metrics

### 6.1 Primary Metric

**带有效引用的外部知识型回答占比**

- [assumption: Current 10% -> Target 60%，measure 30 days post-launch]

### 6.2 Secondary Metrics

**外部知识型问题的一次回答解决率**

- [assumption: Current 40% -> Target 60%，measure 30 days post-launch]

**搜索后继续抓取正文的比例**

- [assumption: Current 0% -> Target 50%，measure 30 days post-launch]

**官方来源命中率**

- [assumption: Current N/A -> Target 50%+ of consumed results，measure 30 days post-launch]

**用户追问“来源在哪 / 这是最新的吗 / 你确定吗”的比例**

- [assumption: Current baseline unknown -> Target relative decrease 30%，measure 30 days post-launch]

### 6.3 Guardrail Metrics

- `web_search` 平均返回结果条数保持在产品上限内
- `web_fetch` 平均正文体积可控，不显著放大单轮工具输出
- 搜索与抓取失败不应导致会话中断
- 外部搜索相关错误率维持在可接受范围

### 6.4 反指标

以下不是成功指标，不应被误用：

- 搜索调用次数
- 抓取次数
- 返回链接总数

这些数据可用于观测，但不能代表产品价值。

---

## 7. User Stories & Requirements

### 7.1 Epic Hypothesis

If we provide a structured `web_search + web_fetch` capability for Learning-Agent, for users asking external-knowledge questions, then we will increase cited-answer rate and reduce outdated or unverifiable responses within 30 days of launch.

### 7.2 User Story 1

As a 正在学习新主题的用户, I want the agent to find authoritative external sources, so that I can trust and verify the answer.

**Scenario: 官方来源发现**

Given: 用户提出依赖外部知识的问题  
When: Agent 调用 `web_search`  
Then: 系统返回少量结构化候选来源

**Acceptance Criteria**

- 默认返回不超过 5 条候选
- 每条结果必须包含 `title`、`url`、`snippet`、`domain`
- 每条结果必须有 `source_type`
- 无结果时返回空结果结构，而不是未处理异常

### 7.3 User Story 2

As a 关注最新变化的用户, I want newer and more authoritative sources to rank higher, so that I do not receive stale or low-quality advice.

**Scenario: 排序与可信度**

Given: 搜索结果同时命中官方文档、博客与聚合站  
When: 系统对结果排序  
Then: 高可信来源优先展示

**Acceptance Criteria**

- `official_docs` 默认高于普通博客
- 可识别的聚合站默认低权
- 同域相似结果应做去重或降权
- 若可获取发布时间，应保留 `published_at`

### 7.4 User Story 3

As an Agent, I want search and fetch to be separate steps, so that I can keep context compact and controllable.

**Scenario: 搜索与抓取解耦**

Given: `web_search` 已返回多个候选  
When: Agent 仅选择少量结果继续消费  
Then: 通过 `web_fetch` 单独读取正文

**Acceptance Criteria**

- `web_search` 不直接返回整页正文
- `web_fetch` 返回正文提取结果，不返回整页 HTML
- `web_fetch` 支持截断和 `next_offset`
- 单个抓取失败不应污染其他结果

### 7.5 User Story 4

As a 需要比较来源的用户, I want the agent to preserve source identity, so that it can cite and compare sources clearly.

**Scenario: 多来源对比**

Given: Agent 对多个来源执行抓取  
When: Agent 生成最终回答  
Then: 不同来源的身份与结论可以被清晰区分

**Acceptance Criteria**

- 每个结果都必须保留原始 URL
- 每个结果都必须保留标题与域名
- 工具层不得把多个来源混成匿名文本块
- 来源冲突时，Agent 有能力指出“来源观点不一致”

### 7.6 Constraints & Edge Cases

- 搜索结果为空
- 搜索结果很多但高度重复
- 页面为登录墙、跳转页、广告页、JS 渲染页
- 页面抓取失败
- 页面正文极长
- 多个来源结论冲突
- 某些来源缺少可提取发布时间

---

## 8. Out Of Scope

本期明确不做以下能力：

- 通用搜索引擎或浏览器产品
- 自动抓取所有结果全文
- 深度网页 crawling
- 向量语义 Web Search
- 个性化搜索推荐系统
- 搜索结果人工编辑后台
- 多轮复杂检索编排器
- 复杂网页交互式自动化

原因：

- 这些能力会显著扩大范围，但不直接服务当前目标
- 当前目标是建立“可信、可消费、可引用”的最小外部知识闭环，而不是构建完整互联网浏览层

---

## 9. Dependencies & Risks

### 9.1 Technical Dependencies

- 搜索服务或搜索结果提供方
- 网页正文提取能力
- 结果结构化 schema
- 截断与分页机制
- 基础 observability 和评估埋点

### 9.2 External Dependencies

- 第三方搜索结果稳定性
- 网页可访问性
- 反爬、重定向、登录墙、地区限制
- 站点内容格式差异

### 9.3 关键风险

#### 风险 1：结果噪音高

- SEO 农场、镜像站、聚合站、低质量博客可能污染结果集

**Mitigation**

- 引入来源分类
- 引入可信度排序
- 对已知低质量来源降权或过滤

#### 风险 2：上下文膨胀

- 搜索结果过多或抓取正文过长会冲击上下文预算

**Mitigation**

- Top-K 控制
- 搜索与抓取分离
- 正文截断与续读

#### 风险 3：来源可信度被误解

- 用户可能误以为“搜到就是真的”

**Mitigation**

- 输出中保留来源
- 默认带引用
- 多来源冲突时显式标注

#### 风险 4：抓取成功率不稳定

- 某些页面可能无法稳定抽取正文

**Mitigation**

- 抓取失败时保留搜索 snippet 与 URL
- 不让抓取失败拖垮主任务

#### 风险 5：只做工具，不做评估

- 容易陷入“功能上线了，但不知道质量好不好”的局面

**Mitigation**

- 上线前定义最小评估指标
- 持续记录搜索结果被消费、引用、失败的情况

---

## 10. Open Questions

- 初版是否支持 `freshness` 参数，默认值是否为 `any`
- 初版是否支持 `source_preferences`
- `source_type` 由提供方直接给出，还是由产品侧二次归类
- 哪些域名进入低权名单
- `web_fetch` 对 JS 重页面是否要做额外降级策略
- 回答中的引用格式是否统一为“标题 + 域名 + URL”
- 是否记录“最终被消费的来源”作为后续评估输入
- 是否为“来源冲突”增加显式标签

---

## 附录 A：推荐输入输出草案

### A.1 `web_search` 输入草案

```json
{
  "query": "fastapi streaming response best practices",
  "top_k": 5,
  "freshness": "any",
  "source_preferences": ["official_docs", "github_repo", "blog"],
  "language": "zh|en|any"
}
```

### A.2 `web_search` 输出草案

```json
{
  "query": "fastapi streaming response best practices",
  "results": [
    {
      "id": "r1",
      "title": "StreamingResponse - FastAPI",
      "url": "https://fastapi.tiangolo.com/...",
      "snippet": "FastAPI official docs for streaming responses...",
      "domain": "fastapi.tiangolo.com",
      "source_type": "official_docs",
      "published_at": null,
      "score": 0.94
    }
  ],
  "total_returned": 5,
  "search_time_ms": 620
}
```

### A.3 `web_fetch` 输出草案

```json
{
  "url": "https://fastapi.tiangolo.com/...",
  "title": "StreamingResponse - FastAPI",
  "domain": "fastapi.tiangolo.com",
  "content": "...cleaned readable content...",
  "content_type": "article",
  "truncated": true,
  "next_offset": 4000
}
```

---

## 附录 B：产品结论

- `web_search` 对 Learning-Agent 不是“可有可无”的附加工具，而是学习场景对外部知识消费能力的基础设施
- 该能力的价值不在“能搜网页”，而在“能为 Agent 提供可信、可引用、可继续消费的外部知识”
- MVP 必须坚持克制边界：少而准、搜索与抓取分离、结构化输出、默认可引用
