# 技术设计文档：Learning-Agent Web Search 外部知识检索能力

> **目标**：为当前学习场景 Agent 设计一套可实现、可演进、可观测的 `web_search` 外部知识检索能力，使 Agent 能稳定完成“搜索候选来源 -> 抓取正文 -> 归纳引用”的工作流。
>
> **文档日期**：2026-05-25
>
> **实现状态**：方案定稿，待评审，未开始实现
>
> **依赖文档**：`docs/output/web-search-learning-agent-prd-2026-05-25.md`
>
> **设计边界**：本文只定义外部知识检索工具的产品实现路径、系统边界与演进方案，不展开讨论模式开关、前端入口策略与具体第三方供应商采购问题。

---

## 一、问题定义

### 1.1 当前系统缺的不是“网页抓取能力”，而是“外部知识消费闭环”

当前 Learning-Agent 已具备以下本地能力：

- 本地文件读取
- 本地内容搜索（`grep`）
- 大文件截断与“先搜再读”工作流

但面对项目外问题时，系统仍缺少一条稳定的链路：

```text
提出外部知识问题
  ->
找到可信来源
  ->
读取少量来源正文
  ->
输出带来源结论
```

这意味着 Agent 面对“最新文档”“社区经验”“官方说明”“项目外背景知识”时，只能：

- 靠参数知识猜
- 等用户给链接
- 或未来被迫走一个无法控噪的“抓网页全文”路径

三者都不满足当前学习场景需求。

### 1.2 技术问题本质

`web_search` 的核心技术问题不是“能不能搜到网页”，而是以下 5 个约束如何同时成立：

1. **来源质量可控**
2. **结果结构可被 Agent 消费**
3. **正文抓取不把上下文撑爆**
4. **失败不拖垮当前会话**
5. **后续可观测、可评估、可替换 provider**

因此，这个能力不能实现成一个“返回字符串列表”的工具函数，而要实现成一个带明确边界的检索子系统。

### 1.3 本次设计的判断

本次方案采用以下 6 条硬决策：

1. **`web_search` 与 `web_fetch` 分离，不做黑箱式一体工具。**
2. **搜索结果必须结构化，不能只返回大段拼接文本。**
3. **初版结果排序优先来源可信度，其次才是相关度与时效。**
4. **正文抓取必须支持截断与续读，避免单条工具结果过大。**
5. **Provider 接口必须可替换，避免上层逻辑绑定单一供应商。**
6. **观测与评估从第一版就接入，避免功能上线后无法判断质量。**

一句话概括：

> **我们不是在接一个网页搜索 API，而是在为 Agent 增加一条外部知识检索执行链。**

---

## 二、目标与非目标

### 2.1 目标

| 目标 | 说明 |
|------|------|
| **提供外部知识发现能力** | 让 Agent 能主动找到项目外可信来源 |
| **控制上下文成本** | 搜索结果和抓取正文都必须可截断、可分页 |
| **保持分层稳定** | Provider 可替换，Runtime 不直接耦合外部搜索 SDK |
| **保留来源链** | 搜索、抓取、回答三层都可回指来源 |
| **支持后续演进** | 初版支持通用网页检索，后续可拓展 GitHub、论文、文档站专项增强 |

### 2.2 非目标

本次不做以下内容：

- 不做完整浏览器自动化
- 不做全网页 crawling
- 不做向量语义 Web Search
- 不做用户侧搜索 UI
- 不做复杂多轮 query planner
- 不做网页全文永久入库或全文索引

---

## 三、设计总览

### 3.1 总体架构

本次能力按项目既有四层架构收口：

```text
Interface
  - CLI / Web / API 暂不新增产品入口约束
  - 只负责透传工具调用结果

Product / Application
  - 负责配置注入、provider 选择、可见性策略、评估开关

Agent Runtime
  - 通过 ToolRegistry / ToolExecutor 调用工具
  - 不持有网页搜索业务策略

Infrastructure
  - WebSearchProvider
  - WebFetchProvider
  - 结果清洗、正文提取、超时控制、错误包装
```

### 3.2 组件分解

建议新增如下模块：

```text
learning_agent/
└── learning_agent/
    ├── extensions/
    │   └── web_search_tools.py
    ├── services/
    │   └── web_search_service.py
    └── providers/
        ├── web_search_provider.py
        ├── web_fetch_provider.py
        └── adapters/
            ├── builtin_web_search.py
            └── builtin_web_fetch.py
```

