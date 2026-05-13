"""
工具权限守卫扩展 v2.0：借鉴 Claude Code 权限系统设计。

核心策略：
- 三级决策：allow / ask / deny（ask = 暂停等待用户确认）
- 规则引擎：支持精确、前缀、通配符匹配
- 文件操作：区分 read / write / create，差异化检查
- bash 检查：token 级解析，危险命令 ask/deny，可疑模式 ask

向后兼容：
- disabled / readonly / allowlisted / unrestricted 四级模式行为保持不变
- 新增 default 模式作为推荐默认配置（简单命令 allow，可疑模式 ask）
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.ai import (
    BeforeToolExecuteInput,
    BeforeToolExecuteResult,
    FileOperationType,
    HookDecision,
    HookName,
    PermissionBehavior,
    PermissionDecision,
    PermissionDecisionReason,
)

logger = logging.getLogger(__name__)

# ─── 常量定义 ───

# 文件类工具映射到操作类型
_FILE_TOOL_OPERATIONS: dict[str, FileOperationType] = {
    "read_file": FileOperationType.READ,
    "read_material": FileOperationType.READ,
    "write_file": FileOperationType.WRITE,
    "edit_file": FileOperationType.WRITE,
}

# 默认只读命令（readonly 模式白名单）
_DEFAULT_READONLY_COMMANDS = {
    "ls", "cat", "find", "grep", "head", "tail", "wc", "pwd",
    "echo", "which", "stat", "file", "diff", "strings", "awk", "sed", "sort",
    "git",
}

# 系统级 denylist（始终生效，匹配即 deny）
_DEFAULT_DENYLIST = {
    "rm -rf /",
    "rm -rf /*",
    "> /dev",
    "dd if=",
    "mkfs",
    "fdisk",
    "format",
}

# 危险二进制（始终 deny）
_DANGEROUS_BINARIES = {
    "sudo", "su", "doas", "pkexec",
    "mkfs", "mkswap", "swapon", "swapoff",
    "fdisk", "gdisk", "parted", "partprobe",
    "dd", "wipefs", "shred",
    "halt", "poweroff", "reboot", "shutdown",
    "killall", "pkill",
}

# 可疑模式（不直接 deny，在 default 模式下转为 ask）
_SUSPICIOUS_PATTERNS = [
    # 命令替换
    re.compile(r'\$\([^)]*\)'),
    re.compile(r'`[^`]*`'),
    # 管道
    re.compile(r'\|'),
    # 逻辑运算符（多命令）
    re.compile(r'&&|\|\||;'),
    # 重定向（输出）
    re.compile(r'[0-9]*>[>]?'),
    # 交互式 shell
    re.compile(r'\b(bash|sh|zsh)\s+-[iIs]'),
]

# 危险文件（写操作触及 → ask）
_DANGEROUS_FILES = {
    ".env", ".env.local", ".env.production",
    ".gitconfig", ".gitmodules",
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv",
    ".profile", ".login", ".logout",
    ".ripgreprc",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".netrc", ".pgpass", ".htpasswd",
    ".npmrc", ".pypirc", ".dockercfg",
}

# 危险目录（写操作触及 → ask）
_DANGEROUS_DIRECTORIES = {
    ".git", ".ssh", ".aws", ".kube", ".docker", ".gnupg",
    ".claude", ".learning_agent",
}

# git 只读子命令（readonly 模式）
_READONLY_GIT_SUBCOMMANDS = {
    "log", "show", "status", "diff", "branch", "remote", "tag",
}

# git 破坏性操作（readonly 模式下 → ask）
_GIT_DESTRUCTIVE_SUBCOMMANDS = {
    "push", "reset", "revert", "clean", "stash",
}

# ─── 规则解析引擎 ───


class RuleType(str, Enum):
    EXACT = "exact"
    PREFIX = "prefix"
    WILDCARD = "wildcard"


@dataclass
class ParsedRule:
    type: RuleType
    pattern: str  # 原始模式或正则字符串


def parse_rule(rule_str: str) -> ParsedRule:
    """
    解析权限规则字符串。

    格式:
      - "ls -la"      → 精确匹配
      - "git:*"       → 前缀匹配（匹配 "git log" 等）
      - "ls *"        → 通配符匹配（* 匹配任意字符序列）
    """
    rule_str = rule_str.strip()

    # 前缀匹配: "git:*" → 匹配所有以 "git" 开头（后接空格或结束）的命令
    if rule_str.endswith(":*"):
        return ParsedRule(RuleType.PREFIX, rule_str[:-2])

    # 通配符匹配: 包含 * 但不是 :* 结尾
    if "*" in rule_str:
        # 将通配符转换为正则表达式
        regex = ""
        i = 0
        while i < len(rule_str):
            if rule_str[i] == "*":
                regex += ".*"
            else:
                regex += re.escape(rule_str[i])
            i += 1
        return ParsedRule(RuleType.WILDCARD, regex)

    # 精确匹配
    return ParsedRule(RuleType.EXACT, rule_str)


def match_rule(command: str, parsed: ParsedRule, case_insensitive: bool = True) -> bool:
    """检查命令是否匹配规则。"""
    cmd = command.strip()
    if case_insensitive:
        cmd = cmd.lower()
        pattern = parsed.pattern.lower()
    else:
        pattern = parsed.pattern

    if parsed.type == RuleType.EXACT:
        return cmd == pattern
    elif parsed.type == RuleType.PREFIX:
        if not cmd.startswith(pattern):
            return False
        remainder = cmd[len(pattern):]
        return remainder == "" or remainder.startswith(" ")
    elif parsed.type == RuleType.WILDCARD:
        regex = f"^{pattern}$"
        flags = re.IGNORECASE if case_insensitive else 0
        return bool(re.match(regex, cmd, flags=flags))

    return False


# ─── 权限决策引擎 ───


class PermissionEngine:
    """
    权限决策引擎。

    决策优先级（从高到低）：
    1. deny 规则 → DENY
    2. allow 规则 → ALLOW
    3. ask 规则 → ASK
    4. 系统 denylist → DENY
    5. 危险二进制 → DENY
    6. 模式特定检查（disabled/readonly/allowlisted/unrestricted/default）
    7. 安全检查（敏感路径 → ASK）
    8. 默认 → 取决于模式（旧模式 deny，default 模式 ask/allow）
    """

    def __init__(self, config: dict[str, Any]):
        guard_cfg = config.get("tool_guard", {})
        self.bash_mode = guard_cfg.get("bash_mode", "disabled")

        # 解析新规则系统
        rules_cfg = guard_cfg.get("rules", {})
        self.allow_rules = self._parse_rules(rules_cfg.get("allow", []))
        self.deny_rules = self._parse_rules(rules_cfg.get("deny", []))
        self.ask_rules = self._parse_rules(rules_cfg.get("ask", []))

        # 向后兼容：旧版的 bash_allowlist / bash_denylist 作为前缀规则处理
        for item in guard_cfg.get("bash_allowlist", []):
            self.allow_rules.append(("bash", ParsedRule(RuleType.PREFIX, item)))
        for item in guard_cfg.get("bash_denylist", []):
            self.deny_rules.append(("bash", ParsedRule(RuleType.PREFIX, item)))

        self.readonly_commands = _DEFAULT_READONLY_COMMANDS.copy()
        self.default_denylist = _DEFAULT_DENYLIST.copy()
        self.dangerous_binaries = _DANGEROUS_BINARIES.copy()

    @staticmethod
    def _parse_rules(rules: list[str]) -> list[tuple[str, ParsedRule]]:
        """解析规则列表，返回 (tool_name, parsed_rule) 列表。"""
        result = []
        for rule_str in rules:
            rule_str = rule_str.strip()
            if not rule_str:
                continue

            # 格式: "tool_name:rule_content" 或 "tool_name(rule_content)"
            if ":" in rule_str:
                tool_name, content = rule_str.split(":", 1)
            elif "(" in rule_str and rule_str.endswith(")"):
                tool_name = rule_str[:rule_str.index("(")]
                content = rule_str[rule_str.index("(") + 1:-1]
            else:
                # 没有指定工具名，默认为 bash
                tool_name = "bash"
                content = rule_str

            result.append((tool_name, parse_rule(content)))

        return result

    def _check_rules(
        self,
        tool_name: str,
        content: str,
    ) -> Optional[PermissionDecision]:
        """
        按优先级检查规则。
        返回第一个匹配的规则对应的决策，无匹配返回 None。
        """
        # 1. deny 规则（最优先）
        for tname, rule in self.deny_rules:
            if tname == tool_name and match_rule(content, rule):
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message=f"Matches deny rule: '{rule.pattern}'",
                    decision_reason=PermissionDecisionReason(
                        type="rule",
                        detail=f"deny rule matched: {rule.pattern}",
                    ),
                )

        # 2. allow 规则
        for tname, rule in self.allow_rules:
            if tname == tool_name and match_rule(content, rule):
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message=f"Matches allow rule: '{rule.pattern}'",
                    decision_reason=PermissionDecisionReason(
                        type="rule",
                        detail=f"allow rule matched: {rule.pattern}",
                    ),
                )

        # 3. ask 规则
        for tname, rule in self.ask_rules:
            if tname == tool_name and match_rule(content, rule):
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"Matches ask rule: '{rule.pattern}'",
                    decision_reason=PermissionDecisionReason(
                        type="rule",
                        detail=f"ask rule matched: {rule.pattern}",
                    ),
                )

        return None

    def check_bash(self, command: str) -> PermissionDecision:
        """检查 bash 命令权限。"""
        cmd_lower = command.lower().strip()

        # 空命令
        if not cmd_lower:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="Empty command",
                decision_reason=PermissionDecisionReason(
                    type="safety_check", detail="empty command"
                ),
            )

        # 1. 规则检查
        rule_result = self._check_rules("bash", command)
        if rule_result:
            return rule_result

        # 2. 系统 denylist（始终生效）
        for deny in self.default_denylist:
            if deny.lower() in cmd_lower:
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message=f"Command matches system denylist: '{deny}'",
                    decision_reason=PermissionDecisionReason(
                        type="safety_check",
                        detail=f"system denylist: {deny}",
                    ),
                )

        # 3. 解析命令 token
        try:
            tokens = shlex.split(command)
            first_token = tokens[0].lower() if tokens else ""
        except ValueError:
            first_token = cmd_lower.split()[0] if cmd_lower else ""
            tokens = [first_token]

        # 4. 危险二进制检测（基于第一个 token，而非子串）
        if first_token in self.dangerous_binaries:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Dangerous binary detected: '{first_token}'",
                decision_reason=PermissionDecisionReason(
                    type="safety_check",
                    detail=f"dangerous binary: {first_token}",
                ),
            )

        # 5. 模式特定检查
        if self.bash_mode == "disabled":
            # disabled 不再一刀切拒绝，而是走完整安全检查流程。
            # 规则匹配、危险二进制、可疑模式等检查仍然生效，
            # 只有普通命令最终默认拒绝。
            return self._check_disabled(command, cmd_lower)

        if self.bash_mode == "unrestricted":
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="unrestricted mode",
                decision_reason=PermissionDecisionReason(
                    type="mode", detail="bash_mode=unrestricted"
                ),
            )

        if self.bash_mode == "readonly":
            return self._check_readonly(command, tokens, first_token)

        if self.bash_mode == "allowlisted":
            return self._check_allowlisted(command)

        # 6. default 模式（新模式）
        return self._check_default(command, cmd_lower)

    def _check_readonly(
        self,
        command: str,
        tokens: list[str],
        first_token: str,
    ) -> PermissionDecision:
        """readonly 模式检查。不匹配则 DENY（保持向后兼容）。"""
        cmd_lower = command.lower()

        # git 子命令检查
        if first_token == "git" and len(tokens) >= 2:
            sub = tokens[1]

            # 破坏性操作 → ask（新行为：以前是 deny，现在 ask 更友好）
            if sub in _GIT_DESTRUCTIVE_SUBCOMMANDS:
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"Git '{sub}' requires approval",
                    decision_reason=PermissionDecisionReason(
                        type="safety_check", detail=f"git {sub}"
                    ),
                )

            if sub in _READONLY_GIT_SUBCOMMANDS:
                # 额外检查破坏性参数
                if sub == "branch" and len(tokens) >= 3 and tokens[2] in ("-D", "-d", "--delete"):
                    return PermissionDecision(
                        behavior=PermissionBehavior.ASK,
                        message="Git branch deletion requires approval",
                        decision_reason=PermissionDecisionReason(
                            type="safety_check", detail="git branch delete"
                        ),
                    )
                if sub == "remote" and len(tokens) >= 3 and tokens[2] in ("add", "remove", "rm", "set-url"):
                    return PermissionDecision(
                        behavior=PermissionBehavior.ASK,
                        message="Git remote modification requires approval",
                        decision_reason=PermissionDecisionReason(
                            type="safety_check", detail="git remote modify"
                        ),
                    )
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message=f"Read-only git command: {sub}",
                )

            if sub == "config" and "--list" in command:
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="git config --list",
                )

            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Git sub-command '{sub}' is not in the readonly whitelist.",
                decision_reason=PermissionDecisionReason(
                    type="mode", detail=f"readonly: git {sub}"
                ),
            )

        # 白名单命令
        if first_token in self.readonly_commands:
            # 禁止重定向和管道（防止只读命令被利用写入）
            if ">" in command or "|" in command or "tee" in cmd_lower:
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message="Redirection / pipe / tee is not allowed in readonly mode.",
                    decision_reason=PermissionDecisionReason(
                        type="mode", detail="readonly: redirection/pipe"
                    ),
                )
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"Read-only command: {first_token}",
            )

        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=f"Command '{first_token}' is not in the readonly whitelist.",
            decision_reason=PermissionDecisionReason(
                type="mode", detail=f"readonly: {first_token} not allowed"
            ),
        )

    def _check_allowlisted(self, command: str) -> PermissionDecision:
        """allowlisted 模式检查。不匹配则 DENY（保持向后兼容）。"""
        # allowlist 规则已在 _check_rules 中检查
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message="Command is not in the allowlist.",
            decision_reason=PermissionDecisionReason(
                type="mode", detail="allowlisted: no match"
            ),
        )

    def _check_default(self, command: str, cmd_lower: str) -> PermissionDecision:
        """default 模式检查：简单命令 allow，可疑模式 ask，语义敏感操作 ask。"""
        # 解析 token
        try:
            tokens = shlex.split(command)
            first_token = tokens[0].lower() if tokens else ""
        except ValueError:
            first_token = cmd_lower.split()[0] if cmd_lower else ""
            tokens = [first_token]

        # git 破坏性操作 → ASK
        if first_token == "git" and len(tokens) >= 2:
            if tokens[1] in _GIT_DESTRUCTIVE_SUBCOMMANDS:
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"Git '{tokens[1]}' is a destructive operation. Approve?",
                    decision_reason=PermissionDecisionReason(
                        type="safety_check",
                        detail=f"git destructive: {tokens[1]}",
                    ),
                )

        # 可疑模式检测（管道、重定向、命令替换等）→ ASK
        for pattern in _SUSPICIOUS_PATTERNS:
            if pattern.search(command):
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=(
                        "Command contains suspicious pattern "
                        "(pipe, redirection, command substitution, etc.). Approve?"
                    ),
                    decision_reason=PermissionDecisionReason(
                        type="safety_check",
                        detail="suspicious pattern detected",
                    ),
                )

        # 默认允许
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Default allow in default mode",
            decision_reason=PermissionDecisionReason(
                type="mode", detail="bash_mode=default"
            ),
        )

    def _check_disabled(self, command: str, cmd_lower: str) -> PermissionDecision:
        """disabled 模式：走完整安全检查流程，但最终默认拒绝普通命令。

        规则匹配、危险二进制检测、可疑模式检测仍然生效。
        只有未命中任何检查的普通命令才会被拒绝。
        """
        decision = self._check_default(command, cmd_lower)
        if decision.behavior == PermissionBehavior.ALLOW:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="bash is disabled by policy (tool_guard.bash_mode=disabled). "
                        "Use rules to explicitly allow specific commands.",
                decision_reason=PermissionDecisionReason(
                    type="mode", detail="bash_mode=disabled"
                ),
            )
        return decision

    def check_file(
        self,
        raw_path: str,
        operation: FileOperationType,
    ) -> PermissionDecision:
        """检查文件路径权限。"""
        # 1. 空路径
        if not raw_path:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="'path' argument is required.",
                decision_reason=PermissionDecisionReason(
                    type="path_check", detail="empty path"
                ),
            )

        # 2. 规则检查（按操作类型）
        op_str = operation.value
        rule_result = self._check_rules(f"file:{op_str}", raw_path)
        if rule_result:
            return rule_result

        # 也检查通配符 "file:*"
        rule_result = self._check_rules("file:*", raw_path)
        if rule_result:
            return rule_result

        # 3. 路径解析和遍历检查
        try:
            cwd = Path.cwd().resolve()
            target = (cwd / raw_path).resolve()
        except (OSError, ValueError) as e:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Invalid path '{raw_path}': {e}",
                decision_reason=PermissionDecisionReason(
                    type="path_check", detail=f"invalid path: {e}"
                ),
            )

        # 4. 越界检查（限制在工作目录内）
        try:
            target.relative_to(cwd)
        except ValueError:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"Path '{raw_path}' is outside the working directory '{cwd}'. Approve?",
                decision_reason=PermissionDecisionReason(
                    type="path_check",
                    detail=f"outside working directory: {raw_path}",
                ),
            )

        # 5. 路径遍历检查（规范化后仍包含 .. 则异常）
        normalized = os.path.normpath(raw_path)
        parts = normalized.replace("\\", "/").split("/")
        if ".." in parts:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"Path '{raw_path}' contains traversal sequences. Approve?",
                decision_reason=PermissionDecisionReason(
                    type="path_check", detail=f"path traversal: {raw_path}"
                ),
            )

        # 6. 敏感文件/目录检查（写操作）
        path_str_lower = str(target).lower()
        path_name_lower = target.name.lower()

        if operation in (FileOperationType.WRITE, FileOperationType.CREATE):
            # 危险文件（精确匹配文件名）
            if path_name_lower in {f.lower() for f in _DANGEROUS_FILES}:
                matched = next(
                    (f for f in _DANGEROUS_FILES if f.lower() == path_name_lower),
                    path_name_lower,
                )
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"Writing to sensitive file '{matched}' requires approval.",
                    decision_reason=PermissionDecisionReason(
                        type="safety_check",
                        detail=f"sensitive file: {matched}",
                    ),
                )

            # 危险目录（路径中包含这些目录）
            for dangerous_dir in _DANGEROUS_DIRECTORIES:
                dir_lower = dangerous_dir.lower()
                # 检查路径段是否精确匹配危险目录名
                path_parts = path_str_lower.replace("\\", "/").split("/")
                if dir_lower in path_parts:
                    return PermissionDecision(
                        behavior=PermissionBehavior.ASK,
                        message=f"Writing inside sensitive directory '{dangerous_dir}' requires approval.",
                        decision_reason=PermissionDecisionReason(
                            type="safety_check",
                            detail=f"sensitive directory: {dangerous_dir}",
                        ),
                    )

        # 7. 符号链接检查
        try:
            for part in target.parents:
                if part.is_symlink():
                    return PermissionDecision(
                        behavior=PermissionBehavior.ASK,
                        message=f"Path '{raw_path}' traverses a symbolic link. Approve?",
                        decision_reason=PermissionDecisionReason(
                            type="path_check", detail=f"symlink: {part}"
                        ),
                    )
            if target.is_symlink():
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    message=f"Path '{raw_path}' is a symbolic link. Approve?",
                    decision_reason=PermissionDecisionReason(
                        type="path_check", detail=f"symlink target: {target}"
                    ),
                )
        except (OSError, PermissionError):
            pass

        # 通过检查
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Path check passed",
            decision_reason=PermissionDecisionReason(
                type="path_check", detail="passed"
            ),
        )


# ─── Hook Handler ───


async def _before_tool_execute(
    hook_input: BeforeToolExecuteInput,
    config: dict[str, Any],
) -> BeforeToolExecuteResult:
    """
    `before_tool_execute` Hook：拦截未授权的工具调用。
    """
    tool_id = hook_input.tool_name
    args = hook_input.arguments
    engine = PermissionEngine(config)

    # ── 文件类工具 ──
    if tool_id in _FILE_TOOL_OPERATIONS:
        path = args.get("path", "")
        operation = _FILE_TOOL_OPERATIONS[tool_id]
        decision = engine.check_file(path, operation)

        if decision.behavior == PermissionBehavior.DENY:
            logger.warning(f"[ToolGuard] Blocked {tool_id}: {decision.message}")
            return BeforeToolExecuteResult(
                decision=HookDecision.DENY,
                deny_reason=f"[ToolGuard] {tool_id} blocked: {decision.message}",
            )
        elif decision.behavior == PermissionBehavior.ASK:
            logger.info(f"[ToolGuard] Ask {tool_id}: {decision.message}")
            return BeforeToolExecuteResult(
                decision=HookDecision.ASK,
                ask_message=f"[ToolGuard] {tool_id} requires approval: {decision.message}",
                annotations={
                    "decision_type": decision.decision_reason.type if decision.decision_reason else "unknown",
                    "decision_detail": decision.decision_reason.detail if decision.decision_reason else "",
                },
            )

    # ── bash 工具 ──
    elif tool_id == "bash":
        command = args.get("command", "")
        if not command:
            return BeforeToolExecuteResult(
                decision=HookDecision.DENY,
                deny_reason="'bash' requires a 'command' argument.",
            )

        decision = engine.check_bash(command)

        if decision.behavior == PermissionBehavior.DENY:
            logger.warning(f"[ToolGuard] Blocked bash: {decision.message}")
            return BeforeToolExecuteResult(
                decision=HookDecision.DENY,
                deny_reason=f"[ToolGuard] bash blocked: {decision.message}",
            )
        elif decision.behavior == PermissionBehavior.ASK:
            logger.info(f"[ToolGuard] Ask bash: {decision.message}")
            return BeforeToolExecuteResult(
                decision=HookDecision.ASK,
                ask_message=f"[ToolGuard] bash requires approval: {decision.message}",
                annotations={
                    "decision_type": decision.decision_reason.type if decision.decision_reason else "unknown",
                    "decision_detail": decision.decision_reason.detail if decision.decision_reason else "",
                },
            )

    # 通过检查
    return BeforeToolExecuteResult()


# ─── 扩展工厂 ───


def create_tool_guard_extension(config: dict[str, Any] | None = None) -> Extension:
    ext = Extension(
        id="core-tool-guard",
        name="Tool Guard",
        version="0.2.0",
        type="builtin",
        config=config or {},
    )

    async def activate(ctx: ExtensionContext) -> None:
        async def _hook_with_config(hook_input: BeforeToolExecuteInput) -> BeforeToolExecuteResult:
            return await _before_tool_execute(hook_input, ctx.config)

        ctx.register_hook(
            HookName.BEFORE_TOOL_EXECUTE,
            _hook_with_config,
            priority=200,
        )

    ext.on_activate(activate)
    return ext
