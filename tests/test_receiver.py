"""webhook 接收器集成测试：真实 HTTP 服务（随机端口）+ 标准库客户端。"""

import json
import threading
import urllib.error
import urllib.request

import pytest
from pydantic import BaseModel

from core.skill import BaseSkill, register_skill
from webhook_receiver import make_server


class PingInput(BaseModel):
    text: str


class PingOutput(BaseModel):
    text: str


@register_skill
class ReceiverPingSkill(BaseSkill):
    name = "receiver_ping"
    description = "大写回显（供接收器集成测试）"
    input_schema = PingInput
    output_schema = PingOutput
    middleware_names = []

    async def execute(self, context):
        return {"text": context.input_data.text.upper()}


@pytest.fixture()
def server_base_url():
    server = make_server(port=0, default_skill="receiver_ping", token=None)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_health_endpoint(server_base_url):
    with urllib.request.urlopen(f"{server_base_url}/health", timeout=10) as response:
        assert json.loads(response.read().decode("utf-8")) == {"status": "ok"}


def test_post_executes_skill_and_returns_result(server_base_url):
    status, body = _post(server_base_url + "/", {"skill": "receiver_ping", "input": {"text": "abc"}})

    assert status == 200
    assert body["ok"] is True
    assert body["errors"] == []
    assert body["final_output"] == {"text": "ABC"}


def test_default_skill_used_when_missing(server_base_url):
    status, body = _post(server_base_url + "/", {"input": {"text": "xyz"}})

    assert status == 200
    assert body["final_output"] == {"text": "XYZ"}


def test_unknown_skill_reported_not_raised(server_base_url):
    status, body = _post(server_base_url + "/", {"skill": "no_such_skill", "input": {}})

    assert status == 200
    assert body["ok"] is False
    assert any("未注册" in err for err in body["errors"])


def test_token_check_rejects_unsigned_requests():
    server = make_server(port=0, default_skill="receiver_ping", token="s3cret")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        status, _ = _post(base + "/", {"input": {"text": "a"}})
        assert status == 401

        status, body = _post(base + "/", {"input": {"text": "a"}}, headers={"X-Webhook-Token": "s3cret"})
        assert status == 200
        assert body["ok"] is True
    finally:
        server.shutdown()
        server.server_close()
