"""core/ 单元测试：Context、洋葱模型链序、注册表。"""

import pytest

from core.context import SkillContext
from core.middleware import BaseMiddleware, MiddlewareChain, middleware_registry
from core.skill import BaseSkill, skill_registry


class RecorderMiddleware(BaseMiddleware):
    name = "recorder"

    def __init__(self, tag: str, order: list[str]) -> None:
        self.tag = tag
        self.order = order

    async def process(self, context: SkillContext, next):
        self.order.append(f"{self.tag}:before")
        context = await next(context)
        self.order.append(f"{self.tag}:after")
        return context


# ---------- 洋葱模型 ----------


async def test_middleware_chain_onion_order():
    order: list[str] = []

    async def terminal(ctx: SkillContext) -> SkillContext:
        order.append("terminal")
        ctx.final_output = "done"
        return ctx

    chain = MiddlewareChain(
        [RecorderMiddleware("outer", order), RecorderMiddleware("inner", order)],
        terminal,
    )
    result = await chain.execute(SkillContext(skill_name="t"))

    assert order == ["outer:before", "inner:before", "terminal", "inner:after", "outer:after"]
    assert result.final_output == "done"


async def test_middleware_can_short_circuit():
    calls = 0

    async def terminal(ctx: SkillContext) -> SkillContext:
        nonlocal calls
        calls += 1
        return ctx

    class Blocker(BaseMiddleware):
        name = "blocker"

        async def process(self, context: SkillContext, next):
            context.add_error("blocked")
            return context  # 不调用 next()，短路

    result = await MiddlewareChain([Blocker()], terminal).execute(SkillContext(skill_name="t"))

    assert calls == 0
    assert result.errors == ["blocked"]


# ---------- Context ----------


def test_context_defaults_and_ok():
    ctx = SkillContext(skill_name="x")
    assert ctx.metadata == {}
    assert ctx.errors == []
    assert ctx.ok

    ctx.add_error("boom")
    assert not ctx.ok


# ---------- 注册表 ----------


def _dummy_skill(name: str) -> type[BaseSkill]:
    class _Dummy(BaseSkill):
        async def execute(self, context: SkillContext):
            return None

    _Dummy.name = name
    return _Dummy


def test_skill_registry_register_get_create():
    cls = _dummy_skill("registry_alpha")
    assert skill_registry.register(cls) is cls
    assert skill_registry.get("registry_alpha") is cls
    assert skill_registry.create("registry_alpha").name == "registry_alpha"


def test_skill_registry_rejects_duplicate():
    skill_registry.register(_dummy_skill("registry_beta"))
    with pytest.raises(ValueError, match="重复注册"):
        skill_registry.register(_dummy_skill("registry_beta"))


def test_skill_registry_missing_name():
    with pytest.raises(KeyError, match="未注册"):
        skill_registry.get("no_such_skill")


def test_middleware_registry_register_and_create():
    class CachedMW(BaseMiddleware):
        name = "registry_cached_mw"

        async def process(self, context: SkillContext, next):
            return await next(context)

    middleware_registry.register(CachedMW)
    assert middleware_registry.get("registry_cached_mw") is CachedMW
    assert isinstance(middleware_registry.create("registry_cached_mw"), CachedMW)
