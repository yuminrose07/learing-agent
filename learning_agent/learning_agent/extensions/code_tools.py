"""
代码工具扩展：提供文件读写、编辑和 Shell 执行能力。
所有工具内置路径安全检查（限制在当前工作目录内）。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.ai import ToolDefinition
from learning_agent.learning_agent.extensions.truncate_utils import (
    BASH_MAX_BYTES,
    BASH_MAX_LINES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    truncate_head,
    truncate_tail,
)

logger = logging.getLogger(__name__)


# ─── 写文件并发保护 ───
#
# 同一文件路径上的 write_file / edit_file 并发请求必须串行化,否则会出现
# read-modify-write 竞态(尤其是 edit_file:读旧内容 → 替换 → 写回)。
# ToolExecutor 默认对同一轮的多个 tool_call 用 asyncio.gather 并行,所以
# 这层保护必须在工具实现侧自包含,不能依赖调用方。
#
# 锁按"已解析的绝对路径"作 key,避免相对路径或软链接绕过。
# 不同 path 之间不互相阻塞,真正并发。
#
# 字典访问本身在 asyncio 单线程下是原子的(_get_path_lock 内部无 await),
# 所以不需要 registry lock。Lock 对象首次使用时才绑定 event loop,因此
# 测试若跨 loop 复用本 module,需要在 setUp 时调用 _reset_file_locks()。

_FILE_LOCKS: dict[str, asyncio.Lock] = {}


def _get_path_lock(path: str) -> asyncio.Lock:
    """按绝对路径返回对应的 asyncio.Lock,惰性创建。"""
    lock = _FILE_LOCKS.get(path)
    if lock is None:
        lock = asyncio.Lock()
        _FILE_LOCKS[path] = lock
    return lock


def _reset_file_locks() -> None:
    """清空写文件锁注册表。仅供测试在切换 event loop 时使用。"""
    _FILE_LOCKS.clear()


# ─── Pydantic Input Models（用于 ToolInputValidator 强校验）───

class ReadFileInput(BaseModel):
    path: str = Field(description="Relative or absolute file path")
    offset: int = Field(default=1, description="Starting line number (1-based). Default 1.")
    limit: int = Field(default=0, description="Maximum number of lines to read. 0 = unlimited.")


class WriteFileInput(BaseModel):
    path: str = Field(description="Relative or absolute file path")
    content: str = Field(description="Full file content to write")


class EditFileInput(BaseModel):
    path: str = Field(description="Relative or absolute file path")
    old_string: str = Field(description="Exact text to replace")
    new_string: str = Field(description="Replacement text")


class BashInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=60, description="Timeout in seconds")
    description: str = Field(default="", description="Optional human-readable description of what the command does")

# ─── 路径安全工具 ───

_SENSITIVE_PATTERNS = {
    ".env",
    ".ssh",
    ".aws",
    ".kube",
    ".docker",
    ".gnupg",
    ".netrc",
    ".pgpass",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    ".htpasswd",
    ".npmrc",
    ".pypirc",
}


def _resolve_and_guard_path(raw_path: str, allow_write: bool = False) -> Path:
    """
    解析并校验路径：
    - 必须是当前工作目录或其子目录下的文件
    - 禁止访问敏感路径（.env, SSH 密钥等）
    - 禁止指向目录外（如 ../etc/passwd）
    """
    cwd = Path.cwd().resolve()
    target = (cwd / raw_path).resolve()

    # 确保目标在工作目录下
    try:
        target.relative_to(cwd)
    except ValueError as exc:
        raise PermissionError(
            f"Path '{raw_path}' is outside the working directory '{cwd}'."
        ) from exc

    # 敏感路径检查（读和写都禁止）
    path_str = str(target).lower()
    for pat in _SENSITIVE_PATTERNS:
        if pat.lower() in path_str:
            raise PermissionError(
                f"Access to sensitive path '{raw_path}' is not allowed."
            )

    # 写操作额外保护：禁止覆盖已存在的敏感文件（二次确认）
    if allow_write and target.exists():
        for pat in _SENSITIVE_PATTERNS:
            if pat.lower() in path_str:
                raise PermissionError(
                    f"Overwriting sensitive file '{raw_path}' is not allowed."
                )

    return target


# ─── 工具 Handler ───

async def _tool_read_file(path: str, offset: int = 1, limit: int = 0, **kwargs: Any) -> dict[str, Any]:
    """读取文本文件内容。"""
    target = _resolve_and_guard_path(path, allow_write=False)

    if not target.exists():
        return {"error": f"File not found: {path}"}
    if target.is_dir():
        return {"error": f"'{path}' is a directory, not a file."}

    try:
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return {"error": f"File '{path}' is not a valid text file (encoding issue)."}
    except Exception as e:
        return {"error": str(e)}

    total_lines = len(lines)
    start = max(0, offset - 1)
    end = total_lines if limit <= 0 else min(total_lines, start + limit)
    selected = lines[start:end]
    selected_content = "".join(selected)
    truncation = truncate_head(
        selected_content,
        max_lines=DEFAULT_MAX_LINES,
        max_bytes=DEFAULT_MAX_BYTES,
    )
    content = truncation.content
    next_offset = None
    relative_path = str(target.relative_to(Path.cwd()))

    if truncation.first_line_exceeds_limit and selected:
        line_no = start + 1
        line_size = format_size(len(selected[0].encode("utf-8")))
        content = (
            f"[Line {line_no} is {line_size}, exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
            f"Use bash: sed -n '{line_no}p' {shlex.quote(relative_path)} | head -c {DEFAULT_MAX_BYTES}]"
        )
    elif truncation.truncated and truncation.output_lines > 0:
        next_offset = start + truncation.output_lines + 1
        shown_start = start + 1
        shown_end = start + truncation.output_lines
        if truncation.truncated_by == "bytes":
            notice = (
                f"[Showing lines {shown_start}-{shown_end} of {total_lines} "
                f"({format_size(DEFAULT_MAX_BYTES)} limit). Use offset={next_offset} to continue.]"
            )
        else:
            notice = (
                f"[Showing lines {shown_start}-{shown_end} of {total_lines}. "
                f"Use offset={next_offset} to continue.]"
            )
        content = f"{content}\n\n{notice}" if content else notice
    elif limit > 0 and end < total_lines:
        next_offset = end + 1
        remaining = total_lines - end
        notice = f"[{remaining} more lines. Use offset={next_offset} to continue.]"
        content = f"{selected_content}\n\n{notice}" if selected_content else notice

    return {
        "path": relative_path,
        "content": content,
        "total_lines": total_lines,
        "shown_lines": truncation.output_lines if truncation.truncated else len(selected),
        "offset": start + 1,
        "truncated": truncation.truncated,
        "truncated_by": truncation.truncated_by,
        "next_offset": next_offset,
    }


async def _tool_write_file(path: str, content: str, **kwargs: Any) -> dict[str, Any]:
    """覆盖写入文件内容。"""
    target = _resolve_and_guard_path(path, allow_write=True)
    lock = _get_path_lock(str(target))

    async with lock:
        # 自动创建父目录
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            return {
                "path": str(target.relative_to(Path.cwd())),
                "bytes_written": len(content.encode("utf-8")),
                "status": "written",
            }
        except Exception as e:
            return {"error": str(e)}


async def _tool_edit_file(path: str, old_string: str, new_string: str, **kwargs: Any) -> dict[str, Any]:
    """基于字符串替换编辑文件。"""
    target = _resolve_and_guard_path(path, allow_write=False)
    lock = _get_path_lock(str(target))

    async with lock:
        if not target.exists():
            return {"error": f"File not found: {path}"}
        if target.is_dir():
            return {"error": f"'{path}' is a directory, not a file."}

        try:
            with open(target, "r", encoding="utf-8") as f:
                original = f.read()
        except Exception as e:
            return {"error": str(e)}

        if old_string not in original:
            return {
                "error": f"old_string not found in '{path}'.",
                "hint": "Make sure the old_string matches exactly (including whitespace).",
            }

        # 防止模糊匹配导致多处替换，先检查出现次数
        occurrences = original.count(old_string)
        if occurrences > 1:
            return {
                "error": f"old_string appears {occurrences} times in '{path}'. "
                          "Please provide a more unique old_string to avoid ambiguity.",
            }

        new_content = original.replace(old_string, new_string, 1)

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(new_content)
            return {
                "path": str(target.relative_to(Path.cwd())),
                "status": "edited",
                "occurrences_replaced": 1,
                "old_length": len(old_string),
                "new_length": len(new_string),
            }
        except Exception as e:
            return {"error": str(e)}


async def _tool_bash(command: str, timeout: int = 60, description: str = "", **kwargs: Any) -> dict[str, Any]:
    """执行 shell 命令。实际权限由 tool_guard 扩展通过 Hook 控制。"""
    logger.info(f"[bash] {description or command}")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")
        combined = "\n".join(part for part in (stdout, stderr) if part).strip()
        truncation = truncate_tail(
            combined,
            max_lines=BASH_MAX_LINES,
            max_bytes=BASH_MAX_BYTES,
        )
        output = truncation.content

        if truncation.truncated:
            start_line = max(1, truncation.total_lines - truncation.output_lines + 1)
            end_line = truncation.total_lines
            if truncation.last_line_partial:
                notice = (
                    f"[Showing last {format_size(truncation.output_bytes)} of line {end_line}. "
                    "Use file operations to inspect full output.]"
                )
            elif truncation.truncated_by == "lines":
                notice = (
                    f"[Showing lines {start_line}-{end_line} of {truncation.total_lines}. "
                    "Use file operations to inspect full output.]"
                )
            else:
                notice = (
                    f"[Showing lines {start_line}-{end_line} of {truncation.total_lines} "
                    f"({format_size(BASH_MAX_BYTES)} limit). Use file operations to inspect full output.]"
                )
            output = f"{output}\n\n{notice}" if output else notice

        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": output,
            "stderr": "",
            "description": description,
            "truncated": truncation.truncated,
            "truncated_by": truncation.truncated_by,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {
            "command": command,
            "error": f"Command timed out after {timeout} seconds.",
        }
    except Exception as e:
        return {
            "command": command,
            "error": str(e),
        }


# ─── 扩展工厂 ───

def create_code_tools_extension() -> Extension:
    ext = Extension(
        id="core-code-tools",
        name="Code Tools",
        version="0.4.0-alpha.1",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.register_tool(
            ToolDefinition(
                id="read_file",
                name="read_file",
                description=(
                    "Read the contents of a text file. Supports line-range slicing via offset and limit. "
                    f"Output is truncated to {DEFAULT_MAX_LINES} lines or {format_size(DEFAULT_MAX_BYTES)} "
                    "(whichever is hit first). Use offset/limit for large files. "
                    "When you need the full file, continue with offset until complete."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative or absolute file path"},
                        "offset": {"type": "integer", "description": "Starting line number (1-based). Default 1.", "default": 1},
                        "limit": {"type": "integer", "description": "Maximum number of lines to read. 0 = unlimited.", "default": 0},
                    },
                    "required": ["path"],
                },
                input_model=ReadFileInput,
            ),
            _tool_read_file,
        )

        ctx.register_tool(
            ToolDefinition(
                id="write_file",
                name="write_file",
                description="Write (overwrite) content to a file. Parent directories are created automatically.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative or absolute file path"},
                        "content": {"type": "string", "description": "Full file content to write"},
                    },
                    "required": ["path", "content"],
                },
                input_model=WriteFileInput,
            ),
            _tool_write_file,
        )

        ctx.register_tool(
            ToolDefinition(
                id="edit_file",
                name="edit_file",
                description="Edit a file by replacing a unique old_string with new_string. "
                            "If old_string appears multiple times, the edit is rejected to avoid ambiguity.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative or absolute file path"},
                        "old_string": {"type": "string", "description": "Exact text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
                input_model=EditFileInput,
            ),
            _tool_edit_file,
        )

        ctx.register_tool(
            ToolDefinition(
                id="bash",
                name="bash",
                description=(
                    "Execute a shell command. Output keeps the tail and is truncated to "
                    f"{BASH_MAX_LINES} lines or {format_size(BASH_MAX_BYTES)} (whichever is hit first). "
                    "⚠️ Actual execution is guarded by the tool_guard extension; dangerous commands may "
                    "be blocked depending on system policy."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60},
                        "description": {"type": "string", "description": "Optional human-readable description of what the command does"},
                    },
                    "required": ["command"],
                },
                input_model=BashInput,
            ),
            _tool_bash,
        )

    ext.on_activate(activate)
    return ext
