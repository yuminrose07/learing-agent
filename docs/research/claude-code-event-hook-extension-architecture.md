# Claude Code 事件、Hook 与扩展系统架构调研

> 调研时间：2026-05-13  
> 源码路径：`/Users/roseannk/claude-code-analysis/src`  
> 核心模块：`QueryEngine`（核心循环）、`utils/hooks.ts`（Hook 系统）、`utils/permissions/`（权限系统）、`utils/plugins/`（插件系统）

---

## 一、整体架构概览

Claude Code 是**单体应用架构**（非 Monorepo 分层），所有代码集中在 `src/` 目录下，按功能模块组织：

```
src/
├── QueryEngine.ts          ← 核心循环（查询引擎）
├── query.ts                ← 主查询入口
├── Tool.ts                 ← 工具类型定义
├── commands.ts             ← 命令注册表
├── commands/               ← 各命令实现
├── tools/                  ← 各工具实现
├── utils/
│   ├── hooks.ts            ← Hook 执行引擎（5022 行）
│   ├── permissions/        ← 权限系统（1486 行）
│   ├── plugins/            ← 插件系统（3302 行）
│   └── settings/           ← 设置系统
├── hooks/                  ← React hooks（UI 层）
├── ink/                    ← TUI 渲染引擎
├── plugins/                ← 内置插件注册
├── skills/                 ← 技能系统
└── services/               ← 外部服务（MCP、LSP、Analytics）
```

**关键特征**：没有严格的分层边界，核心循环、Hook 系统、权限系统、插件系统都在同一层级，通过模块导入相互调用。

---

## 二、事件系统（Event System）

### 2.1 三层事件机制

Claude Code 有三类不同的事件系统：

| 事件系统 | 位置 | 用途 | 特点 |
|---------|------|------|------|
| **TUI EventEmitter** | `ink/events/emitter.ts` | 终端 UI 事件 | 支持 `stopImmediatePropagation()` |
| **Hook Execution Events** | `utils/hooks/hookEvents.ts` | Hook 执行状态广播 | 用于 SDK 输出 |
| **Telemetry Events** | `utils/telemetry/events.ts` | 遥测/分析事件 | 异步、不阻塞 |

### 2.2 TUI EventEmitter

基于 Node.js 的 `EventEmitter`，但增强了对自定义 `Event` 类的支持：

```typescript
// src/ink/events/emitter.ts
export class EventEmitter extends NodeEventEmitter {
    override emit(type: string | symbol, ...args: unknown[]): boolean {
        if (type === 'error') {
            return super.emit(type, ...args);
        }
        const listeners = this.rawListeners(type);
        const ccEvent = args[0] instanceof Event ? args[0] : null;
        for (const listener of listeners) {
            listener.apply(this, args);
            if (ccEvent?.didStopImmediatePropagation()) {
                break;  // 支持中断事件传播
            }
        }
        return true;
    }
}
```

### 2.3 Hook Execution Events

专门用于向 SDK 消费者广播 Hook 的执行状态：

```typescript
// src/utils/hooks/hookEvents.ts
export type HookExecutionEvent =
  | { type: 'started'; hookId: string; hookName: string; hookEvent: string }
  | { type: 'progress'; hookId: string; hookName: string; hookEvent: string; stdout: string; stderr: string; output: string }
  | { type: 'response'; hookId: string; hookName: string; hookEvent: string; output: string; exitCode?: number; outcome: 'success' | 'error' | 'cancelled' };

export function registerHookEventHandler(handler: HookEventHandler | null): void {
    eventHandler = handler;
    // 处理 pending 的缓存事件
    if (handler && pendingEvents.length > 0) {
        for (const event of pendingEvents.splice(0)) {
            handler(event);
        }
    }
}
```

**特点**：
- 单 handler 模式（不是广播给多个 listener）
- 有事件缓存机制（`MAX_PENDING_EVENTS = 100`）
- 默认只发射 `SessionStart` 和 `Setup`，开启 `allHookEventsEnabled` 后才发射全部

