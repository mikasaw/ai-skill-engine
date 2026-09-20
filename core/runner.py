"""技能执行引擎 (Runner)：构建 Context → 装配中间件链 → 执行。

中间件装配优先级（外层 → 内层）：
    全局中间件 (settings.global_middlewares) → 技能级 middleware_names；
    调用 run(middlewares=...) 时完全覆盖前两者（支持单次调用级组合）。

有状态中间件（如熔断器）的实例按 (name, options) 缓存复用，
保证同一 Runner 内多次调用共享熔断/统计状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from config import settings
from core.context import SkillContext
from core.middleware import BaseMiddleware, MiddlewareChain, MiddlewareSpec, Next, middleware_registry
from core.skill import BaseSkill, skill_registry


class InputValidationError(Exception):
    """输入未通过技能的 input_schema 校验。"""


def _freeze(value: Any) -> Any:
    """把 options 中的 list/dict 递归转成 tuple，使装配缓存键可哈希。"""
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, set)):
        return tuple(_freeze(v) for v in value)
    return value


class SkillRunner:
    def __init__(self, global_middlewares: Sequence[MiddlewareSpec] | None = None) -> None:
        self._global_specs: list[MiddlewareSpec] = (
            list(global_middlewares)
            if global_middlewares is not None
            else list(settings.global_middlewares)
        )
        self._instances: dict[tuple, BaseMiddleware] = {}

    async def run(
        self,
        skill: BaseSkill | str,
        input_data: Any,
        middlewares: Sequence[MiddlewareSpec] | None = None,
    ) -> SkillContext:
        """执行一个技能（接受注册名或实例），返回执行完毕的 Context。

        引擎层异常（含技能名未注册）不向上抛：统一记入 context.errors，
        调用方据此判断结果——run() 本身永不抛异常。
        """
        context = SkillContext(
            skill_name=skill if isinstance(skill, str) else skill.name,
            input_data=input_data,
        )
        try:
            skill_obj = skill_registry.create(skill) if isinstance(skill, str) else skill
            chain = MiddlewareChain(
                middlewares=self._assemble(skill_obj, middlewares),
                terminal=self._terminal(skill_obj),
            )
            return await chain.execute(context)
        except Exception as exc:
            context.add_error(f"{type(exc).__name__}: {exc}")
            return context

    def _assemble(
        self,
        skill_obj: BaseSkill,
        override: Sequence[MiddlewareSpec] | None,
    ) -> list[BaseMiddleware]:
        if override is not None:
            specs: list[MiddlewareSpec] = list(override)
        else:
            specs = [*self._global_specs, *type(skill_obj).middleware_names]
        return [self._resolve(spec) for spec in specs]

    def _resolve(self, spec: MiddlewareSpec) -> BaseMiddleware:
        if isinstance(spec, tuple):
            name, options = spec[0], dict(spec[1])
        else:
            name, options = spec, {}
        key = (name, _freeze(options))
        if key not in self._instances:
            self._instances[key] = middleware_registry.create(name, **options)
        return self._instances[key]

    @staticmethod
    def _terminal(skill_obj: BaseSkill) -> Next:
        """洋葱最内层：校验输入 → 执行技能 → 写回结果。"""

        async def invoke(context: SkillContext) -> SkillContext:
            try:
                context.input_data = skill_obj.input_schema.model_validate(context.input_data)
            except ValidationError as exc:
                raise InputValidationError(f"输入未通过 {skill_obj.input_schema.__name__} 校验: {exc}") from exc
            result = await skill_obj.execute(context)
            context.llm_response = result
            context.final_output = result
            return context

        return invoke
