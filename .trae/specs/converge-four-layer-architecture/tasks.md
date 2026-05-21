# Tasks
- [x] Task 1: 收口 Interface 层对内部状态的穿透访问
  - [x] 为 `LearningAgentSystem`、`SessionManager`、`MemoryManager` 补齐当前 Web/CLI 所需的公开读取与写入接口
  - [x] 将 `learning_agent/web/web_server.py` 中对 `_sessions`、`_l1_working`、runtime 私有字段的直接访问替换为公开接口
  - [x] 将 `learning_agent/learning_agent/main.py` 中仍依赖私有状态的交互逻辑替换为公开接口
  - [x] 验证 `learning_agent/learning_agent/main.py` 与 `learning_agent/web/web_server.py` 中不再直接访问上述私有字段

- [x] Task 2: 明确 Product/Application 层的编排边界
  - [x] 将会话创建、查询、更新、删除、保存、知识确认、运行时重置等产品级操作收敛到 `LearningAgentSystem` 的公开方法
  - [x] 保持 `SessionManager` 聚焦会话树与会话数据，保持 `MemoryManager` 聚焦记忆领域服务，不把文件持久化和接口适配逻辑塞回管理器
  - [x] 清理 Interface 层自行组合的“运行时清理 + 持久化 + 数据删除”流程，改为由产品层统一编排
  - [x] 验证 API 语义与现有 Web/CLI 行为保持兼容，至少覆盖会话查询、聊天、fork、更新、删除、知识确认、保存状态

- [x] Task 3: 为 Agent Runtime 提供只读观测与维护接口
  - [x] 为 `AgentLoop` 增加面向上层的只读 runtime 摘要查询接口
  - [x] 如有必要，为单个 session runtime 清理保留明确的公开维护接口，并限制返回值为稳定摘要而非内部对象
  - [x] 将 `/observability/runtimes` 和相关 Web 路由改为依赖这些公开接口
  - [x] 验证上层代码不再直接遍历 `_session_runtimes` 或读取 runtime 私有字段

- [x] Task 4: 固化四层职责边界并对齐实现表达
  - [x] 检查 `learning_agent/learning_agent/main.py`、`learning_agent/agent/agent_loop.py`、`learning_agent/learning_agent/extension_manager.py` 等关键模块的注释、文档字符串和接口命名，使其与四层职责一致
  - [x] 明确 `Memory` 仍是 Product/Application 的独立子域，不新增 `AgentLoopSession` memory 私有状态
  - [x] 保留现有物理目录结构可不立即迁移，但逻辑职责与依赖方向必须按四层收敛
  - [x] 验证实现、设计文档和检查项之间不存在明显冲突

- [x] Task 5: 进行边界验证与回归检查
  - [x] 用 `grep` 或等效检查确认 Interface 层已移除目标私有字段访问
  - [x] 运行与本次改动直接相关的诊断、测试或最小回归检查
  - [x] 修复本次改动引入的诊断问题
  - [x] 完成 `checklist.md` 勾选，若存在未通过项则回写新任务再继续

# Task Dependencies
- [Task 2] depends on [Task 1]
- [Task 3] depends on [Task 1]
- [Task 4] depends on [Task 2], [Task 3]
- [Task 5] depends on [Task 2], [Task 3], [Task 4]
