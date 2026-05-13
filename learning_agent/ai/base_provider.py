"""
Provider 抽象基类：所有 LLM Provider 必须实现的接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterable

from learning_agent.ai.models import ChatChunk, ChatParams


class BaseProvider(ABC):
    """
    Provider 层抽象基类。
    统一流式/非流式接口，上层代码不直接依赖任何 SDK。
    """

    @abstractmethod
    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        """流式调用（默认方式）。"""
        pass

    @abstractmethod
    async def chat(self, params: ChatParams) -> ChatChunk:
        """非流式调用（特殊场景兜底）。"""
        pass

    @abstractmethod
    def supports_tool_calling(self) -> bool:
        pass

    @abstractmethod
    def supports_vision(self) -> bool:
        pass

    @abstractmethod
    def get_max_context_length(self) -> int:
        pass

    @property
    def default_model(self) -> str:
        """返回默认模型名称，供上层调用方使用。"""
        return ""
