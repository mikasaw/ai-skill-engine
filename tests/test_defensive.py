"""defensive 中间件测试：强制超时、滑动窗口熔断、半开恢复。"""

import asyncio

import pytest

from core.context import SkillContext
from middlewares.defensive import CircuitBreakerMiddleware, CircuitOpenError


def _ctx() -> SkillContext:
    return SkillContext(skill_name="defensive_test")


async def test_timeout_raises_and_counts_as_failure():
    mw = CircuitBreakerMiddleware(timeout_seconds=0.05)

    async def slow(ctx: SkillContext) -> SkillContext:
        await asyncio.sleep(0.5)
        return ctx

    with pytest.raises(asyncio.TimeoutError):
        await mw.process(_ctx(), slow)
    assert not mw.is_open  # 窗口样本数不足 min_calls，尚未熔断


async def test_circuit_opens_and_short_circuits():
    mw = CircuitBreakerMiddleware(
        window_size=5,
        min_calls=3,
        failure_rate_threshold=0.5,
        timeout_seconds=1.0,
        reset_after=60.0,
    )

    async def failing(ctx: SkillContext) -> SkillContext:
        raise ValueError("boom")

    for _ in range(3):
        with pytest.raises(ValueError):
            await mw.process(_ctx(), failing)
    assert mw.is_open

    next_calls = 0

    async def spy(ctx: SkillContext) -> SkillContext:
        nonlocal next_calls
        next_calls += 1
        return ctx

    with pytest.raises(CircuitOpenError):
        await mw.process(_ctx(), spy)
    assert next_calls == 0  # 熔断打开后不再触达下游


async def test_half_open_after_cooldown():
    mw = CircuitBreakerMiddleware(
        window_size=5,
        min_calls=3,
        failure_rate_threshold=0.5,
        timeout_seconds=1.0,
        reset_after=0.05,
    )

    async def failing(ctx: SkillContext) -> SkillContext:
        raise ValueError("boom")

    for _ in range(3):
        with pytest.raises(ValueError):
            await mw.process(_ctx(), failing)
    assert mw.is_open

    await asyncio.sleep(0.06)  # 冷却结束 → 半开
    executed = 0

    async def ok(ctx: SkillContext) -> SkillContext:
        nonlocal executed
        executed += 1
        return ctx

    await mw.process(_ctx(), ok)
    assert executed == 1  # 半开状态放行了试探请求
    assert not mw.is_open


async def test_success_keeps_circuit_closed():
    mw = CircuitBreakerMiddleware(
        window_size=5, min_calls=3, failure_rate_threshold=0.5, timeout_seconds=1.0
    )

    async def ok(ctx: SkillContext) -> SkillContext:
        return ctx

    for _ in range(6):
        await mw.process(_ctx(), ok)
    assert not mw.is_open
