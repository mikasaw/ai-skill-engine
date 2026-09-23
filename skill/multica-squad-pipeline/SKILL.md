---
name: multica-squad-pipeline
description: 用 Multica 小队跑「派遣 → 开发 → 等待 → 独立验收 → 打回重做 → 自动关单」的全自动流水线。凡用户提到：派活/派遣任务给 agent 或小队、验收/质检 agent 交付、Multica issue 跟踪与打回、decide 追评还是新建、mdboard、或想把开发任务交给 AI 小队——即使没说 "multica" 一词也应触发本技能。
---

# Multica 小队流水线：派遣 → 开发 → 独立验收 → 关单

经三个迭代、17+ 子任务实战验证的完整流程。核心信念只有一条：**agent 自报 PASS ≠ 完成**——
每一层结论都必须由下一层独立复跑取证。

## 前置

- 工具链：`$ENGINE`（配套工具仓 `ai_skill_engine` 的根目录，**按你的部署路径调整**，下同），
  Python 用其虚拟环境 `$ENGINE/.venv/Scripts/python`。
- multica CLI（随 Multica 桌面版分发，典型路径 `%LOCALAPPDATA%\Programs\@multicadesktop\resources\app.asar.unpacked\resources\bin\multica.exe`，以实际安装为准）。
  **调用一律走 Python subprocess list-args**（`$ENGINE` 的 `skills/multica_qa.py: run_multica` 已封装，
  含瞬态重试）——PowerShell argv 的 GBK 双重编码会把乱码写进数据库且不可逆。
- 小队：`mdboard-squad`（leader=architect，成员 dev/verifier/reviewer）。其它项目可复制
  `agent create` 四件套模板，instructions 必须冻结**项目绝对路径**（daemon 的 task workdir 是
  空目录，agent 行为由 instructions/CLAUDE.md 的主路径决定——漏写 = 零交付）。

## 核心循环（六步）

```bash
# ① 派遣决策（机验规则表 A-F，首条命中即生效）
python multica_qa_loop.py decide --worktree <仓库> --prev-issue <旧单> --baseline <基线sha>
#    追评 → comment <issue> --file 本轮判据.md（正文必须自带判据）
#    新建 → ② dispatch

# ② 派遣（建 issue 并指派，指派即开工；标题默认取方案首行）
python multica_qa_loop.py dispatch <agent|squad> <方案.md> --title "..."

# ③ 等待：后台运行（退出即唤醒会话），poll 20s / timeout 2700s
python multica_qa_loop.py watch <issue-id>

# ④ 抓取验收材料（建议后台跑，退出唤醒评审）
python multica_qa_loop.py fetch <issue-id>

# ⑤ 会话内评审：读 qa_reviews/<id>.material.md，产出 verdict JSON
#    {"passed": bool, "score": 0-100, "reasons": [...], "suggestions": [...]}
#    写入 qa_reviews/<id>.verdict.json

# ⑥ 回写：submit 侧做 Pydantic 契约校验；通过自动关单(--no-close 可关)
python multica_qa_loop.py submit <issue-id>
```

打回（passed=false）时 reasons 就是给 agent 的修复指令——评论会触发 agent 重做，
回到 ③。reasons 必须精确到 文件:行号 与修法，让 agent 拿到即可动手、无需重新摸索。

## 验收纪律（每一条都流过血）

1. **独立复跑，不复读自报。** 亲手跑测试/构建/最恶劣场景。实测案例：71 个单测全绿的背景下，
   真实工件核验抓到引号解析缺陷。单元测试绿 ≠ 无缺陷。
2. **范围一致性检查（防串台）。** 交付前先 `git show --stat HEAD` 确认改动与单据范围一致。
   实测案例：agent 在正确单据下交付了完全无关的 726 行工具，靠这一步拦住。
3. **变异自证要第三方复核。** dev 贴的"删守卫后测试失败"输出，评审方要亲手再做一次变异。
4. **追评正文必须自带本轮判据**，不能只写"继续"。
5. **comment 先于 status done**（证据顺序）；子单交付方落 in_review，done 归验收方。
6. **已 done 子单被验收评论再唤醒：只回报、不提交**（实测威胁：6 次再唤醒 1 次真改了 main）。
7. **历史改写（reset/rebase/amend 抹事故痕迹）= BLOCKER**，停下回报项目主。
   事故提交对按"净零例外"保留：`git diff <事故前> <revert后>` 为空 + 树对象同一即放行。
8. **不可达防御代码 = MAJOR**：给守卫加分支前必须先证明该分支独立可达。
9. 探索与诊断的完整陷阱表：**读 `$ENGINE/README.md` 的「Multica 实操经验备忘」节**；
   事故处置全流程模板：**读本技能 `references/incident-playbook.md`**。

## 验收时的标准动作清单

- [ ] `git log --oneline` / `--stat`：提交前缀合规、范围一致、worktree 干净
- [ ] 亲跑全量测试（基线只增不减、无 skipped）
- [ ] 至少一个**真实工件**功能核验（造最恶劣输入，查实际产物）
- [ ] 对照单据验收判据逐条打勾；发现判据自身有坑 → 回报 Architect 裁定，不将错就错
- [ ] verdict reasons/suggestions 精确、可执行；submit 后核对关单状态

## 诊断顺序（任务不动时）

1. `issue runs <id>` 看最新 run（`failed` 后紧跟更新的 running 任务 = daemon 自动恢复，继续等）；
2. `issue comment list <id>` 找 `[system]` 评论（429/402 错误原文只在 system 评论）；
3. `invalid workspace_id`(rc=5) = daemon 并发压力，等 30-60s 重试，**勿改配置**；
4. `issue list` 空输出先查 session 过期（exit 3 → login）；
5. 任务停在 backlog/无 run：backlog 不会被拾取（这是防护不是 bug）——提升 = assign **+ status todo** 两步。

完整 CLI 形态速查见 `references/tooling.md`；全部陷阱细节见 `references/pitfalls.md`。
