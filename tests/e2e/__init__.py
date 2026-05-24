"""
E2E (L3) 端到端测试脚手架。

设计目标
========
- **声明式**：每个 scenario 是一个 YAML 文件（位于 ./scenarios/），描述：
    1. 用户输入是什么
    2. LLM 应该按顺序返回什么（脚本化）
    3. 跑完之后，对最终事件流/会话状态做什么断言（不变式）
- **数据驱动**：新增一个 e2e 用例 = 加一个 yaml，不用写 Python。
- **失败可重放**：失败时把完整事件流 dump 到 .test_artifacts/，配合
  session_event_store 可单独回放。

scenario 文件格式见 scenarios/example_chat_smoke.yaml。
"""
