"""
OpenAI Provider：基于 openai SDK 的实现。
支持流式传输、重试、错误转换。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterable, Optional

from learning_agent.ai.models import (
    ChatChunk,
    ChatMessage,
    ChatParams,
    ProviderConfig,
    ProviderUsage,
)
from learning_agent.ai.base_provider import BaseProvider

logger = logging.getLogger(__name__)

try:
    import openai
except ImportError:
    openai = None


class OpenAIProvider(BaseProvider):
    """
    OpenAI SDK Provider 实现。
    自动处理重试、超时、错误转换。
    """

    def __init__(self, config: ProviderConfig):
        self.config = config
        if openai is None:
            raise RuntimeError("openai package is not installed. Please run: pip install openai")
        self.client = openai.AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )

    def _convert_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        result = []
        for m in messages:
            msg: dict[str, Any] = {"role": m.role.value, "content": m.content}
            if m.name:
                msg["name"] = m.name
            if m.tool_calls:
                msg["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            if m.reasoning_content is not None:
                msg["reasoning_content"] = m.reasoning_content
            result.append(msg)
        return result

    def _resolve_temperature(self, model: str, temperature: float) -> float:
        """Moonshot kimi-k2.x 系列模型强制 temperature=1.0。"""
        if "kimi-k2" in model:
            return 1.0
        return temperature

    @staticmethod
    def _extract_usage(raw_usage: Any) -> ProviderUsage | None:
        if raw_usage is None:
            return None
        prompt_tokens = getattr(raw_usage, "prompt_tokens", None)
        completion_tokens = getattr(raw_usage, "completion_tokens", None)
        total_tokens = getattr(raw_usage, "total_tokens", None)
        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            return None
        return ProviderUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        """流式聊天，返回 AsyncIterable[ChatChunk]。"""
        model = params.model or self.config.model
        request = {
            "model": model,
            "messages": self._convert_messages(params.messages),
            "temperature": self._resolve_temperature(model, params.temperature),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if params.max_tokens:
            request["max_tokens"] = params.max_tokens
        if params.tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.id,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}}},
                }
                for t in params.tools
            ]

        logger.info(f"[OpenAIProvider] Streaming request model={request['model']}")

        try:
            stream = await self.client.chat.completions.create(**request)
            async for chunk in stream:
                provider_usage = self._extract_usage(getattr(chunk, "usage", None))
                if provider_usage is not None:
                    yield ChatChunk(
                        content="",
                        metadata={"provider_usage": provider_usage.model_dump()},
                    )

                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                delta = choice.delta

                # yield content chunk（如果有文本或推理内容）
                if delta.content or getattr(delta, "reasoning_content", None):
                    yield ChatChunk(
                        content=delta.content or "",
                        finish_reason=choice.finish_reason,
                        reasoning_content=getattr(delta, "reasoning_content", None) or None,
                        metadata=(
                            {"provider_usage": provider_usage.model_dump()}
                            if provider_usage is not None
                            else {}
                        ),
                    )

                # yield 每个 tool call chunk（支持并行 tool calls）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        yield ChatChunk(
                            content="",
                            tool_call={
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name if tc.function else None,
                                    "arguments": tc.function.arguments if tc.function else "",
                                },
                            },
                            tool_call_index=tc.index,
                            finish_reason=choice.finish_reason,
                            reasoning_content=getattr(delta, "reasoning_content", None) or None,
                            metadata=(
                                {"provider_usage": provider_usage.model_dump()}
                                if provider_usage is not None
                                else {}
                            ),
                        )

                # 如果既没有内容也没有 tool_calls，但可能有 finish_reason，也 yield 一个空 chunk
                if not delta.content and not getattr(delta, "reasoning_content", None) and not delta.tool_calls:
                    yield ChatChunk(
                        content="",
                        finish_reason=choice.finish_reason,
                        reasoning_content=getattr(delta, "reasoning_content", None) or None,
                        metadata=(
                            {"provider_usage": provider_usage.model_dump()}
                            if provider_usage is not None
                            else {}
                        ),
                    )
        except Exception as e:
            logger.exception(f"[OpenAIProvider] Stream error: {e}")
            raise ProviderError(f"OpenAI stream failed: {e}") from e

    async def chat(self, params: ChatParams) -> ChatChunk:
        """非流式调用。"""
        model = params.model or self.config.model
        request = {
            "model": model,
            "messages": self._convert_messages(params.messages),
            "temperature": self._resolve_temperature(model, params.temperature),
            "stream": False,
        }
        if params.max_tokens:
            request["max_tokens"] = params.max_tokens
        if params.tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.id,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}}},
                }
                for t in params.tools
            ]

        try:
            resp = await self.client.chat.completions.create(**request)
            choice = resp.choices[0]
            message = choice.message
            tool_call = None
            if message.tool_calls:
                tc = message.tool_calls[0]
                tool_call = {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
            return ChatChunk(
                content=message.content or "",
                tool_call=tool_call,
                finish_reason=choice.finish_reason,
                reasoning_content=getattr(message, "reasoning_content", None) or None,
                metadata=(
                    {"provider_usage": provider_usage.model_dump()}
                    if (provider_usage := self._extract_usage(getattr(resp, "usage", None))) is not None
                    else {}
                ),
            )
        except Exception as e:
            logger.exception(f"[OpenAIProvider] Chat error: {e}")
            raise ProviderError(f"OpenAI chat failed: {e}") from e

    def supports_tool_calling(self) -> bool:
        return True

    def supports_vision(self) -> bool:
        return True

    def get_max_context_length(self) -> int:
        return 128000

    @property
    def default_model(self) -> str:
        return self.config.model


class ProviderError(Exception):
    """统一的 Provider 错误类型。"""
    pass
