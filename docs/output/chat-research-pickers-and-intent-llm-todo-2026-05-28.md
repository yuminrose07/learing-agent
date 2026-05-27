# 闲谈 + 研习 UI / LLM 兜底 — 待办清单（2026-05-28 起）

配套 [chat-research-pickers-and-intent-llm-done-2026-05-28.md](chat-research-pickers-and-intent-llm-done-2026-05-28.md)。
分三档：**必做（浏览器自检）** / **可选优化** / **更远的可能性**。

---

## P0 · 浏览器自检（CLI 跑不动，必须人工眼睛过一遍）

下次起 dev server 时，按顺序检查这五项；任何一项不对都是阻塞回归。

| # | 场景 | 期望 | 文件锚点 |
|---|------|------|----------|
| 1 | home + 闲谈 | 输入框右下角看到 `☾ 关闭`（或当前陪伴风格名） | [web/index.html](web/index.html) · [.companion-trigger](web/static/style.css) |
| 1' | home 切到研习 | 同位置变成 `☰ 默认`（或当前思路名） | [web/index.html](web/index.html) · [.thinking-trigger](web/static/style.css) |
| 1'' | 再切回闲谈 | 又是 `☾`，picker 整块在原位 | [syncCompanionPickerVisibility](web/static/app.js) |
| 2 | 闲谈发出首条消息 | 陪伴 picker 整块消失（`display:none`，不是 disabled） | [hideWelcome](web/static/app.js) |
| 2' | 研习开卷 | 思路 picker 整块消失 | [syncThinkingPickerVisibility](web/static/app.js) |
| 3 | 点新建对话回首页 | 两个 picker 按当前 mode 重新出现 | [showWelcome](web/static/app.js) |
| 4 | 点开任一菜单 | **向上展开**，不被输入框 / 发送按钮挡 | [.companion-menu, .thinking-menu](web/static/style.css) |
| 5 | `☰` 图标（U+2630, 三爻 / 乾卦） | 在系统字体里正常渲染，不是空 tofu | [.thinking-trigger::before](web/static/style.css) |

第 5 项如果渲染失败，1 行 CSS 改字符即可：`◯` / `☷` / `✦` / `❋` 任选其一，
不用动 JS 也不用动 HTML。已在 [thinking-picker change doc](../changes/2026-05-28-thinking-picker-input-relocate-and-lock.md) 边界一节标注。

---

## P1 · 可选优化（看实际使用反馈再决定要不要做）

### 移动端双 picker 横向布局
- 现在没单独适配，桌面优先（用户是 N=1 桌面用户）。
- 输入框右侧同时塞两个 picker，窄屏可能挤；要做的话考虑：
  - chat 模式只显示 `☾`，learning 模式只显示 `☰`（反正一次只可能用一个），
    用 mode 切换决定哪个 visible，CSS 一行 toggle。
  - 或者 trigger 缩成纯图标（去掉文字），点开后菜单全宽 sheet。
- 触发条件：用户哪天在 iPad / 手机上用觉得不舒服。

### `☰` vs `☾` 视觉权重
- 现已 `font-size: 13px` vs `14px` 做了配平（CJK 字符 + 月牙的视觉差），
  实际看起来如果还是一个粗一个细，再调 1–2px 或换字符。

### Esc 键覆盖
- 当前点其他地方关菜单是靠 outside-click，没显式 Esc handler。
- 顺手加一行 `document.addEventListener('keydown', ...)` 让 Escape 关任意 picker，
  键盘 power user 体验更顺。半小时活。

---

## P2 · 此前讨论过但**没选**的方向（按需再起）

这些是闲聊 V1 / picker 下沉过程中我列过的备选，最后没做；记一下避免下次又花脑力重推一遍。

### 意图分类侧
- **少样例 prompt 注入高频意图**：现在 LLM classifier 是 zero-shot
  （[companion_intent_classifier.py](../../learning_agent/learning_agent/companion_intent_classifier.py)），
  如果实际跑下来 venting / tired 之间反复误判，再加 2–3 行 few-shot。
- **多轮 stress_relief 记忆**：现在每轮独立分类，
  没有"上一轮已经判定 tired，本轮即便没词也保持 tired"的 sticky；
  实测如果出现"用户说了一次累，下一句简短回应又被打回普通客服"再加 N=2 滑窗。
- **intent classifier 评估数据集**：现在测试是 mock 验证代码路径，
  真实模型效果靠用户自己用一阵子反馈。要做的话，
  收 30–50 条真实闲聊（[docs/output/](../output/)）人工标注 → 跑 qwen-plus / qwen-turbo / qwen-max
  → 看 confusion matrix。**前提**：用户先攒够样本量。

### 观测 / 评测侧
- **observability 页接 companion_intent_source**：
  事件 payload 里 `source` 字段已就位（[main.py](../../learning_agent/learning_agent/main.py) 的
  `COMPANION_SIGNAL_DETECTED` 分发处），观测页前端没消费。
  做的话在 events 列表里给 `source: "llm"` 加个小标记，
  方便统计"关键字漏多少 / LLM 救多少"。

### 冷启动侧
- **首次进闲谈的发现性提示**：用户第一次切到 chat 模式，
  可能不知道右下角能换风格。可以在欢迎页加一条"试试右下角的 `☾` 切换陪伴风格"
  toast 或 inline hint，登录态 localStorage 标记看过就不再弹。
- 优先级低 — N=1，用户自己写的就知道在哪。

---

## P3 · 已知技术债（不阻塞，但记一笔）

### `_TURN_TRIGGER_KEYWORDS` 还是硬编码
- 在 [companion_policy.py](../../learning_agent/learning_agent/companion_policy.py) 顶部，
  没走 config / 环境变量。改的频率低，硬编码 OK；
  哪天要 A/B 试不同词表才需要外移。

### 双 picker 合并选择器的耦合
- [style.css](../../web/static/style.css) 现在 `.companion-picker, .thinking-picker`
  共用一组样式（position / display / hidden / menu 方向）。
  哪天两个 picker 设计分叉（比如思路 picker 想做更大的卡片菜单），
  得把合并选择器拆回去。**当前一致 = 合并合理**，N=1 不预先拆。

### `applyPersonaSelection` 中 mid-session PUT 已删
- 原本 [app.js:919](../../web/static/app.js#L919) 有研习中途换 persona 的 PUT 分支，
  现在 picker 只在 home 可见，`currentSessionId` 必为 null，这条分支不可达，删了。
- 后端 `PUT /sessions/{id}/persona` 接口**保留**（评测 / 数据导入路径可能用到）。
- 边界已在 [change doc](../changes/2026-05-28-thinking-picker-input-relocate-and-lock.md#非目标) 写明。

---

## 决策记录（避免下次又问一遍）

- **思路 picker 牺牲了"研习中途换 persona"的灵活性**——这是用户主动选的
  （AskUserQuestion 选项 "完全照搬：下沉 + 开卷前锁定"，
  优先级 = 两个 picker 视觉对称 > 中途切换的可能性）。
  要换得新开一卷，picker 不接受"我学了 10 分钟想从苏格拉底切费曼"的覆层。
- **LLM 不让决定 RETURN_TO_STUDY**——语义判断对它失误代价过高
  （会突然放开工具、切研习心智），关键字精准捕获足够稳。
- **picker 选风格 = 持久（PUT 写回 session metadata）**，
  **关键字 / LLM 触发减压轮 = 一次性（不写回 metadata）**——
  两层语义彻底分开，避免"聊到一半切风格"的状态打架。
