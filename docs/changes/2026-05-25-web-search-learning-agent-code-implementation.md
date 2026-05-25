# 2026-05-25 Learning-Agent Web Search 代码实现

## 范围

本次变更将前序 Web Search PRD 与技术设计文档落实为代码实现，新增学习场景 Agent 的外部知识检索工具链。

实现目标：

- 提供 `web_search` 外部来源搜索能力
- 提供 `web_fetch` 正文抓取能力
- 让能力以内置工具形式接入当前 Agent
- 通过配置、服务层与测试，保证可用、可扩展、可回归

## 代码变更

### 新增 provider 协议与内置 adapter

新增：

- `learning_agent/learning_agent/providers/web_search_provider.py`
- `learning_agent/learning_agent/providers/web_fetch_provider.py`
- `learning_agent/learning_agent/providers/adapters/builtin_web_search.py`
- `learning_agent/learning_agent/providers/adapters/builtin_web_fetch.py`

职责：

- 定义 `WebSearchProvider` / `WebFetchProvider` 协议
- 定义搜索与抓取的原始数据结构、错误类型
- 提供基于标准库 `urllib` + `HTMLParser` 的轻量内置实现

### 新增 service 层

新增：

- `learning_agent/learning_agent/services/web_search_service.py`

职责：

- 规范化搜索结果
- URL canonicalization
- 来源分类
- 去重与排序
- 正文截断与续读
- 工具输出结构化

### 新增工具扩展

新增：

- `learning_agent/learning_agent/extensions/web_search_tools.py`

并将其注册到：

- `learning_agent/learning_agent/extensions/built_in.py`

结果：

- Agent 默认可获得 `web_search`
- Agent 默认可获得 `web_fetch`

### 配置接入

更新：

- `learning_agent/learning_agent/config.py`
- `config.example.yaml`
- `config.example.json`

新增配置段：

- `web_search.enabled`
- `web_search.provider`
- `web_search.default_top_k`
- `web_search.max_top_k`
- `web_search.default_limit_chars`
- `web_search.timeout_seconds`
- `web_search.allowed_source_types`

### 测试

新增：

- `tests/test_web_search_tools.py`

覆盖：

- 搜索结果去重与排序
- 正文抓取截断
- 扩展注册与工具调用
- 空 query 的结构化错误返回

## 关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 工具边界 | `web_search` 与 `web_fetch` 分离 | 控制上下文成本，便于调试和评估 |
| 实现形态 | `provider + service + extension` | 符合项目分层，避免把外部检索逻辑塞进 Runtime |
| 默认 provider | `builtin` | 先给出无额外依赖的可运行实现 |
| 抓取方式 | 正文清洗而非原始 HTML | 让 Agent 更容易消费 |
| 结果处理 | service 层做去重、分类、排序 | 保持工具层轻量，质量策略集中管理 |

## 当前边界

- 当前内置 provider 采用轻量 HTML 搜索与正文提取，适合作为 MVP，不代表最终最强检索质量
- `published_at` 等字段当前仍受上游页面结构限制，初版并不保证高覆盖
- 当前实现优先服务 Agent 工具调用链，未额外扩展用户侧独立 Web Search UI

## 关联文档

- PRD：`docs/output/web-search-learning-agent-prd-2026-05-25.md`
- 技术设计：`docs/design/design-web-search-learning-agent-implementation.md`
