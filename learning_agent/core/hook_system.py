"""
Hook 系统：扩展介入核心流程的"阀门"
每个 Hook 点有明确的输入输出契约。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from learning_agent.models import HookPoint, HookResult, TraceSpan

logger = logging.getLogger(__name__)

HookHandler = Callable[..., Awaitable[Any]]


class HookAbortError(Exception):
    """Hook 通过抛出此异常中断后续处理（如澄清循环）。"""
    pass


class _HookRegistration:
    def __init__(
        self,
        handler: HookHandler,
        priority: int = 0,
        extension_id: Optional[str] = None,
    ):
        self.handler = handler
        self.priority = priority
        self.extension_id = extension_id


class HookSystem:
    """
    Hook 系统管理所有扩展点的注册与执行。

    执行规则：
    - 同个 Hook 点可注册多个处理函数，按优先级排序（数值大的优先）
    - 处理函数可以返回 HookResult 来修改数据或中断流程
    - 所有 Hook 异步执行，支持 await
    """

    def __init__(self):
        self._hooks: dict[HookPoint, list[_HookRegistration]] = {}

    def register(
        self,
        point: HookPoint | str,
        handler: HookHandler,
        priority: int = 0,
        extension_id: Optional[str] = None,
    ) -> None:
        """注册一个 Hook 处理函数。"""
        if isinstance(point, str):
            point = HookPoint(point)
        if point not in self._hooks:
            self._hooks[point] = []
        self._hooks[point].append(_HookRegistration(handler, priority, extension_id))
        self._hooks[point].sort(key=lambda r: r.priority, reverse=True)
        logger.debug(
            f"[HookSystem] Registered '{point.value}' (ext={extension_id}, prio={priority})"
        )

    def unregister(
        self,
        point: HookPoint | str,
        handler: HookHandler,
    ) -> None:
        """注销一个 Hook 处理函数。"""
        if isinstance(point, str):
            point = HookPoint(point)
        if point in self._hooks:
            self._hooks[point] = [r for r in self._hooks[point] if r.handler != handler]

    async def execute(
        self,
        point: HookPoint | str,
        data: Any,
        context: dict[str, Any],
        trace_span: Optional[TraceSpan] = None,
    ) -> HookResult:
        """
        执行指定 Hook 点的所有注册处理函数。

        Args:
            data: 当前流程数据，可被 Hook 修改
            context: 上下文信息（session, memory_manager 等）
            trace_span: 可选的追踪 span，用于记录性能

        Returns:
            HookResult: 汇总结果
        """
        if isinstance(point, str):
            point = HookPoint(point)

        registrations = self._hooks.get(point, [])
        if not registrations:
            return HookResult(modified=False, data=data)

        current_data = data
        modified = False

        for reg in registrations:
            ext_id = reg.extension_id or "unknown"
            start_ts = __import__("time").time()
            try:
                result = await reg.handler(current_data, context)

                if isinstance(result, HookResult):
                    if result.abort:
                        logger.info(f"[HookSystem] Hook '{point.value}' aborted by {ext_id}")
                        if trace_span:
                            trace_span.tags[f"hook.{point.value}.aborted_by"] = ext_id
                        return HookResult(
                            modified=True,
                            data=result.data or current_data,
                            abort=True,
                            abort_reason=result.abort_reason or f"Aborted by {ext_id}",
                        )
                    if result.modified:
                        current_data = result.data
                        modified = True

            except HookAbortError as e:
                logger.info(f"[HookSystem] Hook '{point.value}' HookAbortError by {ext_id}: {e}")
                if trace_span:
                    trace_span.tags[f"hook.{point.value}.aborted_by"] = ext_id
                    trace_span.tags[f"hook.{point.value}.abort_reason"] = str(e)
                raise

            except Exception as e:
                logger.exception(f"[HookSystem] Hook '{point.value}' error in {ext_id}: {e}")
                if trace_span:
                    trace_span.tags[f"hook.{point.value}.{ext_id}.error"] = str(e)
                # 默认策略：Hook 错误不中断核心流程
                continue

            finally:
                duration_ms = int((__import__("time").time() - start_ts) * 1000)
                if trace_span:
                    trace_span.tags[f"hook.{point.value}.{ext_id}.duration_ms"] = duration_ms

        return HookResult(modified=modified, data=current_data)

    def list_registered(self) -> dict[str, list[str]]:
        """列出所有已注册的 Hook 点及其扩展来源。"""
        result = {}
        for point, regs in self._hooks.items():
            result[point.value] = [r.extension_id or "unknown" for r in regs]
        return result
