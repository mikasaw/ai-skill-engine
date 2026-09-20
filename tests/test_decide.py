"""decide 决策表机验测试：Rule A（脏 worktree）/C（基线前进）/E（唤醒超限）/默认/覆盖，及追评入口。"""

import json
import subprocess

import pytest

import multica_qa_loop as qa_loop


def _git(cwd, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _head(repo) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("v1", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


# ---------- Rule A：脏 worktree ----------


async def test_rule_a_dirty_worktree_verdicts_followup(git_repo):
    (git_repo / "dirty.txt").write_text("wip", encoding="utf-8")  # 未提交

    result = await qa_loop.decide_phase(worktree=str(git_repo))

    assert result["verdict"] == "追评"
    assert result["rule"] == "A"
    assert "禁 rerun" in result["reason"]


# ---------- Rule C：基线前进 ----------


async def test_rule_c_baseline_moved_verdicts_new_issue(git_repo):
    baseline = _head(git_repo)
    (git_repo / "b.txt").write_text("v2", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "second")  # 基线前进，worktree 干净

    result = await qa_loop.decide_phase(worktree=str(git_repo), baseline=baseline)

    assert result["verdict"] == "新建"
    assert result["rule"] == "C"


async def test_rule_a_takes_precedence_over_c(git_repo):
    baseline = _head(git_repo)
    (git_repo / "b.txt").write_text("v2", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "second")  # C 条件成立
    (git_repo / "c.txt").write_text("wip", encoding="utf-8")  # 但 A 条件也成立

    result = await qa_loop.decide_phase(worktree=str(git_repo), baseline=baseline)

    assert result["rule"] == "A"  # 表序：A 先命中即生效
    assert result["verdict"] == "追评"


# ---------- Rule E：上下文已肥 ----------


async def test_rule_e_fat_context_verdicts_new_issue(monkeypatch):
    async def fake_runs(*args: str) -> str:
        return json.dumps([{"id": str(i)} for i in range(8)])

    monkeypatch.setattr(qa_loop, "run_multica", fake_runs)

    result = await qa_loop.decide_phase(prev_issue="MIT-1")

    assert result["verdict"] == "新建"
    assert result["rule"] == "E"


async def test_rule_e_not_triggered_below_limit(monkeypatch):
    async def fake_runs(*args: str) -> str:
        return json.dumps([{"id": "1"}, {"id": "2"}])

    monkeypatch.setattr(qa_loop, "run_multica", fake_runs)

    result = await qa_loop.decide_phase(prev_issue="MIT-1")

    assert result["rule"] == "默认"  # 2 次 < 8，落到默认


# ---------- 默认与人工覆盖 ----------


async def test_default_verdicts_new_issue_with_human_checklist(git_repo):
    result = await qa_loop.decide_phase(worktree=str(git_repo))  # 干净、无基线

    assert result["verdict"] == "新建"
    assert result["rule"] == "默认"
    assert len(result["human_items"]) == 3  # B/D/F 留人工


async def test_git_failure_defers_to_human(git_repo, monkeypatch):
    missing = git_repo.parent / "no_such_repo"

    result = await qa_loop.decide_phase(worktree=str(missing))

    assert result["verdict"] == "人工判断"
    assert result["rule"] == "A"


async def test_force_overrides_machine_verdict():
    result = await qa_loop.decide_phase(force="追评")
    assert result == {"verdict": "追评", "rule": "manual", "reason": "人工覆盖", "human_items": []}


# ---------- 追评入口 ----------


async def test_comment_phase_posts_via_temp_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    async def fake_run_multica(*args: str) -> str:
        argv = list(args)
        content_file = argv[argv.index("--content-file") + 1]
        captured["argv"] = argv
        captured["content"] = open(content_file, encoding="utf-8").read()
        return "[]"

    monkeypatch.setattr(qa_loop, "run_multica", fake_run_multica)

    await qa_loop.comment_phase("MIT-1", "本轮判据：……", parent="cmt-9")

    assert captured["argv"][:3] == ["issue", "comment", "add"]
    assert captured["argv"][captured["argv"].index("--parent") + 1] == "cmt-9"
    assert "本轮判据" in captured["content"]
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("qa_comment_")]
    assert leftovers == []  # 临时文件已清理
