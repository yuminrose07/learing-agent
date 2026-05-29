# 2026-05-27 前端：研学模式入口体验（首页感知 + 显式选择 + 弹窗收口）

> 本轮一个 commit 落在 `web/` 与 `tests/` 下，共 5 个文件。
>
> 与 [2026-05-27-frontend-chat-finalization-and-observability-cleanup.md] 同日，
> 那份是"闲聊收尾 + 观测页"，这份是"研学入口"——两份合在一起，前端层把
> 闲聊/研习 的分离工作彻底落到用户感知层。

## 背景

用户首次完整跑了研习模式后给了三连反馈：

> "现在前端只标注了研学和闲聊模式，但是研学也分进行中和已完成，但是初始页面
> 感知不到，然后从首页点击研学后在进入就会到上一个未完成的研学中去，而且
> 此时的弹窗也非常不明显且有重叠。"

三个独立但连贯的问题：

1. **首页感知不到「进行中的研学」**：欢迎页只有"研习 / 闲谈"两个静态卡，没有
   "上一卷还没学完"的提示。
2. **点击「研学」就被静默拖进上一卷**：后端 `POST /learning-units` 如果检测到
   active unit 会返回 409，前端历史实现是直接 fetch 那一卷 + `hydrateFromUnit`
   加载 + 弹 2 秒右下角 toast——整个过程"不预设，自动跳"，用户感受是"我刚选
   研习就跳进了上一卷，但没人问我"。
3. **弹窗非常不明显且有重叠**：指的就是 #2 那个右下 toast——和同时渲染出来的
   `#learning-unit-card` 顶部目标卡分处两个角落、2 秒后 toast 自动消失，
   信息既轻飘又凌乱。

前置 AskUserQuestion 三选三对齐：
- 欢迎页 **仅展示进行中**（`phase ∈ {absorbing, outputting}`），不展示已完成/已停止。
- 409 冲突路径 **不预设，要求用户明确选**：继续 vs 先停掉、开新主题。
- 待修的"弹窗"就是右下角 reused toast。

需求边界："你主要开发体验方面的，在前端改动"——只动 `web/`。

## 一、首页"未完成研习卷"横幅

### HTML

[web/index.html](web/index.html) `.welcome-content` 内，`.welcome-pill-row`
（"闲谈 / 研习 / 思路" 三个 pill）与 `.home-mode-grid`（两个模式大卡）之间插入
占位：

```html
<div id="welcome-active-units" class="welcome-active-units hidden" aria-live="polite"></div>
```

由 JS 按需填充；无数据时保持 `hidden` 不占空间，欢迎页主体布局不变。

### JS（[web/static/app.js](web/static/app.js)）

三个新函数，挂在 `showWelcome()` 附近：

- `fetchActiveLearningUnits()` —— `GET /learning-units`（已有 endpoint，无需后端
  改动），过滤 `phase ∈ {absorbing, outputting}`，按 `updated_at` 倒序返回。
  后端单 active 约束下实际最多 1 条，但前端按列表渲染以保证容错。
- `renderWelcomeActiveUnits(units)` —— 每卷渲染一张 `.welcome-active-unit-card`：
  - kicker "还有一卷未完成"
  - phase pill（复用 `lu-phase.is-current` token class：研习中 / 复述检验）
  - `objective.text` 或 `working_objective` 作为标题
  - 两个按钮（复用 `.lu-btn.lu-btn-secondary` / `.lu-btn.lu-btn-primary`）：
    - 次：「先停掉这卷」`data-action="stop"`
    - 主：「继续这一卷」`data-action="continue"`
  - 列表为空时整块加 `hidden`。
- `refreshWelcomeActiveUnits()` —— 失败时静默隐藏，不打扰首页。

接入：`showWelcome()` 末尾追一行 `refreshWelcomeActiveUnits().catch(() => {})`，
异步、不阻塞首屏。

