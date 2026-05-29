# 2026-05-27 前端：闲聊模式收尾 + 观测页修复

> 本轮三个 commit 都落在 `web/` 下：
> - `09a8bfd` feat(frontend): 闲聊与研习分离收尾 — 思路仅服务研习 + 闲聊青瓷视觉
> - `fd7c2aa` fix(observability): 主页面不能下滑 — 补 grid-template-rows + min-height:0 滚链
> - `ad1fd72` fix(observability): 10 处视觉与可用性问题一并清理
>
> 与研学方的 `2026-05-27-chat-mode-identity-post-separation.md`（CHAT_MODE_PROMPT
> 去残留 + 隔离锁测试）一同构成"闲聊与研习分离"工作的最后一块：那边是后端
> 提示词与契约 §7，这边是前端 picker 隐藏 + chat-only persona gating + 视觉分离。

## 背景

两件事一起做：

1. **闲聊收尾**：研学方已经把 absorbing 路由从借用 `AgentMode.CHAT` 改走
   `AgentMode.STUDY`（参见 `2026-05-27-learning-chat-study-profile-separation.md`），
   后端提示词侧也已清理（参见 `2026-05-27-chat-mode-identity-post-separation.md`）。
   现在路由层只剩闲聊一条路径会取用 `CHAT_PROFILE`。前端这一侧还残留两处历史：
   - "思路"（persona overlay：苏格拉底 / 费曼 / 蒙田 / 朱熹 / 笛卡尔）原本对
     CHAT 和 STUDY 都可叠加；分离之后 persona 是教学/思考风格的延伸，
     应只服务研习。
   - 闲聊与研习共用同一套朱砂书斋视觉，二者完全无视觉区分；用户在闲聊时
     感受到的依然是研学的"庄重感"，与"轻松日常"的定位不匹配。

2. **观测页**：用户首次上手观测页时反馈"界面不能下滑"，并提示"很多问题"。
   排查发现根因是布局滚链断了，另外还有 10 处视觉与可用性问题需要清理。

## 一、闲聊与研习分离收尾（commit 09a8bfd）

### 1. 思路（persona overlay）仅服务研习

三处改动收口在 `web/static/app.js`：

- `resolvePersonaKeyForMode(mode, session)` 在闲聊态恒返回 `'neutral'`，
  不再去读 session.mode_metadata 或 in-memory selection。
- `sendMessage()` 决定 `requestedPersonaKey` 时：闲聊传 `null`，研习才把
  当前已选 persona 绑定到新会话。
- 顶部"思路"picker 通过 `syncThinkingPickerVisibility()` 在闲聊态隐藏
  `#topbar-thinking`；并把这次同步收口进 `updateModeToolbar()`，让所有
  `currentMode` 切换点（selectSession / switchMode / init / 输入框占位
  同步等）都自动覆盖到。

### 2. 闲聊青瓷视觉：`body.chat-view.mode-chat` 作用域

- `updateModeToolbar()` 同时把 `mode-chat` / `mode-learning` 反映到
  `<body>` 上，与既有的 `home-view` / `chat-view` 视图维度正交。
- `web/static/style.css` 末尾新增"青瓷·清谈"作用域块：
  - 在 `body.chat-view.mode-chat` 作用域里把强调色族
    （`--vermillion` / `-deep` / `-soft` / `-line` / `-glow`）整体重定义为
    青瓷绿。让既有规则（发送键 / 输入框聚焦 / 模式按钮 active / typing
    indicator / 链接 / modal 顶边等）自动跟随，零规则重写。
  - 单独把闲聊气泡与输入框做更圆润的圆角；助手气泡加极淡青瓷底；
    头像变圆形。
  - 研习态（`mode-learning`）与欢迎页（`home-view`）不在此作用域，
    保持朱砂书斋的正式感不变。

### 3. 欢迎页文案微调（web/index.html）

