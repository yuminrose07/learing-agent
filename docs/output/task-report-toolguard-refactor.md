# 任务报告：ToolGuard 重构 —— 借鉴 Claude Code 权限系统设计

**报告时间**：2026-05-10
**任务类型**：代码重构与安全策略升级
**涉及项目**：Learning-Agent（个人长期学习 Agent 框架）

---

## 一、Situation（情境）

用户在使用 Learning-Agent 项目时遇到了一个权限拦截错误：

```
失败: [ToolGuard] bash blocked: bash is disabled by policy (tool_guard.bash_mode=disabled)
```

用户发送的指令是"阅读一下当前代码仓库，分析一下这个项目"，但系统默认将 `bash` 工具设置为 `disabled` 模式，导致所有 bash 命令被直接拒绝。用户要求我分析原因，并且不要修改代码。

经过初步分析，我发现 Learning-Agent 的 ToolGuard 扩展存在以下根本性问题：

1. **配置传递断裂**：`Config.to_dict()` 遗漏了 `tool_guard` 字段，导致配置文件中的权限设置无法生效
2. **子串匹配粗暴**：黑名单和危险命令检测使用子串匹配，`"rm"` 会误杀 `"rm_safe_file.txt"`，`"dd"` 会误杀 `"addr2line"`
3. **无用户确认机制**：只有 allow/deny 两种结果，遇到可疑操作直接拒绝，没有"询问用户"的中间态
4. **规则系统过于简单**：仅支持 `bash_allowlist` 前缀匹配，不支持精确匹配、通配符、按行为分类的规则

与此同时，用户提供了一个参考实现 —— Claude Code 的源码（位于 `/Users/roseannk/claude-code-analysis/src`），要求我学习其安全策略设计并应用到当前项目中。

---

## 二、Task（任务）

核心任务分为三个阶段：

1. **分析阶段**：深入阅读 Claude Code 的权限系统源码，提炼其设计亮点；同时全面分析 Learning-Agent 当前 ToolGuard 的实现和缺陷
2. **计划阶段**：基于对比分析，制定一份可执行的修改计划，提交给用户审批
3. **实施阶段**：按照获批计划，分阶段完成代码修改，确保向后兼容

---

## 三、Action（行动）—— 重点

### 3.1 探索 Claude Code 权限系统

我首先对 Claude Code 的源码进行了系统性的探索，重点研究了 `src/utils/permissions/` 目录下的 20+ 个文件，核心发现包括：

| 设计亮点 | 说明 |
|---------|------|
| **三级决策结果** | `allow` / `ask` / `deny` —— 可疑操作不直接拒绝，而是询问用户确认 |
| **多模式权限** | `default` / `plan` / `acceptEdits` / `auto` / `dontAsk` / `bypassPermissions` |
| **丰富的规则系统** | `alwaysAllowRules` / `alwaysDenyRules` / `alwaysAskRules`，支持精确、前缀、通配符匹配 |
| **规则来源追踪** | userSettings / projectSettings / localSettings / policySettings / cliArg / command / session |
| **决策原因结构化** | 每个决策附带 `PermissionDecisionReason`（rule / mode / hook / safetyCheck 等） |
| **文件操作类型区分** | `read` / `write` / `create` 三种操作类型，差异化检查 |
| **深度路径安全** | 符号链接解析、Windows 路径绕过检测、大小写规范化、路径遍历防护 |
| **危险文件/目录保护** | `.gitconfig`、`.bashrc`、`.zshrc`、`.claude/` 等自动触发 ask |
| **拒绝追踪** | 连续拒绝次数统计，防止无限循环 |
| **AI 分类器** | 自动判断 bash 命令安全性（内部构建为完整实现，外部为 stub） |

这些发现成为后续重构的核心参考。

### 3.2 分析当前项目问题

我对 Learning-Agent 的扩展系统进行了全面分析，覆盖 `extensions/tool_guard.py`、`core/hook_system.py`、`core/extension_manager.py`、`config.py` 等关键文件。总结出的关键缺陷：

| 问题 | 严重程度 | 根因 |
|------|---------|------|
| 配置传递断裂 | 🔴 高 | `Config.to_dict()` 未包含 `tool_guard` 字段 |
| 子串匹配粗暴 | 🔴 高 | 黑名单和危险二进制检测使用 `in` 子串匹配 |
| 无用户确认机制 | 🟡 中 | `HookResult` 只有 `abort`/`modified`，没有 `ask` |
| 规则系统过于简单 | 🟡 中 | 仅支持 `bash_allowlist` 前缀匹配 |
| 无决策原因追踪 | 🟡 中 | 拦截/放行只有日志，没有结构化原因 |
| 缺少审计日志 | 🟢 低 | 安全事件未持久化 |

### 3.3 制定修改计划

我使用 Plan Mode 将分析结果整理成一份详细的修改计划，包含 6 个阶段：

