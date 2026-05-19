# Learning-Agent 大文件处理技术方案

> 基于 pi (Claude Code 参考实现) 的截断策略分析，结合学习场景需求设计。

---

## 目录

- [一、背景与问题分析](#一背景与问题分析)
- [二、参考实现：pi 的策略分析](#二参考实现pi-的策略分析)
- [三、技术方案总览](#三技术方案总览)
- [四、截断引擎设计](#四截断引擎设计)
- [五、read_file 工具改进](#五read_file-工具改进)
- [六、grep 工具设计](#六grep-工具设计)
- [七、bash 工具改进](#七bash-工具改进)
- [八、Agent Loop 层兜底](#八agent-loop-层兜底)
- [九、与现有架构集成](#九与现有架构集成)
- [十、风险评估与回退方案](#十风险评估与回退方案)
- [十一、实施计划](#十一实施计划)

---

## 一、背景与问题分析

### 1.1 当前系统的脆弱点

当前 learning-agent 的工具系统存在**四层防御缺失**：

| 层级 | 当前行为 | 风险 |
|---|---|---|
| **工具层** | `read_file` 返回完整内容；`bash` 返回完整 stdout/stderr | 读一个 1MB 的日志文件，工具直接返回 1MB 文本 |
| **Agent Loop 层** | 工具结果直接 `json.dumps` 后存入 `SessionEntry.content`，无任何截断 | 1MB 文本作为单条 TOOL 消息进入会话历史 |
| **上下文构建层** | `_build_context_for_turn` 直接把 `entry.content` 塞进 `ChatMessage` | 单条消息可能就超过 LLM 上下文限制 |
| **异常处理层** | `ContextLengthError` 直接 yield 错误并 return，没有任何补救 | 会话直接死掉 |

### 1.2 学习场景的特殊性

与 coding 场景不同，学习场景的典型文件类型：

- **教材/笔记**：Markdown、纯文本，通常几千到几万字
- **代码练习**：小型代码文件，通常几百行
- **参考资料**：PDF 转文本、网页摘录，可能较长
- **日志/数据**：学习数据分析时可能遇到

**核心矛盾**：学习者需要"理解全文"（比如读一篇论文、一章教材），但 LLM 上下文有限，无法一次性放入全文。

### 1.3 根本问题：文件内容 > LLM 上下文容量

当文件本身超过 LLM 上下文容量时，**任何"把文件内容放进 prompt"的方案都不可行**。此时必须：

1. **让 LLM 自己决定需要哪些部分**（分页读取、搜索定位）
2. **或预处理文件，只给 LLM 关键信息**（摘要、索引、结构化提取）

pi 采用的是策略 1（工具层截断 + 分页提示 + grep 搜索），本方案在此基础上针对学习场景做适配。

---

## 二、参考实现：pi 的策略分析

### 2.1 pi 的四层防御

```
┌─────────────────────────────────────────┐
│  第1层：工具层截断（最前置）              │
│  read / grep / bash 返回时主动截断        │
│  "内容太长，只给你看前N行/前N字符"        │
├─────────────────────────────────────────┤
│  第2层：Agent Loop 层（不截断）           │
│  pi 的 Agent Loop 完全信任工具层          │
│  因为工具层截断能给出可恢复的用户提示     │
├─────────────────────────────────────────┤
│  第3层：上下文构建层（依赖扩展）            │
│  context_compressor 扩展从旧消息丢弃      │
│  但无法处理单条消息就超限的情况           │
├─────────────────────────────────────────┤
│  第4层：ContextLengthError（当前无补救）   │
│  直接报错退出                             │
└─────────────────────────────────────────┘
```

### 2.2 pi 的关键设计决策

**决策 1：截断放在工具层，而非 Agent Loop 层**

原因：
- 工具层知道输出格式，能给出精确的、可恢复的用户提示
- Agent Loop 层截断 JSON/字符串会让 LLM 看到不完整的结构，导致困惑
- 每个工具对自己的输出最了解，知道哪里该切

**决策 2：双独立限制（行数 + 字节数）**

```typescript
DEFAULT_MAX_LINES = 2000;
DEFAULT_MAX_BYTES = 50 * 1024; // 50KB
```

哪个先触发就用哪个。这样避免了：
- 只限行数 → 一行 10MB 的 JSON 照样爆炸
- 只限字节 → 2000 行空行只占 2KB，浪费上下文

**决策 3：方向性截断**

- `read_file` → `truncateHead`（保留开头）：文件开头通常最重要（导入、定义、概述）
- `bash` → `truncateTail`（保留尾部）：错误和最终结果通常在最后

**决策 4：被截断时必须给 LLM 可恢复提示**

```
[Showing lines 1-2000 of 5000. Use offset=2001 to continue.]
[Showing lines 1801-2000 of 5000. Full output: /tmp/pi-bash-a1b2c3d4.log]
```

**决策 5：grep 作为大文件的"导航工具"**

pi 把 grep 定位为"先搜索、再精读"的导航工具：
- LLM 先用 grep 定位关键词所在的行号
- 再用 `read_file offset=X limit=Y` 精确读取相关段落
- 避免"读全文→被截断→不知道后面有什么"的困境

### 2.3 pi 的 OutputAccumulator（bash 的流式截断）

pi 的 bash 工具不是等命令跑完再截断，而是**边跑边控制内存**：

```
接收 Buffer 流 → 解码为文本 → 只保留尾部滚动缓冲区(100KB)
                                ↓
              总输出超过阈值 → 自动 spill 到临时文件
                                ↓
              命令结束 → 对尾部缓冲区做 truncateTail()
                                ↓
              返回截断内容 + 临时文件路径（如果需要全文）
```

**内存有界**：无论命令输出多少 GB，内存中最多只保留 100KB 的尾部缓冲区。

---

## 三、技术方案总览

### 3.1 目标

1. **防止单条工具结果把上下文撑爆**
2. **让 LLM 能够处理远超上下文容量的文件**（通过搜索+分页）
3. **会话不因 ContextLengthError 直接死掉**

### 3.2 方案架构

```
┌──────────────────────────────────────────────┐
│           新增工具：grep                       │
│  作用：在文件中搜索模式，返回匹配行+行号        │
│  解决："不知道文件里有什么，不敢读"的问题       │
└──────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│           改进工具：read_file                  │
│  新增：truncate_head 截断 + 可恢复提示          │
│  解决：单条消息过大导致上下文爆炸               │
└──────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│           改进工具：bash                       │
│  新增：truncate_tail 截断 + 可恢复提示          │
│  解决：命令输出过大导致上下文爆炸               │
└──────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│           新增模块：truncate_utils.py          │
│  作用：通用截断引擎，被各工具复用               │
│  解决：避免各工具重复实现截断逻辑               │
└──────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│           Agent Loop 层兜底                     │
│  新增：ContextLengthError 时截断最长 TOOL 消息   │
│  解决：前面所有防线都失效时的最后一道保险       │
└──────────────────────────────────────────────┘
```

### 3.3 学习场景的阈值调整

pi 的默认值针对 coding 场景（2000 行 / 50KB）。学习场景建议更保守：

| 参数 | pi (coding) | 本方案 (learning) | 理由 |
|---|---|---|---|
| `DEFAULT_MAX_LINES` | 2000 | **500** | 学习对话轮次更长，需要给历史留更多空间 |
| `DEFAULT_MAX_BYTES` | 50KB | **32KB** | 学习材料通常是自然语言，token 密度更高 |
| `GREP_MAX_LINE_LENGTH` | 500 | **500** | 保持一致，单行 500 字符足够 |
| `GREP_DEFAULT_LIMIT` | 100 | **50** | 学习场景搜索结果更精简 |
| `BASH_MAX_LINES` | 2000 | **500** | 同上 |
| `BASH_MAX_BYTES` | 50KB | **32KB** | 同上 |

---

## 四、截断引擎设计

### 4.1 模块位置

```
learning_agent/
└── learning_agent/
    └── extensions/
        └── truncate_utils.py    # 新增
```

### 4.2 数据模型

```python
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class TruncationResult:
    """截断结果。"""
    content: str                      # 截断后的内容
    truncated: bool                   # 是否发生了截断
    truncated_by: Optional[Literal["lines", "bytes"]]  # 哪个限制先触发
    total_lines: int                  # 原始总行数
    total_bytes: int                  # 原始总字节数
    output_lines: int                 # 输出多少行
    output_bytes: int                 # 输出多少字节
    last_line_partial: bool           # 最后一行是否被部分截断（仅 tail）
    first_line_exceeds_limit: bool    # 第一行是否超过字节限制（仅 head）
    max_lines: int                    # 应用的行数限制
    max_bytes: int                    # 应用的字节数限制
```

### 4.3 truncate_head（保留头部）

**适用场景**：`read_file`、`grep`

**算法**：

```python
def truncate_head(content: str, max_lines: int = 500, max_bytes: int = 32 * 1024) -> TruncationResult:
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    # 无需截断
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content, truncated=False, truncated_by=None,
            total_lines=total_lines, total_bytes=total_bytes,
            output_lines=total_lines, output_bytes=total_bytes,
            last_line_partial=False, first_line_exceeds_limit=False,
            max_lines=max_lines, max_bytes=max_bytes,
        )

    # 检查第一行是否超过字节限制
    first_line_bytes = len(lines[0].encode("utf-8"))
    if first_line_bytes > max_bytes:
        return TruncationResult(
            content="", truncated=True, truncated_by="bytes",
            total_lines=total_lines, total_bytes=total_bytes,
            output_lines=0, output_bytes=0,
            last_line_partial=False, first_line_exceeds_limit=True,
            max_lines=max_lines, max_bytes=max_bytes,
        )

    # 逐行收集，直到触及限制
    output_lines_arr = []
    output_bytes_count = 0
    truncated_by = "lines"

    for i, line in enumerate(lines):
        if i >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)  # +1 for newline
        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines_arr.append(line)
        output_bytes_count += line_bytes

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content, truncated=True, truncated_by=truncated_by,
        total_lines=total_lines, total_bytes=total_bytes,
        output_lines=len(output_lines_arr), output_bytes=final_output_bytes,
        last_line_partial=False, first_line_exceeds_limit=False,
        max_lines=max_lines, max_bytes=max_bytes,
    )
```

### 4.4 truncate_tail（保留尾部）

**适用场景**：`bash`

**算法**：

```python
def truncate_tail(content: str, max_lines: int = 500, max_bytes: int = 32 * 1024) -> TruncationResult:
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    # 无需截断
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content, truncated=False, truncated_by=None,
            total_lines=total_lines, total_bytes=total_bytes,
            output_lines=total_lines, output_bytes=total_bytes,
            last_line_partial=False, first_line_exceeds_limit=False,
            max_lines=max_lines, max_bytes=max_bytes,
        )

    # 从尾部向前收集
    output_lines_arr = []
    output_bytes_count = 0
    truncated_by = "lines"
    last_line_partial = False

    for i in range(len(lines) - 1, -1, -1):
        if len(output_lines_arr) >= max_lines:
            truncated_by = "lines"
            break
        line = lines[i]
        line_bytes = len(line.encode("utf-8")) + (1 if output_lines_arr else 0)

        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            # Edge case: 如果还没加任何行，且这一行就超限制，取行的尾部
            if not output_lines_arr:
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines_arr.insert(0, truncated_line)
                output_bytes_count = len(truncated_line.encode("utf-8"))
                last_line_partial = True
            break

        output_lines_arr.insert(0, line)
        output_bytes_count += line_bytes

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content, truncated=True, truncated_by=truncated_by,
        total_lines=total_lines, total_bytes=total_bytes,
        output_lines=len(output_lines_arr), output_bytes=final_output_bytes,
        last_line_partial=last_line_partial, first_line_exceeds_limit=False,
        max_lines=max_lines, max_bytes=max_bytes,
    )
```

### 4.5 truncate_line（单行截断）

**适用场景**：`grep` 的匹配行

```python
def truncate_line(line: str, max_chars: int = 500) -> tuple[str, bool]:
    """截断单行，超过 max_chars 时加 [truncated] 后缀。"""
    if len(line) <= max_chars:
        return line, False
    return f"{line[:max_chars]}... [truncated]", True
```

### 4.6 format_size（人类可读大小）

```python
def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count}B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    else:
        return f"{bytes_count / (1024 * 1024):.1f}MB"
```

---

## 五、read_file 工具改进

### 5.1 当前问题

当前 `read_file`：
- 没有截断逻辑
- `offset`/`limit` 是用户主动指定的，如果用户不指定就读全文
- 大文件直接全量返回，进入历史后撑爆上下文

### 5.2 改进后的执行流程

```
用户调用 read_file(path, offset, limit)
    ↓
读取文件内容
    ↓
按用户 offset/limit 切片（如果用户指定了）
    ↓
对切片后的内容调用 truncate_head()
    ↓
根据截断情况返回不同的提示
```

### 5.3 四种返回情况

| 情况 | 返回内容 | 说明 |
|---|---|---|
| **无需截断** | 完整内容 | 文件在限制范围内 |
| **第一行超限制** | `[Line X is 120KB, exceeds 32KB limit. Use bash: sed -n 'Xp' file \| head -c 32768]` | 单行就超限，引导用 bash 读取 |
| **按行截断** | 前 N 行 + `[Showing lines X-Y of Z. Use offset={next} to continue.]` | 行数限制先触发 |
| **按字节截断** | 前 N 行 + `[Showing lines X-Y of Z (32KB limit). Use offset={next} to continue.]` | 字节限制先触发 |
| **用户 limit 已到但文件还有** | 内容 + `[{remaining} more lines. Use offset={next} to continue.]` | 用户主动限制，但文件有剩余 |

### 5.4 工具描述更新

```python
description = (
    "Read the contents of a text file. Supports line-range slicing via offset and limit. "
    "Output is truncated to 500 lines or 32KB (whichever is hit first). "
    "Use offset/limit for large files. When you need the full file, continue with offset until complete."
)
```

**关键**：在工具描述里明确告诉 LLM 有截断行为，这样 LLM 会主动使用 `offset` 分页读取大文件。

### 5.5 返回结构

```python
{
    "path": "relative/path",
    "content": "truncated or full content",
    "total_lines": 5000,
    "shown_lines": 500,
    "offset": 1,
    "truncated": True,
    "truncated_by": "lines",  # or "bytes"
    "next_offset": 501,  # 如果截断了，提示下一段从哪里开始
}
```

### 5.6 与 pi 的差异

| 差异点 | pi | 本方案 |
|---|---|---|
| 默认行数限制 | 2000 | 500 |
| 默认字节限制 | 50KB | 32KB |
| 第一行超限时 | 提示用 bash sed/head | 同上 |
| 图像处理 | 自动 resize 到 2000x2000 | 当前不支持图像，暂不考虑 |

---

## 六、grep 工具设计

### 6.1 为什么需要 grep

grep 是解决大文件问题的**关键导航工具**：

```
场景：在 5000 行教材中找一个概念

没有 grep：
  read_file → 被截断到前 500 行 → 概念在后面没看到
  → LLM 问"请继续" → read_file offset=501 → 又被截断
  → 可能需要读 10 轮才能找到概念

有 grep：
  grep "概念名" → 返回 "materials/chapter3.md:847: The concept of..."
  → read_file offset=840 limit=20 → 直接读到相关段落
  → 1 轮搞定
```

### 6.2 参数设计

```python
{
    "id": "grep",
    "name": "grep",
    "description": (
        "Search file contents for a pattern. Returns matching lines with file paths and line numbers. "
        "Supports regex and literal string matching. "
        "Use this to locate specific content before reading with read_file. "
        "Output is limited to 50 matches or 32KB (whichever is hit first). "
        "Long lines are truncated to 500 chars."
    ),
    "parameters": {
        "pattern": {
            "type": "string",
            "description": "Search pattern (regex or literal string)"
        },
        "path": {
            "type": "string",
            "description": "Directory or file to search (default: current directory)"
        },
        "glob": {
            "type": "string",
            "description": "Filter files by glob pattern, e.g. '*.md' or '**/*.py'"
        },
        "ignore_case": {
            "type": "boolean",
            "description": "Case-insensitive search (default: false)"
        },
        "literal": {
            "type": "boolean",
            "description": "Treat pattern as literal string instead of regex (default: false)"
        },
        "context": {
            "type": "integer",
            "description": "Number of lines to show before and after each match (default: 0)"
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of matches to return (default: 50)"
        }
    }
}
```

### 6.3 实现方式：纯 Python

**不依赖外部 `rg` 命令**，原因：
1. 学习场景文件量不大，Python `re` + `os.walk` 性能足够
2. 零依赖，开箱即用
3. 跨平台，不需要用户安装 ripgrep

**实现逻辑**：

```python
import os
import re
import fnmatch
from pathlib import Path

async def _tool_grep(
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    ignore_case: bool = False,
    literal: bool = False,
    context: int = 0,
    limit: int = 50,
    **kwargs: Any
) -> dict[str, Any]:
    # 1. 解析搜索路径
    cwd = Path.cwd()
    search_path = cwd / path
    
    # 2. 编译正则
    flags = re.IGNORECASE if ignore_case else 0
    if literal:
        regex = re.compile(re.escape(pattern), flags)
    else:
        regex = re.compile(pattern, flags)
    
    # 3. 收集文件列表
    files = []
    if search_path.is_file():
        files = [search_path]
    elif search_path.is_dir():
        for root, _, filenames in os.walk(search_path):
            for filename in filenames:
                if glob and not fnmatch.fnmatch(filename, glob):
                    continue
                files.append(Path(root) / filename)
    
    # 4. 逐文件搜索
    matches = []
    for file_path in files:
        if len(matches) >= limit:
            break
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (UnicodeDecodeError, OSError):
            continue
        
        for i, line in enumerate(lines, 1):
            if len(matches) >= limit:
                break
            if regex.search(line):
                # 收集上下文
                start = max(0, i - 1 - context)
                end = min(len(lines), i + context)
                match_lines = []
                for j in range(start, end):
                    line_num = j + 1
                    line_text = lines[j].rstrip("\n")
                    # 截断长行
                    truncated_text, was_truncated = truncate_line(line_text)
                    prefix = f"{file_path.relative_to(cwd)}:{line_num}:"
                    if j + 1 == i:
                        match_lines.append(f"{prefix} {truncated_text}")
                    else:
                        match_lines.append(f"{prefix}- {truncated_text}")
                matches.append("\n".join(match_lines))
    
    # 5. 组装输出
    output = "\n".join(matches) if matches else "No matches found"
    
    # 6. 截断总输出
    truncation = truncate_head(output, max_lines=float('inf'))  # 只限字节
    
    notices = []
    if len(matches) >= limit:
        notices.append(f"{limit} matches limit reached. Refine pattern or use limit={limit * 2}")
    if truncation.truncated:
        notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
    
    result = truncation.content
    if notices:
        result += f"\n\n[{'. '.join(notices)}]"
    
    return {
        "matches": len(matches),
        "limit_reached": len(matches) >= limit,
        "output": result,
    }
```

### 6.4 输出格式

```
materials/physics.md:87: The concept of entropy is defined as...
materials/physics.md:88- In statistical mechanics, entropy measures...
materials/physics.md:89- The second law of thermodynamics states...

materials/chemistry.md:245: Entropy also plays a role in chemical...

[50 matches limit reached. Refine pattern or use limit=100]
```

### 6.5 与 pi 的差异

| 差异点 | pi | 本方案 |
|---|---|---|
| 实现方式 | 调用系统 `rg` 命令 | Python `re` + `os.walk` 纯原生 |
| .gitignore 过滤 | `rg` 自动处理 | 暂不支持（未来可扩展） |
| 性能 | 极快（C 实现） | 够用（学习场景文件量小） |
| 依赖 | 需要安装 ripgrep | 零依赖 |
| 默认匹配限制 | 100 | 50 |

---

## 七、bash 工具改进

### 7.1 当前问题

当前 `bash` 工具：
- 返回完整的 stdout + stderr
- 没有输出长度限制
- 命令输出大时直接进入历史，撑爆上下文

### 7.2 改进方案

虽然 `bash` 默认已 `disabled`，但用户开启时也需要保护。

**简化版实现**（不需要 pi 的 OutputAccumulator 流式截断）：

```python
async def _tool_bash(command: str, timeout: int = 60, description: str = "", **kwargs: Any) -> dict[str, Any]:
    # ... 执行命令 ...
    stdout = ...
    stderr = ...
    
    # 合并输出
    combined = f"{stdout}\n{stderr}".strip()
    
    # 截断尾部（bash 保留尾部，因为错误通常在最后）
    truncation = truncate_tail(combined)
    
    result = truncation.content
    if truncation.truncated:
        start_line = truncation.total_lines - truncation.output_lines + 1
        end_line = truncation.total_lines
        if truncation.last_line_partial:
            result += f"\n\n[Showing last {format_size(truncation.output_bytes)} of line {end_line}. Use file operations to inspect full output.]"
        elif truncation.truncated_by == "lines":
            result += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. Use file operations to inspect full output.]"
        else:
            result += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). Use file operations to inspect full output.]"
    
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": result,
        "stderr": "",
        "truncated": truncation.truncated,
    }
```

### 7.3 与 pi 的差异

pi 的 bash 截断更复杂（OutputAccumulator 流式处理 + 临时文件）。我们的简化版：
- 命令结束后一次性截断
- 不保存临时文件（学习场景命令输出通常不大）
- 如果用户需要全文，引导用 `read_file` 读取输出重定向的文件

**未来可扩展**：如果学习场景确实需要处理超大命令输出（比如数据分析），再引入 OutputAccumulator 和临时文件机制。

---

## 八、Agent Loop 层兜底

### 8.1 当前问题

当 `ContextLengthError` 发生时：

```python
except ContextLengthError as e:
    # 上下文压缩已从核心层移除，交由 BEFORE_CONTEXT_BUILD hook 或扩展层实现
    yield ChatChunk(content=f"\n[Error] Context length exceeded: {e}\n")
    return
```

直接报错退出，没有任何补救。

### 8.2 兜底方案：截断最长 TOOL 消息后重试

```python
except ContextLengthError as e:
    logger.warning(f"[AgentLoop] Context length exceeded, attempting emergency truncation")
    
    # 1. 找到历史中最长的 TOOL 消息
    history = self.sessions.get_message_history(session.id)
    longest_tool_entry = None
    longest_tool_length = 0
    for entry in history:
        if entry.role == MessageRole.TOOL and len(entry.content) > longest_tool_length:
            longest_tool_length = len(entry.content)
            longest_tool_entry = entry
    
    if longest_tool_entry and longest_tool_length > 1000:
        # 2. 截断这条消息到 1000 字符
        original = longest_tool_entry.content
        truncated = original[:1000] + "\n\n[Content truncated due to context limit]"
        longest_tool_entry.content = truncated
        logger.info(f"[AgentLoop] Emergency truncated TOOL message from {len(original)} to {len(truncated)} chars")
        
        # 3. 重试（消耗一次重试次数）
        if attempt < max_attempts:
            continue
    
    # 4. 如果截断后还是不行，降级到 chat-only
    yield ChatChunk(content="\n[Context limit reached. Switching to chat-only mode for this turn.]\n")
    self._chat_only_mode = True
    return
```

### 8.3 设计原则

- **只截断 TOOL 消息**：USER/ASSISTANT/SYSTEM 消息优先级更高
- **保留消息存在性**：不删除消息，只截断内容，避免历史结构破坏
- **告知用户**：让用户知道发生了什么，而不是静默截断
- **有上限**：最多尝试 1 次紧急截断，避免无限循环

---

## 九、与现有架构集成

### 9.1 文件变更清单

| 文件 | 操作 | 说明 |
|---|---|---|
| `learning_agent/learning_agent/extensions/truncate_utils.py` | 新增 | 通用截断引擎 |
| `learning_agent/learning_agent/extensions/code_tools.py` | 修改 | read_file + bash 增加截断 |
| `learning_agent/learning_agent/extensions/grep_tools.py` | 新增 | grep 工具 |
| `learning_agent/learning_agent/extensions/built_in.py` | 修改 | 注册 grep 扩展 |
| `learning_agent/agent/agent_loop.py` | 修改 | ContextLengthError 兜底 |
| `learning_agent/ai/models.py` | 可能修改 | 如需要新增 TruncationResult 模型 |

### 9.2 扩展注册

在 `built_in.py` 的 `create_builtin_extensions` 中注册 grep：

```python
def create_builtin_extensions(config: dict | None = None) -> list[Extension]:
    return [
        _create_observability_extension(),
        _create_fulltrace_extension(),
        create_code_tools_extension(),
        create_grep_tools_extension(),  # 新增
        create_tool_guard_extension(config),
        create_security_audit_extension(config),
    ]
```

### 9.3 配置项

建议新增配置（可选）：

```yaml
tool_truncation:
  max_lines: 500        # 默认行数限制
  max_bytes: 32768      # 默认字节限制 (32KB)
  grep_max_line_length: 500  # grep 单行限制
  grep_match_limit: 50  # grep 匹配数限制
```

---

## 十、风险评估与回退方案

### 10.1 风险

| 风险 | 可能性 | 影响 | 缓解措施 |
|---|---|---|---|
| LLM 不理解截断提示，反复读取同一页 | 中 | 浪费 token | 截断提示中明确标注 `next_offset`，工具描述中教育 LLM |
| grep 纯 Python 实现性能不足 | 低 | 搜索慢 | 学习场景文件量小；未来可 fallback 到 `rg` |
| 截断导致 LLM 看到不完整的 JSON/代码 | 中 | 误解 | 截断在换行符处，不截断半行；代码场景保留语法完整性 |
| ContextLengthError 兜底截断后仍失败 | 低 | 会话降级 | 兜底失败时自动切换到 chat-only 模式 |

### 10.2 回退方案

如果新方案导致问题，可以：

1. **关闭截断**：在配置中设 `tool_truncation.max_lines = 0`（0 表示不限制）
2. **移除 grep**：从 `built_in.py` 的扩展列表中注释掉 `create_grep_tools_extension()`
3. **回退 Agent Loop**：恢复 `ContextLengthError` 的原始处理逻辑

---

## 十一、实施计划

### Phase 1：截断引擎 + read_file 改进（优先级：高）

1. 实现 `truncate_utils.py`
2. 修改 `read_file` 增加截断逻辑
3. 运行测试验证

### Phase 2：grep 工具（优先级：高）

1. 实现 `grep_tools.py`
2. 在 `built_in.py` 注册
3. 测试搜索功能

### Phase 3：bash 改进 + Agent Loop 兜底（优先级：中）

1. 修改 `bash` 增加截断
2. 修改 `agent_loop.py` 增加 ContextLengthError 兜底
3. 集成测试

### Phase 4：配置项与调优（优先级：低）

1. 增加 `tool_truncation` 配置项
2. 根据实际使用调整阈值

---

## 附录：与 pi 的对比总结

| 维度 | pi (coding) | 本方案 (learning) |
|---|---|---|
| **截断位置** | 工具层 | 工具层 |
| **限制维度** | 行数 + 字节数 | 行数 + 字节数 |
| **read 截断方向** | 保头 (truncateHead) | 保头 |
| **bash 截断方向** | 保尾 + OutputAccumulator 流式 | 保尾（简化版，命令后截断） |
| **grep 实现** | 系统 `rg` 命令 | Python `re` + `os.walk` |
| **默认阈值** | 2000行/50KB | 500行/32KB |
| **临时文件** | bash 截断时保存 | 暂不支持 |
| **ContextLengthError 兜底** | 依赖扩展层压缩 | Agent Loop 层紧急截断 |
| **图像处理** | 自动 resize 到 2000x2000 | 暂不支持 |

---

*报告完成。如需调整任何章节的深度或优先级，请告知。*
