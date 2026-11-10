"""把「一次任务」统一包进 预算 → 隔离执行 → 观测 三个横切关注点。

刻意做成外层包装而不是改 P3Pipeline：P0–P4 的协议、事件形状、裁决逻辑一行不动，
P5 只是给「外部命令」这一动作加一层护栏。任何超限都会在动作发生**之前**抛
BudgetExceeded，不产生子进程，也就不会产生需要事后撤销的副作用。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .budget import Budget, BudgetGuard, Charge
from .sandbox import IsolationReceipt, SandboxPolicy, container_command, isolated_run
from .telemetry import Tracer


class RunHarness:
    def __init__(self, workdir: Path, run_id: str, *, policy: SandboxPolicy | None = None,
                 budget: Budget | None = None, service: str = "michelin-workbench"):
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.policy = policy or SandboxPolicy()
        self.budget = budget or Budget()
        self.tracer = Tracer(self.workdir / "spans.jsonl", run_id, service=service)
        self.guard = BudgetGuard(self.budget, self.workdir / "budget.jsonl", run_id)
        self.receipts: list[IsolationReceipt] = []
        self.closed = False

    def execute(
        self,
        label: str,
        argv: tuple[str, ...] | list[str],
        *,
        source_root: Path | None = None,
        charge: Charge | None = None,
        extra_env: dict[str, str] | None = None,
        policy: SandboxPolicy | None = None,
    ) -> IsolationReceipt:
        active = policy or self.policy
        with self.tracer.span(f"execute.{label}", **{
            "michelin.sandbox.backend": active.backend,
            "michelin.network.policy": active.network,
            "gen_ai.request.model": "scripted-local",
        }) as span:
            decision = self.guard.check(label, charge)
            span.set(**{"michelin.budget.ledger_hash": decision["ledger_hash"]})
            receipt = isolated_run(
                argv, label=label, workdir=self.workdir / "boxes" / label,
                policy=active, source_root=source_root, extra_env=extra_env,
            )
            span.set(**{
                "michelin.sandbox.exit_code": receipt.exit_code,
                "michelin.sandbox.timed_out": receipt.timed_out,
                "michelin.sandbox.env_hash": receipt.env_hash,
                "michelin.sandbox.source_fingerprint": receipt.source_fingerprint or "none",
                "michelin.duration_ms": receipt.duration_ms,
            })
            self.receipts.append(receipt)
            return receipt

    def container_plan(self, argv: tuple[str, ...] | list[str], *,
                        source_root: Path, label: str) -> tuple[str, ...]:
        return container_command(argv, policy=self.policy, source_root=source_root,
                                 workdir=self.workdir / "boxes" / label)

    def close(self) -> dict[str, Any]:
        """写 runtime-report.json；返回含链校验与预算重放的证据包。"""
        self.closed = True
        report = {
            "run_id": self.run_id,
            "workdir": str(self.workdir),
            "policy": self.policy.to_dict(),
            "budget": self.budget.to_dict(),
            "spans": self.tracer.summary(),
            "budget_journal": self.guard.replay(),
            "kill_switch_engaged": self.guard.kill_switch_engaged,
            "trip_reason": self.guard.trip_reason,
            "receipts": [r.to_dict() for r in self.receipts],
        }
        (self.workdir / "runtime-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
