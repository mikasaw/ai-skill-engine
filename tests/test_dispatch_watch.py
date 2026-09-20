"""dispatch / watch / 通过自动关单测试：全部 stub multica CLI，离线运行。"""

import json

import pytest

import multica_qa_loop as qa_loop


@pytest.fixture()
def in_tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------- dispatch ----------


async def test_dispatch_creates_issue_and_cleans_temp_plan(in_tmp_workspace, monkeypatch):
    plan = in_tmp_workspace / "plan.md"
    plan.write_text("# 实现登录页\n\n详细需求……", encoding="utf-8")
    captured: dict = {}

    async def fake_run_multica(*args: str) -> str:
        argv = list(args)
        plan_path = argv[argv.index("--description-file") + 1]
        captured["argv"] = argv
        captured["plan_content"] = open(plan_path, encoding="utf-8").read()
        return json.dumps({"id": "issue-uuid", "identifier": "MIT-600", "title": "实现登录页"})

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)

    result = await qa_loop.dispatch_phase("代码开发助手", str(plan))

    assert result == {
        "issue": "issue-uuid",
        "identifier": "MIT-600",
        "title": "实现登录页",
        "assignee": "代码开发助手",
    }
    assert captured["argv"][:2] == ["issue", "create"]
    assert captured["argv"][captured["argv"].index("--assignee") + 1] == "代码开发助手"
    assert captured["plan_content"].startswith("# 实现登录页")  # 方案内容完整传入
    leftovers = [p.name for p in in_tmp_workspace.iterdir() if p.name.startswith("dispatch_plan_")]
    assert leftovers == []  # 临时方案文件已清理


async def test_dispatch_derives_title_from_first_heading(in_tmp_workspace, monkeypatch):
    plan = in_tmp_workspace / "plan.md"
    plan.write_text("# 修复熔断器半开窗口\n\n正文", encoding="utf-8")
    captured: dict = {}

    async def fake_run_multica(*args: str) -> str:
        argv = list(args)
        captured["title"] = argv[argv.index("--title") + 1]
        return json.dumps({"id": "uuid", "identifier": "MIT-601", "title": captured["title"]})

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)

    await qa_loop.dispatch_phase("dev", str(plan))  # 未传 --title
    assert captured["title"] == "修复熔断器半开窗口"


# ---------- watch ----------


async def test_watch_returns_when_run_reaches_terminal_status(monkeypatch):
    calls = {"n": 0}

    async def fake_latest(issue_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "running"}
        return {"status": "completed", "completed_at": "2026-09-20T06:00:00Z", "error": None}

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.01)

    assert summary["run_status"] == "completed"
    assert calls["n"] == 2


async def test_watch_times_out_when_still_running(monkeypatch):
    async def fake_latest(issue_id: str):
        return {"status": "running"}

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.01, timeout_seconds=0.03)

    assert summary["timed_out"] is True
    assert summary["run_status"] == "running"


async def test_watch_tolerates_missing_run_after_dispatch(monkeypatch):
    async def fake_latest(issue_id: str):
        return None  # 刚派遣，run 尚未入库

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.01, timeout_seconds=0.03)

    assert summary["run_status"] == "unknown"
    assert summary["timed_out"] is True


def test_latest_run_picks_newest_by_created_at(in_tmp_workspace, monkeypatch):
    async def fake_run_multica(*args: str) -> str:
        return json.dumps([
            {"id": "old", "status": "completed", "created_at": "2026-09-20T03:00:00Z"},
            {"id": "new", "status": "running", "created_at": "2026-09-20T06:00:00Z"},
        ])

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)

    import asyncio

    run = asyncio.run(qa_loop.latest_run("MIT-1"))
    assert run["id"] == "new"


# ---------- submit 通过自动关单 ----------


@pytest.fixture()
def verdict_workspace(in_tmp_workspace):
    verdict_file = qa_loop.verdict_path("MIT-1")
    verdict_file.parent.mkdir(parents=True, exist_ok=True)
    verdict_file.write_text(
        json.dumps({"passed": True, "score": 90, "reasons": [], "suggestions": []}),
        encoding="utf-8",
    )
    return verdict_file


async def test_submit_pass_closes_issue_without_waking_agent(in_tmp_workspace, verdict_workspace, monkeypatch):
    cli_calls: list[list[str]] = []
    sent: list[str] = []

    async def fake_run_multica(*args: str) -> str:
        cli_calls.append(list(args))
        return "[]"

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)
    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.submit_phase("MIT-1")

    assert result == {"issue": "MIT-1", "passed": True, "score": 90, "closed": True}
    assert ["issue", "status", "MIT-1", "done", "--no-start"] in cli_calls  # --no-start 防止唤醒 agent
    assert not verdict_workspace.exists()  # 工单消费


async def test_submit_reject_never_closes_issue(in_tmp_workspace, monkeypatch):
    verdict_file = qa_loop.verdict_path("MIT-1")
    verdict_file.parent.mkdir(parents=True, exist_ok=True)
    verdict_file.write_text(
        json.dumps({"passed": False, "score": 30, "reasons": ["问题"], "suggestions": []}),
        encoding="utf-8",
    )
    cli_calls: list[list[str]] = []

    async def fake_run_multica(*args: str) -> str:
        cli_calls.append(list(args))
        return "[]"

    async def fake_post(issue_id: str, content: str) -> None:
        return None

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)
    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.submit_phase("MIT-1")

    assert result["closed"] is False  # 打回 = 留单重做
    assert not any(args[:2] == ["issue", "status"] for args in cli_calls)


async def test_submit_no_close_flag_skips_closing(in_tmp_workspace, verdict_workspace, monkeypatch):
    cli_calls: list[list[str]] = []

    async def fake_run_multica(*args: str) -> str:
        cli_calls.append(list(args))
        return "[]"

    async def fake_post(issue_id: str, content: str) -> None:
        return None

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)
    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.submit_phase("MIT-1", close_on_pass=False)

    assert result["closed"] is False
    assert not any(args[:2] == ["issue", "status"] for args in cli_calls)