事件代理挂在 `#welcome-active-units` 上：
- `continue` → `switchMode('learning')` + `selectSession(sessionId, '研习')`。
- `stop` → `POST /learning-units/{id}/stop {reason:"user_stopped"}`，成功后
  `refreshWelcomeActiveUnits()` 重渲染（一般会变空、横幅隐藏）。

### CSS（[web/static/learning-unit.css](web/static/learning-unit.css) 末尾）

`.welcome-active-units` / `.welcome-active-unit-card` / `.welcome-active-unit-head`
/ `.welcome-active-unit-kicker` / `.welcome-active-unit-title` /
`.welcome-active-unit-actions` —— 卡片视觉与 `#learning-unit-card`
同语言（`border-left: 3px solid var(--vermillion)` + `paper-1` 底 + soft shadow），
让用户在欢迎页就能看出"这是研习卷的资产"。

640px 以下卡片按钮转列方向并撑满，与 stop-modal footer 的窄屏行为一致。

## 二、409 冲突 → 显式复用确认弹窗

### HTML

[web/index.html](web/index.html) `#learning-stop-modal` 之后追加平级的
`#learning-resume-modal`。结构完全复用 `learning-stop-modal-content / header /
body / footer` 全套 class——天然继承 vermillion 主题、圆角、阴影、字体，
**零样式重复**：

```html
<div id="learning-resume-modal" class="modal hidden" aria-hidden="true">
  <div class="modal-overlay"></div>
  <div class="modal-content modal-small learning-stop-modal-content">
    <div class="modal-header learning-stop-modal-header">
      <div class="learning-stop-modal-copy">
        <span class="learning-stop-modal-kicker">研习未完成</span>
        <h2>还有一卷研习没结束</h2>
      </div>
      <button class="btn-close">&times;</button>
    </div>
    <div class="modal-body learning-stop-modal-body">
      <p>先决定怎么处理上一卷研习，再开新主题。</p>
      <div class="learning-stop-modal-summary">
        <span class="learning-stop-modal-summary-label">上一卷主题</span>
        <p id="learning-resume-modal-objective">-</p>
      </div>
    </div>
    <div class="modal-footer learning-stop-modal-footer">
      <button id="btn-stop-and-new" class="btn-secondary">先停掉，开新主题</button>
      <button id="btn-continue-active" class="btn-primary">继续这一卷</button>
    </div>
  </div>
</div>
```

CSS 末尾仅追加 `#learning-resume-modal { z-index: 130 }`（高于 stop-modal 的 120）。

### JS 改动

