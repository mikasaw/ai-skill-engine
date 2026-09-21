# AI 工作循环技能引擎 (AI Skill Engine)

基于 **洋葱模型（中间件模式）** + **注册表模式** 的 AI 工程化底座：核心引擎极简，
合约校验、防御（超时/熔断）、监控等一切扩展能力均通过中间件插件化注入。

## 快速开始

```bash
# 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/python -m pip install "pydantic[email]" opentelemetry-api opentelemetry-sdk pytest pytest-asyncio

# 真实 LLM 模式（可选）：设置 Key 后自动启用 litellm + instructor
set OPENAI_API_KEY=sk-...
.venv/Scripts/python -m pip install instructor litellm

# 运行演示（未设 Key 时自动进入离线 Mock 模式，链路行为与真实模式一致）
.venv/Scripts/python main.py

# 运行测试
.venv/Scripts/python -m pytest
```

`main.py` 演示三个用例：**正常 Case**（一次通过）、**脏数据 Case**（坏邮箱触发
contract 中间件自修复重试）、**超时 Case**（慢响应被强制超时切断）。

## 目录结构

```text
ai_skill_engine/
├── core/               # 核心引擎（Micro-Kernel，不含业务逻辑）
│   ├── context.py      # SkillContext：一次调用的全部状态载体（含 metadata 通道约定键）
│   ├── skill.py        # BaseSkill + SkillRegistry + @register_skill
│   ├── middleware.py   # BaseMiddleware + MiddlewareRegistry + MiddlewareChain（洋葱模型）
│   └── runner.py       # SkillRunner：装配 Context 与中间件链并执行（run() 永不抛异常）
├── middlewares/        # 内置中间件（切面逻辑，import 即注册）
│   ├── observability.py# TracingMiddleware：OpenTelemetry Span 埋点
│   ├── defensive.py    # CircuitBreakerMiddleware：滑动窗口熔断 + 强制超时
│   ├── contract.py     # SchemaValidationMiddleware：契约校验 + 自修复重试
│   ├── fallback.py     # FallbackMiddleware：模型降级链（主模型失败自动切换备用）
│   ├── cost_limiter.py # CostLimiterMiddleware：单 run 调用次数上限 + 累计 token 预算
│   └── notify.py       # NotifyMiddleware：成功/失败 webhook 通知（旁路，绝不影响主流程）
├── skills/             # 业务技能（只关注 Prompt 与逻辑）
│   ├── user_extractor.py
│   └── multica_qa.py   # Multica 交付质检技能
├── tests/              # pytest（全部 Mock，无需网络）
├── config.py           # 全局配置（模型、降级链、webhook、multica CLI 路径）
├── webhook_receiver.py # 极薄 webhook 接收器：HTTP 事件 → 技能执行（stdlib，零新依赖）
├── multica_qa_loop.py  # 质检闭环编排：本地质检 → 结论回写 Multica issue 评论
└── main.py             # 运行入口与四用例演示
```

## 设计原则

1. **核心极简 (Micro-Kernel)**：`core/` 只负责生命周期调度与中间件执行。
2. **洋葱模型**：请求穿过中间件链到达技能，再原路返回；所有切面逻辑（校验、
   重试、熔断、监控）都是中间件，可按 全局 → 技能级 → 单次调用级 三层组合。
3. **注册表模式**：`@register_skill` / `@register_middleware` 装饰器自动注册，
   新增能力零修改核心代码。
4. **强类型契约 (Schema-First)**：输入输出基于 Pydantic V2；LLM 输出由
   instructor 强制结构化。

### 中间件执行顺序

```
全局 (settings.global_middlewares，默认 ["tracing"])
  → 技能级 (Skill.middleware_names，如 ["circuit_breaker", "contract"])
    → 技能 execute()
```

调用 `runner.run(..., middlewares=[...])` 时完全覆盖上两层（单次调用级组合），
元素支持 `"name"` 或 `("name", {构造参数})`。有状态中间件（熔断器）实例按
`(name, options)` 缓存，同一 Runner 内多次调用共享状态。

### 双层重试体系（契约自愈）

- **instructor 层**（技能内）：`max_retries=2`，库自带的结构化重试；
- **引擎层**（`SchemaValidationMiddleware`）：校验 `final_output`，失败时把
  Pydantic 错误明细写入 `context.metadata["retry_prompt"]` 并重新执行技能，
  最多重试 2 次；耗尽后记入 `context.errors` 并保留最后一次坏输出供排查。

技能通过读取 `context.metadata.get("retry_prompt")` 把纠正提示并入 Prompt
（这是 metadata 作为中间件通信通道的约定用法），除此之外 execute 不感知重试。

### metadata 通道约定键（core/context.py 定义）

| 键 | 写入方 | 含义 |
|---|---|---|
| `retry_prompt` | contract | 输出校验失败后的纠正提示，技能应并入 Prompt |
| `llm_model_override` | fallback | 降级后的备用模型，技能应用它替代默认模型 |
| `llm_calls` / `estimated_tokens` | cost_limiter | 本 run 的调用计数与估算消耗 |

