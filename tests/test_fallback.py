"""fallback 中间件测试：降级链切换、耗尽抛原始异常、metadata 审计、Runner 集成。"""

import pytest
from pydantic import BaseModel

from core.context import MODEL_OVERRIDE_KEY, SkillContext
from core.runner import SkillRunner
from core.skill import BaseSkill, register_skill
from middlewares.fallback import FallbackMiddleware


async def test_fallback_switches_model_on_failure():
    mw = FallbackMiddleware(fallback_models=["backup-a"])
    seen_models: list[str] = []

    async def terminal(ctx: SkillContext) -> SkillContext:
        seen_models.append(ctx.metadata.get(MODEL_OVERRIDE_KEY, "default"))
        if ctx.metadata.get(MODEL_OVERRIDE_KEY) != "backup-a":
            raise ConnectionError("primary down")
        ctx.final_output = "ok"
        return ctx

    ctx = SkillContext(skill_name="t")
    result = await mw.process(ctx, terminal)

    assert result.final_output == "ok"
    assert seen_models == ["default", "backup-a"]
    assert result.metadata["fallback_in_use"] == "backup-a"
    assert len(result.metadata["fallback_log"]) == 1
    assert "ConnectionError" in result.metadata["fallback_log"][0]


async def test_fallback_exhaustion_reraises_last_exception():
    mw = FallbackMiddleware(fallback_models=["backup-a", "backup-b"])

    async def failing(ctx: SkillContext) -> SkillContext:
        raise TimeoutError("all models down")

    ctx = SkillContext(skill_name="t")
    with pytest.raises(TimeoutError):  # 原始异常类型不丢失
        await mw.process(ctx, failing)

    assert len(ctx.metadata["fallback_log"]) == 3  # default + 两个备用模型


async def test_fallback_noop_on_success():
    mw = FallbackMiddleware(fallback_models=["backup-a"])

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = "ok"
        return ctx

    ctx = SkillContext(skill_name="t")
    result = await mw.process(ctx, terminal)

    assert result.final_output == "ok"
    assert MODEL_OVERRIDE_KEY not in result.metadata
    assert result.metadata["fallback_log"] == []


# ---------- Runner 集成（list 形式 options 的装配缓存） ----------


class FallbackInput(BaseModel):
    text: str


class FallbackOutput(BaseModel):
    text: str


@register_skill
class ModelAwareSkill(BaseSkill):
    name = "fallback_model_aware"
    description = "默认模型必然失败，仅备用模型成功"
    input_schema = FallbackInput
    output_schema = FallbackOutput
    middleware_names = []

    def __init__(self) -> None:
        self.models: list[str] = []

    async def execute(self, context: SkillContext):
        model = context.metadata.get(MODEL_OVERRIDE_KEY, "default")
        self.models.append(model)
        if model == "default":
            raise ConnectionError("primary down")
        return {"text": f"used:{model}"}


async def test_runner_integration_with_list_options():
    skill = ModelAwareSkill()
    runner = SkillRunner(global_middlewares=[])
    ctx = await runner.run(
        skill,
        {"text": "x"},
        middlewares=[("fallback", {"fallback_models": ["backup-1", "backup-2"]})],
    )

    assert skill.models == ["default", "backup-1"]  # 第二次尝试即成功
    assert ctx.final_output == {"text": "used:backup-1"}
    assert ctx.errors == []
    assert ctx.metadata["fallback_in_use"] == "backup-1"
