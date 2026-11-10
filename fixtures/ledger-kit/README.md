# ledger-kit

一个刻意做成与 `michelin-workbench` **完全不同形态**的外部仓库，用于 P4 泛化验证：

| 维度 | michelin-workbench | ledger-kit |
|---|---|---|
| 源码布局 | `src/michelin/`（src layout） | `ledgerkit/`（flat layout，根目录直接可导入） |
| 测试目录 | `tests/` | `spec/` |
| 测试框架 | unittest（`discover -s tests`） | unittest（`discover -s spec -p "test_*.py"`） |
| 用户入口 | `run.py` / `michelin` console script | `bin/ledger`（无 `.py` 后缀的 Python 脚本） |
| 治理文件 | 无 | `AGENTS.md` |
| 配置 | `compose.yaml` / `.env.example` | `config/settings.json` / `Makefile` |
| 结构化规则 | eval / os.system / requests / TAX_RATE | pickle.loads / subprocess.getoutput / yaml / DEBUG |

目标：同一份 `generate_generic_skill()` 与同一个 `verify_helper.py`，**一行不改**，
换一份 `VerificationPlan` 就能在这个仓库上端到端 PROVEN + 变异全开火 + PASS。

本仓库不是 michelin-workbench 的子模块，也不 import 它的任何代码。
`spec/fixtures_target.py` 是变异注入靶子：文件名不匹配 `test_*.py`，因此不会被
任何 journey 导入，注入违规代码不会污染旅程结果。