1. **阶段 1**：修复 `Config.to_dict()` 遗漏 `tool_guard` 字段
2. **阶段 2**：新增 Permission Decision 模型，扩展 `HookResult` 支持 `ask`
3. **阶段 3**：重写 ToolGuard 核心逻辑（规则引擎、决策流程、路径检查）
4. **阶段 4**：集成 Hook 系统 ask 行为到 Agent Loop
5. **阶段 5**：新增安全审计日志扩展
6. **阶段 6**：更新配置示例和文档

用户选择了"分阶段实施"方案，计划获批后开始执行。

### 3.4 分阶段实施过程

#### 阶段 1：修复配置传递 Bug

**修改文件**：`learning_agent/config.py`

问题定位：`to_dict()` 方法导出了所有配置项，唯独遗漏了 `tool_guard`。这意味着即使用户在 `config.yaml` 中设置了 `tool_guard.bash_mode`，配置也无法传递到 ToolGuard 扩展中。

**修复方式**：在 `to_dict()` 返回的字典中增加 `"tool_guard": self.tool_guard`。

```python
return {
    # ... 其他配置
    "tool_guard": self.tool_guard,  # ← 新增
}
```

#### 阶段 2：模型层扩展

**修改文件**：`learning_agent/models/models.py`、`learning_agent/models/__init__.py`

核心设计决策：引入 Claude Code 风格的三级决策模型，同时保持向后兼容。

新增模型：
- `PermissionBehavior` 枚举：`ALLOW` / `ASK` / `DENY`
- `PermissionDecisionReason`：结构化的决策原因（type + detail + rule）
- `PermissionDecision`：完整的决策结果（behavior + message + reason + suggestions）
- `PermissionRule`：规则定义（tool_name + rule_content + behavior）
- `FileOperationType` 枚举：`READ` / `WRITE` / `CREATE`

扩展 `HookResult`：
```python
class HookResult(BaseModel):
    modified: bool = False
    data: Any = None
    abort: bool = False
    abort_reason: Optional[str] = None
    ask: bool = False                    # 新增
    ask_message: Optional[str] = None    # 新增
    ask_suggestions: Optional[list[dict[str, Any]]] = None  # 新增
```

#### 阶段 3：重写 ToolGuard 核心逻辑

**修改文件**：`learning_agent/extensions/tool_guard.py`（完全重写，239 行 → ~520 行）

这是整个任务中最复杂的部分。我将原文件的单体 `ToolGuardPolicy` 类重构为三个核心组件：

**（1）规则解析引擎**

```python
class RuleType(str, Enum):
    EXACT = "exact"
    PREFIX = "prefix"
    WILDCARD = "wildcard"
```

支持三种规则格式：
- 精确匹配：`"ls -la"` → 只匹配 `"ls -la"`
- 前缀匹配：`"git:*"` → 匹配 `"git log"`、`"git status"` 等
- 通配符匹配：`"ls *"` → `*` 匹配任意字符序列

解析函数 `parse_rule()` 将字符串规则转换为结构化对象，匹配函数 `match_rule()` 基于规则类型执行精确匹配。

**关键设计**：旧配置的 `bash_allowlist` / `bash_denylist` 被解析为 `PREFIX` 规则而非 `EXACT` 规则，确保 `"ls"` 能匹配 `"ls -la"`，保持向后兼容。

**（2）权限决策引擎 `PermissionEngine`**

决策流程（按优先级）：
```
1. deny 规则（最优先）→ DENY
2. allow 规则 → ALLOW
3. ask 规则 → ASK
4. 系统 denylist → DENY
5. 危险二进制检测（基于第一个 token）→ DENY
6. 模式特定检查：
   - disabled → DENY
   - unrestricted → ALLOW
   - readonly → 检查只读白名单 → ALLOW / DENY（保持兼容）
   - allowlisted → 检查白名单 → ALLOW / DENY（保持兼容）
   - default → 简单命令 ALLOW，可疑模式 ASK
7. 默认 → ASK（default 模式）/ DENY（旧模式）
```

**bash 检查改进**：
- 使用 `shlex.split()` 做 token 级解析，替代粗暴的子串匹配
- 危险二进制检测基于第一个 token（如 `sudo`），而非子串（避免 `"addr2line"` 误杀）
- 新增"可疑模式"检测（不直接拒绝，转为 ASK）：
  - 命令替换 `$(...)` / `` `...` ``
  - 管道 `|`
  - 重定向 `>` / `>>`
  - 逻辑运算符 `&&` / `||` / `;`
- git 破坏性操作（`push`、`reset`、`revert` 等）在 default 模式下 → ASK
- readonly 模式下 git 子命令精细化检查：`branch -D`、`remote add`、`push` 等 → ASK

**文件检查改进**：
- 引入 `FileOperationType` 区分 read/write/create
- 路径遍历防护：检查 `..` 序列
- 越界检查：限制在工作目录内，越界 → ASK（而非直接 DENY）
- 敏感文件保护（写操作）：`.env`、`.gitconfig`、`.bashrc`、密钥文件等 → ASK
- 敏感目录保护（写操作）：`.git`、`.ssh`、`.claude` 等 → ASK
- 符号链接检测：路径中包含 symlink → ASK
- 大小写规范化：防止 `.cLauDe/Settings.json` 绕过

**（3）Hook Handler 集成**

