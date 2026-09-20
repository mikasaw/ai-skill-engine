"""Skill 基类与全局注册表 (Registry Pattern)。

约束（洋葱模型纪律）：execute 内只允许出现业务逻辑（Prompt 构造 + 一次 LLM 调用），
重试、校验、日志、监控等切面逻辑一律放入 middlewares/。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Type

from pydantic import BaseModel

from core.context import SkillContext


class BaseSkill(ABC):
    """技能基类：声明契约 (Schema) 与核心 execute 逻辑。"""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    input_schema: ClassVar[Type[BaseModel]]
    output_schema: ClassVar[Type[BaseModel]]
    # 该技能默认挂载的中间件（在全局中间件的内层执行）；
    # 元素为中间件名，或 (名字, 构造参数字典)。
    middleware_names: ClassVar[list] = []

    @abstractmethod
    async def execute(self, context: SkillContext) -> Any:
        """执行业务逻辑，返回值将被写入 context.final_output。"""


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Type[BaseSkill]] = {}

    def register(self, skill_cls: Type[BaseSkill]) -> Type[BaseSkill]:
        name = skill_cls.name
        if not name:
            raise ValueError("Skill 类必须定义非空 name")
        if name in self._skills:
            raise ValueError(f"Skill '{name}' 重复注册")
        self._skills[name] = skill_cls
        return skill_cls

    def get(self, name: str) -> Type[BaseSkill]:
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(
                f"Skill '{name}' 未注册，已注册: {sorted(self._skills)}"
            ) from None

    def create(self, name: str, **kwargs: Any) -> BaseSkill:
        return self.get(name)(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._skills)


skill_registry = SkillRegistry()


def register_skill(skill_cls: Type[BaseSkill]) -> Type[BaseSkill]:
    """装饰器：将 Skill 类注册到全局注册表。"""
    return skill_registry.register(skill_cls)
