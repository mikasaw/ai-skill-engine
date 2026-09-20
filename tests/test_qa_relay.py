"""半程接力测试：材料落盘、verdict 契约校验、工单消费、回写路由。"""

import json

import pytest
from pydantic import ValidationError

import multica_qa_loop as qa_loop
from skills.multica_qa import QaVerdict


@pytest.fixture()
def in_tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # qa_reviews/ 落在临时目录，测试互不污染
    return tmp_path


def _stub_fetch(monkeypatch, title="实现登录页", description="带表单校验", comments=None):
    async def fake_fetch_issue(issue_id: str) -> dict:
        return {"title": title, "description": description}

    async def fake_fetch_comments(issue_id: str) -> list[str]:
        return list(comments or [])

    monkeypatch.setattr(qa_loop, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(qa_loop, "fetch_comments", fake_fetch_comments)


# ---------- fetch ----------


async def test_fetch_phase_writes_review_material(in_tmp_workspace, monkeypatch):
    _stub_fetch(monkeypatch, comments=["交付说明：已完成", "自测报告：全绿"])

    path = await qa_loop.fetch_phase("MIT-551")

    content = path.read_text(encoding="utf-8")
    assert path == qa_loop.material_path("MIT-551")
    assert path.parent.name == "qa_reviews"
    assert "# QA 评审材料 — MIT-551" in content
    assert "实现登录页" in content
    assert "带表单校验" in content
    assert "### 评论 1" in content and "交付说明：已完成" in content
    assert "submit MIT-551" in content  # 材料自带下一步指引


async def test_fetch_phase_handles_issue_without_comments(in_tmp_workspace, monkeypatch):
    _stub_fetch(monkeypatch, comments=[])

    path = await qa_loop.fetch_phase("abc-123")

    content = path.read_text(encoding="utf-8")
    assert "（无评论）" in content


def test_path_sanitization():
    assert qa_loop.safe_id("01a0bf3e-54ef-7be5-a811-fddfc490a5c4") == "01a0bf3e-54ef-7be5-a811-fddfc490a5c4"
    assert qa_loop.safe_id("MIT-551") == "MIT-551"
    assert "/" not in qa_loop.safe_id("a/b:c")
    assert qa_loop.verdict_path("MIT-551").name == "MIT-551.verdict.json"


# ---------- verdict 契约校验 ----------


async def test_submit_posts_comment_and_consumes_verdict_ticket(in_tmp_workspace, monkeypatch):
    verdict_file = qa_loop.verdict_path("MIT-551")
    verdict_file.parent.mkdir(parents=True)
    verdict_file.write_text(
        json.dumps({"passed": False, "score": 40, "reasons": ["缺少限流"], "suggestions": []}),
        encoding="utf-8",
    )
    sent: list[tuple[str, str]] = []

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append((issue_id, content))

    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.submit_phase("MIT-551")

    assert result == {"issue": "MIT-551", "passed": False, "score": 40, "closed": False}  # 打回=留单重做，不关单
    assert "❌ 自动质检未通过" in sent[0][1]
    assert not verdict_file.exists()  # 工单已消费，防止陈旧结论重复提交


async def test_submit_with_override_path_keeps_default_ticket(in_tmp_workspace, monkeypatch):
    override = in_tmp_workspace / "external_verdict.json"
    override.write_text(json.dumps({"passed": True, "score": 95, "reasons": [], "suggestions": []}), encoding="utf-8")
    sent: list[tuple[str, str]] = []

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append((issue_id, content))

    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.submit_phase("MIT-551", override_path=str(override))

    assert result["passed"] is True
    assert "✅ 自动质检通过" in sent[0][1]
    assert override.exists()  # 外部 verdict 不消费


def test_load_verdict_rejects_schema_violation(in_tmp_workspace):
    verdict_file = qa_loop.verdict_path("MIT-551")
    verdict_file.parent.mkdir(parents=True)
    verdict_file.write_text(json.dumps({"passed": True, "score": 150, "reasons": []}), encoding="utf-8")

    with pytest.raises(ValidationError):  # score 超出 0-100，评审员同样受契约约束
        qa_loop.load_verdict("MIT-551")


def test_load_verdict_missing_file_gives_actionable_error(in_tmp_workspace):
    with pytest.raises(FileNotFoundError, match="评审结论不存在"):
        qa_loop.load_verdict("MIT-551")
