"""observability 中间件测试：Span 属性与错误记录（InMemorySpanExporter）。"""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, in_memory_span_exporter
from opentelemetry.trace import StatusCode

from core.context import SkillContext
from middlewares.observability import TracingMiddleware


def _middleware_with_memory_exporter():
    provider = TracerProvider()
    exporter = in_memory_span_exporter.InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return TracingMiddleware(tracer=tracer), exporter


async def test_success_span_attributes():
    mw, exporter = _middleware_with_memory_exporter()

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = {"ok": True}
        return ctx

    await mw.process(SkillContext(skill_name="demo", input_data={"text": "hello"}), terminal)

    (span,) = exporter.get_finished_spans()
    assert span.name == "skill.demo"
    assert span.attributes["skill.name"] == "demo"
    assert span.attributes["skill.input_size"] > 0
    assert span.attributes["skill.latency_ms"] >= 0
    assert span.status.status_code is StatusCode.UNSET


async def test_error_span_attributes():
    mw, exporter = _middleware_with_memory_exporter()

    async def failing(ctx: SkillContext) -> SkillContext:
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await mw.process(SkillContext(skill_name="demo"), failing)

    (span,) = exporter.get_finished_spans()
    assert span.attributes["error.type"] == "ValueError"
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["skill.latency_ms"] >= 0  # finally 中仍记录了耗时
