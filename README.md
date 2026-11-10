# miqilinone
米其林厨房

> **AI 米其林智能工作台** —— 一条把「自然语言需求」变成「可验证交付」的流水线。
> 调度、执行、裁决三权分离；每一次交付都带一条 append-only 哈希证据链，
> 门禁未证明就不得派发。

本仓库是一个**可运行**的工程骨架（P0–P6），不是设计稿：门禁在
**真实的 GitHub Actions** 上跑通过（push 与 pull_request 两个 run 均成功，
`gate-report.json` artifact 可下载复核）；接入真实模型后门禁扩到 **15 项**
（新增仓库密钥扫描与真实模型冒烟，后者无密钥时自动跳过）。

---

## 1. 它解决什么问题

把任务交给 agent，最常见的失败不是「模型不够强」，而是**没人能证明它做对了**。
这套系统的赌注是：

| 常见做法 | 本仓库的做法 |
|---|---|
| worker 自己说「我完成了」 | **独立 verifier** 重跑，丢弃 worker 自证 |
| 规则写了但没人验证它会触发 | 每条规则配 **mutation test**，不触发就红 |
| 出错就重试 | 错误**回流成环境规则**，同一个坑不踩第二次 |
| 事后翻日志 | **append-only 哈希链**，篡改可检出 |
| 门禁红了靠人肉判断 | 门禁本身就是代码，**没证明就不派发** |

核心不变量（贯穿 P0–P5，改动需重跑全部门禁）：

1. **调度 / 执行 / 裁决隔离** —— 干活的人不能给自己打分。
2. **append-only 事件链** —— 事件只能追加，带前序哈希；禁 UPDATE / DELETE。
3. **一手上下文不可变引用** —— 只存引用与哈希，不复制可漂移的原文。
4. **门禁未通过即阻断** —— 不是「记录失败」，是「不允许下一步」。
5. **产物即证据** —— 没有跑过的 skill 只是草稿，不是交付物。

## 2. 架构：七层 + 三支柱

```
接入层 L1  webhook / CLI / 定时触发        —— 幂等键 + 签名校验 + DLQ
总厨层 L2  coordinator / 政策 / 审批门     —— 只调度，不执行
上下文层 L3 一手来源 / 快照 / 指纹        —— 不可变引用
执行层 L4  worker 池 / 沙箱 / 技能         —— 无裁决权
验证层 L5  独立 verifier / holdout / 变异  —— 唯一有权下裁决的层
验收层 L6  抽样 / 评分 / 交付 / 重放        —— 对外交付与回放
护栏层 L7  结构化规则 / 预算 / kill switch —— 环境级约束
```

横切三支柱：**事件状态**（append-only 证据链）、**可观测成本**（span 哈希链 + 预算）、
**安全治理**（最小权限 + 凭证不落盘）。

完整设计与取舍见 [`docs/智能工作台架构设计.md`](docs/智能工作台架构设计.md)。

## 3. 目录结构

```
src/michelin/
  platform/      事件库（SQLite / Postgres）、信封与哈希链、存储契约
  inbox/         webhook 签名验证、规范化、幂等占位、DLQ
  chief/         总厨：工作流、政策、DBOS durable workflow、P2/P3 编排
  workers/       无裁决权的执行者（含真实 git worktree worker）
  verification/  六段式 skill 生成、holdout、独立 verifier、变异测试、
                 规则双后端（python-ast / ast-grep）、仓库访谈器、自动起草
  runtime/       进程级隔离、span 哈希链、预算与 kill switch
  guardrails/    结构化规则
ci/
  gate.py        **唯一门禁入口**（15 项检查，产出 gate-report.json）
  pr_diff.py     真实 git 三方 diff（A/M/R/C/D/T、NUL 安全）
  pr_fixture.py  可复现的 PR 靶场
  github_api_push.py  Git Data API 推送（git push 被代理拦死时的通道）
  ssh_push.py    SSH 通道推送（可写 .github/workflows/，无 scope 限制）
  ci_sim.py      **本地复现 GitHub Actions runner**
fixtures/ledger-kit/   异构外部仓库（CI 离线可复现的泛化靶子）
plans/                 验证计划（journeys / structured rules / mutations）
tests/                 契约测试（含「门禁必须会红」的负向测试）
```

