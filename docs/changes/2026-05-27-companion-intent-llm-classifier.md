# Companion Intent — LLM 兜底分类器

## 背景

闲聊 V1 的 [`classify_companion_intent`](../../learning_agent/learning_agent/companion_policy.py) 是关键字 if-else 链。对显式信号（"哄我/我累了/焦虑"）工作得很好，但语义级表达完全漏掉，例如：

- "今天 PR 又被驳回了" → 该是 VENTING，词典里没有相关词 → 落到 NONE
- "脑子糊住了打不开 IDE" → 该是 TIRED → NONE
- "不知道怎么跟同事开口" → 该是 ANXIOUS → NONE

NONE 意味着 prompt addendum 没有意图段，模型不知道要"接住"什么，AI 回应会退化为客服式机械回复，违背"先减压后建议"的产品意图。

## 变更

- 新增 `learning_agent.learning_agent.companion_intent_classifier`：仿 [`ConceptExtractor`](../../learning_agent/learning_agent/concept_extractor.py) 模式，构造 cheap-tier provider 调用。`async classify(text)` 返回 5 类意图之一（venting/tired/anxious/distraction/lonely）或 `None`；超时/异常/解析失败一律 `None`。
- `prepare_companion_turn` 增加可选 `intent_override` 参数；当关键字结果是 `NONE` 且 `intent_override` 非 `NONE` 时替换为 override，并把 `message_metadata.companion_intent_source` 标记为 `"llm"`，否则标记 `"keyword"`。LLM 命中也参与 `enabled` / `signal_detected` 计算。
- `LearningAgentSystem._maybe_classify_companion_intent`：在 `_prepare_session_turn` 调用 `prepare_companion_turn` 之前，对 `mode==CHAT` 且 input ≥ 6 字符且关键字未命中的情况调一次 classifier。
- `SessionEventType.COMPANION_SIGNAL_DETECTED` 事件 payload 增加 `source` 字段（`keyword` 或 `llm`），方便后续观测页 / 评估区分两种来源。
- `config.py` 增加 `companion_intent_model` 字段，对应环境变量 `LA_COMPANION_INTENT_MODEL`。本项目接入阿里云 DashScope（OpenAI 兼容接口），主模型是 `qwen-max`；意图分类只需"6 选 1"，建议指向 **`qwen-turbo`**——比主模型便宜约 40 倍、首 token 延迟更低，6 选 1 的输出稳定胜任。不配则沿用 `provider.default_model`。

## 边界

- **关键字优先，LLM 兜底**：命中关键字时不调 LLM，零额外延迟、零额外成本。
- **RETURN_TO_STUDY 不让 LLM 决定**：保留给关键字精准捕获——这条意图会"放开工具"，模型语义判断的误差代价过高。Classifier 只接受 5 类意图字符串，看到 `return_to_study` 也返回 `None`。
- **失败 100% 降级**：`asyncio.wait_for(timeout=2.5s)` 包裹 provider 调用；任何异常 / 超时 / 输出无法解析都返回 `None`，让 `prepare_companion_turn` 走关键字结果。
- **短输入跳过**：≥ 6 字符才进 LLM；过滤 "嗯"、"好"、"哈" 这种短跟进语。
- **UI / SSE 不变**：`companion_intent_source` 通过 `message_metadata` 已有通道流转，无需新增 SSE 字段。

## 验证

```bash
python3 -m pytest -q \
  tests/test_companion_intent_classifier.py \
  tests/test_companion_policy.py \
  tests/test_chat_study_separation.py \
  tests/test_mode_layering.py
node --test tests/test_web_static_app.js
```

实际结果：79 pytest passed（旧 60 + 新 19）+ 15 JS passed；更宽范围 144 pytest 零回归。