### 任务自动化组合

**稳定性与成本**
- `fallback`：按 `["默认模型", *fallback_models]` 依序尝试（构造参数或
  `SKILL_ENGINE_FALLBACK_MODELS` 环境变量），全部失败抛最后一次原始异常；
- `cost_limiter`：`max_calls_per_run` 短路重试/降级风暴，`max_estimated_tokens`
  按实例累计估算 token 预算（Runner 内同一技能共享实例即共享预算）；
- 推荐挂载顺序：`["circuit_breaker", "fallback", "contract", "cost_limiter"]`
  （列表首为最外层；cost_limiter 在 contract 之内可精确统计每次真实 LLM 调用）。

**Multica 联动闭环：任务自动化开发与验收（全链路）**

```text
dispatch（派遣开发）──→ watch（后台等待，退出唤醒会话）──→ fetch → 会话内评审 → submit
      ↑                                                              │
      └──────────── 打回评论触发 agent 重做 ←───── 不通过 ───────┤
                                                                     ↓ 通过 → 自动关单（done --no-start，不惊动 agent）
```

```bash
# ① 开发派遣：建 issue 指派 agent，指派即自动开工（标题默认取方案首个非空行）
python multica_qa_loop.py dispatch 代码开发助手 qa_reviews/plan.md --title "实现XX"

# ② 开发等待：后台运行，run 终止即退出并唤醒 ZCode 会话
python multica_qa_loop.py watch <issue-id> --poll 20 --timeout 1800

# ③④ 验收接力：fetch 抓材料 → 会话内评审写 verdict → submit 回写
python multica_qa_loop.py fetch  <issue-id>
#    ……评审者产出 qa_reviews/<id>.verdict.json……
python multica_qa_loop.py submit <issue-id>   # 不通过：打回评论=修复指令，agent 自动重做 → 回到②
                                              # 通过：✅ 评论 + 自动关单（--no-close 可关闭）
```

半程接力评审（无需任何 LLM API key）：

```bash
# ① 抓取材料（建议后台运行：任务退出自动唤醒 ZCode 会话开始评审）
python multica_qa_loop.py fetch MIT-551
#    → 材料写入 qa_reviews/MIT-551.material.md

# ② 评审者（ZCode 会话）阅读材料，产出 QaVerdict JSON 写入：
#    qa_reviews/MIT-551.verdict.json
#    {"passed": bool, "score": 0-100, "reasons": [...], "suggestions": [...]}

# ③ 回写评论（submit 侧做契约校验；verdict 提交即消费，防陈旧重复）
python multica_qa_loop.py submit MIT-551
```

评审员同样受 Schema 纪律约束：verdict 不满足 `QaVerdict` 会被拒收。
材料/verdict 文件即状态机：material 存在=待评审，verdict 存在=待回写。

`auto` 路线为引擎全自动（抓取 → instructor 评审 → 回写），适合批量与无人值守，
需配置 LLM API key：

```bash
python multica_qa_loop.py auto <issue-id>...
```

### Multica 实操经验备忘（源自 hermes 三技能 + 本项目实测）

**编码与调用**
- CLI 调用一律走 Python `subprocess` list-args（`CreateProcessW` UTF-16）——PowerShell argv 的
  GBK 双重编码会把乱码写进数据库且不可逆；本项目 `run_multica` 即此姿势。
- `--output json` 必须显式传（默认 table）；`issue get` 默认 JSON。
- `issue comment add` 用位置参数 `<issue-id>`（无 `--issue-id`）；`agent get` 只认 UUID 不认名字；
  `squad member add <squad-id> --member-id <uuid>`；`issue view` 不存在，统一 `issue get`。
- 长文本优先 `--description-stdin` / `--content-file`；本项目统一走 cwd 内临时文件 + `newline="\n"`。

**任务语义**
- `issue rerun` = 取消当前 + 重新入队；assign 后不要再 rerun。
- 已取消的 issue 可能被完成中的 agent 推回 in_review——取消后复查最终状态。
- `issue create --assignee` 有并发空位会立即拾取、无视 stage：依赖前置的子单创建时不给 assignee。
- comment 必须先于 `issue status done`（证据顺序）；`--no-start` 防止状态变更唤醒 agent。
- `type=system` 评论携带 429 用量 / 402 余额等平台错误原文，是"任务卡住"的第一诊断位。

**agent 管理**
- `agent update --instructions` 是**整表替换**：先 GET 全量、追加后写回；单次 ≤1800 字符；
  严禁用短串试探（会把默认 prompt 擦没且不可恢复）；更新后立即回读核对长度。
- 新建 agent 的 instructions 必须冻结**项目绝对路径**（daemon 的 task workdir 是空目录，
  agent 行为由 instructions/CLAUDE.md 的主路径决定——MIT-553 零交付的根因）。
- agent 的 feat branch 不自动 merge main：验收查提交要 `git log --branches --all`。

