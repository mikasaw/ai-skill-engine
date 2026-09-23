# 工具链速查（multica_qa_loop.py + CLI 形态）

前置：`cd $ENGINE`（你的 ai_skill_engine 根目录），Python 用 `./.venv/Scripts/python`。
所有 multica 调用经 `skills/multica_qa.py: run_multica`（Python list-args + 瞬态重试），
不要直接在 shell 里拼 multica 命令传中文参数。

## loop 子命令

| 命令 | 用途 | 关键 flags |
|---|---|---|
| `dispatch <agent> <方案.md>` | 建 issue 指派 agent（指派即开工） | `--title`（默认取方案首行） |
| `watch <issue-id>...` | 轮询最新 run 至终止态（后台跑） | `--poll 20 --timeout 2700` |
| `fetch <issue-id>` | 抓材料 → `qa_reviews/<id>.material.md` | — |
| `submit <issue-id>` | 契约校验 verdict → 回写评论，通过自动关单 | `--verdict <路径>` `--no-close` |
| `comment <issue-id>` | 追评（触发 agent 唤醒 = resume） | `--file` / `--text` / `--parent` |
| `decide` | 机验追评 vs 新建（Rule A/C/E） | `--worktree --prev-issue --baseline --force` |
| `auto <issue-id>...` | 引擎全自动验收（需 LLM API key，批量场景） | — |

watch 语义：`failed` 后自动探测 runtime_recovery 姊妹任务；backlog/无 run 状态报 `unknown`
持续等待；超时退出（重跑即可续等）。**必须用受跟踪方式挂**（run_in_background），
否则进程退出不会唤醒会话。

## CLI 形态速查（实测，勿凭记忆）

| 操作 | 正确形态 |
|---|---|
| 查单条 | `issue get <ID/identifier>`（默认 JSON；`issue view` 不存在） |
| 改状态 | `issue status <id> <key>`（todo/in_progress/in_review/done/blocked/cancelled） |
| 指派 | `issue assign <id> --to-id <uuid>`（assign 后**不要** rerun） |
| 重跑 | `issue rerun <id>` = 取消当前 + 重新入队（只在显式要重启时用） |
| 中断 | `issue cancel-task <id>` |
| 评论 | `issue comment add <id> --content-file <cwd内路径>`（多行/中文必走文件；无 --body） |
| 子任务 | `issue create --parent <父id> --stage N`（N ≥ 1） |
| 执行历史 | `issue runs <id>`（list，按时间倒序，status/error/completed_at/attribution） |
| 单 run 消息 | `issue run-messages <task-id>` |
| token 账本 | `issue usage <id>`（聚合可能延迟，unreported 属正常） |
| agent 详情 | `agent get <uuid>`（只认 UUID 不认名字；先 `agent list` 拿 id） |
| agent 任务 | `agent tasks <uuid>`（顶层 JSON 数组，含 work_dir/failure_reason） |
| 改 instructions | `agent update <uuid> --instructions <全量文本>`——**整表替换**，见 pitfalls |
| 小队 | `squad create --name --leader`；`squad member add <squad-id> --member-id <uuid> --role` |

## 关键 JSON 形态

- `issue list --output json` → `{has_more, issues[], ...}`，默认 limit=50 按最近排序（旧单不在前 50，
  查单条用 `issue get`）；`--limit 200` 会撞 30s 超时，加 `MULTICA_HTTP_TIMEOUT=120` 或按 `--project` 过滤。
- `issue comment list <id> --output json --compact` → **裸 list**；`type=system` 评论携带
  429/402 等平台错误原文——诊断"任务卡住"第一现场。
- `issue runs` → 裸 list；`attribution.evidence.kind` / `delegated_from_task_id` 可追溯触发来源。
- `issue get` 的 `assignee` 是 dict `{id,name,type}`，遍历前 `isinstance` 判型。
- `daemon status` → `skipped_agents` 是"agent 跑不起来"的第一诊断位；CLI 显示 stopped 可能是
  phantom（桌面端 daemon 独立存活），以实际任务能否拉起为准。

## runtime_recovery

`failed`（failure_reason=runtime_recovery）后 1 秒内 daemon 自动派生姊妹任务重跑——
**不算失败、不算重复派发**。watch 已内置该探测；手动诊断时按 issue 分组看最新 completed 任务。
