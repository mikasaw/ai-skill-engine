"""内置中间件（切面逻辑）包：import 即完成全部注册。"""

from . import contract, cost_limiter, defensive, fallback, notify, observability  # noqa: F401

__all__ = ["contract", "cost_limiter", "defensive", "fallback", "notify", "observability"]
