"""技能文档经验合入的回归测试：瞬态重试、runtime_recovery 恢复链路、system 评论标记。"""

import asyncio
import json

import multica_qa_loop as qa_loop
import skills.multica_qa as multica_qa


class FakeProc:
    def __init__(self, rc: int, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self._out = out.encode("utf-8")
        self._err = err.encode("utf-8")

    async def communicate(self):
        return self._out, self._err


async def test_run_multica_retries_on_transient_daemon_pressure(monkeypatch):
    """invalid workspace_id = daemon 并发压力假性失败：重试一次应成功（勿改配置）。"""
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc(5, "", "Invalid request: invalid workspace_id")
        return FakeProc(0, "[]", "")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await multica_qa.run_multica("issue", "list", "--output", "json")

    assert result == "[]"
    assert calls["n"] == 2


async def test_run_multica_no_retry_on_real_failure(monkeypatch):
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        return FakeProc(1, "", "multica CLI 执行失败: 真错误")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    try:
        await multica_qa.run_multica("issue", "get", "MIT-X")
        raised = False
    except RuntimeError:
        raised = True

    assert raised
    assert calls["n"] == 1  # 非瞬态错误不重试


async def test_fetch_comments_marks_system_comments(monkeypatch):
    """type=system 评论携带 429/402 等平台诊断，须保留并标记给评审者。"""

    async def fake_run(*args: str) -> str:
        return json.dumps([
            {"content": "交付完成", "type": "comment"},
            {"content": "API Error: 429 usage limit reached", "type": "system"},
            {"content": "", "type": "comment"},
        ])

    monkeypatch.setattr(multica_qa, "run_multica", fake_run)

    result = await multica_qa.fetch_comments("MIT-1")

    assert result == ["交付完成", "[system] API Error: 429 usage limit reached"]


async def test_watch_continues_past_failed_when_recovery_task_appears(monkeypatch):
    """runtime_recovery：failed 后紧跟更新的 running 任务 = 正常恢复链路，不算失败。"""
    seq = [
        {"status": "failed", "created_at": "2026-09-21T10:00:00Z"},
        {"status": "running", "created_at": "2026-09-21T10:00:05Z"},
        {"status": "completed", "created_at": "2026-09-21T10:01:00Z"},
    ]

    async def fake_latest(issue_id: str):
        return seq.pop(0) if seq else {"status": "completed", "created_at": "2026-09-21T10:02:00Z"}

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.01)

    assert summary["run_status"] == "completed"
    assert not seq  # 恢复任务被消费后才判定终止
