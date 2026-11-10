"""L2 领域状态机与「可验证单元」契约 —— 第一处必须自研的核心。

所有 durable 引擎（DBOS / Temporal）只提供 step / signal / waitpoint，
不懂什么叫「总厨不下场」、什么叫「返工两次后升级人工」、什么叫「evidence_spec 为空不得派发」。
这些语义只能写在这里，并且用显式迁移表锁死，防止后来者随手跳过验证直接交付。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    INBOX_RECEIVED = "INBOX_RECEIVED"
    NORMALIZED = "NORMALIZED"
    SCOPED = "SCOPED"
    DISPATCHED = "DISPATCHED"
    IN_PROGRESS = "IN_PROGRESS"
    SELF_CHECKED = "SELF_CHECKED"
    VERIFYING = "VERIFYING"
    VERDICT_PASS = "VERDICT_PASS"
    VERDICT_FAIL = "VERDICT_FAIL"
    DELIVERED = "DELIVERED"
    SAMPLED = "SAMPLED"
    ACCEPTED = "ACCEPTED"
    RULE_UPDATED = "RULE_UPDATED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    BLOCKED_NEEDS_HUMAN = "BLOCKED_NEEDS_HUMAN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({
    TaskState.ACCEPTED, TaskState.RULE_UPDATED, TaskState.NEEDS_HUMAN,
    TaskState.BLOCKED_NEEDS_HUMAN, TaskState.BUDGET_EXCEEDED,
    TaskState.TIMEOUT, TaskState.CANCELLED,
})

TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.INBOX_RECEIVED: frozenset({TaskState.NORMALIZED, TaskState.CANCELLED}),
    TaskState.NORMALIZED: frozenset({TaskState.SCOPED, TaskState.BLOCKED_NEEDS_HUMAN}),
    TaskState.SCOPED: frozenset({TaskState.DISPATCHED, TaskState.BLOCKED_NEEDS_HUMAN}),
    TaskState.DISPATCHED: frozenset({
        TaskState.IN_PROGRESS, TaskState.NEEDS_HUMAN,
        TaskState.TIMEOUT, TaskState.BUDGET_EXCEEDED,
    }),
    TaskState.IN_PROGRESS: frozenset({
        TaskState.SELF_CHECKED, TaskState.NEEDS_HUMAN,
        TaskState.TIMEOUT, TaskState.BUDGET_EXCEEDED, TaskState.CANCELLED,
    }),
    TaskState.SELF_CHECKED: frozenset({TaskState.VERIFYING, TaskState.DISPATCHED}),
    TaskState.VERIFYING: frozenset({
        TaskState.VERDICT_PASS, TaskState.VERDICT_FAIL, TaskState.NEEDS_HUMAN,
    }),
    TaskState.VERDICT_FAIL: frozenset({TaskState.DISPATCHED, TaskState.NEEDS_HUMAN}),
    TaskState.VERDICT_PASS: frozenset({TaskState.DELIVERED}),
    TaskState.DELIVERED: frozenset({TaskState.SAMPLED}),
    TaskState.SAMPLED: frozenset({TaskState.ACCEPTED, TaskState.RULE_UPDATED}),
}
for _terminal in TERMINAL_STATES:
    TRANSITIONS.setdefault(_terminal, frozenset())


class StateError(RuntimeError):
    pass


def assert_transition(src: TaskState, dst: TaskState) -> None:
    if dst not in TRANSITIONS.get(src, frozenset()):
        raise StateError(f"非法状态迁移: {src.value} -> {dst.value}")


# ---------- 可验证证据规格 ----------

@dataclass(frozen=True, slots=True)
class Predicate:
    """一条确定性、可变异测试的验收谓词。

    P2 会换成 ast-grep YAML / 类型约束 / schema；P0 先用字符串谓词把协议跑通，
    但「每条规则必须配一个故意破坏的变体证明它会开火」这条纪律现在就锁死。
    """

    name: str
    kind: str  # diff_contains | test_output_contains | diff_absent | governance_contains
    value: str

    def check(self, bundle: Any) -> tuple[bool, str]:
        if self.kind == "diff_contains":
            ok = self.value in bundle.diff
            return ok, f"diff 包含 {self.value!r}: {ok}"
        if self.kind == "test_output_contains":
            ok = self.value in bundle.test_output
            return ok, f"test_output 包含 {self.value!r}: {ok}"
        if self.kind == "diff_absent":
            ok = self.value not in bundle.diff
            return ok, f"diff 不含 {self.value!r}: {ok}"
        if self.kind == "governance_contains":
            ok = any(self.value in d for d in bundle.governance_docs)
            return ok, f"governance_docs 提到 {self.value!r}: {ok}"
        raise ValueError(f"未知谓词类型: {self.kind}")

    def mutation_field(self) -> str:
        return {
            "diff_contains": "diff",
            "test_output_contains": "test_output",
            "diff_absent": "diff",
            "governance_contains": "governance_docs",
        }[self.kind]

    def mutate(self, bundle: Any) -> Any:
        """生成「故意破坏」的变体：门禁必须在它上面开火，否则规则形同虚设。"""
        field = self.mutation_field()
        if self.kind == "diff_contains":
            value = bundle.diff.replace(self.value, "")
        elif self.kind == "test_output_contains":
            value = bundle.test_output.replace(self.value, "FAIL")
        elif self.kind == "diff_absent":
            value = bundle.diff + self.value
        else:
            value = tuple(d.replace(self.value, "") for d in bundle.governance_docs)
        return replace(bundle, **{field: value})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidenceSpec:
    predicates: tuple[Predicate, ...]

    def __post_init__(self) -> None:
        if not self.predicates:
            raise ValueError("evidence_spec 为空不得派发（架构文档 §L2 kickoff 硬约束）")

    def to_dict(self) -> dict:
        return {"predicates": [p.to_dict() for p in self.predicates]}

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceSpec":
        return cls(tuple(Predicate(**p) for p in data["predicates"]))


@dataclass(frozen=True, slots=True)
class Kickoff:
    """总厨交给 worker 的唯一输入。P0 只读任务下 writable_paths 必须为空。"""

    task_id: str
    intent: str
    source_hashes: tuple[str, ...]
    writable_paths: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    evidence_spec: EvidenceSpec
    risk_level: str
    budget: float
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "intent": self.intent,
            "source_hashes": list(self.source_hashes),
            "writable_paths": list(self.writable_paths),
            "forbidden_actions": list(self.forbidden_actions),
            "evidence_spec": self.evidence_spec.to_dict(),
            "risk_level": self.risk_level,
            "budget": self.budget,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class TaskIntent:
    """L0 标准化后的需求对象。source_bodies 是一手原文，不是摘要。"""

    intent: str
    source_bodies: tuple[str, ...]
    source_system: str = "manual"
    risk_level: str = "low"
    budget: float = 5.0
    evidence_marker: str = "EVIDENCE:ok"
