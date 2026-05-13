"""
ResilientProvider：包装 BaseProvider，注入应用级重试、Fallback、熔断。

内部状态：
- _circuit_breakers: dict[str, CircuitBreaker] —— 按 provider 标识维护熔断器
- _attempt_counts: dict[str, int] —— 当前 provider 的连续失败计数

重试策略：
- 仅对 RetryableError 子类触发指数退避
- AuthError / InvalidRequestError 直接抛出不重试
- 每次重试前检查目标 provider 的熔断器状态，若 OPEN 则直接切换 Fallback
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, AsyncIterable, Optional

from learning_agent.models import (
    AuthError,
    ChatChunk,
    ChatParams,
    ContextLengthError,
    InvalidRequestError,
    ResilienceConfig,
    RetryableError,
    ServiceUnavailable,
)
from learning_agent.provider.base_provider import BaseProvider

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """基于计数和超时恢复的熔断器状态机。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        return self._state

    def check(self) -> bool:
        """检查当前是否允许请求通过。"""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if self._last_failure_time is not None:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self._recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._failure_count = 0
                    logger.info("[CircuitBreaker] Transition OPEN -> HALF_OPEN")
                    return True
            return False
        # HALF_OPEN：允许试探性请求
        return True

    def record_success(self) -> None:
        """记录成功，重置为 CLOSED。"""
        if self._state != CircuitState.CLOSED:
            old = self._state
            self._state = CircuitState.CLOSED
            logger.info(f"[CircuitBreaker] Transition {old.value} -> CLOSED")
        self._failure_count = 0
        self._last_failure_time = None

    def record_failure(self) -> None:
        """记录失败，计数+1，若超阈值则 OPEN。"""
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("[CircuitBreaker] Transition HALF_OPEN -> OPEN")
        elif self._failure_count >= self._failure_threshold:
            old = self._state
            self._state = CircuitState.OPEN
            logger.warning(
                f"[CircuitBreaker] Transition {old.value} -> OPEN "
                f"(failures={self._failure_count})"
            )


class ResilientProvider(BaseProvider):
    """韧性 Provider 包装器：重试 + Fallback + 熔断。"""

    def __init__(
        self,
        primary: BaseProvider,
        fallback_chain: list[BaseProvider],
        config: ResilienceConfig,
    ):
        self._primary = primary
        self._fallbacks = fallback_chain
        self._config = config
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._attempt_counts: dict[str, int] = {}
        self._provider_name_map: dict[str, BaseProvider] = {}

        # 初始化所有 provider 的熔断器映射
        all_providers = [primary] + fallback_chain
        for idx, provider in enumerate(all_providers):
            name = f"provider-{idx}"
            self._provider_name_map[name] = provider
            self._circuit_breakers[name] = CircuitBreaker(
                failure_threshold=config.circuit_breaker_failure_threshold,
                recovery_timeout=config.circuit_breaker_recovery_timeout,
            )
            self._attempt_counts[name] = 0

    def _get_provider_name(self, provider: BaseProvider) -> str:
        for name, p in self._provider_name_map.items():
            if p is provider:
                return name
        return "unknown"

    def _classify_exception(self, exc: Exception) -> Exception:
        """将底层异常转换为韧性错误类型。"""
        msg = str(exc).lower()
        if "auth" in msg or "unauthorized" in msg or "api key" in msg:
            return AuthError(str(exc))
        if "context length" in msg or "too long" in msg or "maximum context" in msg:
            return ContextLengthError(str(exc))
        if "invalid request" in msg or "bad request" in msg:
            return InvalidRequestError(str(exc))
        if "rate limit" in msg or "timeout" in msg or "connection" in msg or "service unavailable" in msg:
            return ServiceUnavailable(str(exc))
        if "stream failed" in msg or "network" in msg:
            return RetryableError(str(exc))
        # 默认视为可重试
        return RetryableError(str(exc))

    async def _execute_with_resilience(
        self,
        provider: BaseProvider,
        params: ChatParams,
        is_stream: bool = True,
    ) -> AsyncIterable[ChatChunk]:
        """对单个 provider 执行流式/非流式调用，带重试和熔断。"""
        name = self._get_provider_name(provider)
        cb = self._circuit_breakers[name]
        max_attempts = self._config.provider_retry_max_attempts
        backoff_base = self._config.provider_retry_backoff_base
        max_delay = self._config.provider_retry_max_delay

        for attempt in range(max_attempts + 1):
            if not cb.check():
                logger.warning(f"[ResilientProvider] Circuit breaker OPEN for '{name}', skipping.")
                raise ServiceUnavailable(f"Circuit breaker OPEN for provider '{name}'")

            try:
                if is_stream:
                    async for chunk in provider.stream_chat(params):
                        yield chunk
                else:
                    yield await provider.chat(params)
                # 成功：重置熔断器和失败计数
                cb.record_success()
                self._attempt_counts[name] = 0
                return
            except Exception as e:
                classified = self._classify_exception(e)
                # 不可重试错误直接抛出
                if isinstance(classified, (AuthError, InvalidRequestError)):
                    cb.record_failure()
                    raise classified from e
                if isinstance(classified, ContextLengthError):
                    # 上下文长度错误不重试 Provider 层，交给上层处理
                    raise classified from e

                # 可重试错误
                if isinstance(classified, RetryableError):
                    self._attempt_counts[name] += 1
                    cb.record_failure()
                    if attempt < max_attempts:
                        delay = min(backoff_base ** attempt, max_delay)
                        logger.warning(
                            f"[ResilientProvider] Retryable error on '{name}' (attempt {attempt + 1}): {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(
                            f"[ResilientProvider] Max retries exceeded for '{name}': {e}"
                        )
                        raise classified from e

                # 其他未知错误
                raise classified from e

    async def _try_providers(
        self,
        params: ChatParams,
        is_stream: bool = True,
    ) -> AsyncIterable[ChatChunk]:
        """依次尝试主 provider 和 fallback chain。"""
        providers = [self._primary] + self._fallbacks
        last_error: Optional[Exception] = None

        for provider in providers:
            name = self._get_provider_name(provider)
            try:
                async for chunk in self._execute_with_resilience(provider, params, is_stream):
                    yield chunk
                return
            except (AuthError, InvalidRequestError, ContextLengthError):
                raise
            except Exception as e:
                last_error = e
                logger.warning(f"[ResilientProvider] Provider '{name}' failed: {e}")
                # 尝试下一个 fallback
                continue

        if last_error:
            raise last_error
        raise ServiceUnavailable("All providers failed")

    async def stream_chat(self, params: ChatParams) -> AsyncIterable[ChatChunk]:
        async for chunk in self._try_providers(params, is_stream=True):
            yield chunk

    async def chat(self, params: ChatParams) -> ChatChunk:
        result = None
        async for chunk in self._try_providers(params, is_stream=False):
            result = chunk
        if result is None:
            raise ServiceUnavailable("All providers failed")
        return result

    def supports_tool_calling(self) -> bool:
        return self._primary.supports_tool_calling()

    def supports_vision(self) -> bool:
        return self._primary.supports_vision()

    def get_max_context_length(self) -> int:
        return self._primary.get_max_context_length()

    @property
    def default_model(self) -> str:
        return self._primary.default_model
