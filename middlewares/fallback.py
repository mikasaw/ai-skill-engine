"""模型降级中间件：主模型失败时按降级链自动切换备用模型。

约定：技能在 execute 中通过 context.metadata 读取模型覆盖
（`llm_model_override`），未设置时用 config.settings.llm_model。
这是 metadata 作为中间件通信通道的第二个约定键（第一个是 retry_prompt）。

推荐挂载位置：circuit_breaker 之内、contract 之外——
熔断器把一次"完整降级尝试"记为一个逻辑调用；contract 的自修复重试
在每次模型切换后重新获得完整预算。
"""

from __future__ import annotations

from typing import Sequence

from config import settings
from core.context import MODEL_OVERRIDE_KEY, SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware


@register_middleware
class FallbackMiddleware(BaseMiddleware):
    """按 [默认模型, *fallback_models] 依序尝试，全部失败则抛出最后一次异常。

    每次失败的 {尝试序号, 模型, 异常} 记入 context.metadata["fallback_log"]，
    实际生效的备用模型写入 context.metadata["fallback_in_use"]。
    """

    name = "fallback"

    def __init__(self, fallback_models: Sequence[str] | None = None) -> None:
        self._fallback_models = list(fallback_models) if fallback_models is not None else list(settings.fallback_models)

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        attempts: list[str] = ["", *self._fallback_models]  # "" 表示技能默认模型
        log = context.metadata.setdefault("fallback_log", [])
        last_exc: Exception | None = None
        for index, model in enumerate(attempts):
            if model:
                context.metadata[MODEL_OVERRIDE_KEY] = model
                context.metadata["fallback_in_use"] = model
            try:
                return await next(context)
            except Exception as exc:
                last_exc = exc
                label = model or "default"
                log.append(
                    f"attempt {index + 1} (model={label}) failed: {type(exc).__name__}: {exc}"
                )
        raise last_exc  # 循环至少执行一次，此处必非 None；保留原始异常类型
