# 2026-05-27 · 陪伴 picker 下沉到输入框 + 进入会话即锁定

## 背景

陪伴 picker（小月亮）此前挂在顶栏右上角，和"思路"挤在同一条线，并且**全程可点**——
意味着用户在聊到一半时切风格，本轮已生成的 prompt addendum 还是旧风格，
profile 在 session metadata 里也会被中途回写，造成 UI 表征和 session 表征短暂打架。

另外 `companion_policy.prepare_companion_turn` 还保留了一条"自然语言切风格 / 关陪伴"的副路径
（`_detect_style_request` / `_detect_disable_request`）：用户在聊天里打"切到女友模式"或
"关闭女友模式"也会改 session metadata。这是 picker 出现之前的兜底入口，但 picker 上线之后，
两条入口并存会让 profile 来源变得不可观测：metadata 突然变了，但事件流里看不到 PUT 调用。

借用户的提议——"陪伴选择放在输入框右侧，只有首界面才能选"——一次性把这两个问题一起收掉：
**picker 是陪伴风格的唯一入口；进入会话后整块消失；自然语言不再有任何副作用**。

## 变更

### 前端

- [web/index.html](web/index.html)：删 topbar 里的 `topbar-companion` 块；
  把 `companion-picker` 块挪进 `input-wrapper`，介于 `textarea` 和 `btn-send` 之间。
  trigger 简化：去掉"陪伴"字样，只留 `☾ + 当前风格名` + 下拉箭头。
- [web/static/style.css](web/static/style.css)：
  - 删 `.topbar-companion` 选择器；
  - 新增 `.companion-picker { position: relative; flex: 0 0 auto; }`；
  - `.companion-menu` 改为 `bottom: calc(100% + 8px)` 向上展开（避免遮住 send 按钮）；
  - `.companion-picker.hidden { display: none; }` —— 锁定时整块消失，不留占位。
- [web/static/app.js](web/static/app.js)：
  - `els.topbarCompanion` → `els.companionPicker`（全局重命名）；
  - `syncCompanionPickerVisibility` 加 home-only 判定：
    `currentMode === 'chat' && currentView === 'home'`，否则整块隐藏；
  - `hideWelcome()` 增加 `syncCompanionPickerVisibility()` 调用，保证进入会话立即锁定。

### 后端

- [learning_agent/learning_agent/companion_policy.py](learning_agent/learning_agent/companion_policy.py)：
  - `prepare_companion_turn` 删掉 `_detect_style_request` / `_detect_disable_request` 两条分支：
    用户在聊天里打"切到女友模式"或"关闭女友模式"都不再回写 session metadata，
    `profile_changed` 永远是 False，`COMPANION_PROFILE_CHANGED` 事件不再从本路径触发；
  - `_TURN_TRIGGER_KEYWORDS`（哄我 / 好累 / 焦虑 / …）和 LLM 兜底意图保留，
    仍可在用户未开 picker 时单轮兜底 WARM_GIRLFRIEND 陪伴，但**不写回 session**，
    本质是"减压轮 = 一次性"，picker 是"减压持续 = 持久"——两层语义彻底分开；
  - 删掉 `_detect_style_request` / `_detect_disable_request` 两个函数定义（已无引用）。

### 测试

- [tests/test_web_static_app.js](tests/test_web_static_app.js)：
  - 断言 `id="companion-picker"` 出现在 `input-wrapper` 内（结构断言而非纯字符串包含）；
  - 断言不再有 `id="topbar-companion"` / `companion-trigger-label`；
  - 断言 home-only 判定字符串 `currentMode === 'chat' && currentView === 'home'`。
- [tests/test_companion_policy.py](tests/test_companion_policy.py)：
  - 删 `test_explicit_girlfriend_mode_persists_companion_profile`、
    `test_explicit_companion_style_updates_session_metadata`、
    `test_disable_companion_profile_clears_metadata`（行为已移除）；
  - 加 `test_text_based_style_switch_no_longer_takes_effect`、
    `test_text_based_style_switch_does_not_persist_session_metadata`、
    `test_text_based_disable_ignored_when_session_already_enabled`，
    锁定"自然语言切风格 / 关陪伴"= 零副作用。

## 边界

- **picker 完全消失，不是 disabled**：用户视觉上看不到，不会去戳。
  这一点比"灰色不可点"更明确——避免"为什么我点不动"的体验。
- **自然语言也锁**：UI 锁了但代码层留口子会让用户疑惑（"打字也行啊"），
  干脆一锁到底，唯一入口是 picker（也即 PUT `/sessions/{id}/companion`）。
- **减压轮不变**：用户没开 picker 但说"好累"，这一轮仍走 WARM_GIRLFRIEND 兜底，
  下一轮回归默认——这是单轮自适应，不是持久切换，无需写回 metadata。
- **现有 session 兼容**：已开陪伴的 session 不受影响；只是无法再用自然语言改它，
  必须新建对话或者 PUT API。

## 验证

```bash
python3 -m pytest -q tests/test_companion_policy.py tests/test_companion_intent_classifier.py tests/test_chat_study_separation.py tests/test_mode_layering.py
node --test tests/test_web_static_app.js
python3 -m pytest -q tests/ --ignore=tests/e2e
```

结果：79 + 15 + 495 全部通过。

## 非目标

- 不引入新 SSE 字段（visibility 是纯前端状态，不需后端推送）。
- 不接观测页（picker 是 UI 层操作，已经走 PUT API 进事件流，
  后续观测页统一接事件流即可，无需为本次新增字段）。
- 不改 `web_server.py`（API 不变）。
- 移动端的 picker 位置（输入框右侧空间紧张）暂不处理，N=1 桌面优先。
