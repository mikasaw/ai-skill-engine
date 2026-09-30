"""第一梯队加固测试：成本账本、watch 双加固、agent-harden。"""

import json

import pytest

import multica_qa_loop as qa_loop


@pytest.fixture()
def in_tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------- ① 成本账本 ----------


def test_append_ledger_writes_jsonl(in_tmp_workspace):
    qa_loop.append_ledger("dispatch", "MIT-1", assignee="dev")
    qa_loop.append_ledger("watch", "MIT-1", run_status="completed", waited_seconds=120)
    qa_loop.append_ledger("submit", "MIT-1", passed=False, score=70, closed=False)

    lines = qa_loop.LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    records = [json.loads(ln) for ln in lines]
    assert [r["event"] for r in records] == ["dispatch", "watch", "submit"]
    assert records[0]["issue"] == "MIT-1" and records[0]["assignee"] == "dev"
    assert all("ts" in r for r in records)


def test_ledger_summary_aggregates(in_tmp_workspace):
    qa_loop.append_ledger("watch", "A", waited_seconds=100)
    qa_loop.append_ledger("watch", "B", waited_seconds=50)
    qa_loop.append_ledger("submit", "A", passed=True, score=90, closed=True)
    qa_loop.append_ledger("submit", "B", passed=False, score=70, closed=False)

    summary = qa_loop.ledger_summary()

    assert summary["events"]["watch"] == 2
    assert summary["watch_seconds"] == 150
    assert summary["reject_rate"] == 0.5


def test_ledger_summary_empty_and_corrupt_lines(in_tmp_workspace):
    assert qa_loop.ledger_summary() == {"events": {}, "watch_seconds": 0, "reject_rate": None}
    qa_loop.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    qa_loop.LEDGER_PATH.write_text("not-json\n", encoding="utf-8")
    assert qa_loop.ledger_summary()["events"] == {}  # 坏行跳过不炸


# ---------- ② watch 双加固 ----------


async def test_watch_rejects_wrong_assignee(monkeypatch):
    async def fake_get(*args: str) -> str:
        return json.dumps({"assignee_id": "agent-uuid-x", "assignee": {"id": "agent-uuid-x", "name": "别的项目的agent"}})

    monkeypatch.setattr(qa_loop, "run_multica", fake_get)

    with pytest.raises(RuntimeError, match="可能盯错了单"):
        await qa_loop.watch_phase("MIT-1", poll_seconds=0.01, expect_agent="mdboard-dev")


async def test_watch_accepts_matching_assignee(monkeypatch):
    checks = {"polls": 0}

    async def fake_get(*args: str) -> str:
        return json.dumps({"assignee_id": "mdboard-dev", "assignee": {"id": "mdboard-dev", "name": "mdboard-dev"}})

    async def fake_latest(issue_id: str):
        checks["polls"] += 1
        return {"status": "completed", "completed_at": "T", "created_at": "T"}

    monkeypatch.setattr(qa_loop, "run_multica", fake_get)
    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.01, expect_agent="mdboard-dev")

    assert summary["run_status"] == "completed"


async def test_watch_loop_resets_timeout(monkeypatch):
    calls = {"n": 0}

    async def fake_latest(issue_id: str):
        calls["n"] += 1
        return {"status": "running", "created_at": "T"}  # 永远 running

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    # timeout=0.02 poll=0.01：第一次超时后 loop 重置；给 monkeypatch 打补丁让第二轮超时直接退出
    # （用 pytest 的 timeout 防死循环：若 loop 失效会永转，测试超时失败）
    async def stop_after_two_timeouts():
        return None

    import asyncio

    async def run_bounded():
        task = asyncio.create_task(qa_loop.watch_phase("MIT-1", poll_seconds=0.001, timeout_seconds=0.002, loop_on_timeout=True))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        if not done:
            task.cancel()
            raise AssertionError("--loop 模式未按预期重置计时（或卡死）")
        return task.result()

    # loop 模式永不返回：验证它在超时后仍在运行（即没有直接退出）
    with pytest.raises(AssertionError):
        await run_bounded()


async def test_watch_timeout_without_loop_returns(in_tmp_workspace, monkeypatch):
    async def fake_latest(issue_id: str):
        return {"status": "running", "created_at": "T"}

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    summary = await qa_loop.watch_phase("MIT-1", poll_seconds=0.001, timeout_seconds=0.002)

    assert summary["timed_out"] is True
    assert qa_loop.LEDGER_PATH.exists()  # 超时也记账