---

## 三、Hook 系统（完整的 Hook 引擎）

### 3.1 Hook 与 pi-mono 的本质区别

Claude Code 拥有**完整的、独立的 Hook 引擎**（`utils/hooks.ts`，5022 行），而 pi-mono 只有配置注入式的回调函数。

Claude Code 的 Hook 特点：
- **28 种 Hook 事件**，覆盖完整生命周期
- **4 种 Hook 类型**：command（shell）、prompt（LLM）、http、agent
- **matcher 条件过滤**：支持 `if` 条件匹配工具名和参数
- **同步/异步执行**：sync hook 阻塞，async hook 后台运行
- **插件和技能都可以配置 hooks**

### 3.2 Hook 事件类型（28 种）

```typescript
// src/entrypoints/sdk/coreTypes.ts
export const HOOK_EVENTS = [
  'PreToolUse',         // 工具使用前
  'PostToolUse',        // 工具使用后
  'PostToolUseFailure', // 工具使用失败后
  'Notification',       // 通知事件
  'UserPromptSubmit',   // 用户提交 prompt 时
  'SessionStart',       // 会话开始
  'SessionEnd',         // 会话结束
  'Stop',               // 停止时
  'StopFailure',        // 停止失败时
  'SubagentStart',      // 子代理启动
  'SubagentStop',       // 子代理停止
  'PreCompact',         // 上下文压缩前
  'PostCompact',        // 上下文压缩后
  'PermissionRequest',  // 权限请求时
  'PermissionDenied',   // 权限被拒绝时
  'Setup',              // 设置/初始化时
  'TeammateIdle',       // 队友空闲时
  'TaskCreated',        // 任务创建时
  'TaskCompleted',      // 任务完成时
  'Elicitation',        // 请求澄清时
  'ElicitationResult',  // 澄清结果
  'ConfigChange',       // 配置变更时
  'WorktreeCreate',     // 工作树创建
  'WorktreeRemove',     // 工作树移除
  'InstructionsLoaded', // 指令加载
  'CwdChanged',         // 工作目录变更
  'FileChanged',        // 文件变更
] as const;
```

### 3.3 Hook 类型（4 种）

```typescript
// src/schemas/hooks.ts
// 1. Shell Command Hook
const BashCommandHookSchema = z.object({
    type: z.literal('command'),
    command: z.string(),           // 要执行的 shell 命令
    if: z.string().optional(),     // 匹配条件（如 "Bash(git *)"）
    shell: z.enum(['bash', 'powershell']).optional(),
    timeout: z.number().positive().optional(),
    statusMessage: z.string().optional(),
    once: z.boolean().optional(),  // 是否只执行一次
    async: z.boolean().optional(), // 是否异步执行
    asyncRewake: z.boolean().optional(), // 异步但 exit code 2 时唤醒模型
});

// 2. LLM Prompt Hook
const PromptHookSchema = z.object({
    type: z.literal('prompt'),
    prompt: z.string(),            // 给 LLM 的 prompt，可用 $ARGUMENTS 占位
    if: z.string().optional(),
    timeout: z.number().positive().optional(),
    model: z.string().optional(),  // 可指定模型
    statusMessage: z.string().optional(),
    once: z.boolean().optional(),
});

// 3. HTTP Hook
const HttpHookSchema = z.object({
    type: z.literal('http'),
    url: z.string().url(),         // POST 目标 URL
    if: z.string().optional(),
    timeout: z.number().positive().optional(),
    headers: z.record(z.string(), z.string()).optional(),
    allowedEnvVars: z.array(z.string()).optional(),
});

// 4. Agent Hook
const AgentHookSchema = z.object({
    type: z.literal('agent'),
    prompt: z.string(),            // 验证 prompt
    if: z.string().optional(),
    timeout: z.number().positive().optional(),
    model: z.string().optional(),
    statusMessage: z.string().optional(),
    once: z.boolean().optional(),
});
```

