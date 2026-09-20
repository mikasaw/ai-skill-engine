"""质检闭环测试：Mock 掉 multica CLI 与 LLM 评审，验证结论格式与回写路由。"""

import pytest

import multica_qa_loop as qa_loop
import skills.multica_qa as multica_qa
from core.skill import skill_registry
from skills.multica_qa import MulticaQaSkill, QaVerdict


@pytest.fixture()
def stubbed_skill(monkeypatch):
    """让真实注册的 multica_qa 技能在离线环境跑通：抓取走内存数据，评审走固定结论。"""
    issues = {
        "ISSUE-1": {"title": "实现登录页", "description": "带表单校验的登录页"},
    }
    verdicts: dict[str, QaVerdict] = {}

    async def fake_fetch_issue(issue_id: str) -> dict:
        if issue_id not in issues:
            raise RuntimeError(f"multica CLI 执行失败: issue {issue_id} not found")
        return issues[issue_id]

    async def fake_fetch_comments(issue_id: str) -> list[str]:
        return ["交付说明：已完成登录页并通过测试"]

    async def fake_review(self, issue, comments, context) -> QaVerdict:
        return verdicts[context.input_data.issue_id]

    monkeypatch.setattr(multica_qa, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(multica_qa, "fetch_comments", fake_fetch_comments)
    monkeypatch.setattr(MulticaQaSkill, "_review", fake_review)
    return issues, verdicts


def test_skill_is_registered_with_expected_contract():
    cls = skill_registry.get("multica_qa")
    assert cls.output_schema is QaVerdict
    assert "contract" in cls.middleware_names
    assert "cost_limiter" in cls.middleware_names


async def test_pass_verdict_writes_confirm_comment(stubbed_skill, monkeypatch):
    _, verdicts = stubbed_skill
    verdicts["ISSUE-1"] = QaVerdict(passed=True, score=90, reasons=[], suggestions=["补充 E2E 用例"])
    sent: list[tuple[str, str]] = []

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append((issue_id, content))

    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.run_qa_and_writeback("ISSUE-1")

    assert result == {"issue": "ISSUE-1", "executed": True, "passed": True, "score": 90, "llm_calls": 1}
    issue_id, content = sent[0]
    assert issue_id == "ISSUE-1"
    assert content.startswith("✅ 自动质检通过")
    assert "90/100" in content


async def test_reject_verdict_writes_actionable_comment(stubbed_skill, monkeypatch):
    _, verdicts = stubbed_skill
    verdicts["ISSUE-1"] = QaVerdict(
        passed=False,
        score=40,
        reasons=["表单缺少邮箱格式校验", "密码输入未做最小长度限制"],
        suggestions=["复用 core/middleware.py 的校验模式"],
    )
    sent: list[tuple[str, str]] = []

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append((issue_id, content))

    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    await qa_loop.run_qa_and_writeback("ISSUE-1")

    _, content = sent[0]
    assert content.startswith("❌ 自动质检未通过")
    assert "1. 表单缺少邮箱格式校验" in content  # reasons 编号化为可执行修复指令
    assert "2. 密码输入未做最小长度限制" in content
    assert "复用 core/middleware.py 的校验模式" in content


async def test_engine_failure_posts_manual_intervention_notice(stubbed_skill, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_post(issue_id: str, content: str) -> None:
        sent.append((issue_id, content))

    monkeypatch.setattr(qa_loop, "post_comment", fake_post)

    result = await qa_loop.run_qa_and_writeback("ISSUE-404")

    assert result["executed"] is False
    assert sent[0][1].startswith("⚠️ 自动质检未能完成")


def test_comment_formatting_is_pure():
    verdict = QaVerdict(passed=False, score=30, reasons=["问题 A"], suggestions=[])
    text = qa_loop.build_reject_comment(verdict)
    assert text.startswith("❌")
    assert "1. 问题 A" in text