- subtitle 加上"放松的日常对话"，并把"思路…"句子限定到"在研习里"。
- welcome-pill 的"思路 · 可选思想风格" → "思路 · 研习里可选"。

## 二、观测页主页面不能下滑（commit fd7c2aa）

### 根因

`web/static/observability.css` 里 `#app` 是 `display: grid; height: 100vh`
但**缺 `grid-template-rows`**。Grid 默认会把唯一一行设成 `auto`，长内容
（侧栏会话列表 / 主区时间线）把行撑到 100vh 之外；外层
`tokens.css` 的 `body { overflow: hidden }` 直接把溢出剪掉 —— 视觉上
就是"页面卡在某一截，怎么滚都没用"。

### 4 处补丁

| 文件 | 位置 | 改动 |
|---|---|---|
| `observability.css` | `#app` | 加 `grid-template-rows: minmax(0, 1fr)` + `overflow: hidden`，把外框严格夹在视口里 |
| `observability.css` | `aside.sidebar` | 加 `min-height: 0`，让 `.session-list` 的 `overflow:auto` 能拿到有限高度 |
| `observability.css` | `main.main` | 加 `min-height: 0`，让 `.timeline` / `.tab-panel` 的内部 `overflow:auto` 生效 |
| `observability.css` | `.tab-panel[data-tab="learning-units"]` | 加 `overflow-y: auto` —— events tab 由 `.timeline` 自带滚动，learning-units tab 是静态指标 + 诊断块、没有内层滚动器，得让面板自身负责纵向滚动 |

## 三、观测页 10 处视觉与可用性问题清理（commit ad1fd72）

修完滚动后，自己又扫了一遍观测页，归纳出 10 处明显错位或影响阅读的问题，一并修。
分三档列。

### A. 明显错位（语义/数据一致性）

**A1. visibility 双轨色 → 统一到 token class**

事件行里的 visibility 内联小标原本走 `VIS_COLORS = {蓝/灰/青/橙}` 行内
style，而顶部的 visibility 过滤器走 `.vis-chip[data-vis=...]` 朱砂/灰/靛/赭
token —— 同一含义两套配色。改法：

- `web/static/observability.js`：删 `VIS_COLORS`，`visChip()` 改为产出
  `<span class="vis-chip is-inline" data-vis="...">`，复用顶部过滤器同一套
  token class。
- `web/static/observability.css`：新增 `.vis-chip.is-inline` 仅做尺寸/字号
  微调，颜色仍由 `.vis-chip[data-vis=...]` 控制。

**A2. 学习卷指标失败时清空旧数据**

`loadLearningUnitMetrics()` 失败分支原本只清掉 `luStatsText` 和
`luDiagnostics`，4 张卡的 metric-value 仍是上一次的数字 —— 切窗口
（7 天 → 30 天）失败会出现"7 天数据 + 30 天窗口标签"的静默错位。
新增 `clearLearningUnitMetrics()` 把所有数值复位为占位 + 加 `.is-empty` 类。

**A3. session ID UUID 截断显示**

新增 `shortId()` 统一截前 12 字符 + ellipsis。两处接入：

- 侧栏 `renderSessionList()` 的 `.session-id` 文本用 `shortId(sid)`，
  外层 `.session-row` 加 `title={完整 sid}` 兜底。
- toolbar 的 `els.currentSession.textContent = shortId(sessionId)`，
  同时设 `title=sessionId`。
- `.session-row .session-id` CSS 加 `white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis` 作为兜底（即便 shortId 失效也不会撑破排版）。

**A4. event_id / parent_id 在 event-meta 里也走 shortId**

之前 parent 跳转链路里用 `slice(0, 12)` 截断，但 event-meta 里的
`event_id=` 和 `parent=` 是完整 36 字符 UUID 铺开。改为复用 shortId，
完整 ID 通过 `<code title="...">` 暴露。

### B. 可用性

**B5. 窄屏不再隐藏侧栏**

