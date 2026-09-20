"""契约中间件：输出 Schema 校验 + 失败自修复重试（引擎层自愈的核心）。"""

from __future__ import annotations

import json

from pydantic import ValidationError

from core.context import RETRY_PROMPT_KEY, SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware
from core.skill import skill_registry

__all__ = ["RETRY_PROMPT_KEY", "SchemaValidationMiddleware"]


@register_middleware
class SchemaValidationMiddleware(BaseMiddleware):
    """校验 context.final_output 是否满足技能的 output_schema。

    校验失败时：
    1. 把 Pydantic 错误明细注入 context.metadata[RETRY_PROMPT_KEY]；
    2. 重新调用 next()（即带着纠正提示重新执行技能的 LLM 调用），
       最多重试 max_retries 次；
    3. 重试耗尽仍失败则把错误记入 context.errors 并返回最后一次输出，
       不向上抛异常，交由调用方根据 errors 决策。
    """

    name = "contract"

    def __init__(self, max_retries: int = 2) -> None:
        self._max_retries = max_retries

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        schema = skill_registry.get(context.skill_name).output_schema
        failures = 0
        while True:
            await next(context)
            try:
                # 校验通过的同时归一化：final_output 恒为 Schema 实例
                context.final_output = schema.model_validate(context.final_output)
                return context
            except ValidationError as exc:
                failures += 1
                context.metadata["contract_failures"] = failures
                # 瞬态重试走 metadata 审计日志；errors 只承载最终失败
                context.metadata.setdefault("contract_retry_log", []).append(_summarize(exc))
                if failures > self._max_retries:
                    context.add_error(
                        f"OutputValidationError: 输出在 {self._max_retries} 次重试后仍未通过校验: {_summarize(exc)}"
                    )
                    return context
                context.metadata[RETRY_PROMPT_KEY] = _build_retry_prompt(exc)


def _build_retry_prompt(exc: ValidationError) -> str:
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors(include_url=False)
    ]
    return (
        "你上一次返回的数据未通过 JSON Schema 校验，错误明细如下：\n"
        f"{json.dumps(details, ensure_ascii=False, indent=2)}\n"
        "请严格对照 Schema 修正所有字段后重新输出，不要附加任何解释。"
    )


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_url=False)
    )