## 4. 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[astgrep]"
.venv/bin/python run.py demo                          # P0 全链路：证据链 + 六锚点回放
.venv/bin/python run.py verify-chain                  # 校验 append-only 哈希链
.venv/bin/python run.py replay --run-id <RUN_ID>     # 只读回放因果链
.venv/bin/python run.py p2-verify --scenario ok      # P2 六段式 skill + 变异门禁
.venv/bin/python run.py p3-interview --repo fixtures/ledger-kit
.venv/bin/python run.py p3-verify --repo <仓库> --plan <计划.json>
.venv/bin/python run.py p4-draft --repo fixtures/ledger-kit --out /tmp/plan.json
.venv/bin/python run.py p5-runtime --trip             # 演示预算熔断与 kill switch
```

### 真实模型（DeepSeek）

```bash
# 密钥只放本机文件（600 权限），绝不进仓库
mkdir -p ~/.config/michelin && chmod 700 ~/.config/michelin
printf '%s' 'sk-...' > ~/.config/michelin/deepseek_api_key && chmod 600 ~/.config/michelin/deepseek_api_key

python3 run.py p6-live --backend deepseek --allow-same-family
```

真实调用 DeepSeek（`deepseek-flash` 做 worker、`deepseek-v4-pro` 做 verifier），
输出真实 token 用量、耗时与成本，并把这些数字记进证据链。门禁里的
`real_model_smoke` 会在有密钥时做一次真实冒烟，**没有密钥就跳过**（CI 不依赖密钥）。

**关于模型家族独立性**：`deepseek-flash` 与 `deepseek-v4-pro` 同属
`open-source-cn` 家族，默认会被 holdout 护栏拒绝。放行要显式声明
（`--allow-same-family` 或 `MW_ALLOW_SAME_MODEL_FAMILY=1`），而且这份豁免
**会被写进证据链**：事件流里有一条 `model.independence_waived`，verdict 里也有
`model_independence_waived` 检查项。降级可见，不静默。

`p1-webhook`（验签/去重/DLQ）、`p1-durable`（DBOS 持久工作流与崩溃恢复）、
`p1-status` 需要 Postgres 或 DBOS 环境，未装时相关检查会按环境 skip。

### 门禁

```bash
python3 ci/gate.py --report data/ci/gate-report.json   # 15 项检查，唯一入口
python3 ci/gate.py --fast                               # 快速版
```

15 项分别是：单测、自举验证、异构外部仓库、换 ast-grep 主后端、自动起草、
无应用知识污染、**生成的 YAML 规则交给真正的 ast-grep CLI 扫**、
PR 增量规则扫描、负向阻断、head_sha 敏感性、**真实 git PR diff**、
运行时隔离证据、埋点与预算熔断、**仓库密钥扫描**、**真实模型冒烟**。

最后一项在没有配置密钥时**标记为 SKIP 而不是 PASS** —— 报告的 `summary.skipped`
会单独列出，让人一眼看出这项没被验过。

### 复现 CI

「本地能过」不等于「CI 能过」——本地有 `.venv`、有运行产物、有历史 ref，CI 全都没有。
`ci/ci_sim.py` 复制 CI 的四个前提并执行 workflow 里那条一模一样的命令：

```bash
python3 ci/ci_sim.py                       # 用本地已提交内容
python3 ci/ci_sim.py --repo wjzxl/miqilinone   # 用真实远端
```

## 5. 证据链与回放

每个事件带前序哈希，构成可校验链；`verify_chain()` 检测篡改与断链。
一次交付固定留下**六个锚点**，任何一环缺失即可判定「不可回放」：

```
intent → context_snapshot → dispatch → verdict → delivery → receipt
```

P2 引入的 **holdout** 把 verifier 的输入锁死：只允许 `issue_body / diff /
test_output / governance_docs` 四类字段，worker 的自述（PR 描述、plan、
transcript）无法进入裁决通道；字段签名由单测锁死，防止被「不小心」放宽。

## 6. 验证不是口号：变异测试

每条结构化规则都必须能**被证伪**。做法是为规则造「变异体」：故意把代码改成
违规形态，若规则不报红，就判定门禁失效。

```
base_line clean=True  →  逐条注入变异  →  4/4 条规则命中
```

变异统计口径在 P2 修过一次：Launch 阶段失败不能算「变异开火」，
必须确认 Drive 阶段真的执行过。

## 7. 泛化：生成物不含目标应用知识

同一套生成器要能服务于任意仓库，因此 P3 引入了**可配置仓库访谈器**
（`interview()`）与**结构化规则**双后端（`python-ast` 与 `ast-grep`）：

- 访谈器读仓库结构 → 推导入口、测试目标、治理文档、候选规则。
- 规则以 AST 模式表达，双后端必须给出**一致**结果（对拍），否则阻断。
- 生成物里不得出现主体工程的硬编码知识（CI 会静态扫描 33 个生成文件）。

已验证的仓库：主体工程自身、异构 fixture（flat layout / `spec/` 目录）、
真实第三方 `pallets/itsdangerous`（src layout + pytest）——**生成器零改动**。

## 8. 运行时护栏与诚实边界

`src/michelin/runtime/` 提供进程级隔离（env 白名单、只读源快照、rlimits、
超时 SIGKILL、cwd/HOME 隔离）、append-only 哈希链 span、预算与黏性 kill switch。

**明确不夸大的边界**：

- 进程级隔离**强制不了**文件系统越界与出站网络，证据里如实标 `declared-only`，
  CI 断言不允许写成 `enforced`；生产需换 `container_command()` 生成的后端
  （gVisor / Kata + seccomp + 非 root）。
- rlimit 由子进程在 exec 前把**实测读回值**写进证据；某些平台会静默忽略某些
  limit（例如 macOS 忽略 `RLIMIT_AS`），报告写「3/4 生效，平台忽略 RLIMIT_AS」，
  不把「请求过」当成「生效过」。

## 9. 推送到远端的两个通道

某些网络环境下 `git push` 会被代理以 `CONNECT tunnel failed, response 502` 拒绝，
而 `git fetch` / REST API 正常。因此提供两条通道：

| 通道 | 文件 | 适用 |
|---|---|---|
| Git Data API | `ci/github_api_push.py` | 常规推送；**无法写 `.github/workflows/`** |
| SSH（443） | `ci/ssh_push.py` | 需要写 workflow 文件；SSH 认证无 scope 限制 |

**平台语义坑（踩过，记下来）**：GitHub 对写入 `.github/workflows/` 的请求，
在 token 缺 `workflow` scope 时返回 **404 Not Found**（不是 403）。
同一 token 下 `docs/x.txt`、`.github/x.md` 都成功，只有 workflows 目录失败 ——
极易被误判成「仓库不存在」。工具因此内置 `--capabilities` 探测。

两条通道都遵守同一条纪律：**只接受 `login == 目标仓库 owner` 的凭证**，
凭证只在内存里使用、不回显、不落仓；推送全部 fast-forward，**不 force push**，
远端独有的文件（哪怕本地没有）绝不进提交。

## 10. 还没做的部分

诚实列出，避免被误当成已完成：

- 每任务的容器级隔离与网络策略（当前只有进程级 + `declared-only` 声明，
  需换 gVisor / Kata 后端）。
- 规则后端只支持 Python 的三种 kind；PR 增量扫描只吃 `.py`。
- OTel 只落地了哈希链 span 与 GenAI 属性，尚未接具体可观测平台。
- 夜间 cron、fan-out、durable queue 并发限流未接。
- MCP 网关尚未接入；模型侧目前只做了 DeepSeek 与通用 OpenAI 兼容端点。
- 真实模型目前只接在 P0 只读链路（P6）；P2/P3 的 git 任务仍是脚本化模型。

## 11. 许可

代码遵循 MIT。设计方法论参考了公开的 agent 工程实践（详见架构文档的核实清单），
未复制任何受限制许可的源码。
