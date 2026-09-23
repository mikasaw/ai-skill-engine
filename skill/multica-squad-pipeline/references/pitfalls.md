# 陷阱全表（三个迭代实战 + 上游 Multica 运营实践蒸馏，全部实测）

每条都流过血。诊断"任务不动/输出不对"时按本表排查，不要凭直觉重试。

## 编码与调用（不可逆类，最优先）

- **中文参数走 shell/PowerShell argv = 数据库级乱码且不可逆**（GBK 字节被 Go 端按 UTF-8 解码）。
  唯一可靠姿势：Python `subprocess.run([MC, ...], list args)`（CreateProcessW UTF-16）。
  已发生乱码只能用方案 A 写正确中文覆盖，无法"修复"。
- 长文本（≥1000 字符或含中文）：`--description-stdin` / `--content-file`（cwd 内临时文件 +
  `newline="\n"` 防止 \r\n 污染）。
- `--output json` 必须显式（默认 table；table 喂 json.loads 会静默失败无报错）。
- PowerShell `> file` 写 UTF-16 LE BOM、`Out-File -Encoding utf8` 写 UTF-8 BOM——
  Python 读盘用 `raw[:3]` 探 BOM 再选 `utf-16`/`utf-8-sig`/`utf-8` + `errors='replace'`。

## 任务语义

- `issue rerun` = 取消当前 + 重新入队。assign 后 rerun 会把 in-flight 新 run 立刻 cancel
  （审计多一条 cancelled，勿误判为重复派发/失败）。
- **已取消的 issue 可能被完成中的 agent 推回 in_review**——取消后复查最终状态；
  取消前先检查 agent 是否已改代码（污染要回滚）。
- `issue create --assignee` 有并发空位会**立即拾取、无视 stage 屏障**。依赖前置的子单
  创建时不给 assignee，提升时 assign **+ status todo** 两步（漏 todo = 永远停在 backlog）。
- **backlog + assignee 不被拾取是防护不是 bug**。卡住时检查是不是操作者漏了提升步骤。
- 子单交付方落 `in_review`，`done` 归验收方；`done` 才关闭 stage 屏障。
- **已 done 子单被验收评论再唤醒 = 实测威胁**（6 次再唤醒 1 次真改了 main）。
  防护条款已固化进 mdboard-dev/verifier instructions：只回报、不提交。
- comment 必须先于 `status done`（顺序反了 daemon 偶发把证据关联到上一状态）。
- agent run completed 无 error 但无完成评论、issue 卡 in_progress = 收尾中断 → rerun 修复。
- issue 卡 in_progress 但看板不动：**以 `issue runs` 的 task 状态为准**，看板状态不可信；
  桌面 UI 显示 queued 而服务端已 completed 同理。

## 失败分类诊断（先分类再动手）

| 症状 | 根因 | 处置 |
|---|---|---|
| run failed + error 含 `429 usage limit` | 模型 5 小时用量墙（system 评论有原文） | 等倒计时，重置前 rerun 必再失败 |
| run failed + `402 insufficient balance` | Cloud 余额 | 控制台处理后 rerun |
| run failed(runtime_recovery) 后紧跟新 run | daemon 自动恢复链路 | **不算失败**，继续等 |
| completed + 无 error + 无完成评论 | 收尾中断 | rerun |
| rc=5 `invalid workspace_id` | daemon 并发压力（非配置错！） | 等 30-60s 重试一次；勿改 workspace_id/profile |
| issue list 空输出 / exit 3 | session 过期（token 被清） | `multica login`；恢复后以 git log 核实真实进展 |
| task 长期 running + 工作区无文件活动 | 可能撞 429（system 评论有原文） | 同 429 处置 |

## agent 管理（高危区）

- `agent update --instructions` 是**整表替换**：先 `agent get` 落全量 → 追加 → 写回全部；
  单次 ≤1800 字符（超长报 `accepts 1 arg, received 2`）；**严禁短串试探**（擦掉默认 prompt
  不可恢复）；更新后立刻回读核对长度与关键内容。
- agent instructions 必须冻结**项目绝对路径**：daemon task workdir 是空目录（只有
  CLAUDE.md/.agent_context/.claude/.multica），agent 去哪干活由 instructions/CLAUDE.md 的
  主路径决定。漏写 = agent 无处可去 = 零交付（真实事故：某子单因漏配路径零交付）。
- **CLAUDE.md/instructions 优先级 > 派活单 description**。派活单里写 worktree 路径只算建议，
  除非给出"完整绝对路径 + 分支 + 严禁 cd 回主仓 + 独立 build 目录"的强约束段。
- git worktree 并发隔离**不可依赖**：多 agent 并发改同一主仓会真撞车。有效方案：
  `max_concurrent_tasks=1` 串行、或每 agent 管不重叠文件、或 frozen contract。
- agent 名字工作区内唯一（含已归档）；重名冲突用 `agent list --include-archived` 查。
- agent feat branch 不自动 merge main：验收查提交用 `git log --branches --all`。
- 新建 agent 必须挂 status=online 的 runtime；offline runtime 上的任务永远 queued。

## 流程与验收

- **验收 = 独立复跑**。实测：71 单测全绿背景下真实工件核验抓到解析缺陷；
  agent 在正确单据下交付过完全无关的 726 行工具（范围一致性检查拦截）。
- 变异自证需第三方复核（评审方亲手做一次变异，不只看 dev 贴的输出）。
- 不可达防御代码（读侧先抛 ⇒ 写侧守卫走不到）= 加了也是死代码，判 MAJOR。
  "真失败裸 errno 未转译"若属父单已裁范围外，是正确取舍不是缺口。
- 事故提交对（污染 + revert）按**净零例外**保留在历史：`git diff <前> <revert后>` 为空 +
  树对象同一即放行；**任何 reset/rebase/amend 抹痕迹的提议 = BLOCKER**。
- `--stage 0` 报错（N ≥ 1）；改 stage 用 `issue update <id> --stage N --no-start`。
- stage 屏障只管"父任务唤醒时机"，不是串行保证——靠"提升时才 assign"实现真串行。
