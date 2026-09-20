"""极薄 webhook 接收器：把 HTTP 事件映射为本地技能执行（stdlib 实现，零新依赖）。

请求契约：
    POST /  {"skill": "技能注册名", "input": {技能 input_schema 的字段}}
    - "input_data" 为 "input" 的别名；
    - 未携带 "skill" 时使用 --default-skill；
    - 响应：{"skill", "ok", "errors", "final_output"}。

接入 Multica 的两种方式：
1. 本地进程直连（推荐，无需公网）：multica_qa_loop.py / 巡逻脚本直接 POST 到本服务；
2. Multica autopilot 的 webhook 触发：需要公网可达地址（隧道），
   且 autopilot 的原生 payload 需要在前面加一层适配转换为上述请求契约
   （首次拿到真实 payload 形状后再补适配器）。

安全：默认只绑定 127.0.0.1；公网暴露时务必配 --token（X-Webhook-Token 头校验）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core.runner import SkillRunner

MAX_BODY_BYTES = 1 << 20  # 1 MiB


def _to_jsonable(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else str(value)


def make_server(
    port: int,
    default_skill: str | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    runner = SkillRunner()

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=_to_jsonable).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802（http.server 命名约定）
            if self.path == "/health":
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if token and self.headers.get("X-Webhook-Token") != token:
                self._send_json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"error": "invalid body size"})
                return
            try:
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                skill_name = request.get("skill") or default_skill
                input_data = request.get("input", request.get("input_data"))
                if not skill_name:
                    self._send_json(400, {"error": "缺少 'skill' 且未配置 default_skill"})
                    return
                # 引擎层异常统一落账进 context.errors，run() 本身不抛
                context = asyncio.run(runner.run(str(skill_name), input_data))
            except Exception as exc:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(
                200,
                {
                    "skill": context.skill_name,
                    "ok": context.ok,
                    "errors": context.errors,
                    "final_output": context.final_output,
                },
            )

        def log_message(self, *args: Any) -> None:  # 静默默认访问日志
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="ai_skill_engine webhook 接收器")
    parser.add_argument("--port", type=int, default=int(os.getenv("SKILL_ENGINE_WEBHOOK_PORT", "8787")))
    parser.add_argument("--default-skill", default=os.getenv("SKILL_ENGINE_DEFAULT_SKILL"))
    parser.add_argument("--token", default=os.getenv("SKILL_ENGINE_WEBHOOK_TOKEN"))
    args = parser.parse_args()

    import middlewares  # noqa: F401  触发中间件注册
    import skills.user_extractor  # noqa: F401  触发技能注册
    import skills.multica_qa  # noqa: F401

    server = make_server(args.port, default_skill=args.default_skill, token=args.token)
    print(
        f"webhook receiver listening on http://127.0.0.1:{args.port}/ "
        f"(default_skill={args.default_skill or '未配置'}, token={'启用' if args.token else '未启用'})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
