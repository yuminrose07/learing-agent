# pi-mono 与 Claude Code 事件、Hook、扩展系统对比分析

> 调研时间：2026-05-13
> 对比对象：pi-mono (`/Users/roseannk/pi-mono`) vs Claude Code (`/Users/roseannk/claude-code-analysis/src`)

---

## 一、架构范式对比

| 维度 | **pi-mono** | **Claude Code** |
|------|-------------|-----------------|
| **架构风格** | Monorepo 洋葱分层 | 单体应用模块化 |
| **核心层大小** | 极小（6 个文件，~1500 行） | 大（QueryEngine 1295 行 + query.ts + 工具系统） |
| **分层边界** | 严格（core 不感知扩展/UI） | 松散（各模块相互导入） |
| **代码组织** | packages/ai → agent → coding-agent → tui | src/ 下按功能模块平铺 |
| **依赖方向** | 外层依赖内层，内层不依赖外层 | 各模块可相互依赖（存在循环依赖风险） |

### 1.1 架构示意图

**pi-mono（洋葱分层）**：
```
交互层 (tui/web-ui)
    ↓ 依赖
应用层 (coding-agent: extensions, tools, session)
    ↓ 依赖
核心层 (agent: agent-loop, types)
    ↓ 依赖
协议层 (ai: providers, stream)
```

**Claude Code（模块化单体）**：
```
QueryEngine ←→ utils/hooks.ts
    ↕              ↕
utils/permissions.ts ←→ utils/plugins/
    ↕              ↕
commands/ ←→ tools/ ←→ services/mcp/
```

---

## 二、事件系统对比

| 维度 | **pi-mono** | **Claude Code** |
|------|-------------|-----------------|
| **事件层数** | 2 层（Core Event + Extension Event） | 3 层（TUI Event + Hook Execution Event + Telemetry） |
| **Core 事件** | `AgentEvent`（单向流，不可阻断） | 无独立的 Core 事件系统 |
| **扩展事件** | `ExtensionEvent`（部分可阻断） | `HookExecutionEvent`（仅观测 Hook 执行状态） |
| **事件发射方式** | `emit()` 函数调用 | `EventEmitter.emit()` + 专用 emit 函数 |
| **能否阻断流程** | Core Event ❌ 不能；Extension Event ✅ 部分能 | TUI Event ✅ 能（stopImmediatePropagation） |
| **事件缓存** | 无 | 有（Hook Event 缓存最多 100 条） |
| **消费者数量** | 多消费者（EventStream 可被多处订阅） | 单消费者（HookExecutionEvent 只有一个 handler） |

### 2.1 关键差异：事件的目的

**pi-mono 的事件**：
- Core Event 纯粹用于**通知/观测**（UI 渲染、审计日志）
- Extension Event 用于**扩展介入**（修改数据、阻断流程）
- 两者职责分离清晰

**Claude Code 的事件**：
- TUI Event 用于**UI 交互**（键盘输入、焦点变化）
- Hook Execution Event 用于**SDK 输出**（让外部知道 Hook 正在执行）
- Telemetry Event 用于**分析上报**
- 没有独立的"扩展通知事件"——扩展介入直接通过 Hook 系统完成

---

## 三、Hook 系统对比

这是两者**最核心的差异**。

| 维度 | **pi-mono** | **Claude Code** |
|------|-------------|-----------------|
| **机制** | 配置注入（Config Injection） | 完整 Hook 引擎（Hook Engine） |
| **Hook 数量** | 5 个（convertToLlm, transformContext, beforeToolCall, afterToolCall, shouldStopAfterTurn） | 28 个（PreToolUse, PostToolUse, UserPromptSubmit, SessionStart...） |
| **Hook 类型** | 纯 TypeScript 回调函数 | command / prompt / http / agent |
| **配置方式** | 代码中传入函数 | JSON 配置文件（hooks.json） |
| **条件过滤** | 无（由应用层桥接处理） | 有（`if` 条件 + `matcher` 匹配） |
| **同步/异步** | 全部是同步（await） | 支持 sync 和 async（后台运行） |
| **单次执行** | 不支持 | 支持（`once: true`） |
| **执行引擎** | 无（直接调用回调函数） | 有（`utils/hooks.ts`，5022 行） |
| **错误处理** | 由调用方 try-catch | 引擎内统一处理 |
| **可观测性** | 通过 Event 间接观测 | 专用 `HookExecutionEvent` 实时广播 |

### 3.1 实现方式对比

**pi-mono（配置注入）**：
```typescript
// 核心层只定义注入点
interface AgentLoopConfig {
    beforeToolCall?: (ctx) => Promise<{ block?: boolean } | undefined>;
}

// 应用层创建 Agent 时传入
const agent = new Agent({
    beforeToolCall: async ({ toolCall }) => {
        // 应用层自己实现拦截逻辑
        return { block: true };
    }
});

// 核心层直接调用
if (config.beforeToolCall) {
    const result = await config.beforeToolCall(ctx);
    if (result?.block) return error;
}
```

