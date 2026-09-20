"""contract 中间件测试：Mock 技能输出，断言自修复重试与重试上限。"""

from pydantic import BaseModel, EmailStr

from core.context import SkillContext
from core.runner import SkillRunner
from core.skill import BaseSkill, register_skill
from middlewares.contract import RETRY_PROMPT_KEY


class ContractInput(BaseModel):
    text: str


class ContractOutput(BaseModel):
    name: str
    email: EmailStr
    age: int | None = None


@register_skill
class HealSkill(BaseSkill):
    name = "contract_heal"
    description = "首次返回坏邮箱，收到 retry_prompt 后自愈"
    input_schema = ContractInput
    output_schema = ContractOutput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: SkillContext):
        self.calls += 1
        if context.metadata.get(RETRY_PROMPT_KEY):
            return {"name": "李四", "email": "lisi@example.com", "age": 35}
        return {"name": "李四", "email": "not-an-email", "age": 35}


@register_skill
class AlwaysBadSkill(BaseSkill):
    name = "contract_always_bad"
    description = "始终返回坏邮箱"
    input_schema = ContractInput
    output_schema = ContractOutput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: SkillContext):
        self.calls += 1
        return {"name": "李四", "email": "still-not-an-email"}


@register_skill
class GoodSkill(BaseSkill):
    name = "contract_good"
    description = "始终返回合法输出"
    input_schema = ContractInput
    output_schema = ContractOutput

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, context: SkillContext):
        self.calls += 1
        return {"name": "张三", "email": "zhangsan@example.com", "age": 28}


async def test_contract_heals_on_second_call():
    skill = HealSkill()
    ctx = await SkillRunner(global_middlewares=[]).run(
        skill, {"text": "x"}, middlewares=["contract"]
    )

    assert skill.calls == 2  # 首次 + 一次自修复重试
    assert isinstance(ctx.final_output, ContractOutput)
    assert ctx.final_output.email == "lisi@example.com"
    assert ctx.metadata["contract_failures"] == 1
    assert RETRY_PROMPT_KEY in ctx.metadata  # 纠正提示已注入
    assert ctx.errors == []  # 自愈成功，不产生最终错误
    assert ctx.ok


async def test_contract_gives_up_after_max_retries():
    skill = AlwaysBadSkill()
    ctx = await SkillRunner(global_middlewares=[]).run(
        skill, {"text": "x"}, middlewares=["contract"]
    )

    assert skill.calls == 3  # 首次 + 最多 2 次重试
    assert any(err.startswith("OutputValidationError") for err in ctx.errors)
    assert ctx.metadata["contract_failures"] == 3
    assert len(ctx.metadata["contract_retry_log"]) == 3
    assert not isinstance(ctx.final_output, ContractOutput)  # 坏输出原样保留供排查


async def test_contract_passes_valid_output_without_retry():
    skill = GoodSkill()
    ctx = await SkillRunner(global_middlewares=[]).run(
        skill, {"text": "x"}, middlewares=["contract"]
    )

    assert skill.calls == 1
    assert ctx.errors == []
    assert RETRY_PROMPT_KEY not in ctx.metadata
    assert isinstance(ctx.final_output, ContractOutput)
