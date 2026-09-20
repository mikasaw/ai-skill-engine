"""防御性中间件：滑动窗口熔断 + asyncio.wait_for 强制超时。"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from core.context import SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware


class CircuitOpenError(RuntimeError):
    """熔断器处于打开状态，请求被短路。"""


@register_middleware
class CircuitBreakerMiddleware(BaseMiddleware):
    """滑动窗口熔断器。

    - 最近 window_size 次调用中，失败率 ≥ failure_rate_threshold 时熔断，
      直接抛出 CircuitOpenError，不执行 next()；
    - 熔断经过 reset_after 秒后进入半开状态：清空窗口，放行一次试探请求；
    - 每次 next() 都被 asyncio.wait_for 包裹，超时按失败计。
    """

    name = "circuit_breaker"

    def __init__(
        self,
        window_size: int = 10,
        min_calls: int = 5,
        failure_rate_threshold: float = 0.5,
        timeout_seconds: float = 30.0,
        reset_after: float = 20.0,
    ) -> None:
        self._window: deque[bool] = deque(maxlen=window_size)  # True 表示失败
        self._min_calls = min_calls
        self._failure_rate_threshold = failure_rate_threshold
        self._timeout_seconds = timeout_seconds
        self._reset_after = reset_after
        self._opened_at: float | None = None

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        self._ensure_closed()
        try:
            result = await asyncio.wait_for(next(context), timeout=self._timeout_seconds)
        except Exception:
            self._record(failure=True)
            raise
        self._record(failure=False)
        return result

    def _ensure_closed(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < self._reset_after:
            raise CircuitOpenError(
                f"熔断器打开中，{self._reset_after}s 后进入半开试探（last failure rate too high）"
            )
        # 半开：冷却结束，清空窗口放行一次试探请求
        self._opened_at = None
        self._window.clear()

    def _record(self, failure: bool) -> None:
        self._window.append(failure)
        if len(self._window) < self._min_calls:
            return
        failure_rate = sum(self._window) / len(self._window)
        if failure_rate >= self._failure_rate_threshold:
            self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        return (
            self._opened_at is not None
            and time.monotonic() - self._opened_at < self._reset_after
        )
