"""cost_limiter 中间件测试：单 run 次数上限、累计 token 预算、失败调用也计费、契约内计数。"""

import pytest
from pydantic import BaseModel

from core.context import LLM_CALLS_KEY, SkillContext
from core.middleware import MiddlewareChain
from core.runner import SkillRunner
from core.skill import BaseSkill, register_skill
from middlewares.cost_limiter import BudgetExceededError, CostLimiterMiddleware


async def test_max_calls_blocks_runaway_retries():
    mw = CostLimiterMiddleware(max_calls_per_run=2)
    executed = 0

    async def terminal(ctx: SkillContext) -> SkillContext:
        nonlocal executed
        executed += 1
        return ctx

    ctx = SkillContext(skill_name="t")
    with pytest.raises(BudgetExceededError):
        for _ in range(5):
            await mw.process(ctx, terminal)

    assert executed == 2  # 第 3 次起被短路，不再触达下游
    assert ctx.metadata[LLM_CALLS_KEY] == 2


async def test_cumulative_budget_blocks_second_run_on_same_instance():
    mw = CostLimiterMiddleware(max_calls_per_run=10, max_estimated_tokens=60, chars_per_token=4)

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = "ok"
        return ctx

    first = SkillContext(skill_name="s", input_data={"text": "x" * 200})  # 估算 ≈ 53 token
    await mw.process(first, terminal)
    assert 0 < mw.used_estimated_tokens <= 60

    second = SkillContext(skill_name="s", input_data={"text": "x" * 200})
    with pytest.raises(BudgetExceededError):
        await mw.process(second, terminal)


async def test_failed_calls_still_count_toward_budget():
    mw = CostLimiterMiddleware(max_calls_per_run=5, max_estimated_tokens=1000)

    async def failing(ctx: SkillContext) -> SkillContext:
        raise ValueError("boom")

    ctx = SkillContext(skill_name="s", input_data={"text": "hello"})
    with pytest.raises(ValueError):
        await mw.process(ctx, failing)

    assert mw.used_estimated_tokens > 0  # 失败调用同样计费
    assert ctx.metadata["estimated_tokens"] == mw.used_estimated_tokens


# ---------- Runner 集成 ----------


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    text: str


@register_skill
class CostLimitEchoSkill(BaseSkill):
    name = "cost_limit_echo"
    description = "原样回显（供预算共享测试）"
    input_schema = EchoInput
    output_schema = EchoOutput
    middleware_names = []

    async def execute(self, context: SkillContext):
        return {"text": context.input_data.text}


@register_skill
class AlwaysInvalidSkill(BaseSkill):
    name = "cost_limit_always_invalid"
    description = "输出永远不满足契约（供契约内计数测试）"
    input_schema = EchoInput
    output_schema = EchoOutput
    middleware_names = []

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: SkillContext):
        self.calls += 1
        return {"wrong_field": True}


async def test_runner_shares_budget_across_runs():
    runner = SkillRunner(global_middlewares=[])
    specs = [("cost_limiter", {"max_calls_per_run": 10, "max_estimated_tokens": 60})]
    big_input = {"text": "x" * 200}

    first = await runner.run("cost_limit_echo", big_input, middlewares=specs)
    assert first.ok

    second = await runner.run("cost_limit_echo", big_input, middlewares=specs)
    assert any("超出预算" in err for err in second.errors)
    assert second.final_output is None


async def test_cost_limiter_inside_contract_caps_retries():
    """挂载顺序 ["contract", "cost_limiter"]：cost_limiter 在契约之内，
    每次（含重试的）真实执行都被计数，第 2 次执行前被截断。"""
    skill = AlwaysInvalidSkill()
    runner = SkillRunner(global_middlewares=[])
    context = await runner.run(
        skill,
        {"text": "x"},
        middlewares=["contract", ("cost_limiter", {"max_calls_per_run": 1})],
    )

    assert skill.calls == 1  # 首次执行后，契约的重试被成本闸短路
    assert any("BudgetExceededError" in err for err in context.errors)
    assert context.metadata[LLM_CALLS_KEY] == 1
