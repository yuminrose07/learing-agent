"""
grep 工具扩展：为大文件场景提供先搜索再精读的导航能力。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from learning_agent.ai import ToolDefinition
from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.learning_agent.extensions.code_tools import _resolve_and_guard_path
from learning_agent.learning_agent.extensions.truncate_utils import (
    DEFAULT_MAX_BYTES,
    GREP_DEFAULT_LIMIT,
    GREP_MAX_LINE_LENGTH,
    format_size,
    truncate_head,
    truncate_line,
)

logger = logging.getLogger(__name__)


class GrepInput(BaseModel):
    pattern: str = Field(description="Search pattern (regex or literal string)")
    path: str = Field(default=".", description="Directory or file to search")
    glob: Optional[str] = Field(default=None, description="Optional glob filter such as '*.md' or '**/*.py'")
    ignore_case: bool = Field(default=False, description="Case-insensitive search")
    literal: bool = Field(default=False, description="Treat pattern as a literal string")
    context: int = Field(default=0, description="Number of lines to show before and after each match")
    limit: int = Field(default=GREP_DEFAULT_LIMIT, description="Maximum number of matches to return")


def _match_glob(file_path: Path, base_path: Path, glob_pattern: Optional[str]) -> bool:
    if not glob_pattern:
        return True

    relative = file_path.relative_to(base_path).as_posix()
    return fnmatch.fnmatch(relative, glob_pattern) or fnmatch.fnmatch(file_path.name, glob_pattern)


async def _tool_grep(
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    ignore_case: bool = False,
    literal: bool = False,
    context: int = 0,
    limit: int = GREP_DEFAULT_LIMIT,
    **kwargs: Any,
) -> dict[str, Any]:
    """搜索文件内容，返回带行号的匹配结果。"""

    search_path = _resolve_and_guard_path(path, allow_write=False)
    match_limit = limit if limit > 0 else GREP_DEFAULT_LIMIT
    context_lines = max(0, context)

    try:
        flags = re.IGNORECASE if ignore_case else 0
        compiled = re.compile(re.escape(pattern) if literal else pattern, flags)
    except re.error as exc:
        return {"error": f"Invalid grep pattern: {exc}"}

    files: list[Path] = []
    if search_path.is_file():
        files = [search_path]
        base_path = search_path.parent
    elif search_path.is_dir():
        base_path = search_path
        for root, _, filenames in os.walk(search_path):
            for filename in filenames:
                file_path = Path(root) / filename
                if _match_glob(file_path, base_path, glob):
                    files.append(file_path)
    else:
        return {"error": f"Path not found: {path}"}

    cwd = Path.cwd()
    matches: list[str] = []
    for file_path in files:
        if len(matches) >= match_limit:
            break

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except (UnicodeDecodeError, OSError):
            continue

        for index, line in enumerate(lines):
            if len(matches) >= match_limit:
                break
            if not compiled.search(line):
                continue

            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            relative_path = file_path.relative_to(cwd)
            match_lines: list[str] = []

            for context_index in range(start, end):
                line_num = context_index + 1
                line_text = lines[context_index].rstrip("\n")
                truncated_text, _ = truncate_line(line_text, max_chars=GREP_MAX_LINE_LENGTH)
                if context_index == index:
                    match_lines.append(f"{relative_path}:{line_num}: {truncated_text}")
                else:
                    match_lines.append(f"{relative_path}:{line_num}- {truncated_text}")

            matches.append("\n".join(match_lines))

    output = "\n\n".join(matches) if matches else "No matches found"
    truncation = truncate_head(output, max_lines=0, max_bytes=DEFAULT_MAX_BYTES)

    notices: list[str] = []
    if len(matches) >= match_limit:
        notices.append(
            f"{match_limit} matches limit reached. Refine pattern, reduce context, or increase limit."
        )
    if truncation.truncated:
        notices.append(
            f"{format_size(DEFAULT_MAX_BYTES)} output limit reached. Narrow the path or pattern."
        )

    rendered_output = truncation.content
    if notices:
        notice_text = f"[{' '.join(notices)}]"
        rendered_output = f"{rendered_output}\n\n{notice_text}" if rendered_output else notice_text

    return {
        "matches": len(matches),
        "limit_reached": len(matches) >= match_limit,
        "truncated": truncation.truncated,
        "output": rendered_output,
    }


def create_grep_tools_extension() -> Extension:
    ext = Extension(
        id="core-grep-tools",
        name="Grep Tools",
        version="0.4.0-alpha.1",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        ctx.register_tool(
            ToolDefinition(
                id="grep",
                name="grep",
                description=(
                    "Search file contents for a pattern. Returns matching lines with file paths and "
                    "line numbers. Supports regex and literal matching. Use this to locate specific "
                    f"content before reading with read_file. Output is limited to {GREP_DEFAULT_LIMIT} "
                    f"matches or {format_size(DEFAULT_MAX_BYTES)} (whichever is hit first). "
                    f"Long lines are truncated to {GREP_MAX_LINE_LENGTH} characters."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern (regex or literal string)"},
                        "path": {"type": "string", "description": "Directory or file to search", "default": "."},
                        "glob": {"type": "string", "description": "Filter files by glob, e.g. '*.md' or '**/*.py'"},
                        "ignore_case": {"type": "boolean", "description": "Case-insensitive search", "default": False},
                        "literal": {"type": "boolean", "description": "Treat pattern as a literal string", "default": False},
                        "context": {"type": "integer", "description": "Number of context lines before and after each match", "default": 0},
                        "limit": {"type": "integer", "description": "Maximum number of matches to return", "default": GREP_DEFAULT_LIMIT},
                    },
                    "required": ["pattern"],
                },
                input_model=GrepInput,
            ),
            _tool_grep,
        )

    ext.on_activate(activate)
    return ext