### 3.4 Hook 配置结构

```typescript
// HooksSettings 结构
{
  "PreToolUse": [
    {
      "matcher": "Bash",           // 匹配工具名
      "hooks": [
        { "type": "command", "command": "echo 'Running bash'", "if": "Bash(*)" },
        { "type": "prompt", "prompt": "Check if this is safe: $ARGUMENTS" }
      ]
    }
  ],
  "PostToolUse": [
    {
      "hooks": [
        { "type": "command", "command": "git status" }
      ]
    }
  ]
}
```

### 3.5 Hook 执行引擎

`utils/hooks.ts` 是核心执行引擎，负责：
- 匹配条件（`if` 字段）
- 执行不同类型的 hook（command/prompt/http/agent）
- 处理同步/异步模式
- 收集和返回结果
- 发射 hook execution events

```typescript
// 核心执行流程示意
function executeHooks(hookEvent, input, matchers) {
    for (const matcher of matchers) {
        // 1. 检查 matcher 是否匹配
        if (matcher.matcher && !matches(input, matcher.matcher)) continue;
        
        for (const hook of matcher.hooks) {
            // 2. 检查 if 条件
            if (hook.if && !matchesIfCondition(input, hook.if)) continue;
            
            // 3. 执行 hook
            const result = await executeHookByType(hook, input);
            
            // 4. 处理结果
            if (result.decision === 'block') {
                return { blocked: true, reason: result.reason };
            }
            
            // 5. 发射 execution event
            emitHookResponse({ hookId, hookName, outcome: 'success', ... });
        }
    }
}
```

### 3.6 Hook 的返回值语义

```typescript
// 同步 hook 返回格式
{
    continue?: boolean;        // 是否继续（默认 true）
    suppressOutput?: boolean;  // 是否隐藏 stdout
    stopReason?: string;       // 停止原因
    decision?: 'approve' | 'block';  // 审批决策
    reason?: string;           // 解释
    systemMessage?: string;    // 警告消息
    hookSpecificOutput?: {     // 事件特定输出
        hookEventName: 'PreToolUse',
        permissionDecision?: 'allow' | 'ask' | 'deny',
        permissionDecisionReason?: string,
        updatedInput?: Record<string, unknown>,
        additionalContext?: string,
    }
}

// 异步 hook 返回格式
{
    async: true,
    asyncTimeout?: number,
}
```

---

## 四、权限系统（Permissions）

### 4.1 三级决策模型

Claude Code 的权限系统（`utils/permissions/permissions.ts`，1486 行）实现了 **allow / ask / deny** 三级决策：

```typescript
// PermissionDecision 类型
type PermissionDecision =
  | { behavior: 'allow' }
  | { behavior: 'ask'; message: string; reason?: PermissionDecisionReason }
  | { behavior: 'deny'; message: string; reason?: PermissionDecisionReason };
```

### 4.2 规则引擎

规则来源（按优先级）：
```typescript
const PERMISSION_RULE_SOURCES = [
    'userSettings',      // 用户设置
    'projectSettings',   // 项目设置
    'localSettings',     // 本地设置
    'policySettings',    // 策略设置
    'cliArg',            // CLI 参数
    'command',           // 命令级
    'session',           // 会话级
] as const;
```

规则格式：
```typescript
// PermissionRule
{
    source: PermissionRuleSource;
    ruleBehavior: 'allow' | 'deny' | 'ask';
    ruleValue: PermissionRuleValue;  // 解析后的规则值
}
```

### 4.3 决策流程

```
1. 收集所有来源的 allow/deny/ask 规则
2. 按优先级排序（deny 最优先）
3. 匹配工具名和参数
4. 如果匹配到 deny → DENY
5. 如果匹配到 allow → ALLOW
6. 如果匹配到 ask → ASK
7. 如果都没匹配 → 进入模式特定检查
   - disabled → DENY
   - unrestricted → ALLOW
   - default → 简单命令 ALLOW，可疑模式 ASK
```

