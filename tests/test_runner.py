"""runner 集成测试：端到端执行、输入校验、异常兜底、中间件装配顺序、单次调用级覆盖。"""

from pydantic import BaseModel

from core.context import SkillContext
from core.middleware import BaseMiddleware, register_middleware
from core.runner import SkillRunner
from core.skill import BaseSkill, register_skill


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    text: str


@register_skill
class EchoSkill(BaseSkill):
    name = "runner_echo"
    description = "大写回显"
    input_schema = EchoInput
    output_schema = EchoOutput
    middleware_names = []

    async def execute(self, context: SkillContext):
        return {"text": context.input_data.text.upper()}


@register_skill
class BoomSkill(BaseSkill):
    name = "runner_boom"
    description = "execute 内抛异常"
    input_schema = EchoInput
    output_schema = EchoOutput
    middleware_names = []

    async def execute(self, context: SkillContext):
        raise ValueError("boom")


@register_middleware
class TaggerMiddleware(BaseMiddleware):
    name = "runner_tagger"

    def __init__(self, tag: str = "") -> None:
        self._tag = tag

    async def process(self, context: SkillContext, next):
        context.metadata.setdefault("tags", []).append(f"{self._tag}:before")
        context = await next(context)
        context.metadata.setdefault("tags", []).append(f"{self._tag}:after")
        return context


@register_skill
class RecordedSkill(BaseSkill):
    name = "runner_recorded"
    description = "验证中间件装配顺序"
    input_schema = EchoInput
    output_schema = EchoOutput
    middleware_names = [("runner_tagger", {"tag": "skill"})]

    async def execute(self, context: SkillContext):
        return {"text": context.input_data.text}


async def test_end_to_end_success():
    ctx = await SkillRunner(global_middlewares=[]).run(EchoSkill(), {"text": "abc"})

    # 未挂 contract 中间件时，final_output 保持技能返回的原始结果
    assert ctx.final_output == {"text": "ABC"}
    assert ctx.llm_response == {"text": "ABC"}
    assert ctx.errors == []
    assert ctx.ok


async def test_run_by_registered_name():
    ctx = await SkillRunner(global_middlewares=[]).run("runner_echo", {"text": "abc"})
    assert ctx.final_output["text"] == "ABC"


async def test_input_validation_failure_recorded():
    ctx = await SkillRunner(global_middlewares=[]).run(EchoSkill(), {"wrong_field": 1})

    assert ctx.final_output is None
    assert ctx.errors and ctx.errors[0].startswith("InputValidationError")


async def test_skill_exception_recorded_not_raised():
    ctx = await SkillRunner(global_middlewares=[]).run(BoomSkill(), {"text": "x"})

    assert any("ValueError: boom" in err for err in ctx.errors)
    assert ctx.final_output is None


async def test_global_and_skill_middlewares_compose_in_onion_order():
    runner = SkillRunner(global_middlewares=[("runner_tagger", {"tag": "global"})])
    ctx = await runner.run(RecordedSkill(), {"text": "x"})

    assert ctx.final_output["text"] == "x"
    # 全局在外层、技能级在内层的洋葱顺序
    assert ctx.metadata["tags"] == [
        "global:before",
        "skill:before",
        "skill:after",
        "global:after",
    ]


async def test_percall_override_replaces_whole_chain():
    runner = SkillRunner(global_middlewares=[("runner_tagger", {"tag": "global"})])
    ctx = await runner.run(
        EchoSkill(), {"text": "x"}, middlewares=[("runner_tagger", {"tag": "percall"})]
    )

    assert ctx.final_output["text"] == "X"
    assert ctx.metadata["tags"] == ["percall:before", "percall:after"]
