# 2026-05-28 · 思路 picker 也下沉到输入框 + 开卷前锁定

## 背景

昨天把陪伴 picker 从 topbar 挪到了输入框右侧（仅 home + chat 可见，进会话就锁定，
见 [2026-05-27-companion-picker-input-relocate-and-lock.md](2026-05-27-companion-picker-input-relocate-and-lock.md)），
但研学的"思路"picker 还留在 topbar 右上角，两个模式的 picker 位置不一致——
切到研习就出现"picker 跳到天上"的视觉割裂感。

研学的思路 picker 此前是"研习全程可切换"（[app.js:919](web/static/app.js#L919) 的
`applyPersonaSelection` 有 mid-session PUT 分支，欢迎页文案也写着"研习里可在右上角切换"）。
理论上"边学边换思路"是合理的覆层使用方式，但和陪伴的"会话级锁定"放在同一个 UI 框架里
反而显得不一致；用户决定**完全照搬陪伴**——下沉 + 开卷前选定 + 开卷后锁定，
两个 picker 在位置和交互上彻底对称。

## 变更

### 前端

- [web/index.html](web/index.html)：
  - 删 topbar 里的 `topbar-thinking` 块；
  - 把 `thinking-picker` 块放进 `input-wrapper`，紧跟 `companion-picker` 之后；
  - trigger 简化：删 `thinking-trigger-label`"思路"字样，只留 `☰ + 当前思路名` + 下拉箭头；
  - 改欢迎页文案：
    - 大段："在研习里需要换一种思考风格时，可在右上角「思路」里切换..."
       → "开研习卷前可在输入框右侧「思路」里选定...开卷后固定不变"；
    - pill："思路 · 研习里可选" → "思路 · 开卷前选定"。
- [web/static/style.css](web/static/style.css)：
  - 删 `.topbar-thinking { margin-left: 12px; ... }`；
  - `.companion-picker` 和 `.thinking-picker` 合成同一组选择器（position relative + inline-flex + flex 0 0 auto + `.hidden { display: none; }`）；
  - 新增 `.thinking-trigger::before { content: "☰"; }`，和陪伴的 `☾` 视觉成对（一组阴-阳 / 月-卦），尺寸略小（13px vs 14px）配合 CJK 字符的视觉权重；
  - `.thinking-menu` 改为 `bottom: calc(100% + 8px)` 向上展开（与 companion-menu 合并选择器）；
  - 更新 `.is-open` / `.has-overlay` 选择器：`.topbar-thinking.*` → `.thinking-picker.*`；
  - 删 `.thinking-trigger-label` 规则（HTML 里的标签字已删）；
  - 删媒体查询里残留的 `.topbar-thinking { margin-left: 0; }`。
- [web/static/app.js](web/static/app.js)：
  - `els.topbarThinking` → `els.thinkingPicker`（id 同步改为 `thinking-picker`）；
  - `syncThinkingPickerVisibility` 改判定：
    `currentMode === 'learning' && currentView === 'home'`；
  - `hideWelcome()` 增加 `syncThinkingPickerVisibility()` 调用，保证开卷立即锁定；
  - `applyPersonaSelection` 简化为本地更新：picker 只在 home 可见，
    `currentSessionId` 必为 null，原来的 mid-session PUT 分支不可达，删掉；
    思路绑定改为单一路径——`sendMessage` 创建研习卷时由 `requestedPersonaKey` PUT 一次。

### 测试

- [tests/test_web_static_app.js](tests/test_web_static_app.js)：
  - 新增 `'研习页思路 picker 与陪伴对称：下沉到输入框右侧 + 开卷前可选 + 开卷后锁定'`：
    - 断言 `id="thinking-picker"` 出现在 `input-wrapper` 内（结构断言）；
    - 断言不再有 `id="topbar-thinking"` / `thinking-trigger-label`；
    - 断言 home-only 判定字符串 `currentMode === 'learning' && currentView === 'home'`；
    - 断言欢迎页文案改成"开研习卷前可在输入框右侧"，原"右上角...切换"已消失；
    - 断言 `.thinking-trigger::before` 存在、`.topbar-thinking` 选择器已退役。

## 验证

```bash
python3 -m pytest -q tests/ --ignore=tests/e2e
node --test tests/test_web_static_app.js
```

结果：495 pytest + 16 JS（昨日 15 + 新增 1）全部通过。

UI 层（位置、菜单方向、图标渲染、模式切换时的显隐）**未在浏览器实际验证过**——
CLI 环境跑不起 Web；你下次起 dev server 时帮我看一眼这几点：

1. 在 home + 闲谈 → 右下角看到 `☾ 关闭`；切到研习 → 右下角变成 `☰ 默认`；切回来 → 又是 `☾`。
2. 闲谈页发出第一条消息 → 陪伴 picker 整块消失；研习页开卷 → 思路 picker 整块消失。
3. 点击新建对话回首页 → 两个 picker 按当前 mode 重新出现。
4. 菜单点开是**向上展开**的（不被输入框挡），落在输入区上方。
5. ☰ 图标在你系统字体里能正常渲染（如果是空 tofu，我换成别的 Unicode 字符）。

## 边界

- **picker 整块消失，不是 disabled**：和陪伴一致，避免"为什么我点不动"。
- **思路绑定路径收敛到一处**：home 选 → currentPersonaKey 暂存 → 第一条消息发出时 PUT 到
  新建的研习卷。原来的 mid-session PUT 分支已删，不存在"绕过 picker 改 persona"的副路径。
- **图标选 ☰（U+2630, 三爻 / 乾卦）**：和陪伴的 ☾ 配对，
  线条粗细一致，且符合 朱熹 / 笛卡尔 / 苏格拉底 这类"思辨覆层"的语义。
  如果某些字体渲染不出来，换成 `◯` / `☷` / `✦` 等都行，这是 1 行 CSS 的事。
- **现有研习卷不受影响**：开过的卷里 persona 已经持久化在 mode_metadata，
  只是开卷后无法再换；要换得新开一卷。

## 非目标

- 不改后端：`PUT /sessions/{id}/persona` 接口还在（评测 / 数据导入路径仍可能用到），
  只是前端不再在会话中途调它。
- 不动 `_TURN_TRIGGER_KEYWORDS` 或其它陪伴减压逻辑——本次只动思路 picker。
- 移动端的双 picker 横向布局未单独优化（输入框右侧空间紧张）；N=1 桌面优先。