说明：

- `extensions/web_search_tools.py`
  - 暴露工具定义
  - 负责参数 schema、调用 service、结果格式包装
- `services/web_search_service.py`
  - 实现排序、去重、来源归类、截断、错误归一化
- `providers/*.py`
  - 定义 provider protocol
  - 对接具体搜索与抓取实现

### 3.3 为什么不是直接把逻辑塞进扩展文件

因为 `web_search` 比 `grep` 更复杂，至少包含：

- provider 调用
- 结果标准化
- 来源分类
- 去重
- 排序
- 正文提取
- 截断
- 错误分类

如果全部塞进一个扩展文件，后续替换 provider、做评估、做专项优化都会很痛。

---

## 四、工具边界与职责

### 4.1 `web_search` 的职责

`web_search` 只做三件事：

1. 接收查询
2. 返回少量结构化候选来源
3. 保留足够元信息供后续抓取与引用

它**不负责**：

- 抓取全文
- 汇总答案
- 自动搜索多跳网页

### 4.2 `web_fetch` 的职责

`web_fetch` 只做三件事：

1. 读取指定 URL
2. 提取适合 Agent 阅读的正文
3. 在过长时支持截断与续读

它**不负责**：

- 再次做网页搜索
- 对正文做最终答案级总结
- 自动合并多来源

### 4.3 与现有 `grep` 的关系

二者职责不同：

| 工具 | 作用域 | 目标 |
|------|--------|------|
| `grep` | 本地仓库 / 本地资料 | 找现有内容 |
| `web_search` | 互联网公开来源 | 找项目外知识 |

因此本次设计不替代 `grep`，也不把两者硬合并。

---

## 五、数据契约设计

### 5.1 `web_search` 输入 schema

推荐最小输入：

```json
{
  "query": "fastapi streaming response best practices",
  "top_k": 5,
  "freshness": "any",
  "source_preferences": ["official_docs", "github_repo", "blog"],
  "language": "any"
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | `str` | 是 | - | 搜索查询 |
| `top_k` | `int` | 否 | `5` | 返回结果上限，建议限制 1-10 |
| `freshness` | `str` | 否 | `any` | `any / month / year` |
| `source_preferences` | `list[str]` | 否 | `[]` | 来源偏好，不是强过滤 |
| `language` | `str` | 否 | `any` | `zh / en / any` |

参数约束：

- `top_k` 上限建议为 10，防止单轮结果膨胀
- `query` 为空时直接返回用户级错误
- `source_preferences` 只允许白名单值

### 5.2 `web_search` 输出 schema

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
  "total_returned": 3,
  "truncated": false,
  "provider": "builtin",
  "search_time_ms": 620
}
```

关键字段：

- `id`
  - 用于后续 fetch / trace / UI 跳转
- `source_type`
  - 供排序和引用层使用
- `score`
  - 供 Agent 或后续重排参考
- `provider`
  - 便于排查与评估

### 5.3 `web_fetch` 输入 schema

```json
{
  "url": "https://fastapi.tiangolo.com/...",
  "offset": 0,
  "limit_chars": 12000
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `url` | `str` | 是 | - | 目标网页 |
| `offset` | `int` | 否 | `0` | 正文偏移量 |
| `limit_chars` | `int` | 否 | `12000` | 返回正文字符上限 |

### 5.4 `web_fetch` 输出 schema

```json
{
  "url": "https://fastapi.tiangolo.com/...",
  "title": "StreamingResponse - FastAPI",
  "domain": "fastapi.tiangolo.com",
  "content": "...cleaned readable content...",
  "content_type": "article",
  "truncated": true,
  "next_offset": 12000,
  "fetched_at": "2026-05-25T12:00:00Z"
}
```

### 5.5 错误返回约定

工具失败时，尽量返回结构化业务错误，不直接抛裸异常：

```json
{
  "error": {
    "code": "FETCH_TIMEOUT",
    "message": "网页抓取超时",
    "retryable": true
  }
}
```

推荐错误码：

- `INVALID_QUERY`
- `SEARCH_TIMEOUT`
- `NO_RESULTS`
- `FETCH_TIMEOUT`
- `FETCH_UNAVAILABLE`
- `UNSUPPORTED_PAGE`
- `PAYWALL_OR_LOGIN_REQUIRED`
- `RATE_LIMITED`

---

## 六、来源分类、去重与排序

### 6.1 来源分类

初版分类函数建议放在 service 层，通过域名与结果特征映射：

| `source_type` | 典型来源 |
|---------------|----------|
| `official_docs` | 官方文档站 |
| `official_blog` | 官方博客 |
| `github_repo` | 官方 GitHub 仓库 |
| `github_issue` | GitHub Issues / Discussions |
| `community_forum` | Stack Overflow / 社区论坛 |
| `blog` | 个人或团队博客 |
| `paper` | arXiv / 论文站 |
| `news` | 新闻站 |
| `aggregator` | 聚合站 / 镜像站 / 搬运站 |

### 6.2 去重规则

初版建议做三层去重：

1. **URL 去重**
2. **规范化 URL 去重**
   - 去 query params 中明显无关追踪参数
3. **同域标题相似去重**
   - 防止同一文档不同镜像页反复出现

### 6.3 排序策略

初版采用加权排序，而不是纯 provider 原始排序：

```text
final_score =
  provider_score * A
  + source_trust_score * B
  + freshness_score * C
  + preference_bonus * D
  - low_quality_penalty * E
