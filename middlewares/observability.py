"""可观测性中间件：OpenTelemetry Span 埋点。"""

from __future__ import annotations

import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from core.context import SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware

_TRACER = trace.get_tracer("ai_skill_engine")


@register_middleware
class TracingMiddleware(BaseMiddleware):
    """为每次技能调用创建 Span：skill.name / skill.input_size / skill.latency_ms，
    异常时记录 error.type 并标记 Span 状态为 ERROR 后原样抛出。"""

    name = "tracing"

    def __init__(self, tracer: trace.Tracer | None = None) -> None:
        self._tracer = tracer or _TRACER

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        with self._tracer.start_as_current_span(f"skill.{context.skill_name}") as span:
            span.set_attribute("skill.name", context.skill_name)
            span.set_attribute("skill.input_size", len(str(context.input_data)))
            start = time.perf_counter()
            try:
                result = await next(context)
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                span.set_attribute("skill.latency_ms", round(latency_ms, 3))
            span.set_attribute("skill.output_size", len(str(context.final_output)))
            return result