async def test_watch_completion_records_ledger(in_tmp_workspace, monkeypatch):
    async def fake_latest(issue_id: str):
        return {"status": "completed", "completed_at": "T", "created_at": "T"}

    monkeypatch.setattr(qa_loop, "latest_run", fake_latest)

    await qa_loop.watch_phase("MIT-1", poll_seconds=0.01)

    record = json.loads(qa_loop.LEDGER_PATH.read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "watch" and record["run_status"] == "completed"


# ---------- ③ agent-harden ----------


def _fake_agents_ops(monkeypatch, current: str, updated: dict):
    async def fake_run(*args: str) -> str:
        argv = list(args)
        if argv[:2] == ["agent", "list"]:
            return json.dumps([{"name": "mdboard-dev", "id": "uuid-1"}])
        if argv[:2] == ["agent", "get"]:
            return json.dumps({"instructions": updated.get("text", current)})
        if argv[:2] == ["agent", "update"]:
            updated["text"] = argv[argv.index("--instructions") + 1]
            return "{}"
        return "{}"

    monkeypatch.setattr(qa_loop, "run_multica", fake_run)


async def test_agent_harden_appends_and_verifies(tmp_path, monkeypatch):
    addition = tmp_path / "add.md"
    addition.write_text("\n## 守卫段（v2）\n- 新条款\n", encoding="utf-8")
    updated = {"text": "原有 instructions。"}
    _fake_agents_ops(monkeypatch, "原有 instructions。", updated)

    result = await qa_loop.agent_harden_phase("mdboard-dev", str(addition), marker="守卫段（v2）")

    assert result["skipped"] is False
    assert "守卫段（v2）" in updated["text"] and "原有 instructions。" in updated["text"]


async def test_agent_harden_idempotent_on_marker(tmp_path, monkeypatch):
    addition = tmp_path / "add.md"
    addition.write_text("\n## 守卫段（v2）\n- 新条款\n", encoding="utf-8")
    base = "原有 instructions。\n## 守卫段（v2）\n- 旧条款\n"
    updated = {"text": base}
    _fake_agents_ops(monkeypatch, base, updated)

    result = await qa_loop.agent_harden_phase("mdboard-dev", str(addition), marker="守卫段（v2）")

    assert result["skipped"] is True
    assert updated["text"] == base  # 未动


async def test_agent_harden_rejects_empty_addition(tmp_path, monkeypatch):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    updated = {"text": "base"}
    _fake_agents_ops(monkeypatch, "base", updated)

    with pytest.raises(ValueError, match="拒绝执行"):
        await qa_loop.agent_harden_phase("mdboard-dev", str(empty), marker="x")


async def test_agent_harden_rejects_empty_baseline(tmp_path, monkeypatch):
    addition = tmp_path / "add.md"
    addition.write_text("## 新段\n", encoding="utf-8")
    updated = {"text": ""}
    _fake_agents_ops(monkeypatch, "", updated)

    with pytest.raises(ValueError, match="空基线"):
        await qa_loop.agent_harden_phase("mdboard-dev", str(addition), marker="x")


async def test_agent_harden_rejects_over_length(tmp_path, monkeypatch):
    addition = tmp_path / "add.md"
    addition.write_text("x" * 100, encoding="utf-8")
    base = "y" * 1750  # 合并后 1850 > 1800
    updated = {"text": base}
    _fake_agents_ops(monkeypatch, base, updated)

    with pytest.raises(ValueError, match="安全线"):
        await qa_loop.agent_harden_phase("mdboard-dev", str(addition), marker="x")


async def test_agent_harden_detects_truncated_writeback(tmp_path, monkeypatch):
    addition = tmp_path / "add.md"
    addition.write_text("## 守卫段\n- 条款\n", encoding="utf-8")
    base = "原有 instructions。"
    updated = {"text": base}

    def fake_run(*args: str):
        import asyncio

        async def inner():
            argv = list(args)
            if argv[:2] == ["agent", "list"]:
                return json.dumps([{"name": "mdboard-dev", "id": "uuid-1"}])
            if argv[:2] == ["agent", "get"]:
                # 写入后回读：模拟 CLI 截断（只回一半）
                if updated.get("written"):
                    return json.dumps({"instructions": (updated["written"])[:10]})
                return json.dumps({"instructions": base})
            if argv[:2] == ["agent", "update"]:
                updated["written"] = argv[argv.index("--instructions") + 1]
                return "{}"
            return "{}"

        return inner()

    monkeypatch.setattr(qa_loop, "run_multica", lambda *a: fake_run(*a))

    with pytest.raises(RuntimeError, match="回读核对失败"):
        await qa_loop.agent_harden_phase("mdboard-dev", str(addition), marker="守卫段")