```

建议默认优先级：

1. 来源可信度
2. 相关度
3. 时效性
4. 用户偏好源

### 6.4 为什么可信度优先于时效性

因为对学习型 Agent 来说，错误的“新内容”比稍旧的官方内容更危险。

本次明确取舍：

- **优先官方、权威、可验证**
- **次优先更新、更快、更多**

---

## 七、正文提取与上下文控制

### 7.1 正文提取目标

`web_fetch` 返回的不是原始 HTML，而是：

- 标题
- 站点信息
- 主要正文文本
- 基础结构信息

必须尽量剔除：

- 导航栏
- 页脚
- 版权区
- 广告与推荐区
- 站点壳信息

### 7.2 截断策略

初版直接复用项目现有“大文件处理”心智：

- 默认返回有限长度正文
- 告知是否截断
- 提供 `next_offset`

建议参数：

- `WEB_FETCH_MAX_CHARS = 12000`
- `WEB_SEARCH_SNIPPET_MAX_CHARS = 300`
- `WEB_SEARCH_MAX_RESULTS = 5`

### 7.3 为什么不用“一次抓全文”

原因有三点：

1. 上下文成本不可控
2. 长网页大多含大量无关信息
3. Agent 实际上通常只需要前一段或几段核心信息

### 7.4 抓取失败降级策略

| 场景 | 降级 |
|------|------|
| 正文提取失败 | 返回标题、URL、snippet、失败码 |
| 页面过长 | 截断并返回 `next_offset` |
| 登录墙 | 返回错误码与可读提示 |
| JS 重页面 | 返回 `UNSUPPORTED_PAGE` 或降级到原始可读文本摘要 |

---

## 八、系统落点与依赖方向

### 8.1 Extension 层

新增 `web_search_tools.py`：

- 注册 `web_search`
- 注册 `web_fetch`
- 定义参数 schema
- 调用 service
- 做最终结果格式包装

### 8.2 Service 层

新增 `web_search_service.py`，负责：

- 调用 search provider
- 标准化结果
- 来源分类
- 去重
- 重排
- 结果裁剪
- 错误映射

建议主要接口：

```python
class WebSearchService:
    async def search(self, req: WebSearchRequest) -> WebSearchResult: ...
    async def fetch(self, req: WebFetchRequest) -> WebFetchResult: ...
```

### 8.3 Provider 层

定义 protocol：

```python
class WebSearchProvider(Protocol):
    async def search(self, query: str, top_k: int, language: str) -> list[RawSearchResult]: ...

class WebFetchProvider(Protocol):
    async def fetch(self, url: str) -> RawFetchedPage: ...
```

这样上层不关心：

- 是内置搜索能力
- 是第三方 API
- 还是未来替换成别的 provider

### 8.4 Config 层

建议新增配置项：

```yaml
web_search:
  enabled: true
  provider: builtin
  default_top_k: 5
  max_top_k: 10
  default_limit_chars: 12000
  timeout_seconds: 12
  allowed_source_types:
    - official_docs
    - official_blog
    - github_repo
    - github_issue
    - community_forum
    - blog
    - paper
    - news
    - aggregator
