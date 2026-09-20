"""示例技能：从自然语言文本中提取用户信息。

洋葱模型纪律示范——execute 内只有业务逻辑（Prompt 构造 + 一次结构化 LLM 调用），
不包含任何重试、校验、日志、监控代码：
- 校验/自修复重试：middlewares/contract.py
- 超时/熔断：middlewares/defensive.py
- 监控埋点：middlewares/observability.py
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field

from config import settings
from core.context import SkillContext
from core.skill import BaseSkill, register_skill


class UserInput(BaseModel):
    text: str = Field(..., min_length=1, description="包含用户信息的原始文本")


class UserOutput(BaseModel):
    name: str
    email: EmailStr
    age: Optional[int] = None


SYSTEM_PROMPT = """你是一个信息抽取引擎，从用户文本中提取姓名、邮箱、年龄。
- 文本中不存在的信息返回 null，不要编造。
- 邮箱必须严格符合 name@domain.tld 格式。
- 只输出符合给定 JSON Schema 的数据，不要输出任何解释。"""


@register_skill
class UserExtractorSkill(BaseSkill):
    name = "user_extractor"
    description = "从文本中提取用户姓名 / 邮箱 / 年龄"
    input_schema = UserInput
    output_schema = UserOutput
    middleware_names = ["circuit_breaker", "contract"]

    async def execute(self, context: SkillContext) -> Any:
        # 延迟导入：核心引擎与离线测试环境无需安装 litellm / instructor
        import instructor
        import litellm

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context.input_data.text},
        ]
        # contract 中间件自修复重试时注入的纠正提示（metadata 是中间件的通信通道）
        retry_prompt = context.metadata.get("retry_prompt")
        if retry_prompt:
            messages.append({"role": "user", "content": retry_prompt})

        client = instructor.from_litellm(litellm.acompletion)
        # 模型选择：fallback 中间件降级时通过 metadata 覆盖（约定键 llm_model_override）
        model = context.metadata.get("llm_model_override") or settings.llm_model
        return await client.chat.completions.create(
            model=model,
            response_model=UserOutput,
            messages=messages,
            max_retries=2,  # instructor 层的结构化重试；引擎层的重试由 contract 中间件编排
        )
