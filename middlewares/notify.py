"""通知中间件：技能成功/失败后向 webhook 推送结果。

设计约束：通知是旁路——推送失败只记入 context.metadata["notify_log"]，
绝不影响主流程，也绝不改变技能的返回值或异常。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Sequence
from typing import Any

from config import settings
from core.context import SkillContext
from core.middleware import BaseMiddleware, Next, register_middleware


@register_middleware
class NotifyMiddleware(BaseMiddleware):
    """在 next() 之后（或异常时）POST 一条 JSON 到 webhook_url。

    - events: 需要通知的事件，取值子集 {"success", "failure"}；
      contract 重试耗尽等非致命失败会以 ok=False 落在 context 上，按 failure 通知；
    - 未配置 webhook_url 时静默跳过。
    """

    name = "notify"

    def __init__(
        self,
        webhook_url: str | None = None,
        events: Sequence[str] = ("success", "failure"),
        timeout_seconds: float = 5.0,
    ) -> None:
        self._url = webhook_url if webhook_url is not None else settings.notify_webhook_url
        self._events = set(events)
        self._timeout = timeout_seconds

    async def process(self, context: SkillContext, next: Next) -> SkillContext:
        start = time.perf_counter()
        try:
            result = await next(context)
        except Exception as exc:
            await self._send("failure", context, start, error=f"{type(exc).__name__}: {exc}")
            raise
        await self._send("success" if context.ok else "failure", context, start)
        return result

    async def _send(
        self,
        event: str,
        context: SkillContext,
        start: float,
        **extra: Any,
    ) -> None:
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        payload = {
            "event": event,
            "skill": context.skill_name,
            "ok": event == "success",
            "errors": context.errors,
            "latency_ms": latency_ms,
            **extra,
        }
        if not self._url:
            context.metadata.setdefault("notify_log", []).append(f"skipped ({event}): no webhook_url")
            return
        if event not in self._events:
            context.metadata.setdefault("notify_log", []).append(f"skipped ({event}): not subscribed")
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._post_sync, payload), timeout=self._timeout
            )
            context.metadata.setdefault("notify_log", []).append(f"sent ({event})")
        except Exception as exc:
            context.metadata.setdefault("notify_log", []).append(
                f"webhook failed ({event}): {type(exc).__name__}: {exc}"
            )

    def _post_sync(self, payload: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout):
            return None
