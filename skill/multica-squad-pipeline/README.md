# multica-squad-pipeline

> 中文 | [English](#english)

用 [Multica](https://github.com/multica-ai/multica) 小队执行「**派遣 → 开发 → 等待 → 独立验收 → 打回重做 → 自动关单**」的全自动流水线技能（ZCode / AI Agent 用）。

经三个迭代、20+ 子任务的真实项目实战验证——包括一次完整的打回重做、一次 agent 串台事故的全流程处置、以及"变异自证 + 双层独立验证"的质量纪律。

## 这是什么

把"AI 小队干活"从玄学变成工程：

- **派遣**：`decide` 机验"追评还是新建"（8 条规则的决策表），`dispatch` 一键建单指派；
- **等待**：`watch` 后台轮询任务状态，完成自动唤醒会话；
- **验收**：`fetch` 抓取交付材料 → 会话内**独立复跑**（不复读 agent 自报）→ 产出结构化 verdict → `submit` 契约校验后回写，通过自动关单、不通过打回重做；
- **沉淀**：全部陷阱（编码/任务语义/agent 管理/失败诊断）与事故处置模板随技能分发。

核心信念：**agent 自报 PASS ≠ 完成**——每层结论由下一层独立复跑取证。实测中这套纪律在
"71 个单测全绿"的背景下抓到了真实解析缺陷，也在 agent 交付完全无关任务（上下文串台）时拦住了污染。

## 前置条件

- [Multica](https://github.com/multica-ai/multica) 桌面版（含 CLI），已登录并配置小队；
- 一个 Multica 小队（推荐四件套：architect / dev / verifier / reviewer，创建模板见 SKILL.md）；
- 配套工具仓 **ai_skill_engine**（本技能的命令行工具链 `multica_qa_loop.py` 所在，
  含 `decide`/`dispatch`/`watch`/`fetch`/`submit` 五个子命令与全量测试）。

## 使用（六步循环）

```bash
# ① 派遣决策：追评还是新建（机验，不靠感觉）
python multica_qa_loop.py decide --worktree <仓库> --prev-issue <旧单>

# ② 派遣（指派即开工）
python multica_qa_loop.py dispatch <agent|squad> <方案.md> --title "..."

# ③ 等待（后台运行，完成自动唤醒会话）
python multica_qa_loop.py watch <issue-id>

# ④ 抓取验收材料
python multica_qa_loop.py fetch <issue-id>

# ⑤ 会话内独立复验，产出 verdict JSON
#    {"passed": bool, "score": 0-100, "reasons": [...], "suggestions": [...]}

# ⑥ 回写（通过自动关单；不通过 = 打回，评论触发 agent 重做）
python multica_qa_loop.py submit <issue-id>
```

详细协议、验收纪律清单、决策表（Rule A–F）、诊断顺序见 **SKILL.md**；
CLI 形态速查见 `references/tooling.md`；陷阱全表见 `references/pitfalls.md`；
事故处置模板见 `references/incident-playbook.md`。

## 技能结构

```text
multica-squad-pipeline/
├── SKILL.md                        # 核心工作流（触发时加载）
└── references/
    ├── tooling.md                  # CLI 形态速查（按需读取）
    ├── pitfalls.md                 # 陷阱全表（实测蒸馏）
    └── incident-playbook.md        # 串台事故处置模板
```

## 致谢

- [Multica](https://github.com/multica-ai/multica) — AI 原生团队工作区，本技能的执行底座。

## License

[MIT](LICENSE)

---

# English

A skill for running a fully automated **dispatch → develop → wait → independent acceptance → reject-and-redo → auto-close** pipeline with [Multica](https://github.com/multica-ai/multica) agent squads (for ZCode / AI agents).

Battle-tested across three real iterations and 20+ sub-tasks — including a complete reject-and-redo cycle, a full agent context-contamination incident response, and a "mutation-evidence + dual-layer independent verification" quality discipline.

## What it does

Turns "AI squads doing work" from folklore into engineering:

- **Dispatch**: `decide` machine-checks "append-comment vs new-issue" (an 8-rule decision table); `dispatch` creates the issue and assigns in one step;
- **Wait**: `watch` polls the task in the background and wakes the session on completion;
- **Acceptance**: `fetch` pulls the delivery material → the session **independently re-runs** the acceptance criteria (never trusting agent self-reports) → produces a structured verdict → `submit` validates it against the schema and writes back: pass auto-closes the issue, fail bounces it with actionable fix instructions;
- **Distilled knowledge**: every trap (encoding / task semantics / agent management / failure diagnosis) and an incident-response playbook ship with the skill.

Core belief: **an agent self-reporting PASS ≠ done** — every layer's conclusion must be independently re-run by the next. In practice this discipline caught a real parsing bug behind 71 green unit tests, and blocked out-of-scope deliveries when an agent context-contamination incident struck.

## Prerequisites

- [Multica](https://github.com/multica-ai/multica) desktop app (with CLI), logged in, with a squad configured;
- A Multica squad (recommended: the four-role template architect / dev / verifier / reviewer — see SKILL.md);
- The companion tooling repo **ai_skill_engine** (hosts the CLI toolchain `multica_qa_loop.py`
  with the `decide`/`dispatch`/`watch`/`fetch`/`submit` subcommands and full test suite).

## Usage (the six-step loop)

```bash
# 1. Dispatch decision: append-comment or new-issue (machine-checked, not vibes)
python multica_qa_loop.py decide --worktree <repo> --prev-issue <old-issue>

# 2. Dispatch (assignment starts the agent immediately)
python multica_qa_loop.py dispatch <agent|squad> <plan.md> --title "..."

# 3. Wait (background; wakes the session on completion)
python multica_qa_loop.py watch <issue-id>

# 4. Fetch the review material
python multica_qa_loop.py fetch <issue-id>

# 5. Independently verify in-session; produce a verdict JSON
#    {"passed": bool, "score": 0-100, "reasons": [...], "suggestions": [...]}

# 6. Write back (pass auto-closes; fail = reject, the comment triggers a redo)
python multica_qa_loop.py submit <issue-id>
```

See **SKILL.md** for the full protocol, acceptance checklists, and the decision table (Rules A–F);
`references/tooling.md` for CLI syntax; `references/pitfalls.md` for the trap compendium;
`references/incident-playbook.md` for the incident-response template.

## Skill layout

```text
multica-squad-pipeline/
├── SKILL.md                        # Core workflow (loaded on trigger)
└── references/
    ├── tooling.md                  # CLI syntax cheat sheet (on demand)
    ├── pitfalls.md                 # Field-tested trap compendium
    └── incident-playbook.md        # Incident response template
```

## Acknowledgments

- [Multica](https://github.com/multica-ai/multica) — the AI-native team workspace this skill runs on.

## License

[MIT](LICENSE)
