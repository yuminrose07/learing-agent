# 闲谈 + 研习 UI / LLM 兜底 — 已完成清单（2026-05-27 / 28）

两天三次连续合并，关键词："picker 下沉对齐，LLM 兜底意图，文本绕过路径锁死"。
每个里程碑都有独立的 change doc，下方只做索引和关键结论。

## 里程碑 1 · 陪伴意图 LLM 兜底分类器（2026-05-27）

**问题**：[companion_policy.classify_companion_intent](learning_agent/learning_agent/companion_policy.py)
原本是纯关键字 if-else，对"PR 又被驳回了""脑子糊住了打不开 IDE"这类语义级情绪表达漏接，
意图落 NONE → prompt addendum 无意图段 → 模型机械客服式回应。

**做法**：keyword 优先，未命中且输入长度 ≥ 6 时调用一次小模型（[companion_intent_classifier.py](learning_agent/learning_agent/companion_intent_classifier.py)）；
模型输出受限 5 个意图（venting / tired / anxious / distraction / lonely / none），
**故意不放 RETURN_TO_STUDY**（语义判断失误对它的代价过高）；
asyncio.wait_for 2.5s + 全 except 兜底，失败 100% 降级到 NONE，不中断闲聊流程。

**可观测**：
- `message_metadata.companion_intent_source: "keyword" | "llm"`
- `COMPANION_SIGNAL_DETECTED` 事件 payload 加 `source` 字段

**模型**：DashScope `qwen-plus`（通过 `LA_COMPANION_INTENT_MODEL` 注入；
qwen-max 主模型对"6 选 1"分类过重，qwen-turbo 又略弱，qwen-plus 是甜点）。

**测试**：mock provider 全套（输出正解、含空白大小写、未知词、空串、TimeoutError、任意 Exception）+ system stub e2e（关键字命中跳过 LLM / 短输入跳过 / LLM 返回 None 保持普通闲聊）。

**Change doc**：[docs/changes/2026-05-27-companion-intent-llm-classifier.md](docs/changes/2026-05-27-companion-intent-llm-classifier.md)

---

## 里程碑 2 · 陪伴 picker 下沉到输入框 + 进会话锁定（2026-05-27）

**问题**：picker 挂在 topbar 全程可点 → 聊到一半切风格会让本轮已生成的 prompt addendum 和
session metadata 中途打架；外加 `_detect_style_request` / `_detect_disable_request`
两条自然语言副路径，让 profile 来源变得不可观测（metadata 突然变了，但事件流里看不到 PUT）。

**做法**：
- HTML：picker 从 topbar 挪到 `input-wrapper`（textarea 和 send 之间），trigger 简化为 `☾ + 当前风格名`。
- CSS：`.companion-picker.hidden { display: none }`（整块消失，非 disabled），菜单向上展开避免遮 send。
- JS：`syncCompanionPickerVisibility` 改判定 `currentMode === 'chat' && currentView === 'home'`，
  `hideWelcome()` 调用同步隐藏，进会话立即锁。
- Python：删 `_detect_style_request` / `_detect_disable_request` 两个函数及其在 `prepare_companion_turn` 里的调用，
  陪伴风格的唯一入口收敛到 picker（即 `PUT /sessions/{id}/companion`）。
- `_TURN_TRIGGER_KEYWORDS`（哄我 / 好累 / 焦虑 / …）和 LLM 兜底意图**保留为单轮减压兜底**，
  但**不写回 session metadata**：减压轮 = 一次性，picker = 持久，两层语义彻底分开。

**测试**：删 `test_explicit_girlfriend_mode_persists_companion_profile` / `test_explicit_companion_style_updates_session_metadata` /
`test_disable_companion_profile_clears_metadata`；加 `test_text_based_style_switch_no_longer_takes_effect` /
`test_text_based_style_switch_does_not_persist_session_metadata` /
`test_text_based_disable_ignored_when_session_already_enabled`，把"自然语言切风格 = 零副作用"锁死。