### 4.4 Classifier（AI 分类器）

对于 bash 命令，Claude Code 使用 AI 分类器判断安全性：
- `TRANSCRIPT_CLASSIFIER`：基于对话上下文的分类器
- `BASH_CLASSIFIER`：专门针对 bash 命令的分类器
- `yoloClassifier`：快速分类器

分类器结果可以覆盖规则引擎的决策。

---

## 五、插件系统（Plugin System）

### 5.1 插件与技能的关系

Claude Code 有两个相似但不同的扩展机制：

| | **Plugin（插件）** | **Skill（技能）** |
|---|---|---|
| 来源 | 外部市场（marketplace） | 内置或用户自定义 |
| 安装方式 | `/plugin` 命令安装 | `/skills` 加载目录 |
| 用户可禁用 | ✅ 可以 | ✅ 可以 |
| 包含 hooks | ✅ 可以 | ✅ 可以 |
| 包含 MCP | ✅ 可以 | ❌ 否 |
| 包含 LSP | ✅ 可以 | ❌ 否 |

### 5.2 插件结构

```
my-plugin/
├── plugin.json          # 可选：manifest 元数据
├── commands/            # 自定义斜杠命令
│   ├── build.md
│   └── deploy.md
├── agents/              # 自定义 AI agents
│   └── test-runner.md
├── skills/              # 技能定义
│   └── my-skill.md
└── hooks/               # Hook 配置
    └── hooks.json       # Hook 定义
```

### 5.3 插件类型定义

```typescript
// src/types/plugin.ts
export type LoadedPlugin = {
    name: string;
    manifest: PluginManifest;
    path: string;
    source: string;
    repository: string;
    enabled?: boolean;
    isBuiltin?: boolean;
    sha?: string;                    // Git commit SHA
    commandsPath?: string;
    commandsPaths?: string[];
    commandsMetadata?: Record<string, CommandMetadata>;
    agentsPath?: string;
    agentsPaths?: string[];
    skillsPath?: string;
    skillsPaths?: string[];
    outputStylesPath?: string;
    outputStylesPaths?: string[];
    hooksConfig?: HooksSettings;     // Hook 配置
    mcpServers?: Record<string, McpServerConfig>;
    lspServers?: Record<string, LspServerConfig>;
    settings?: Record<string, unknown>;
};
```

### 5.4 内置插件

```typescript
// src/plugins/builtinPlugins.ts
export type BuiltinPluginDefinition = {
    name: string;
    description: string;
    version?: string;
    skills?: BundledSkillDefinition[];
    hooks?: HooksSettings;
    mcpServers?: Record<string, McpServerConfig>;
    isAvailable?: () => boolean;
    defaultEnabled?: boolean;
};
```

### 5.5 插件加载流程

```typescript
// utils/plugins/pluginLoader.ts（3302 行）
async function loadPlugin(pluginId: string): Promise<PluginLoadResult> {
    // 1. 解析 plugin identifier（name@marketplace）
    // 2. 检查 marketplace 缓存
    // 3. 从 git clone 或 npm 下载
    // 4. 验证 manifest
    // 5. 加载各组件（commands, agents, skills, hooks, mcpServers, lspServers）
    // 6. 检查依赖（dependencyResolver）
    // 7. 返回 enabled/disabled/errors
}
```

### 5.6 技能系统

技能是 Claude Code 中更轻量的扩展单元：

```typescript
// src/skills/bundledSkills.ts
export type BundledSkillDefinition = {
    name: string;
    description: string;
    aliases?: string[];
    whenToUse?: string;
    argumentHint?: string;
    allowedTools?: string[];
    model?: string;
    disableModelInvocation?: boolean;
    userInvocable?: boolean;
    isEnabled?: () => boolean;
    hooks?: HooksSettings;        // 技能也可以配置 hooks
    context?: 'inline' | 'fork';
    agent?: string;
    files?: Record<string, string>;
    getPromptForCommand: (args: string, context: ToolUseContext) => Promise<ContentBlockParam[]>;
};
```

