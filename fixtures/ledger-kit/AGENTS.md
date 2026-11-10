# AGENTS.md — ledger-kit 治理约定

1. 记账核心只做纯函数，不访问网络、不读环境变量。
2. 外部命令必须显式 argv，禁止 `subprocess.getoutput` 之类拼 shell 的调用。
3. 数据交换只用 JSON；禁止 `pickle` 反序列化与第三方 YAML 解析。
4. `DEBUG` 不允许以字面量 `True` 提交到代码里。
5. 改动必须先跑 `make check`，再跑一次验证 skill 的六段式。

这些约定由 `plans/ledger-kit.json` 编译成结构化规则，且每条规则都必须有故意违规的变异证明。