**Claude Code（Hook 引擎）**：
```json
// hooks.json（配置文件）
{
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        { "type": "command", "command": "safety-check.sh $ARGUMENTS", "if": "Bash(*)" },
        { "type": "prompt", "prompt": "Is this safe? $ARGUMENTS" }
      ]
    }
  ]
}
```

```typescript
// 核心层调用 Hook 引擎
const hookResult = await executePreToolUseHooks(toolName, toolInput);
if (hookResult?.decision === 'block') {
    return { blocked: true, reason: hookResult.reason };
}
```

### 3.2 优缺点分析

| | **pi-mono 配置注入** | **Claude Code Hook 引擎** |
|---|---|---|
| **优点** | 核心层极简；无运行时开销；测试简单（直接 mock 函数） | 功能强大；用户可配置；覆盖完整生命周期；无需写代码 |
| **缺点** | 扩展点少；需要代码才能扩展；用户无法自行配置 | 核心层复杂；配置学习曲线高；引擎本身需要维护 |

---

## 四、扩展/插件系统对比

| 维度 | **pi-mono Extension** | **Claude Code Plugin** |
|------|----------------------|------------------------|
| **所在层级** | 应用层（coding-agent） | 与核心同层（utils/plugins/） |
| **加载机制** | 动态加载 TS 文件（jiti） | 从 marketplace/git/npm 安装 |
| **扩展注册方式** | 代码注册（工厂函数） | 配置文件（plugin.json）+ 代码 |
| **可注册内容** | 事件、工具、命令、快捷键、flag、renderer、provider | 命令、agents、skills、hooks、mcpServers、lspServers |
| **用户可禁用** | 不支持显式禁用 | ✅ 支持（/plugin UI） |
| **市场/生态** | 无（本地文件） | 有（marketplace 系统） |
| **版本管理** | 无 | 有（Git SHA、semver） |
| **依赖管理** | 无 | 有（dependencyResolver） |
| **权限隔离** | 扩展通过 context 访问 | 插件通过文件系统隔离 |

### 4.1 扩展能力矩阵

| 能力 | **pi-mono** | **Claude Code** |
|------|-------------|-----------------|
| 注册工具 | ✅ `api.registerTool()` | ✅ skills/tools |
| 注册命令 | ✅ `api.registerCommand()` | ✅ commands/ |
| 注册快捷键 | ✅ `api.registerShortcut()` | ❌ 无 |
| 注册渲染器 | ✅ `api.registerMessageRenderer()` | ❌ 无 |
| 注册 Provider | ✅ `api.registerProvider()` | ❌ 无（内置 Provider 系统） |
| 注册 MCP Server | ❌ 无 | ✅ `mcpServers` |
| 注册 LSP Server | ❌ 无 | ✅ `lspServers` |
| 注册 Hook | ✅ 通过事件间接实现 | ✅ `hooks.json` |
| 文件系统访问 | 通过 `ctx.cwd` | 通过插件目录隔离 |

### 4.2 技能系统对比

Claude Code 有独立的 **Skill** 系统，pi-mono 没有这个概念：

| | **pi-mono** | **Claude Code Skill** |
|---|---|---|
| 概念 | 等同于 Command | 轻量级 prompt 模板 |
| 定义方式 | TypeScript 代码 | Markdown 文件（.md） |
| 可携带 hooks | ❌ | ✅ |
| 可携带文件 | ❌ | ✅（files 字段） |
| 用户可创建 | 需要写代码 | 可以写 Markdown |

---

## 五、权限系统对比

| 维度 | **pi-mono** | **Claude Code** |
|------|-------------|-----------------|
| **所在层级** | 应用层（通过 ExtensionEvent 实现） | 核心层（utils/permissions/） |
| **决策模型** | allow / ask / deny 三级 | allow / ask / deny 三级 |
| **规则引擎** | 简单（前缀/精确/通配符匹配） | 复杂（多级规则来源、优先级排序） |
| **规则来源** | 配置文件（yaml/json） | userSettings / projectSettings / localSettings / policySettings / cliArg / command / session |
| **AI 分类器** | 无 | 有（TRANSCRIPT_CLASSIFIER、BASH_CLASSIFIER、yoloClassifier） |
| **拒绝追踪** | 有（failure_tracker） | 有（denialTracking） |
| **模式系统** | 4 种（disabled/readonly/allowlisted/unrestricted） | 多种（default/unrestricted/dontAsk/auto/acceptEdits/plan/bypassPermissions） |
| **路径安全** | 基本（路径遍历检查） | 深度（符号链接解析、Windows 路径绕过、大小写规范化） |

