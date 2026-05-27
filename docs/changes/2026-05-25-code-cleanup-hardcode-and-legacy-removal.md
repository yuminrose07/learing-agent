# 2026-05-25 代码清理：硬编码与已退役空实现收缩

## 范围

- 清理已确认未引用的 legacy / no-op 代码。
- 移除前端静态页中的本地后端地址硬编码。
- 删除已下线欢迎页人设画廊对应的空壳 HTML / JS / CSS。
- 不触碰 Memory 半成品能力、未接线扩展和迁移主路径，避免误删规划内能力。

## 变更内容

### 前端 API 地址

- `web/static/app.js`
  - 将 `API_BASE` 从固定 `http://127.0.0.1:8000` 改为基于当前 `window.location.origin` 推导。
  - 增加对无 `window.location` 场景的防御，回退为空字符串，继续走相对路径。

- `web/static/observability.js`
  - 统一为同样的相对路径推导逻辑，删除 `127.0.0.1:8000` 回退硬编码。

### 已退役前端空壳

- `web/index.html`
  - 删除欢迎页中已隐藏的人设画廊占位区块。

- `web/static/app.js`
  - 删除未使用的 `personaGalleryGrid` DOM 引用。
  - 删除 `refreshAvatarImages()`、`scheduleAvatarRefresh()`、`renderPersonaGallery()` 等已退役空实现。
  - 删除初始化时对 `renderPersonaGallery()` 的调用。

- `web/static/style.css`
  - 删除仅服务于欢迎页人设画廊的样式定义与响应式覆盖。

### 后端 Legacy 清理

- `learning_agent/web/web_server.py`
  - 删除未被任何路由使用的空请求模型 `SaveStateRequest`。

- `learning_agent/learning_agent/main.py`
  - 删除未订阅、未调用的 legacy no-op 方法：
    - `_on_entry_appended()`
    - `_on_entry_patched()`
    - `_on_scalar_changed()`
  - 顺手移除未使用导入：`ConceptItem`、`TangentNote`、`UnitObjective`。

- `learning_agent/learning_agent/session_manager.py`
  - 删除未被调用的 legacy 接口：
    - `fork_at()`
    - `legacy_get_path_to_leaf()`

## 验证

- `GetDiagnostics`
  - `web/static/app.js` 无诊断错误
  - `web/static/observability.js` 无诊断错误
  - `learning_agent/web/web_server.py` 无新增错误
  - `learning_agent/learning_agent/main.py` 清掉未使用导入提示
  - `learning_agent/learning_agent/session_manager.py` 无诊断错误

## 备注

- `skills-lock.json` 中仍存在 `/tmp/skills-stage/...` 来源路径，属于环境残留配置，但当前未确认外部运行时是否依赖该文件，本次先不删除。
- `learning_agent/learning_agent/extensions/context_compressor.py` 与 `learning_agent/memory/*` 中的半成品能力当前更接近“未完成产品线”，而非确定垃圾代码，本次不做侵入式清理。
