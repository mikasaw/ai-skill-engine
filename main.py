"""运行入口与演示。

初始化 OpenTelemetry ConsoleExporter，运行三个测试用例：
1. 正常 Case   —— 全链路一次通过；
2. 脏数据 Case —— 输出不满足契约，contract 中间件注入 retry_prompt 自修复重试；
3. 超时 Case   —— 模拟 LLM 响应极慢，defensive 中间件强制超时。

设置 OPENAI_API_KEY 后走真实 LLM（litellm + instructor，模型见 config.settings.llm_model）；
未设置时自动使用内置 Mock 技能离线演示，经过的中间件链路与真实模式完全一致。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from config import settings
from core.context import SkillContext
from core.runner import SkillRunner
from core.skill import BaseSkill, register_skill, skill_registry
import middlewares  # noqa: E402,F401  触发内置中间件注册

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

import skills.user_extractor  # noqa: E402,F401  触发技能注册
from skills.user_extractor import UserInput, UserOutput  # noqa: E402

HAS_REAL_LLM = bool(os.getenv("OPENAI_API_KEY"))


def setup_tracing() -> None:
    """ConsoleExporter：把每个 Span 直接打印到终端（生产可换 OTLP 导出器）。"""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


if not HAS_REAL_LLM:
    # 离线 Mock 技能：与真实技能同 Schema、同中间件链，仅替换 execute 内的数据来源

    class _MockSkillBase(BaseSkill):
        input_schema = UserInput
        output_schema = UserOutput

    @register_skill
    class NormalMockSkill(_MockSkillBase):
        name = "mock_normal"
        description = "离线 Mock：直接返回合法结果"
        middleware_names = ["circuit_breaker", "contract"]

        async def execute(self, context: SkillContext) -> Any:
            return {"name": "张三", "email": "zhangsan@example.com", "age": 28}

    @register_skill
    class DirtyMockSkill(_MockSkillBase):
        name = "mock_dirty"
        description = "离线 Mock：首次返回非法邮箱，收到 retry_prompt 后自愈"
        middleware_names = ["circuit_breaker", "contract"]

        async def execute(self, context: SkillContext) -> Any:
            if context.metadata.get("retry_prompt"):
                return {"name": "李四", "email": "lisi@example.com", "age": 35}
            return {"name": "李四", "email": "lisi#example.com", "age": 35}

    @register_skill
    class SlowMockSkill(_MockSkillBase):
        name = "mock_slow"
        description = "离线 Mock：模拟 5 秒慢响应，触发强制超时"
        middleware_names = ["circuit_breaker", "contract"]

        async def execute(self, context: SkillContext) -> Any:
            await asyncio.sleep(5)
            return {"name": "王五", "email": "wangwu@example.com", "age": 40}

    @register_skill
    class FallbackMockSkill(_MockSkillBase):
        name = "mock_fallback"
        description = "离线 Mock：默认模型必然失败，fallback 切换备用模型后成功"
        middleware_names = ["fallback", "contract"]

        async def execute(self, context: SkillContext) -> Any:
            if not context.metadata.get("llm_model_override"):
                raise ConnectionError("primary model unavailable (mock)")
            return {"name": "赵六", "email": "zhaoliu@example.com", "age": 30}


async def run_demo() -> None:
    runner = SkillRunner()
    if HAS_REAL_LLM:
        cases = [
            ("正常 Case（真实 LLM）", "user_extractor",
             {"text": "我叫张三，邮箱 zhangsan@example.com，今年 28 岁。"}, None),
            ("脏数据 Case（真实 LLM，坏邮箱触发自修复）", "user_extractor",
             {"text": "我叫李四，我的邮箱是 lisi#example.com，今年 35 岁。"}, None),
            ("超时 Case（真实 LLM，0.05s 强制超时）", "user_extractor",
             {"text": "我叫王五，邮箱 wangwu@example.com，今年 40 岁。"},
             ["tracing", ("circuit_breaker", {"timeout_seconds": 0.05}), "contract"]),
        ]
    else:
        cases = [
            ("正常 Case（Mock）", "mock_normal", {"text": "张三 zhangsan@example.com 28"}, None),
            ("脏数据 Case（Mock，坏邮箱触发自修复）", "mock_dirty",
             {"text": "李四 lisi#example.com 35"}, None),
            ("超时 Case（Mock，5s 慢响应 / 1s 超时）", "mock_slow",
             {"text": "王五 wangwu@example.com 40"},
             ["tracing", ("circuit_breaker", {"timeout_seconds": 1.0}), "contract"]),
            ("降级 Case（Mock，主模型失败自动切换备用）", "mock_fallback",
             {"text": "赵六 zhaoliu@example.com 30"},
             ["tracing", ("fallback", {"fallback_models": ["gpt-4o", "claude-sonnet"]}), "contract"]),
        ]

    for title, skill_name, payload, override in cases:
        context = await runner.run(skill_name, payload, middlewares=override)
        report(title, context)


def report(title: str, context: SkillContext) -> None:
    line = "=" * 72
    output = context.final_output
    output_str = output.model_dump() if hasattr(output, "model_dump") else output
    print(f"\n{line}\n■ {title}\n{line}")
    print(f"  skill        : {context.skill_name}")
    print(f"  final_output : {output_str}")
    print(f"  retries      : {context.metadata.get('contract_failures', 0)} 次契约校验失败")
    print(f"  fallback     : {context.metadata.get('fallback_in_use', '—（未降级）')}")
    print(f"  llm_calls    : {context.metadata.get('llm_calls', '—')}（estimated_tokens: {context.metadata.get('estimated_tokens', '—')}）")
    print(f"  errors       : {context.errors if context.errors else '（无）'}")


def main() -> None:
    setup_tracing()
    mode = (
        f"真实 LLM（{settings.llm_model}，litellm + instructor）"
        if HAS_REAL_LLM
        else "离线 Mock（未检测到 OPENAI_API_KEY）"
    )
    print(f"运行模式: {mode}")
    print(f"已注册技能: {skill_registry.names()}")
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
