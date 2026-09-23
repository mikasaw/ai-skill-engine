"""全局配置。

只存放跨层共享的运行参数；技能私有参数放各自技能类上，
中间件参数在装配时通过 (name, options) 传入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # LLM：通过 litellm 的模型字符串切换厂商，如 gpt-4o-mini / anthropic/claude-3-sonnet
    llm_model: str = os.getenv("SKILL_ENGINE_MODEL", "gpt-4o-mini")

    # fallback 中间件默认降级链（逗号分隔，主模型失败后依序切换）
    fallback_models: list[str] = field(
        default_factory=lambda: [m for m in os.getenv("SKILL_ENGINE_FALLBACK_MODELS", "").split(",") if m]
    )

    # notify 中间件默认 webhook 地址
    notify_webhook_url: str = os.getenv("SKILL_ENGINE_WEBHOOK_URL", "")

    # multica CLI 可执行文件路径：默认按 Windows 标准变量展开（不含用户名）；
    # 非 Windows 或自定义安装时务必设置 MULTICA_BIN 环境变量覆盖
    multica_bin: str = os.getenv(
        "MULTICA_BIN",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Programs\@multicadesktop\resources\app.asar.unpacked\resources\bin\multica.exe"
        ),
    )

    # 挂到所有技能外层的全局中间件（在技能级中间件的外层执行）
    global_middlewares: list[str] = field(default_factory=lambda: ["tracing"])


settings = Settings()
