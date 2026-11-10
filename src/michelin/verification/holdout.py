"""L4 holdout 输入契约 —— 第二处必须自研的核心，也是最容易被后人「不小心」放宽的地方。

架构文档 §L4：verifier 的输入结构体只接受
    issue body + diff + test output + governance docs
把作者的 PR 描述 / title / commit message / plan / tool transcript **结构性排除**。
这里用 frozen + slots dataclass 锁死形状，并由 tests/test_holdout.py 用 AST 锁死
字段集合与 verify() 签名，防止半年后有人为了「让 verifier 更聪明」把作者自述塞回去。

思想来自 aberson/void-furnace 的 held-out critic，不是它的代码。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

# 白名单：改这个集合会让 tests/test_holdout.py 立刻变红，这是刻意的。
ALLOWED_FIELDS = frozenset({"issue_body", "diff", "test_output", "governance_docs"})

# 任何作者侧叙述字段都属于污染，禁止以任何形式进入 holdout 结构体。
FORBIDDEN_FIELDS = frozenset({
    "author_plan", "pr_description", "pr_title", "commit_message",
    "tool_transcript", "kickoff", "self_report", "worker_notes", "plan",
})


class HoldoutViolation(RuntimeError):
    pass


def assert_holdout_shape(bundle: object) -> None:
    fields = {f.name for f in dataclasses.fields(bundle)}  # type: ignore[arg-type]
    if fields != ALLOWED_FIELDS:
        raise HoldoutViolation(
            f"holdout 输入形状被改动：期望 {sorted(ALLOWED_FIELDS)}，实际 {sorted(fields)}"
        )
    leaked = sorted(FORBIDDEN_FIELDS & set(dir(bundle)))
    if leaked:
        raise HoldoutViolation(f"作者侧叙述字段泄漏进 holdout: {leaked}")


@dataclass(frozen=True, slots=True)
class HoldoutBundle:
    """独立验证者唯一被允许看到的东西。

    test_output 是 worker 的**自陈**结果，verifier 必须丢弃它并独立重跑；
    字段保留只是为了在证据链里记录「当时作者声称了什么」，便于回放对比。
    """

    issue_body: str
    diff: str
    test_output: str
    governance_docs: tuple[str, ...]

    def __post_init__(self) -> None:
        assert_holdout_shape(self)
        if not isinstance(self.governance_docs, tuple):
            raise HoldoutViolation("governance_docs 必须是不可变 tuple")


def build_holdout(
    *,
    issue_body: str,
    artifact_text: str,
    self_reported_test_output: str,
    governance_docs: tuple[str, ...],
) -> HoldoutBundle:
    """构造 holdout 输入的唯一入口。

    签名里**没有** kickoff / plan / transcript 参数 —— 这是结构性隔离的落点，
    tests/test_holdout.py 会用 AST 逐项检查这个签名不放宽。
    """
    return HoldoutBundle(
        issue_body=issue_body,
        diff=artifact_text,
        test_output=self_reported_test_output,
        governance_docs=tuple(governance_docs),
    )
