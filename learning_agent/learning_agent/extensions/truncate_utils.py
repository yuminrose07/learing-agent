"""
大文件处理相关的通用截断工具。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

DEFAULT_MAX_LINES = 500
DEFAULT_MAX_BYTES = 32 * 1024
GREP_MAX_LINE_LENGTH = 500
GREP_DEFAULT_LIMIT = 50
BASH_MAX_LINES = DEFAULT_MAX_LINES
BASH_MAX_BYTES = DEFAULT_MAX_BYTES


@dataclass
class TruncationResult:
    """描述一次截断的结果。"""

    content: str
    truncated: bool
    truncated_by: Optional[Literal["lines", "bytes"]]
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def _effective_limit(limit: int) -> int:
    """将 0/负数 视为无限制。"""

    return limit if limit and limit > 0 else 2**63 - 1


def _split_text_lines(content: str) -> list[str]:
    """按逻辑行切分，空文本返回空列表。"""

    return content.splitlines() if content else []


def _truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节限制保留字符串尾部。"""

    if max_bytes <= 0:
        return ""

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    truncated = encoded[-max_bytes:]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[1:]
    return ""


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """保留文本头部，在行数/字节数命中上限时截断。"""

    effective_max_lines = _effective_limit(max_lines)
    effective_max_bytes = _effective_limit(max_bytes)
    total_bytes = len(content.encode("utf-8"))
    lines = _split_text_lines(content)
    total_lines = len(lines)

    if total_lines <= effective_max_lines and total_bytes <= effective_max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    if lines and len(lines[0].encode("utf-8")) > effective_max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines_arr: list[str] = []
    output_bytes_count = 0
    truncated_by: Literal["lines", "bytes"] = "lines"

    for index, line in enumerate(lines):
        if index >= effective_max_lines:
            truncated_by = "lines"
            break

        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if output_bytes_count + line_bytes > effective_max_bytes:
            truncated_by = "bytes"
            break

        output_lines_arr.append(line)
        output_bytes_count += line_bytes

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines_arr),
        output_bytes=final_output_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    max_lines: int = BASH_MAX_LINES,
    max_bytes: int = BASH_MAX_BYTES,
) -> TruncationResult:
    """保留文本尾部，在行数/字节数命中上限时截断。"""

    effective_max_lines = _effective_limit(max_lines)
    effective_max_bytes = _effective_limit(max_bytes)
    total_bytes = len(content.encode("utf-8"))
    lines = _split_text_lines(content)
    total_lines = len(lines)

    if total_lines <= effective_max_lines and total_bytes <= effective_max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines_arr: list[str] = []
    output_bytes_count = 0
    truncated_by: Literal["lines", "bytes"] = "lines"
    last_line_partial = False

    for index in range(len(lines) - 1, -1, -1):
        if len(output_lines_arr) >= effective_max_lines:
            truncated_by = "lines"
            break

        line = lines[index]
        line_bytes = len(line.encode("utf-8")) + (1 if output_lines_arr else 0)

        if output_bytes_count + line_bytes > effective_max_bytes:
            truncated_by = "bytes"
            if not output_lines_arr:
                truncated_line = _truncate_string_to_bytes_from_end(line, effective_max_bytes)
                output_lines_arr.insert(0, truncated_line)
                output_bytes_count = len(truncated_line.encode("utf-8"))
                last_line_partial = True
            break

        output_lines_arr.insert(0, line)
        output_bytes_count += line_bytes

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines_arr),
        output_bytes=final_output_bytes,
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """截断单行长文本。"""

    if max_chars <= 0 or len(line) <= max_chars:
        return line, False
    return f"{line[:max_chars]}... [truncated]", True


def format_size(bytes_count: int) -> str:
    """将字节数格式化为更易读的字符串。"""

    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    return f"{bytes_count / (1024 * 1024):.1f}MB"
