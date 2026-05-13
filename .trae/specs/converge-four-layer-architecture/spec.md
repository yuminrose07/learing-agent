# 四层架构收敛 Spec

## Why
当前项目的设计表述仍残留七层概念，但真实主链已经收缩为 Interface、Product/Application、Agent Runtime、Infrastructure 四条主干。`CLI/Web` 仍直接访问 `_sessions`、`_l1_working` 和 runtime 私有状态，导致层间边界漂移、实现与设计文档脱节。

## What Changes
- 将本次重构的顶层目标统一定义为四层架构：`Interface`、`Product/Application`、`Agent Runtime`、`Infrastructure`
- 收口 `CLI/Web` 对内部私有字段的直接访问，统一改为通过 `LearningAgentSystem` 或下层公开接口访问
- 为 Product/Application 层补齐稳定的系统级 API，承接会话、记忆、状态保存、运行时维护等产品编排职责
- 为 Agent Runtime 层补齐只读快照/查询接口，替代对 `_session_runtimes`、runtime 私有字段的直接读取
- 明确 `Memory` 继续作为 Product/Application 层的独立子域，不下沉为 `AgentLoopSession` 私有状态
- 对齐实现注释、边界文档和验证脚本，确保“四层表达”成为后续重构基线

## Impact
- Affected specs: 架构分层、接口层访问约束、产品层公开 API、运行时观测接口、Memory 边界
- Affected code: `learning_agent/main.py`、`learning_agent/web_server.py`、`learning_agent/agent/agent_loop.py`、`learning_agent/session/session_manager.py`、`learning_agent/memory/memory_manager.py`、`learning_agent/core/extension_manager.py`、`learning_agent/core/observability.py`

## ADDED Requirements
### Requirement: Interface 层仅通过公开接口访问业务能力
系统 SHALL 要求 `CLI/Web` 只通过 `LearningAgentSystem` 或被明确公开的下层方法访问会话、记忆、保存与运行时状态，不再直接读写私有字段。

#### Scenario: Web 查询会话不再穿透 `_sessions`
- **WHEN** Web 路由需要读取、更新、删除或 fork 某个 session
- **THEN** 路由通过 `LearningAgentSystem` 或 `SessionManager` 的公开方法完成操作
- **AND** 路由不直接访问 `session_manager._sessions`

#### Scenario: Web 确认知识不再穿透 `_l1_working`
- **WHEN** Web 或 CLI 需要确认某个 L1 候选知识
- **THEN** 调用 `MemoryManager` 或 `LearningAgentSystem` 的公开确认接口
- **AND** 调用方不直接访问 `memory_manager._l1_working`

### Requirement: Product/Application 层暴露系统级编排 API
系统 SHALL 由 `LearningAgentSystem` 承担接口层的统一产品入口，封装会话查找、会话保存、知识确认、状态保存、运行时重置与只读状态查询。

#### Scenario: Interface 调用系统级 API 完成产品操作
- **WHEN** 接口层需要创建 session、发起聊天、删除 session、保存状态或查询运行时摘要
- **THEN** 优先调用 `LearningAgentSystem` 的公开方法
- **AND** 接口层不自行组合持久化、运行时清理和私有字段变更

### Requirement: Agent Runtime 层提供只读观测快照
系统 SHALL 由 `AgentLoop` 提供只读运行时快照/列表接口，向上暴露可观测信息，而不是让上层遍历 runtime 私有对象。

#### Scenario: `/observability/runtimes` 获取运行时状态
- **WHEN** Web 接口请求当前活跃 runtime 列表
- **THEN** Web 路由调用 `AgentLoop` 的只读查询接口获取摘要
- **AND** 不直接读取 `_session_runtimes`、`_chat_only_mode`、`_failure_tracker` 等私有字段

### Requirement: Memory 保持为 Product/Application 独立子域
系统 SHALL 将 `MemoryManager` 继续视为 Product/Application 层中的独立服务，通过显式服务接口参与产品能力，而不是并入 runtime 私有状态。

#### Scenario: 运行时重构时维持 Memory 边界
- **WHEN** 为四层架构调整代码职责
- **THEN** 不给 `AgentLoopSession` 新增长期 memory 私有状态
- **AND** Memory 相关能力通过 `LearningAgentSystem` 或 `MemoryManager` 显式接口暴露

## MODIFIED Requirements
### Requirement: Agent Runtime 只负责 turn 执行与横切支撑
`AgentLoop`、`AgentLoopSession`、`HookSystem`、`EventBus`、`ToolRegistry`、`Observability` SHALL 共同构成 Agent Runtime 层，只负责单轮对话执行、运行时状态机、工具调用、重试降级、事件发射与观测支撑；session 生命周期决策、产品级持久化编排和接口适配不属于本层。

#### Scenario: 删除会话时的职责分配
- **WHEN** 用户删除一个 session
- **THEN** Product/Application 层先通过公开 runtime 维护接口清理该 session 的运行时状态
- **AND** 再执行 session 删除与持久化清理
- **AND** Runtime 层不直接决定 session 是否存在

### Requirement: 七层映射调整为四层执行模型
系统 SHALL 将原有 `Composition`、`Application`、`Domain Services` 收敛到 `Product/Application`，将 `Runtime` 与 `Core Infrastructure` 收敛到 `Agent Runtime`，并以四层职责边界指导新代码归属。

#### Scenario: 新增能力归属判断
- **WHEN** 新增或迁移一个模块
- **THEN** 先按四层边界判断归属，再按层内子域划分职责
- **AND** 不再以七层顶层模型作为未来实现的主要分配依据

## REMOVED Requirements
### Requirement: 七层顶层架构作为后续重构主模型
**Reason**: 七层更适合现状分析，不适合当前真实运行时与产品编排的落地重构，会持续放大顶层边界争议。
**Migration**: 将七层表述保留在现状分析文档中；所有新实现、接口收口和职责迁移以四层架构为唯一执行基线。
