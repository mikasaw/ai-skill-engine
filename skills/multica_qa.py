"""Multica 交付质检技能：拉取 issue 内容 → LLM 结构化评审 → 产出质检结论。

闭环设计（与 multica_qa_loop.py 配合）：
    Multica issue 完成 → 本地跑本技能 → 结论由 loop 回写为 issue 评论；
    打回评论的 reasons 就是给 agent 的 retry_prompt（契约自愈思想在跨系统层面的复用）。

数据获取通过 multica CLI（--output json）；评审通过 instructor 强制结构化。
execute 仍保持纯业务逻辑：抓取数据 + 一次结构化 LLM 调用，无重试/日志/副作用。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

from config import settings
from core.context import MODEL_OVERRIDE_KEY, RETRY_PROMPT_KEY, SkillContext
from core.skill import BaseSkill, register_skill


class QaInput(BaseModel):
    issue_id: str = Field(..., min_length=1, description="Multica issue 的 ID/Key")


class QaVerdict(BaseModel):
    passed: bool = Field(..., description="交付是否达标")
    score: int = Field(..., ge=0, le=100, description="质量分 0-100")
    reasons: list[str] = Field(default_factory=list, description="不达标的具体问题，将原样写回 issue 作为修复指令")
    suggestions: list[str] = Field(default_factory=list, description="改进建议")


QA_SYSTEM_PROMPT = """你是软件交付质检员。基于 issue 的需求描述与 agent 的交付/讨论评论，判断交付是否达标。
- passed：是否达到可接受标准；
- score：0-100 质量分；
- reasons：不达标时列出具体、可执行的问题（会被原样写回 issue 指导修复，务必具体）；
- suggestions：改进建议。
只输出符合给定 JSON Schema 的结论，不要输出解释。"""


async def run_multica(*args: str) -> str:
    """执行 multica CLI 子命令并返回 stdout（Windows 下避免中文编码问题的统一入口）。"""
    proc = await asyncio.create_subprocess_exec(
        settings.multica_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"multica CLI 执行失败: {stderr.decode('utf-8', 'replace')[:300]}")
    return stdout.decode("utf-8", "replace")


async def fetch_issue(issue_id: str) -> dict[str, str]:
    """拉取 issue 的标题与需求描述（模块级，供技能与 qa_loop 半程接力共用）。"""
    data = json.loads(await run_multica("issue", "get", issue_id, "--output", "json"))
    return {"title": str(data.get("title", "")), "description": str(data.get("description", ""))}


async def fetch_comments(issue_id: str) -> list[str]:
    """拉取最近 10 条讨论/交付评论的内容（防御式解析：兼容 list 或 dict 包裹）。"""
    raw = await run_multica(
        "issue", "comment", "list", issue_id,
        "--output", "json", "--compact", "--recent", "10",
    )
    data = json.loads(raw)
    items = data if isinstance(data, list) else data.get("comments", [])
    return [str(c.get("content", "")) for c in items if isinstance(c, dict) and c.get("content")]


@register_skill
class MulticaQaSkill(BaseSkill):
    name = "multica_qa"
    description = "质检 Multica issue 的 agent 交付，产出结构化结论（passed/score/reasons/suggestions）"
    input_schema = QaInput
    output_schema = QaVerdict
    # 顺序即洋葱层次：熔断在外 → 降级 → 契约自修复 → 成本闸在最内（统计每次真实 LLM 调用）
    middleware_names = ["circuit_breaker", "fallback", "contract", "cost_limiter"]

    async def execute(self, context: SkillContext) -> Any:
        issue_id = context.input_data.issue_id
        issue = await fetch_issue(issue_id)
        comments = await fetch_comments(issue_id)
        return await self._review(issue, comments, context)

    async def _review(self, issue: dict[str, str], comments: list[str], context: SkillContext) -> Any:
        # 延迟导入：离线测试环境无需安装 litellm / instructor
        import instructor
        import litellm

        material = (
            f"# Issue 标题\n{issue['title']}\n\n# 需求描述\n{issue['description']}"
            + "\n\n# 最近交付/讨论评论\n" + "\n---\n".join(comments)
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": QA_SYSTEM_PROMPT},
            {"role": "user", "content": material},
        ]
        retry_prompt = context.metadata.get("retry_prompt")
        if retry_prompt:
            messages.append({"role": "user", "content": retry_prompt})

        client = instructor.from_litellm(litellm.acompletion)
        return await client.chat.completions.create(
            model=context.metadata.get(MODEL_OVERRIDE_KEY) or settings.llm_model,
            response_model=QaVerdict,
            messages=messages,
            max_retries=2,
        )
