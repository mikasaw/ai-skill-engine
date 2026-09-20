"""成本控制中间件：单次 run 的调用次数上限 + 实例累计 token 预算。

双闸设计：
- max_calls_per_run：当前 run 内允许经过本中间件的 next() 次数。
  挂在 contract 之内时可精确统计每次 LLM 调用，防止
  「契约重试 × 模型降级」组合放大调用次数；
- max_estimated_tokens：本中间件实例的累计估算 token 预算。
  Runner 按 (name, options) 缓存实例，因此同一 Runner 内对同一技能的
  多次调用共享预算；跨 Runner / 跨进程不共享（无外部存储依赖）。

token 为粗估：字符数 / chars_per_token，用于预算闸门而非计费。
"""

from __future__ import annotations

from core.context import LLM_CALLS_KEY, SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware


class BudgetExceededError(RuntimeError):
    """成本预算耗尽，请求被短路。"""


@register_middleware
class CostLimiterMiddleware(BaseMiddleware):
    name = "cost_limiter"

    def __init__(
        self,
        max_calls_per_run: int = 10,
        max_estimated_tokens: int | None = None,
        chars_per_token: int = 4,
    ) -> None:
        self._max_calls = max_calls_per_run
        self._max_tokens = max_estimated_tokens
        self._chars_per_token = max(1, chars_per_token)
        self._used_tokens = 0

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        calls = int(context.metadata.get(LLM_CALLS_KEY, 0))
        if calls >= self._max_calls:
            raise BudgetExceededError(
                f"本次 run 的 LLM 调用次数达到上限 {self._max_calls}，已熔断以控制成本"
            )
        estimate = self._estimate(context)
        if self._max_tokens is not None and self._used_tokens + estimate > self._max_tokens:
            raise BudgetExceededError(
                f"累计估算 token {self._used_tokens} + 本次 {estimate} 超出预算 {self._max_tokens}，已熔断以控制成本"
            )

        context.metadata[LLM_CALLS_KEY] = calls + 1
        try:
            return await next(context)
        finally:
            # 失败同样计费：真实世界里失败的调用也消耗了 token
            self._used_tokens += estimate
            context.metadata["estimated_tokens"] = (
                int(context.metadata.get("estimated_tokens", 0)) + estimate
            )
            context.metadata["budget_remaining"] = (
                None if self._max_tokens is None else max(0, self._max_tokens - self._used_tokens)
            )

    def _estimate(self, context: SkillContext) -> int:
        size = (
            len(str(context.input_data))
            + len(str(context.final_output or ""))
            + len(str(context.skill_name))
        )
        return max(1, size // self._chars_per_token)

    @property
    def used_estimated_tokens(self) -> int:
        return self._used_tokens
