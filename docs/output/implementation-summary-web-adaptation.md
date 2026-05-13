# 执行后归档：Web 前后端适配方案

> 依据技术文档：`docs/design/design-web-adaptation.md`
> 文档日期：2026-05-12

---

## 一、功能概述

本次实现配合 AgentLoopSession 架构重构，确保 Web 前后端正确支持多 Session 生命周期管理。核心目标：

1. **删除 Session 时同步清理运行时**（P0），防止内存泄漏。
2. **系统启动/关闭时防御性清理所有运行时**（P1）。
3. **新增运行时观测 API 和强制重置 API**（P2），提升可观测性和运维能力。
4. **前端 Observability 面板展示活跃运行时**（P2）。
5. **前端增加运行时重置按钮和缓存清理**（P3）。

---

## 二、修改文件清单

### 后端

| 文件 | 修改说明 |
|------|---------|
| `learning_agent/web_server.py` | 1. `lifespan()`: 启动/关闭时调用 `clear_all_runtimes()`<br>2. `delete_session()`: 删除前调用 `clear_session_runtime()`<br>3. 新增 `GET /observability/runtimes`<br>4. 新增 `POST /sessions/{session_id}/reset-runtime` |
| `tests/test_web_adaptation.py` | 新增 8 个测试，覆盖后端新端点和 lifespan 清理逻辑 |

### 前端

| 文件 | 修改说明 |
|------|---------|
| `web/observability.html` | 1. 侧边栏新增 "Runtimes" 导航项<br>2. 内容区新增 `#panel-runtimes` 面板 |
| `web/static/observability.js` | 1. 新增 `runtimesContainer` DOM 引用<br>2. `switchTab` / `loadTabData` 增加 runtimes 分支<br>3. 新增 `loadRuntimes()` 函数，展示运行时表格 |
| `web/static/app.js` | 1. 新增 `resetSessionRuntime()` 函数<br>2. 会话列表项增加 "重置运行时" 按钮 (`btn-reset`)<br>3. `deleteSession()` 删除后清除 `localStorage`<br>4. `init()` 中若缓存的 `lastSessionId` 不存在于列表，则清除缓存 |

---

## 三、关键决策

### 3.1 后端 API 签名不变

如技术文档所强调，`AgentLoop.run(session, user_input)` 签名未变，因此：
- `POST /sessions/{id}/chat` 无需修改。
- 新增端点完全独立，不影响现有调用链。

### 3.2 运行时清理时机

- **删除时**：`DELETE /sessions/{id}` 中先清理运行时、再删持久化数据。这样即使持久化失败，运行时也不会泄漏。
- **启动/关闭时**：`lifespan()` 中防御性调用 `clear_all_runtimes()`，主要应对未来热重载场景。
- **手动重置**：`POST /sessions/{id}/reset-runtime` 供运维/用户主动重置 failure tracker 和 chat-only 状态。

### 3.3 前端缓存失效策略

风险：用户删除 session 后，另一个标签页的 `localStorage` 仍指向已删除 session。缓解措施：
- 删除成功后调用 `localStorage.removeItem('lastSessionId')`。
- `init()` 加载会话列表后，若找不到缓存的 session，自动清除缓存。

### 3.4 测试策略

- 直接测试端点函数（`await delete_session(...)`）并 patch `_get_system()`，避免 FastAPI `TestClient` 的 lifespan 复杂交互。
- lifespan 测试单独 patch `Config` 和 `LearningAgentSystem`，用 `AsyncMock` 替代真实初始化。

---

## 四、验证状态

### 4.1 测试通过

```bash
pytest tests/ -v
# 36 passed in 10.85s
```

覆盖场景：
- `test_delete_existing_session` — 删除时清理运行时
- `test_delete_nonexistent_session` — 404 不触发清理
- `test_reset_existing_session` — 手动重置运行时
- `test_reset_nonexistent_session` — 404 不触发重置
- `test_empty_runtimes` / `test_with_active_runtimes` / `test_chat_only_runtime_highlighted` — 观测 API
- `test_lifespan_startup_and_shutdown` — 启动/关闭清理

### 4.2 已知限制

- `/observability/runtimes` 中 `banned_tools` 使用 `turn_count=0` 简化展示，可能将窗口外的历史失败也计入。生产环境如需精确展示，可改为使用 runtime 内部当前 turn 计数（需扩展 AgentLoopSession 接口）。
- 前端 `btn-reset` 样式复用现有 `.session-actions button` 通用样式，未引入新 CSS。

---

## 五、API 变更汇总

### 新增端点

| Method | Path | 响应 | 说明 |
|--------|------|------|------|
| GET | `/observability/runtimes` | `{ active_runtime_count, runtimes: [...] }` | 查询活跃运行时 |
| POST | `/sessions/{id}/reset-runtime` | `{ status, session_id }` | 重置指定 session 运行时 |

### 修改端点（内部行为增强，签名不变）

| Method | Path | 变更 |
|--------|------|------|
| DELETE | `/sessions/{id}` | 内部增加 `clear_session_runtime()` |

### 未变更端点（确认兼容）

所有现有端点（`/sessions`, `/sessions/{id}/chat`, `/sessions/{id}/fork`, `/observability/*` 等）均无需修改。
