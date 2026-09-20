"""Middleware 基类、中间件注册表与洋葱模型执行器 (MiddlewareChain)。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, ClassVar, TypeAlias

from core.context import SkillContext

# 终端处理器 / 下一层处理器的统一签名
Next: TypeAlias = Callable[[SkillContext], Awaitable[SkillContext]]

# 中间件装配规格：名字，或 (名字, 构造参数)
MiddlewareSpec: TypeAlias = "str | tuple[str, dict[str, Any]]"


class BaseMiddleware(ABC):
    """中间件基类：实现 process(context, next) 即可织入洋葱模型。

    - 在 await next(context) 之前写「前置」逻辑，之后写「后置」逻辑；
    - 不调用 next() 即短路请求（如熔断打开、限流拒绝）；
    - 可调用多次 next() 实现重试（如契约自修复）。
    """

    name: ClassVar[str] = "middleware"

    @abstractmethod
    async def process(self, context: SkillContext, next: Next) -> SkillContext: ...


class MiddlewareRegistry:
    def __init__(self) -> None:
        self._items: dict[str, type[BaseMiddleware]] = {}

    def register(self, mw_cls: type[BaseMiddleware]) -> type[BaseMiddleware]:
        name = mw_cls.name
        if not name:
            raise ValueError("Middleware 类必须定义非空 name")
        if name in self._items:
            raise ValueError(f"Middleware '{name}' 重复注册")
        self._items[name] = mw_cls
        return mw_cls

    def get(self, name: str) -> type[BaseMiddleware]:
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(
                f"Middleware '{name}' 未注册，已注册: {sorted(self._items)}"
            ) from None

    def create(self, name: str, **kwargs: Any) -> BaseMiddleware:
        return self.get(name)(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._items)


middleware_registry = MiddlewareRegistry()


def register_middleware(mw_cls: type[BaseMiddleware]) -> type[BaseMiddleware]:
    """装饰器：将 Middleware 类注册到全局注册表。"""
    return middleware_registry.register(mw_cls)


def _wrap(middleware: BaseMiddleware, next_call: Next) -> Next:
    async def handler(context: SkillContext) -> SkillContext:
        return await middleware.process(context, next_call)

    return handler


class MiddlewareChain:
    """把中间件列表与终端处理器组合成洋葱模型：列表越靠前越靠外层。"""

    def __init__(self, middlewares: Sequence[BaseMiddleware], terminal: Next) -> None:
        self._middlewares = list(middlewares)
        self._terminal = terminal

    async def execute(self, context: SkillContext) -> SkillContext:
        handler: Next = self._terminal
        for middleware in reversed(self._middlewares):
            handler = _wrap(middleware, handler)
        return await handler(context)