`_on_tool_call()` 处理三种结果：
- `ALLOW` → `HookResult(modified=False, data=data)`
- `DENY` → `HookResult(abort=True, abort_reason=...)`
- `ASK` → `HookResult(ask=True, ask_message=..., ask_suggestions=...)`

#### 阶段 4：Agent Loop 集成

**修改文件**：`learning_agent/agent/agent_loop.py`

在 `_handle_tool_call()` 方法中，在原有的 `abort` 检查之前增加 `ask` 检查：

```python
if hook_result.ask:
    ask_message = hook_result.ask_message or "Tool call requires approval"
    # 构建用户友好的提示消息
    user_message = f"[Approval Required] {ask_message}\n"
    if ask_suggestions:
        user_message += "You can add a rule to skip this prompt in the future..."
    # 发布事件，记录结果
    # 不执行工具，直接返回
```

#### 阶段 5：安全审计日志扩展

**新增文件**：`learning_agent/extensions/security_audit.py`

订阅 `agent.toolCalled` 和 `agent.toolResult` 事件，将权限决策写入 `.observability/audit.jsonl`：

```json
{"timestamp": "2026-05-10T22:00:00+08:00", "event_type": "tool.result", "tool_id": "bash", "decision": "deny", "reason": "Dangerous binary detected: 'sudo'", "session_id": "..."}
```

审计记录包含：决策类型（allow/ask/deny）、原因、工具 ID、会话 ID、时间戳。

**修改文件**：`learning_agent/extensions/built_in.py`

将安全审计扩展注册到内置扩展列表中。

#### 阶段 6：配置示例更新

**修改文件**：`config.example.yaml`、`config.example.json`

新增 Tool Guard v2.0 的完整配置示例，包含：
- 五种 `bash_mode` 的说明
- 旧版 `bash_allowlist` / `bash_denylist` 的兼容配置
- 新版 `rules.allow` / `rules.deny` / `rules.ask` 的规则配置示例

### 3.5 验证与测试

每阶段完成后都进行了即时验证：

1. **阶段 3 单元测试**：对规则解析、规则匹配、权限引擎进行了 15+ 个测试用例的验证
2. **向后兼容测试**：验证了 `disabled` / `readonly` / `allowlisted` / `unrestricted` / `default` 五种模式的行为
3. **最终集成测试**：验证所有模块可以正常导入，关键流程（规则系统、文件检查、bash 检查）全部通过

---

## 四、Result（结果）

### 4.1 交付物统计

| 指标 | 数量 |
|------|------|
| 修改文件 | 9 个 |
| 新增文件 | 2 个 |
| 总代码行变化 | +~850 行（tool_guard.py 从 239 行增至 ~520 行） |

### 4.2 核心改进

| 改进项 | 改进前 | 改进后 |
|--------|--------|--------|
| 决策模型 | 二级（allow/deny） | **三级（allow/ask/deny）** |
| 规则系统 | 仅 `bash_allowlist` 前缀 | **精确/前缀/通配符 + 三种行为** |
| bash 解析 | 子串匹配 | **Token 级（`shlex.split`）** |
| 文件检查 | 不分操作类型 | **区分 read/write/create** |
| 敏感路径 | 直接 deny | **改为 ask（更友好）** |
| 审计日志 | 无 | **`.observability/audit.jsonl`** |
| 配置生效 | 配置文件无效（bug） | **已修复** |

### 4.3 向后兼容

- `disabled` / `readonly` / `allowlisted` / `unrestricted` 四级模式行为**完全保持**
- 旧配置 `bash_allowlist` / `bash_denylist` 仍然有效，被解析为前缀规则
- 新增 `default` 模式作为推荐默认配置（简单命令 allow，可疑模式 ask）

### 4.4 设计借鉴

本次重构从 Claude Code 权限系统中借鉴了以下关键设计：

1. **三级决策结果**（allow/ask/deny）
2. **规则引擎**（精确/前缀/通配符匹配）
3. **决策原因结构化**（PermissionDecisionReason）
4. **文件操作类型区分**（read/write/create）
5. **危险文件/目录保护**（.env、.gitconfig、.ssh 等）
6. **安全审计日志**（持久化到 JSONL）

同时根据 Learning-Agent 的实际情况做了适配：
- 保留四级 `bash_mode` 作为快捷配置（Claude Code 使用更复杂的模式体系）
- 简化了规则来源追踪（Claude Code 支持 7 种来源，当前项目暂不需要）
- 未引入 AI 分类器（Claude Code 的 classifier 是 ant-only 特性，外部构建为 stub）

---

## 五、经验与反思

1. **分析先行，计划驱动**：通过 Plan Mode 先完成分析和计划，再执行修改，避免了边改边想的低效模式
2. **向后兼容优先**：重写核心模块时，优先确保旧配置和旧行为不变，再引入新功能
3. **分阶段验证**：每个阶段完成后立即验证，问题早发现早修复
4. **借鉴而非复制**：Claude Code 的权限系统非常复杂（1486 行的 `permissions.ts`），我提取了核心设计思想，根据当前项目的实际需求做了合理简化
