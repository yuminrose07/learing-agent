"""
Context Compressor 扩展：基于 Token 预算的上下文压缩。

职责：
- 注册到 BEFORE_CONTEXT_BUILD Hook，在核心层构建 LLM 上下文前介入
- 按 Token 预算动态截断历史消息，优先保留最近的信息
- 保证 assistant-tool 对的完整性（不会出现孤儿 tool 消息）

设计原则：
- 压缩策略是可替换的：当前默认实现为 TokenBudgetCompressor
- 未来可接入 SummarizationCompressor、SemanticCompressor 等策略
- 核心层只负责协议完整性，扩展层负责策略选择
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from learning_agent.learning_agent.extension_manager import Extension, ExtensionContext
from learning_agent.agent.hook_system import HookPoint
from learning_agent.ai import HookResult, MessageRole, SessionEntry

logger = logging.getLogger(__name__)

# ───────────────────────────────
# Token 估算
# ───────────────────────────────

_TIKTOKEN_AVAILABLE = False
try:
    import tiktoken

    _TIKTOKEN_AVAILABLE = True
except ImportError:
    pass


def _estimate_tokens(entry: SessionEntry) -> int:
    """
    粗略估算一条 SessionEntry 的 token 数。
    优先使用 tiktoken（如果安装），否则退化为字符数/4 的启发式估算。
    """
    if _TIKTOKEN_AVAILABLE:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            text = entry.content or ""
            # 将 tool_calls 的 JSON 表示也计入
            if entry.tool_calls:
                import json

                for tc in entry.tool_calls:
                    text += json.dumps(
                        {
                            "id": tc.call_id or "",
                            "type": "function",
                            "function": {
                                "name": tc.tool_id,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                                if tc.arguments
                                else "{}",
                            },
                        },
                        ensure_ascii=False,
                    )
            return len(enc.encode(text))
        except Exception:
            pass

    # 启发式估算：英文约 4 字符/token，中文约 1 字/token，取保守值
    base = max(len(entry.content) // 3, 1)
    overhead = 4  # role、metadata 等结构化开销
    if entry.tool_calls:
        # 每个 tool_call 额外约 30 token（id + name + arguments JSON）
        overhead += 30 * len(entry.tool_calls)
    return base + overhead


# ───────────────────────────────
# 压缩策略
# ───────────────────────────────

class TokenBudgetCompressor:
    """
    基于 Token 预算的上下文压缩器。

    策略：
    1. 将历史消息划分为 "turn units"（assistant + 紧随其后的 tool 消息视为一个 unit）
    2. 从 newest 向 oldest 累加 unit，直到触及 Token 预算上限
    3. 丢弃超预算的 unit，保证保留的 unit 内部完整性
    4. system 消息如果位于最前面，优先保留；如果预算实在不够，允许丢弃
    """

    def __init__(self, max_context_tokens: int = 8000):
        self.max_context_tokens = max_context_tokens

    def compress(self, history: list[SessionEntry]) -> list[SessionEntry]:
        if not history:
            return []

        total_tokens = sum(_estimate_tokens(e) for e in history)
        if total_tokens <= self.max_context_tokens:
            return history

        # 1. 划分为 turn units
        units: list[list[SessionEntry]] = []
        i = 0
        while i < len(history):
            entry = history[i]
            if entry.role == MessageRole.ASSISTANT and entry.tool_calls:
                unit = [entry]
                j = i + 1
                while j < len(history) and history[j].role == MessageRole.TOOL:
                    unit.append(history[j])
                    j += 1
                units.append(unit)
                i = j
            else:
                units.append([entry])
                i += 1

        # 2. 从 newest 向 oldest 保留 unit，直到预算耗尽
        kept_units: list[list[SessionEntry]] = []
        kept_tokens = 0
        for unit in reversed(units):
            unit_tokens = sum(_estimate_tokens(e) for e in unit)
            if kept_tokens + unit_tokens <= self.max_context_tokens:
                kept_tokens += unit_tokens
                kept_units.append(unit)
            else:
                # 预算不足，停止保留
                # 但有一个例外：如果当前 unit 是 system 消息且位于最旧端，
                # 并且预算刚好差一点点，可以尝试保留（可选优化，暂不实现）
                logger.debug(
                    f"[TokenBudgetCompressor] Budget exhausted at unit starting with "
                    f"role={unit[0].role.value}, tokens={unit_tokens}. "
                    f"Kept {len(kept_units)}/{len(units)} units, "
                    f"{kept_tokens}/{self.max_context_tokens} tokens."
                )
                break

        # 3. 扁平化并恢复原始顺序
        result: list[SessionEntry] = []
        for unit in reversed(kept_units):
            result.extend(unit)

        # 4. 防御性校验：确保没有孤儿 tool 消息
        valid_tool_call_ids = set()
        for entry in result:
            if entry.tool_calls:
                for tc in entry.tool_calls:
                    if tc.call_id:
                        valid_tool_call_ids.add(tc.call_id)

        filtered_result: list[SessionEntry] = []
        dropped_orphan_count = 0
        for entry in result:
            if entry.role == MessageRole.TOOL:
                tcid = entry.metadata.get("tool_call_id", "")
                if tcid and tcid not in valid_tool_call_ids:
                    dropped_orphan_count += 1
                    continue
            filtered_result.append(entry)

        if dropped_orphan_count:
            logger.warning(
                f"[TokenBudgetCompressor] Dropped {dropped_orphan_count} orphan tool messages "
                f"whose assistant tool_calls were truncated."
            )

        final_tokens = sum(_estimate_tokens(e) for e in filtered_result)
        logger.info(
            f"[TokenBudgetCompressor] Compressed {len(history)} entries ({total_tokens} est. tokens) "
            f"-> {len(filtered_result)} entries ({final_tokens} est. tokens). "
            f"Budget={self.max_context_tokens}"
        )

        return filtered_result


# ───────────────────────────────
# 扩展工厂
# ───────────────────────────────

DEFAULT_MAX_CONTEXT_TOKENS = 8000


def create_context_compressor_extension(config: dict[str, Any] | None = None) -> Extension:
    """
    创建上下文压缩扩展。

    配置项（通过 ExtensionContext.config 传入）：
        - max_context_tokens: int = 8000
          最大上下文 Token 预算。超过此预算时从旧消息开始丢弃。
        - enabled: bool = True
          是否启用压缩。设为 False 时扩展注册但不做任何截断。
    """
    ext = Extension(
        id="builtin-context-compressor",
        name="Context Compressor",
        version="0.1.0",
        type="builtin",
    )

    async def activate(ctx: ExtensionContext) -> None:
        cfg = ctx.config or {}
        max_tokens = cfg.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
        enabled = cfg.get("enabled", True)

        compressor = TokenBudgetCompressor(max_context_tokens=max_tokens)

        async def _hook_compress_context(history: list[SessionEntry], context: dict) -> HookResult:
            if not enabled:
                return HookResult(modified=False, data=history)

            if not isinstance(history, list):
                logger.warning("[ContextCompressor] Hook received non-list data, skipping compression.")
                return HookResult(modified=False, data=history)

            try:
                compressed = compressor.compress(history)
                if compressed is not history and len(compressed) < len(history):
                    return HookResult(modified=True, data=compressed)
                return HookResult(modified=False, data=history)
            except Exception as e:
                # 压缩失败时静默透传，不中断核心流程（防御性兼容原则）
                logger.exception(f"[ContextCompressor] Compression failed, falling back to full history: {e}")
                return HookResult(modified=False, data=history)

        ctx.register_hook(
            HookPoint.BEFORE_CONTEXT_BUILD,
            _hook_compress_context,
            priority=50,  # 中等优先级，允许其他扩展在它之前或之后处理
        )
        logger.info(
            f"[ContextCompressor] Activated with max_context_tokens={max_tokens}, enabled={enabled}"
        )

    ext.on_activate(activate)
    return ext