---

## 六、核心循环对比

### 6.1 循环结构

**pi-mono**：
```typescript
// 函数式 + 配置注入
function agentLoop(prompts, context, config): EventStream<AgentEvent> {
    while (true) {
        // 1. transformContext (config 注入)
        // 2. convertToLlm (config 注入)
        // 3. streamFn (config 注入)
        // 4. 处理 tool calls
        //    - beforeToolCall (config 注入)
        //    - 执行工具
        //    - afterToolCall (config 注入)
        // 5. shouldStopAfterTurn (config 注入)
        // 6. getSteeringMessages / getFollowUpMessages (config 注入)
    }
}
```

**Claude Code**：
```typescript
// 类 + 生成器 + 内部调用 Hook 引擎
class QueryEngine {
    async *submitMessage(prompt): AsyncGenerator<SDKMessage> {
        // 1. processUserInput
        //    - executeUserPromptSubmitHooks (Hook 引擎)
        // 2. query()
        //    - 构建 system prompt
        //    - 调用 LLM
        //    - 处理 response
        //    - 执行 tools
        //      - canUseTool (权限系统 + PermissionRequest Hooks)
        //      - executePreToolUseHooks (Hook 引擎)
        //      - 执行工具
        //      - executePostToolUseHooks (Hook 引擎)
        //      - executePermissionDeniedHooks (Hook 引擎)
    }
}
```

### 6.2 状态管理

| | **pi-mono** | **Claude Code** |
|---|---|---|
| 状态容器 | `AgentContext` + `AgentState` | `QueryEngine` 实例 + `AppState` |
| 消息存储 | `AgentMessage[]`（数组拷贝） | `Message[]`（mutableMessages） |
| 状态更新 | 通过 accessor properties | 通过 `setAppState(fn)` |
| 跨 turn 持久 | SessionManager | transcript + sessionStorage |

---

## 七、设计哲学对比

### 7.1 pi-mono：极简核心 + 应用层桥接

> "核心层只负责消息流转，其他一切能力都是应用层的装饰。"

- **核心层**（agent）：不可变、稳定、无感知
- **应用层**（coding-agent）：灵活、可扩展、承载业务逻辑
- **桥接模式**：应用层把多个扩展的需求汇总成核心层能理解的单个函数

**适合场景**：
- 需要多前端（TUI + Web UI）共享同一核心
- 核心需要长期稳定，不受应用层迭代影响
- 扩展开发者是程序员（写 TypeScript）

### 7.2 Claude Code：一体化 + 配置驱动

> "所有能力都内建在系统中，通过配置文件即可扩展，无需写代码。"

- **核心循环**内建 Hook 调用点
- **Hook 引擎**内建在系统中
- **权限系统**深度集成
- **插件/技能**通过配置文件定义

**适合场景**：
- 终端用户需要自行配置 Hook（不写代码）
- 需要丰富的生命周期介入点
- 需要 marketplace 生态
- 需要 AI 分类器辅助决策

---

## 八、对当前项目的启示

当前项目（learning-agent）的设计特点：
- 有显式的 `HookSystem`（注册式）
- `ExtensionManager` 和 `HookSystem` 都在核心层
- `EventBus` 独立存在
- 权限通过 `ToolGuard` 扩展实现

### 8.1 从 pi-mono 学习

1. **核心层上移 Extension**：把 `ExtensionManager` 从 core 移到应用层，core 只保留 `AgentLoopConfig` 注入点
2. **配置注入替代部分 Hook**：`convertToLlm`、`transformContext` 等可以变成注入点
3. **严格分离 Event 和 Hook**：`EventBus` 只用于通知，`HookSystem` 只用于拦截

### 8.2 从 Claude Code 学习

1. **Hook 事件丰富化**：当前 13 个 HookPoint 可以扩展到更多生命周期节点
2. **Hook 配置化**：考虑支持 JSON 配置 Hook（降低使用门槛）
3. **权限系统内建**：把 `ToolGuard` 从扩展提升为核心能力
4. **AI 分类器**：引入 bash/工具分类器，实现更智能的权限决策
5. **拒绝追踪**：添加 `denialTracking` 防止无限循环

### 8.3 不适合照搬的

1. **Claude Code 的单体架构**：当前项目的分层更清晰，不应退化为单体
2. **Claude Code 的 Hook 引擎复杂度**：5022 行的 Hook 引擎维护成本高，当前项目可以用更轻量的方式实现
3. **Claude Code 的 marketplace**：当前项目是个人学习框架，不需要 marketplace

---

## 九、一句话总结

> **pi-mono 是"核心层留白，应用层填色"——核心极简稳定，扩展在应用层自由发挥；Claude Code 是" everything is a hook"——所有介入点都内建在核心中，通过配置文件驱动。两者没有优劣，只有适用场景不同。**
