"""Durable runtime 契约：把「脊」的选择与「领域状态机」解耦。

架构文档 §6：引擎只给 step / signal / waitpoint，不懂什么叫「总厨不下场」。
这里定义最小契约；两个实现：
* InProcessRuntime —— P0 降级路径，零依赖、无崩溃恢复
* DBOSRuntime —— 见 durable_workflow.py，真 Postgres durable workflow
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..chief.policy import TaskIntent
from ..chief.workflow import ChiefOfStaff


@dataclass(frozen=True, slots=True)
class DurableResult:
    workflow_id: str
    run_id: str
    state: str
    verdict: str | None
    recovered: bool = False

    def to_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id, "run_id": self.run_id,
            "state": self.state, "verdict": self.verdict, "recovered": self.recovered,
        }


class DurableRuntime(Protocol):
    def start(self, delivery_id: str, intent: TaskIntent) -> str: ...
    def send_approval(self, workflow_id: str, decision: str) -> None: ...
    def result(self, workflow_id: str) -> DurableResult: ...


class InProcessRuntime:
    """P0 兼容实现：直接同步跑 ChiefOfStaff.handle，没有检查点与恢复。

    只用于本地零依赖演示；生产路径必须用 DBOSRuntime。
    """

    def __init__(self, store: Any, chief: ChiefOfStaff):
        self.store = store
        self.chief = chief
        self._results: dict[str, DurableResult] = {}

    def start(self, delivery_id: str, intent: TaskIntent) -> str:
        result = self.chief.handle(intent)
        out = DurableResult(
            workflow_id=delivery_id, run_id=result.run_id,
            state=result.state.value, verdict=result.verdict,
        )
        self._results[delivery_id] = out
        return delivery_id

    def send_approval(self, workflow_id: str, decision: str) -> None:
        return None  # 降级路径没有审批门

    def result(self, workflow_id: str) -> DurableResult:
        return self._results[workflow_id]