内置技能（`src/skills/bundled/`）：
- `remember.ts` - 记忆管理
- `claudeInChrome.ts` - Chrome 扩展集成
- `claudeApi.ts` - Claude API 调用
- `verify.ts` - 验证执行
- `stuck.ts` - 卡住检测
- `scheduleRemoteAgents.ts` - 远程代理调度
- `batch.ts` - 批处理

---

## 六、核心循环（QueryEngine）

### 6.1 QueryEngine 架构

`QueryEngine` 是 Claude Code 的核心，采用**生成器模式**：

```typescript
export class QueryEngine {
    async *submitMessage(
        prompt: string | ContentBlockParam[],
        options?: { uuid?: string; isMeta?: boolean }
    ): AsyncGenerator<SDKMessage, void, unknown> {
        // 1. 处理用户输入（slash 命令、附件等）
        const { messages: messagesFromUserInput, shouldQuery } = await processUserInput(...);
        
        // 2. 执行 UserPromptSubmit hooks
        for await (const hookResult of executeUserPromptSubmitHooks(inputMessage, ...)) {
            if (hookResult.blockingError) {
                yield blockingMessage;
                return;
            }
        }
        
        // 3. 调用 query() 进入主循环
        for await (const message of query(...)) {
            yield message;
        }
    }
}
```

### 6.2 主循环中的 Hook 介入点

```
submitMessage()
  ├── processUserInput()           ← 处理用户输入
  │   └── executeUserPromptSubmitHooks()  ← Hook: UserPromptSubmit
  ├── query()                      ← 主循环
  │   ├── 构建 system prompt
  │   ├── 调用 LLM
  │   ├── 处理 assistant response
  │   ├── 执行工具调用
  │   │   ├── canUseTool()       ← 权限检查
  │   │   │   └── executePermissionRequestHooks()  ← Hook: PermissionRequest
  │   │   ├── executePreToolUseHooks()  ← Hook: PreToolUse
  │   │   ├── 实际执行工具
  │   │   ├── executePostToolUseHooks() ← Hook: PostToolUse / PostToolUseFailure
  │   │   └── executePermissionDeniedHooks() ← Hook: PermissionDenied
  │   └── 继续循环（如果有更多工具调用）
  └── SessionEnd hooks             ← Hook: SessionEnd
```

### 6.3 权限检查与 Hook 的交织

```typescript
// canUseTool 中调用 PermissionRequest hooks
const result = await canUseTool(tool, input, toolUseContext, ...);

// permissions.ts 中
export function createPermissionRequestMessage(...) {
    // Hook 决策
    case 'hook':
        return `Hook '${decisionReason.hookName}' blocked this action: ${decisionReason.reason}`;
    // 规则决策
    case 'rule':
        return `Permission rule '${ruleString}' from ${sourceString} requires approval...`;
    // 分类器决策
    case 'classifier':
        return `Classifier '${decisionReason.classifier}' requires approval...`;
}
```

---

## 七、核心设计特征

1. **单体但模块化**：没有严格分层，但通过模块组织保持一定清晰度
2. **Hook 系统极其强大**：28 种事件 × 4 种类型 × 同步/异步 = 高度灵活的介入能力
3. **权限系统深度集成**：规则引擎 + AI 分类器 + Hook 三级决策
4. **插件系统成熟**：支持 marketplace、git、npm，含完整的安装/更新/依赖管理
5. **Hook 即配置**：Hook 通过 JSON 配置文件定义，而非代码注册，降低了使用门槛
6. **SDK 友好**：所有 Hook 执行状态都通过 `HookExecutionEvent` 暴露给 SDK 消费者