```

---

## 九、执行链路

### 9.1 搜索链路

```text
Agent 决定调用 web_search
  ->
ToolExecutor 执行工具
  ->
web_search_tools.py 校验参数
  ->
WebSearchService.search()
  ->
SearchProvider.search()
  ->
标准化结果
  ->
来源分类 / 去重 / 重排 / 裁剪
  ->
返回结构化结果
```

### 9.2 抓取链路

```text
Agent 决定调用 web_fetch
  ->
ToolExecutor 执行工具
  ->
web_search_tools.py 校验参数
  ->
WebSearchService.fetch()
  ->
FetchProvider.fetch()
  ->
正文提取 / 清洗 / 截断
  ->
返回结构化正文结果
```

### 9.3 为什么不让 Runtime 直接调用 Provider

因为那会导致：

- Runtime 直接依赖外部搜索实现
- 工具逻辑与业务策略耦合
- 后续 provider 替换成本高

这违反当前项目的分层方向。

---

## 十、Observability 与评估

### 10.1 需要记录的事件

建议接入以下事件：

- `tool.web_search.started`
- `tool.web_search.completed`
- `tool.web_search.failed`
- `tool.web_fetch.started`
- `tool.web_fetch.completed`
- `tool.web_fetch.failed`

### 10.2 关键字段

搜索事件建议记录：

- `query`
- `top_k`
- `provider`
- `result_count`
- `source_type_breakdown`
- `duration_ms`
- `error_code`

抓取事件建议记录：

- `url`
- `domain`
- `content_length`
- `truncated`
- `duration_ms`
- `error_code`

### 10.3 为什么初版必须带评估

因为这个能力很容易落入两种假象：

- “能搜到结果，所以产品就有价值”
- “能抓到网页，所以回答就一定更好”

如果没有评估，只会做成功能展示，而不是产品能力。

### 10.4 最小评估集

建议准备一组 30 到 50 条典型外部知识问题，覆盖：

- 官方文档发现
- 最新变化
- 社区经验
- 来源冲突
- 抓取失败

评估指标：

- 候选来源质量
- 官方源命中率
- 抓取成功率
- 回答引用率
- 回答可验证性

---

## 十一、实施计划

### Phase 1：工具基础链路

目标：

- 接入 `web_search`
- 接入 `web_fetch`
- 跑通 provider 调用与结果 schema

交付：

- `web_search_tools.py`
- provider protocol
- 初版 adapter

### Phase 2：结果质量层

目标：

- 增加来源分类
- 增加去重
- 增加排序与限制

交付：

- `WebSearchService`
- 排序规则
- 去重规则

### Phase 3：正文清洗与上下文控制

目标：

- 正文提取
- 截断与续读
- 错误码归一化

### Phase 4：观测与评估

目标：

- 记录搜索 / 抓取事件
- 补最小评估集
- 对比上线前后回答质量

---

## 十二、风险与回退方案

### 12.1 若搜索质量过低

回退策略：

- 保留工具但限制可见范围
- 降低默认 `top_k`
- 优先只开放高可信 source type

### 12.2 若抓取质量不稳定

回退策略：

- 先保留 `web_search`
- `web_fetch` 回退为 snippet-only 模式

### 12.3 若上下文成本过高

回退策略：

- 降低抓取字符上限
- 默认只抓取单一来源
- 关闭多来源消费建议

### 12.4 若 provider 不稳定

回退策略：

- service 层切换 provider
- 上层工具 contract 保持不变

---

## 十三、开放问题

- 初版 provider 是否直接复用宿主环境已有 WebSearch / WebFetch 能力，还是独立接三方服务
- `source_type` 规则表是否写死在代码中，还是由配置驱动
- 是否需要单独维护低质量域名黑名单
- `web_fetch` 对 GitHub issue / docs / blog 是否需要不同正文提取策略
- 是否要把“最终被消费的来源”写入后续评估样本

---

## 十四、结论

本次实现的关键不在“接入网页搜索”，而在：

- 明确 `web_search` 与 `web_fetch` 的边界
- 用 service 层承接质量策略
- 用 provider 层承接可替换实现
- 用结构化结果保证 Agent 可消费
- 用截断与观测保证系统稳定

一句话总结：

> **Learning-Agent 的 Web Search 应实现为一条可替换、可观测、可引用的外部知识检索链，而不是一个返回链接文本的临时工具。**
