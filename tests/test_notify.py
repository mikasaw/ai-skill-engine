"""notify 中间件测试：事件推送、失败旁路、事件订阅过滤、异常通知。"""

from core.context import SkillContext
from middlewares.notify import NotifyMiddleware


def make_notifier(sent: list[dict], post_error: Exception | None = None, events=("success", "failure")) -> NotifyMiddleware:
    mw = NotifyMiddleware(webhook_url="http://example.test/hook", events=events)

    def fake_post(payload: dict) -> None:
        if post_error is not None:
            raise post_error
        sent.append(payload)

    mw._post_sync = fake_post  # monkeypatch 传输层，只测语义
    return mw


async def test_success_event_posted_with_payload():
    sent: list[dict] = []
    mw = make_notifier(sent)

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = {"ok": True}
        return ctx

    ctx = SkillContext(skill_name="demo", input_data={"a": 1})
    await mw.process(ctx, terminal)

    assert len(sent) == 1
    assert sent[0]["event"] == "success"
    assert sent[0]["skill"] == "demo"
    assert sent[0]["ok"] is True
    assert sent[0]["errors"] == []
    assert sent[0]["latency_ms"] >= 0
    assert ctx.metadata["notify_log"] == ["sent (success)"]


async def test_context_errors_are_reported_as_failure_event():
    sent: list[dict] = []
    mw = make_notifier(sent)

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.add_error("OutputValidationError: 给出后放弃")  # contract 耗尽式失败
        return ctx

    await mw.process(SkillContext(skill_name="demo"), terminal)

    assert sent[0]["event"] == "failure"
    assert sent[0]["ok"] is False


async def test_downstream_exception_notified_then_reraised():
    sent: list[dict] = []
    mw = make_notifier(sent)

    async def failing(ctx: SkillContext) -> SkillContext:
        raise TimeoutError("llm timeout")

    ctx = SkillContext(skill_name="demo")
    import pytest

    with pytest.raises(TimeoutError):
        await mw.process(ctx, failing)

    assert sent[0]["event"] == "failure"
    assert "TimeoutError" in sent[0]["error"]


async def test_webhook_failure_never_breaks_main_flow():
    sent: list[dict] = []
    mw = make_notifier(sent, post_error=OSError("network down"))

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = "ok"
        return ctx

    ctx = SkillContext(skill_name="demo")
    result = await mw.process(ctx, terminal)

    assert result.final_output == "ok"  # 主流程不受影响
    assert sent == []
    assert "webhook failed (success): OSError" in ctx.metadata["notify_log"][0]


async def test_event_subscription_filters():
    sent: list[dict] = []
    mw = make_notifier(sent, events=("failure",))  # 只订阅失败

    async def terminal(ctx: SkillContext) -> SkillContext:
        ctx.final_output = "ok"
        return ctx

    ctx = SkillContext(skill_name="demo")
    await mw.process(ctx, terminal)

    assert sent == []
    assert ctx.metadata["notify_log"] == ["skipped (success): not subscribed"]


async def test_no_webhook_url_skips_silently():
    mw = NotifyMiddleware(webhook_url="")
    sent: list[dict] = []

    def fake_post(payload: dict) -> None:
        sent.append(payload)

    mw._post_sync = fake_post

    async def terminal(ctx: SkillContext) -> SkillContext:
        return ctx

    ctx = SkillContext(skill_name="demo")
    await mw.process(ctx, terminal)

    assert sent == []
    assert ctx.metadata["notify_log"] == ["skipped (success): no webhook_url"]
