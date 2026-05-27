# 2026-05-25 Web Search 接入 Chat / Study 模式

## 范围

本次变更将已实现的 `web_search` / `web_fetch` 工具正式接入产品模式层，使其在：

- `Chat`
- `Study`

两种模式下可被 Runtime 暴露给模型使用。

## 变更内容

### 模式配置调整

更新 `learning_agent/learning_agent/mode_service.py`：

- 将 `web_search`
- 将 `web_fetch`

加入 `CHAT_PROFILE.tools_enabled` 与 `STUDY_PROFILE.tools_enabled`。

结果：

- 闲聊模式可以查询项目外知识、官方文档与社区资料
- 研学模式可以在深度讲解时调用外部搜索与正文抓取

### 工具提示词调整

同步更新模式层中立 guardrails 的工具优先级说明：

- 把 `web_search` 纳入信息获取第一优先级
- 把 `web_fetch` 纳入网页正文抓取路径

这样模型在 Chat / Study 模式下不仅“看得见”工具，也更容易“想到去用”工具。

### 测试

更新 `tests/test_mode_layering.py`，新增断言：

- `Chat` 模式 profile 暴露 `web_search` / `web_fetch`
- `Study` 模式 profile 暴露 `web_search` / `web_fetch`

## 设计说明

- 本次只接入 `Chat` 与 `Study`
- `Ask` 与 `Teach` 继续保持无工具模式，不扩大范围
- 改动收口在模式配置层，不触碰 Runtime 主循环与已有 learning unit / alignment 逻辑

## 关联

- Web Search PRD：`docs/output/web-search-learning-agent-prd-2026-05-25.md`
- Web Search 技术设计：`docs/design/design-web-search-learning-agent-implementation.md`
