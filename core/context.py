"""执行上下文 (Context)：一次技能调用的全部状态载体。

中间件与技能之间只通过 context 通信：
- metadata  用于中间件之间（以及向重试中的技能）传递数据；
- errors    记录过程中产生的（非致命）错误，保持追加式审计日志。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# metadata 通道约定键：中间件与技能之间传递数据的公共契约（归属核心层，避免反向依赖）
RETRY_PROMPT_KEY = "retry_prompt"          # contract 中间件注入的输出纠正提示
MODEL_OVERRIDE_KEY = "llm_model_override"  # fallback 中间件注入的模型覆盖
LLM_CALLS_KEY = "llm_calls"                # cost_limiter 维护的本 run 调用计数


class SkillContext(BaseModel):
    skill_name: str
    input_data: Any = None
    llm_response: Any = None
    final_output: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors
