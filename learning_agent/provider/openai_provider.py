"""
OpenAI Provider：基于 openai SDK 的实现。
支持流式传输、重试、错误转换。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterable, Optional

from learning_agent.models import ChatChunk, ChatMessage, ChatParams, ProviderConfig
from learning_agent.provider.base_provider import BaseProvider

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
            result.append(msg)
        return result

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        """流式聊天，返回 AsyncIterable[ChatChunk]。"""
        request = {
            "model": params.model or self.config.model,
            "messages": self._convert_messages(params.messages),
            "temperature": params.temperature,
            "stream": True,
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
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                delta = choice.delta
                tool_call = None
                if delta.tool_calls:
                    tc = delta.tool_calls[0]
                    tool_call = {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name if tc.function else None,
                            "arguments": tc.function.arguments if tc.function else "",
                        },
                    }
                yield ChatChunk(
                    content=delta.content or "",
                    tool_call=tool_call,
                    finish_reason=choice.finish_reason,
                )
        except Exception as e:
            logger.exception(f"[OpenAIProvider] Stream error: {e}")
            raise ProviderError(f"OpenAI stream failed: {e}") from e

    async def chat(self, params: ChatParams) -> ChatChunk:
        """非流式调用。"""
        request = {
            "model": params.model or self.config.model,
            "messages": self._convert_messages(params.messages),
            "temperature": params.temperature,
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


class ProviderError(Exception):
    """统一的 Provider 错误类型。"""
    pass