**1. [learning-unit-ui.js:91-109](web/static/learning-unit-ui.js#L91-L109)
`resumeActiveLearningUnitFromConflict()` —— 拆掉"自动 hydrate + 自动 toast"**

旧：探测 409 → fetch unit → `state.sessionId = ...; hydrateFromUnit(unit, sid)` →
`__appShowToast('已回到未完成的研习卷')` → 返回。

新：探测 409 → fetch unit → **仅返回带 `reused_active_unit: true` 标记的 unit
对象**。hydration 推迟到 app.js 在用户确认后通过 `selectSession()` 触发
`learning-unit:session-loaded` 事件，由 learning-unit-ui.js:466-487 走标准
hydrate 路径——和冷启动一致。

**2. [app.js:1031-1036](web/static/app.js#L1031-L1036) `sendMessage` reused 分支
—— 用 modal 替换 toast**

旧顺序：`__createLearningUnit` → `selectSession`（已加载！）→ `if (reused) showToast`。

新顺序：`__createLearningUnit` → **`if (reused) openLearningResumeModal({...})`
+ 回写 input + 不再 selectSession** → 用户在 modal 里选 → 选「继续」才会
selectSession，选「先停掉」会调 `stop` API 后重走 `__createLearningUnit(seedText)`。

**3. 新增 `openLearningResumeModal / closeLearningResumeModal`**

骨架照抄 `openLearningStopModal`（设 objective 文本 → 去 `hidden` →
改 `aria-hidden`）。闭包变量 `pendingResumeContext` 保存当前的
`{activeUnit, seedText}`，两个按钮 click handler 共享。

**4. 两个按钮的语义**

- `#btn-continue-active`：`selectSession(activeUnit.session_id, ...)` →
  把刚才打的字回写到 `els.messageInput` → focus → 关 modal。
  **不自动发送**，给用户回看上下文 / 重新决定的机会。
- `#btn-stop-and-new`：`POST /learning-units/{old_id}/stop` →
  `__createLearningUnit(seedText)`（此时后端不会再 409）→ `selectSession(...)`。
  按钮在 in-flight 期间互斥 disabled，避免双发。
- overlay 点击 / 右上 `×`：等同"取消"——不发请求，输入框已有原文字。

**5. 弹窗关闭钩子挂载**

把 `els.learningResumeModal` 加进
[app.js:1445](web/static/app.js#L1445) 既有的
`[memoryModal, deleteModal, learningStopModal]` overlay-click-to-close 列表，
追加 resume-modal 专属的 `closeLearningResumeModal()` 分支以确保
`pendingResumeContext` 被清掉。

## 三、测试

[tests/test_web_static_app.js](tests/test_web_static_app.js)：

1. **改写** 第 485 行测试 "创建研习卷遇到 active unit 冲突时自动恢复已有卷"
   → 重命名为 "...仅返回标记不再自动 hydrate 或弹 toast"。
   新断言：返回值仍带 `reused_active_unit: true` 与 `session_id / id`；
   内部 state 的 `unitId / sessionId` **保持 null**（不再自动写入）；
   `toasts` 数组 **为空**（不再静默 toast）。

2. **新增** "欢迎页暴露未完成研习卷入口与显式复用确认弹窗" 测试：
   - `INDEX_SOURCE` 必须包含 `id="welcome-active-units"` / `id="learning-resume-modal"`
     / `id="btn-continue-active"` / `id="btn-stop-and-new"`。
   - `APP_SOURCE` 必须包含 `fetchActiveLearningUnits` / `renderWelcomeActiveUnits` /
     `openLearningResumeModal` / `closeLearningResumeModal`。
   - **反向断言**：APP_SOURCE 不再出现旧 toast 文案
     `"已有未停止的研习卷;先学到这里后再发送新主题"`；
     LEARNING_UNIT_UI_SOURCE 不再出现 `"已回到未完成的研习卷"`。

## 验证

- `node --check web/static/{app,learning-unit-ui,observability}.js` → syntax OK
- `node --test tests/test_web_static_app.js` → **11 passed**（原 10 个 + 1 个新测试）

### 浏览器实测尚未覆盖

- 横幅在真实 unit 列表下的视觉密度、active 卷数量 > 1 时的卡片堆叠观感、
  modal 与现有 `#learning-stop-modal` 在极端"双开"场景下的层叠次序、
  640px 以下窄屏的按钮纵向布局，**均未在 Chrome 中跑过**。
- 下次用户在浏览器里点首页时如有偏差再调。诚实交付与上轮 observability 一致。

## 留心事项（共享工作树）

- 本轮改动只动 5 个文件：
  - [web/index.html](web/index.html)
  - [web/static/app.js](web/static/app.js)
  - [web/static/learning-unit-ui.js](web/static/learning-unit-ui.js)
  - [web/static/learning-unit.css](web/static/learning-unit.css)
  - [tests/test_web_static_app.js](tests/test_web_static_app.js)
- 不动 `learning_agent/` 下任何后端文件；GET /learning-units / POST /learning-units/
  {id}/stop 都是既有 endpoint。
- 提交时 `git add` 列出明确路径，不要 `add -A`；不卷入研学方与 web-search 方的
  其他 dirty 改动（mode_service.py / 各类 docs/changes / e2e runner 等）。
- 同日另一份 doc
  [2026-05-27-frontend-chat-finalization-and-observability-cleanup.md](docs/changes/2026-05-27-frontend-chat-finalization-and-observability-cleanup.md)
  是上轮收尾，本轮顺手一起提交。
