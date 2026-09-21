"""Multica 质检闭环编排。三种模式：

1. 半程接力（默认，无需任何 LLM API key）——评审由本会话（ZCode）承担：
       python multica_qa_loop.py fetch  <issue-id>...   # 抓取材料 → qa_reviews/<id>.material.md
       #   ……后台任务退出即唤醒 ZCode 会话，评审者阅读材料、写入 verdict JSON……
       python multica_qa_loop.py submit <issue-id>...   # 校验 verdict → 回写 issue 评论
   状态由文件承载：material 存在=待评审；verdict 存在=待回写；submit 成功即消费 verdict
   （防止陈旧结论被重复提交）。

2. auto（批量/无人值守，需 LLM API key）——引擎全自动：抓取 → instructor 评审 → 回写：
       python multica_qa_loop.py auto <issue-id>...

打回评论的 reasons 就是给 Multica agent 的 retry_prompt——契约自愈思想在跨系统层面的复用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from skills.multica_qa import QaVerdict, fetch_comments, fetch_issue, run_multica

REVIEW_DIR = Path("qa_reviews")


def safe_id(issue_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", issue_id)


def material_path(issue_id: str) -> Path:
    return REVIEW_DIR / f"{safe_id(issue_id)}.material.md"


def verdict_path(issue_id: str) -> Path:
    return REVIEW_DIR / f"{safe_id(issue_id)}.verdict.json"


# ---------- 半程接力：fetch ----------


def build_material(issue_id: str, issue: dict[str, str], comments: list[str]) -> str:
    lines = [
        f"# QA 评审材料 — {issue_id}",
        "",
        f"> 评审指引：阅读下方材料，产出符合 QaVerdict 的 JSON 写入 `{verdict_path(issue_id)}`：",
        '> `{"passed": bool, "score": 0-100, "reasons": ["具体问题(打回时作为修复指令)"], "suggestions": ["改进建议"]}`',
        "> 提交回写：`python multica_qa_loop.py submit " + issue_id + "`",
        "",
        "## 标题",
        issue["title"],
        "",
        "## 需求描述",
        issue["description"] or "（无）",
        "",
        "## 最近交付/讨论评论",
    ]
    if comments:
        lines.extend(f"### 评论 {index}\n{content}" for index, content in enumerate(comments, 1))
    else:
        lines.append("（无评论）")
    return "\n".join(lines) + "\n"


async def fetch_phase(issue_id: str) -> Path:
    """真实抓取 issue 材料，落盘供评审者（本会话）阅读；返回材料路径。"""
    issue = await fetch_issue(issue_id)
    comments = await fetch_comments(issue_id)
    path = material_path(issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_material(issue_id, issue, comments), encoding="utf-8")
    return path


# ---------- 半程接力：submit ----------


def load_verdict(issue_id: str, override_path: str | None = None) -> QaVerdict:
    """读取并契约校验评审者产出的 verdict（评审员同样受 Schema 纪律约束）。"""
    path = Path(override_path) if override_path else verdict_path(issue_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"评审结论不存在：{path}。请先评审 {material_path(issue_id)} 并写入 verdict JSON。"
        ) from exc
    return QaVerdict.model_validate(data)  # 不合法将抛 ValidationError


async def submit_phase(issue_id: str, override_path: str | None = None, close_on_pass: bool = True) -> dict[str, Any]:
    verdict = load_verdict(issue_id, override_path)
    comment = build_pass_comment(verdict) if verdict.passed else build_reject_comment(verdict)
    await post_comment(issue_id, comment)
    if override_path is None:
        verdict_path(issue_id).unlink(missing_ok=True)  # 工单消费，防止陈旧结论重复提交
    closed = False
    if verdict.passed and close_on_pass:
        # --no-start：验收通过关单不应再唤醒 agent（默认改状态会触发新一轮 run）
        await run_multica("issue", "status", issue_id, "done", "--no-start")
        closed = True
    return {"issue": issue_id, "passed": verdict.passed, "score": verdict.score, "closed": closed}


# ---------- 开发派遣与等待（闭环的开发半程） ----------

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "canceled"}


async def dispatch_phase(agent: str, plan_file: str, title: str | None = None) -> dict[str, Any]:
    """读方案文件 → 建 issue 并指派 agent（指派即自动开工）→ 返回 issue 标识。"""
    plan_text = Path(plan_file).read_text(encoding="utf-8")
    if not title:
        first_line = next((ln.strip() for ln in plan_text.splitlines() if ln.strip()), "")
        title = first_line.lstrip("# ").strip()[:80] or Path(plan_file).stem
    # 经 cwd 临时文件传入：规避 CLI 对 --description-file 的 cwd 限制与中文编码问题
    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="dispatch_plan_", dir=os.getcwd())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(plan_text)
        raw = await run_multica(
            "issue", "create", "--title", title, "--description-file", tmp,
            "--assignee", agent, "--output", "json",
        )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    data = json.loads(raw)
    return {
        "issue": data.get("id"),
        "identifier": data.get("identifier"),
        "title": data.get("title"),
        "assignee": agent,
    }


async def latest_run(issue_id: str) -> dict[str, Any] | None:
    runs = json.loads(await run_multica("issue", "runs", issue_id, "--output", "json"))
    items = runs if isinstance(runs, list) else runs.get("runs", [])
    if not items:
        return None
    return sorted(items, key=lambda r: str(r.get("created_at", "")), reverse=True)[0]


async def watch_phase(issue_id: str, poll_seconds: float = 20.0, timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """轮询最新 run 直至终止态；设计为后台运行，退出即唤醒会话进入验收。"""
    waited = 0.0
    while True:
        run = await latest_run(issue_id)
        status = str((run or {}).get("status", "unknown"))
        if status in TERMINAL_RUN_STATUSES:
            if status == "failed":
                # runtime_recovery：daemon 可能自动派生更新的姊妹任务重试
                # （技能文档 v3.20.1：failed + 新任务 running = 正常恢复链路，不算失败）
                await asyncio.sleep(poll_seconds)
                newer = await latest_run(issue_id)
                newer_created = str((newer or {}).get("created_at", ""))
                if newer and newer_created > str((run or {}).get("created_at", "")):
                    run, status = newer, str(newer.get("status", "unknown"))
                    if status not in TERMINAL_RUN_STATUSES:
                        waited += poll_seconds
                        print(f"[watch] {issue_id} 检测到恢复任务（runtime_recovery），继续等待...", flush=True)
                        continue
            summary = {
                "issue": issue_id,
                "run_status": status,
                "completed_at": (run or {}).get("completed_at"),
                "error": (run or {}).get("error"),
            }
            if status == "completed":
                print(f"[watch] run 已完成，进入验收：python multica_qa_loop.py fetch {issue_id}")
            else:
                print(f"[watch] run 异常终止：{summary}")
            return summary
        if waited >= timeout_seconds:
            print(f"[watch] 等待超时（{timeout_seconds:.0f}s），最新状态={status}；可重新运行 watch 继续等待。")
            return {"issue": issue_id, "run_status": status, "timed_out": True}
        await asyncio.sleep(poll_seconds)
        waited += poll_seconds
        print(f"[watch] {issue_id} 仍在运行（status={status}，已等待 {waited:.0f}s）...", flush=True)


# ---------- 派遣决策：追评还是新建（规则表 A-F 的可机验部分） ----------

FAT_CONTEXT_WAKE_LIMIT = 8  # Rule E：同单唤醒次数上限


async def run_git(worktree: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", worktree, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", "replace")


async def decide_phase(
    worktree: str | None = None,
    prev_issue: str | None = None,
    baseline: str | None = None,
    force: str | None = None,
) -> dict[str, Any]:
    """按规则表 A→C→E 机验，首条命中即生效；B/D/F 属语义判断，列入人工清单。

    返回 {"verdict": "追评"|"新建", "rule", "reason", "human_items"}。
    """
    if force in ("追评", "新建"):
        return {"verdict": force, "rule": "manual", "reason": "人工覆盖", "human_items": []}

    human_items = ["B(同靶渐进?)", "D(验收边界未变?)", "F(纯落章?)"]

    # Rule A：worktree 脏 → 禁 rerun，默认追评保上下文
    if worktree:
        code, out = await run_git(worktree, "status", "--porcelain")
        if code == 0 and out.strip():
            return {
                "verdict": "追评",
                "rule": "A",
                "reason": f"worktree 有未提交变更（{len(out.splitlines())} 项）：禁 rerun，追评保上下文或人工交接",
                "human_items": human_items,
            }
        if code != 0:
            return {
                "verdict": "人工判断",
                "rule": "A",
                "reason": f"git status 失败（{out.strip()[:120]}），无法机验",
                "human_items": human_items,
            }

    # Rule C：基线已前进 → 新建
    if worktree and baseline:
        code, out = await run_git(worktree, "rev-list", "--count", f"{baseline}..main")
        if code == 0 and out.strip().isdigit() and int(out) > 0:
            return {
                "verdict": "新建",
                "rule": "C",
                "reason": f"基线 {baseline}..main 已前进 {out.strip()} 个提交；若仍追评必须点名重新 Read 并逐条回判",
                "human_items": human_items,
            }

    # Rule E：同单唤醒次数 ≥ 上限 → 新建（jsonl 体积无法经 CLI 机验，按 run 数近似）
    if prev_issue:
        runs = json.loads(await run_multica("issue", "runs", prev_issue, "--output", "json"))
        items = runs if isinstance(runs, list) else runs.get("runs", [])
        if len(items) >= FAT_CONTEXT_WAKE_LIMIT:
            return {
                "verdict": "新建",
                "rule": "E",
                "reason": f"同单已唤醒 {len(items)} 次（≥{FAT_CONTEXT_WAKE_LIMIT}）：已压缩过，'记得'不可信，冷启动比错记忆便宜",
                "human_items": human_items,
            }

    # 默认：无机验命中 → 新建（B/D/F 请人工过目后再定追评）
    return {
        "verdict": "新建",
        "rule": "默认",
        "reason": "无机验命中；请人工确认 B/D/F 后，若确属同靶渐进或纯落章再改为追评",
        "human_items": human_items,
    }


async def comment_phase(issue_id: str, content: str, parent: str | None = None) -> None:
    """追评入口：把本轮判据写进评论并触发 agent（Rule B：正文必须自带本轮判据）。"""
    fd, path = tempfile.mkstemp(suffix=".md", prefix="qa_comment_", dir=os.getcwd())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        args = ["issue", "comment", "add", issue_id, "--content-file", path]
        if parent:
            args += ["--parent", parent]
        await run_multica(*args)
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------- auto：引擎全自动路线（批量，需 LLM API key） ----------


async def run_qa_and_writeback(issue_id: str) -> dict[str, Any]:
    from core.runner import SkillRunner

    runner = SkillRunner()
    context = await runner.run("multica_qa", {"issue_id": issue_id})
    verdict = context.final_output

    if not isinstance(verdict, QaVerdict):
        detail = "; ".join(context.errors) or "未知错误"
        await post_comment(issue_id, f"⚠️ 自动质检未能完成，请人工介入。原因：{detail}")
        return {"issue": issue_id, "executed": False, "detail": detail}

    comment = build_pass_comment(verdict) if verdict.passed else build_reject_comment(verdict)
    await post_comment(issue_id, comment)
    return {
        "issue": issue_id,
        "executed": True,
        "passed": verdict.passed,
        "score": verdict.score,
        "llm_calls": context.metadata.get("llm_calls", 0),
    }


# ---------- 评论构造与写回（两条路线共用） ----------


def build_pass_comment(verdict: QaVerdict) -> str:
    lines = ["✅ 自动质检通过", f"质量分：{verdict.score}/100"]
    if verdict.suggestions:
        lines.append("")
        lines.append("后续改进建议：")
        lines.extend(f"- {s}" for s in verdict.suggestions)
    return "\n".join(lines)


def build_reject_comment(verdict: QaVerdict) -> str:
    lines = ["❌ 自动质检未通过，请针对以下问题修复后重新提交："]
    lines.extend(f"{index}. {reason}" for index, reason in enumerate(verdict.reasons, 1))
    if verdict.suggestions:
        lines.append("")
        lines.append("修改建议：")
        lines.extend(f"- {s}" for s in verdict.suggestions)
    return "\n".join(lines)


async def post_comment(issue_id: str, content: str) -> None:
    """经临时文件写评论：规避 Windows 下 stdin 管道的中文编码问题（CLI 官方建议 --content-file）。"""
    fd, path = tempfile.mkstemp(suffix=".md", prefix="qa_comment_", dir=os.getcwd())
    try:
        # newline="\n"：阻止 Windows 文本模式把 \n 翻译成 \r\n
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        await run_multica("issue", "comment", "add", issue_id, "--content-file", path)
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ---------- CLI ----------


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "fetch":
        for issue_id in args.issue_ids:
            path = await fetch_phase(issue_id)
            print(f"[fetch] {issue_id} 材料已写入: {path}")
        print(
            "下一步：评审者（ZCode 会话）阅读上述材料，"
            "把 QaVerdict JSON 写入对应 verdict 文件，然后运行 "
            "`python multica_qa_loop.py submit <issue-id>`。"
        )
    elif args.command == "submit":
        for issue_id in args.issue_ids:
            result = await submit_phase(issue_id, override_path=args.verdict, close_on_pass=not args.no_close)
            print(f"[submit] {result}")
    elif args.command == "dispatch":
        result = await dispatch_phase(args.agent, args.plan_file, title=args.title)
        print(f"[dispatch] {result}")
        print(f"下一步：后台运行 `python multica_qa_loop.py watch {result['issue']}`，完成后会唤醒会话进入验收。")
    elif args.command == "decide":
        print(await decide_phase(
            worktree=args.worktree,
            prev_issue=args.prev_issue,
            baseline=args.baseline,
            force=args.force,
        ))
    elif args.command == "comment":
        content = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        await comment_phase(args.issue_id, content, parent=args.parent)
        print(f"[comment] 已追评到 {args.issue_id}（将触发 agent 唤醒）")
    elif args.command == "watch":
        for issue_id in args.issue_ids:
            print(await watch_phase(issue_id, poll_seconds=args.poll, timeout_seconds=args.timeout))
    else:  # auto
        for issue_id in args.issue_ids:
            print(await run_qa_and_writeback(issue_id))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Multica 任务自动化开发与验收闭环：dispatch 派遣 → watch 等待 → fetch/submit 半程接力验收"
            "（打回评论触发 agent 重做，通过自动关单）；auto 为引擎全自动批量路线"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dispatch_parser = sub.add_parser("dispatch", help="开发派遣：建 issue 并指派 agent（指派即自动开工）")
    dispatch_parser.add_argument("agent", help="承接开发的 agent 名（fuzzy match）")
    dispatch_parser.add_argument("plan_file", help="需求方案 Markdown 文件路径")
    dispatch_parser.add_argument("--title", default=None, help="issue 标题（默认取方案首个非空行）")

    watch_parser = sub.add_parser("watch", help="开发等待：轮询最新 run 直至终止态（后台运行，退出唤醒会话）")
    watch_parser.add_argument("issue_ids", nargs="+", help="Multica issue 的 ID/identifier")
    watch_parser.add_argument("--poll", type=float, default=20.0, help="轮询间隔秒数（默认 20）")
    watch_parser.add_argument("--timeout", type=float, default=1800.0, help="最长等待秒数（默认 1800）")

    decide_parser = sub.add_parser("decide", help="派遣决策：机验规则表 A/C/E，输出追评或新建建议")
    decide_parser.add_argument("--worktree", default=None, help="agent 开发的 git 仓库路径（Rule A/C）")
    decide_parser.add_argument("--prev-issue", default=None, help="关联的旧 issue（Rule E 统计唤醒次数）")
    decide_parser.add_argument("--baseline", default=None, help="首跑基线 commit（Rule C）")
    decide_parser.add_argument("--force", choices=["追评", "新建"], default=None, help="人工覆盖机验结论")

    comment_parser = sub.add_parser("comment", help="追评：追加评论触发 agent 唤醒（正文须自带本轮判据）")
    comment_parser.add_argument("issue_id", help="要追评的 issue")
    group = comment_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="评论内容文件（UTF-8）")
    group.add_argument("--text", help="评论内容文本")
    comment_parser.add_argument("--parent", default=None, help="父评论 ID（挂线程用）")

    fetch_parser = sub.add_parser("fetch", help="验收接力①：抓取 issue 材料落盘（后台运行退出后唤醒会话评审）")
    fetch_parser.add_argument("issue_ids", nargs="+", help="Multica issue 的 ID/identifier")

    submit_parser = sub.add_parser("submit", help="验收接力②：契约校验 verdict、回写评论，通过则自动关单")
    submit_parser.add_argument("issue_ids", nargs="+", help="Multica issue 的 ID/identifier")
    submit_parser.add_argument(
        "--verdict", default=None, help="verdict JSON 路径（默认 qa_reviews/<issue-id>.verdict.json）"
    )
    submit_parser.add_argument("--no-close", action="store_true", help="通过时不自动关单")

    auto_parser = sub.add_parser("auto", help="引擎全自动验收：抓取 → LLM 评审 → 回写 → 关单（需 LLM key，适合批量）")
    auto_parser.add_argument("issue_ids", nargs="+", help="Multica issue 的 ID/identifier")

    args = parser.parse_args()
    if args.command == "auto":
        import middlewares  # noqa: F401  引擎路线才需要触发中间件注册
        import skills.multica_qa  # noqa: F401

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
