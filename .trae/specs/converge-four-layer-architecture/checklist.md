* [x] `CLI/Web` 不再直接访问 `session_manager._sessions`

* [x] `CLI/Web` 不再直接访问 `memory_manager._l1_working`

* [x] `/observability/runtimes` 通过 `AgentLoop` 的公开只读接口获取运行时摘要

* [x] `LearningAgentSystem` 成为 Interface 层的主要产品入口，会话、记忆、保存、运行时重置由其公开方法编排

* [x] `SessionManager` 继续只管理会话树与会话数据，不吸收接口适配或文件持久化编排

* [x] `MemoryManager` 继续作为 Product/Application 独立子域暴露能力，不要求 runtime 持有新的 memory 私有状态

* [x] `AgentLoop`/`AgentLoopSession` 继续聚焦 turn 执行、状态机、工具、事件、观测，不吸收 session 生命周期决策

* [x] 关键模块的实现表达、注释或文档字符串与四层职责保持一致

* [x] 相关诊断、搜索检查或回归验证通过，未引入新的明显边界违规