**Change doc**：[docs/changes/2026-05-27-companion-picker-input-relocate-and-lock.md](docs/changes/2026-05-27-companion-picker-input-relocate-and-lock.md)

---

## 里程碑 3 · 思路 picker 同步下沉 + 开卷前锁定（2026-05-28）

**问题**：陪伴下沉后，研习的「思路」picker 还在 topbar 右上角 — 两个模式 picker 位置不一致，
切到研习就出现"picker 跳到天上"的视觉割裂。

**做法**：完全照搬陪伴方案（用户决定）。
- HTML：思路 picker 从 topbar 移到 `input-wrapper`（紧跟 companion-picker 之后），删 "思路" 标签字，
  trigger 简化为 `☰ + 当前思路名`。欢迎页文案改：
  "在研习里需要换思考风格时可在右上角「思路」里切换" → "开研习卷前可在输入框右侧「思路」里选定...开卷后固定不变"。
- CSS：`.topbar-thinking → .thinking-picker`，合并选择器到 companion 一组；菜单向上展开。
- JS：visibility 改 `currentMode === 'learning' && currentView === 'home'`；
  `hideWelcome` 一并触发思路 picker 隐藏；`applyPersonaSelection` 简化为纯本地更新
  （picker 只在 home 可见 → currentSessionId 必为 null → 原 mid-session PUT 分支不可达，删）。
- 思路绑定路径收敛到一处：home 选 → currentPersonaKey 暂存 → 第一条消息时由 sendMessage PUT 一次。

**测试**：新增 `'研习页思路 picker 与陪伴对称：下沉到输入框右侧 + 开卷前可选 + 开卷后锁定'`
（位置、home-only 字符串、欢迎页文案改完、`.topbar-thinking` 已退役）。

**Change doc**：[docs/changes/2026-05-28-thinking-picker-input-relocate-and-lock.md](docs/changes/2026-05-28-thinking-picker-input-relocate-and-lock.md)

---

## 测试与回归

| 命令                                                                  | 结果                  |
|-----------------------------------------------------------------------|-----------------------|
| `python3 -m pytest -q tests/ --ignore=tests/e2e`                      | 495 passed in ~13s    |
| `node --test tests/test_web_static_app.js`                            | 16 / 16 pass          |
| `python3 -m pytest -q tests/test_companion_intent_classifier.py …`    | 79 passed（子集复跑） |

## 关键文件清单

新增：
- [learning_agent/learning_agent/companion_intent_classifier.py](learning_agent/learning_agent/companion_intent_classifier.py)
- [tests/test_companion_intent_classifier.py](tests/test_companion_intent_classifier.py)
- [docs/changes/2026-05-27-companion-intent-llm-classifier.md](docs/changes/2026-05-27-companion-intent-llm-classifier.md)
- [docs/changes/2026-05-27-companion-picker-input-relocate-and-lock.md](docs/changes/2026-05-27-companion-picker-input-relocate-and-lock.md)
- [docs/changes/2026-05-28-thinking-picker-input-relocate-and-lock.md](docs/changes/2026-05-28-thinking-picker-input-relocate-and-lock.md)

修改：
- [learning_agent/learning_agent/companion_policy.py](learning_agent/learning_agent/companion_policy.py)
- [learning_agent/learning_agent/main.py](learning_agent/learning_agent/main.py)
- [learning_agent/learning_agent/config.py](learning_agent/learning_agent/config.py)
- [web/index.html](web/index.html)
- [web/static/style.css](web/static/style.css)
- [web/static/app.js](web/static/app.js)
- [tests/test_companion_policy.py](tests/test_companion_policy.py)
- [tests/test_web_static_app.js](tests/test_web_static_app.js)
- `.env`（用户加了 `LA_COMPANION_INTENT_MODEL=qwen-plus`）