**诊断顺序**（任务不动时）
1. `issue runs <id>` 看最新 run 状态（`failed+runtime_recovery` 后紧跟新任务 = 自动恢复，非失败）；
2. `issue comment list <id>` 找 `[system]` 错误评论；
3. `invalid workspace_id`（rc=5）≠ 配置错误，是 daemon 并发压力——等 30-60s 重试即可；
4. `issue list` 空输出先查 session 过期（exit 3 → `multica login`），再怀疑 API。

### Multica 机制备忘：追加评论 = resume 会话

Multica 没有 session resume（`issue rerun` 自述为 fresh task；agent 的 `--custom-args`
是静态配置、无法按任务注入 resume 参数）。**在同一 issue 上追加评论即可达到等价
resume 的效果**，本项目已实测并作为默认机制使用：

- 评论会触发一轮新的 agent run，agent 带着该 issue 的全部载体上下文继续工作——
  描述、评论线程、附件、执行历史，这就是"延续性"的来源（对话内存不延续，
  事实状态延续，见下方架构纪律：状态要显式沉淀到评论/文件里）；
- 想让 agent 做什么，直接把可执行指令写进评论；CLI 约束 agent 的回复必须挂在
  触发评论的线程下（`comment add --parent` 的设计意图），对话天然线程化；
- **实测依据（MIT-552）**：打回评论提交后，watch 立即观测到新 run 进入 running，
  agent 按评论中的指令完成第二轮交付并规范回报，验收通过后自动关单；
- 引擎侧的对应物：`contract` 中间件把校验失败明细注入 `retry_prompt` 让技能自愈——
  打回评论的 reasons 就是跨系统层面的 retry_prompt，两者同构。

⚠️ 不建议用 `--custom-args` 给底层 runtime 硬塞 `--resume` 旗标：配置静态不可按任务
区分、依赖 runtime 磁盘上的会话残留、且绕过编排层导致执行历史失真。

### 派遣决策表：追评（续单）还是新建（fresh brief）

追评便宜在上下文延续，新建贵在冷启动，但错记忆比冷启动更贵。下表按序判定，
**首条命中即生效**；判据 A/C/E 可由 `decide` 子命令机验，B/D/F 属语义判断交人工：

| 序 | 判据（可机验） | 动作 |
| --- | --- | --- |
| A | `git -C <worktree> status --porcelain` 非空 | 禁 rerun（会把未提交工作坑给下一代）。只剩追评或人工交接，默认追评保上下文 |
| B | 新工作针对同一靶子（同一 diff／同一批文件／上一条结论清单），且 `log --oneline <上轮被审sha>..HEAD` 只多出针对该清单的 commit | 追评（正文必须自带本轮判据，不能只写"继续"） |
| C | 基线已变：`rev-list --count <首跑基线>..main > 0`，或依据文件的 `log -1 --format=%cd` 晚于首跑时刻 | 新建；若仍追评，必须点名"重新 Read 这几个文件并逐条回判" |
| D | 验收边界变了（新条款／换清单／扩范围） | 新建（评审要有可指的清单；改旧单描述不唤醒，旧清单还会污染结论） |
| E | 上下文已肥：同 task 唤醒 ≥8 次，或其 jsonl >2MB | 新建（已压缩过，"记得"不可信；冷启动比错记忆便宜） |
| F | 只是落章／补留痕／收尾 | 追评 |
| 默认 | 以上全不成立 | 新建一张带完整 brief 的单 |

```bash
# 机验 A/C/E（B/D/F 输出为人工确认清单；--force 可人工覆盖）
python multica_qa_loop.py decide --worktree <仓库路径> --prev-issue <旧单id> --baseline <首跑基线sha>

# 追评入口（正文自带本轮判据，Rule B 纪律）
python multica_qa_loop.py comment <issue-id> --file 本轮判据.md
```

## 扩展指南

### 新增“支撑机制”（如 Token 成本控制）

**不要修改 `core/` 或 `skills/`。**

1. 在 `middlewares/` 新建 `cost_limiter.py`；
2. 继承 `BaseMiddleware`，实现 `process(context, next)`（如检查预算）；
3. 类上标注 `@register_middleware`、`name = "cost_limiter"`；
4. 在 Skill 的 `middleware_names` 或 `settings.global_middlewares` 中加入名字。

### 新增“业务技能”

1. 在 `skills/` 新建文件，定义 Input/Output 的 Pydantic Schema；
2. 继承 `BaseSkill`，只实现 `execute`（禁止 try-except / 日志 / 重试）；
3. 标注 `@register_skill`，并在 `middleware_names` 声明所需中间件。

### 改变执行流程（如人工审批）

新增 `HumanInTheLoopMiddleware`：在 `process` 中调用 `next()` 拿到结果后暂停
（Webhook / CLI `input()`），人工确认或修改 `context.final_output` 后返回。

## 架构纪律（Code Review 检查项）

- `core/` 出现具体业务逻辑 → 违反 Micro-Kernel；
- `Skill.execute` 里出现 try-except、logger、重试计数 → 违反洋葱模型，
  应移入对应 Middleware；
- 新增切面能力时修改了 `core/` → 应改为新增 Middleware；
- 所有跨组件数据传递只经 `SkillContext`（`metadata` / `errors`）。