旧版 `@media (max-width: 900px) { aside.sidebar { display: none } }`
是个坑：900px 阈值在 13" 笔记本分屏很容易触发，触发后会话列表整体消失
却**没有提供唤回入口**（不像 index.html 有汉堡按钮 + overlay）。改为：

- 断点压低到 720px（避开 13" 分屏正常触发线）。
- 窄屏不再隐藏侧栏，而是把 grid-template-columns 收成 `220px 1fr`，
  并把主区 padding 一并收紧。
- N=1 桌面工具，不为手机视图增加汉堡 / overlay 等新组件。

**B6. type 子串过滤加 250ms debounce**

原本每输入一个字符就同步 `renderTimeline()`，5000 条事件下会卡。
新增通用 `debounce()` 工具，挂在 `els.typeInput` 的 input 事件上。

**B7. 刷新按钮 loading 态 + 防重复**

新增 `withButtonLoading(btn, label, action)` 装饰器：禁用按钮 + 替换文案，
完成（含错误）自动复原。事件 tab 的 `#btn-refresh` 与学习卷 tab 的
`#lu-btn-refresh` 都套上。

### C. 视觉一致性

**C8. `.toolbar` 与 `.filter-row` 双横线择一**

事件 tab 的 toolbar 和 filter-row 都带 `border-bottom: 1px solid var(--hairline)`，
间距才 12-16px padding，视觉上是一道"双线"。去掉 `.toolbar` 的下边线，
保留 `.filter-row` 的，让 toolbar + filter-row 合为"工具区"。

**C9. 4 张指标卡按数据有无区分视觉权重**

原本所有 `.metric-card` 一律 `border-left: 3px solid var(--vermillion)`，
"有样本"和"零样本"的卡片视觉权重相同，扫一眼分不出。新增
`.metric-card.is-empty` 样式：左条 → `--paper-edge` 灰，metric-value /
metric-footnote → `--ink-4`。判定由 JS `markMetricCardEmpty()` 根据
sample_size / denominator 控制。

**C10. 错误提示用 `--danger` token**

`#b91c1c`（冷红，与朱砂调色不和）在 `loadSessions` / `selectSession` /
`loadLearningUnitMetrics` 三处失败分支中以行内 style 出现。改为加
`.is-error` 类，CSS 里集中走 `color: var(--danger)`。

## 验证

- `node --check web/static/observability.js` → syntax OK
- `node --test tests/test_web_static_app.js` → 10 passed（与 app.js 相关
  的既有断言全部仍然通过，包括"前端只暴露闲谈与研习入口"、"研习前端模式
  发送到后端时映射为 chat"等关键路径）
- `grep '#b91c1c|VIS_COLORS|vis-chip-inline' web/static/observability.{js,css}`
  确认旧硬编码色与旧符号全部清除（残留仅在注释里说明历史）。

### 浏览器实测尚未覆盖

观测页的具体视觉效果（青瓷主题没有跑、UUID 截断后实际宽度、窄屏
220px 侧栏的可读性、debounce 触感等）**没有在真实浏览器里跑过**
（共享工作树起后端比较折腾）。下次用户在 Chrome 里跑一遍后，
如有偏差再调。

## 留心事项（共享工作树）

- 本轮所有改动只落在 `web/` 下 5 个文件：`web/index.html`、
  `web/static/app.js`、`web/static/style.css`、`web/static/observability.js`、
  `web/static/observability.css`。
- 三次 commit 都通过 `git add <named files>` 精确暂存，没有把
  `learning_agent/learning_agent/mode_service.py`（研学方未提交的
  `STUDY_MODE_PROMPT`/CHAT prompt 改动）以及 `docs/changes/`、
  `tests/test_chat_study_separation.py` 等别 AI 的 dirty 改动带走。
- 本文件 `2026-05-27-frontend-chat-finalization-and-observability-cleanup.md`
  是新文件，可独立 `git add` 提交、不牵连他人。
