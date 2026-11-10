"""成本 / 动作预算与 kill switch（架构 §8「成本失控」的代码落点）。

四个维度同时限流：动作数、累计墙钟、外部请求数、按 token 估算的成本。任一超限立即
熔断，且 **熔断是黏性的** —— 同一 run 里之后的每一次 check 都直接拒绝，不给「再试一次
刚好过」的机会。想继续只能开一个全新 run_id，旧账本保留。

账本是 append-only 哈希链，记录的是当时**实测**的 charge（token 数、耗时、请求数），
不是重放时重新调模型算出来的猜测。这正是架构文档里 Replay Divergence 的防线：
`replay()` 只读账本重算，同一份账本必得同一裁决与同一 head_hash。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..platform.envelope import canonical, content_hash

GENESIS = "GENESIS"


@dataclass(frozen=True, slots=True)
class Budget:
    max_actions: int = 20
    max_seconds: float = 60.0
    max_external_requests: int = 10
    max_cost_usd: float = 0.10
    input_usd_per_1k: float = 0.003
    output_usd_per_1k: float = 0.015

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1000.0 * self.input_usd_per_1k
                + output_tokens / 1000.0 * self.output_usd_per_1k)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Charge:
    actions: int = 1
    seconds: float = 0.0
    external_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str, ledger_hash: str):
        super().__init__(f"预算熔断: {reason} (ledger={ledger_hash[:16]})")
        self.reason = reason
        self.ledger_hash = ledger_hash


def _initial_usage() -> dict[str, float]:
    return {"actions": 0, "seconds": 0.0, "external_requests": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


class BudgetGuard:
    def __init__(self, budget: Budget, path: Path, run_id: str):
        self.budget = budget
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usage = _initial_usage()
        self.tripped = False
        self.trip_reason: str | None = None
        self.prev = GENESIS
        self.seq = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            if not ln:
                continue
            row = json.loads(ln)
            payload = row["payload"]
            if payload["kind"] == "charge":
                self.usage = dict(payload["usage_after"])
            elif payload["kind"] == "kill_switch":
                self.tripped = True
                self.trip_reason = payload["reason"]
            self.prev, self.seq = row["hash"], row["seq"]

    def _append(self, payload: dict[str, Any]) -> str:
        self.seq += 1
        body = {"seq": self.seq, "run_id": self.run_id, **payload}
        digest = content_hash({"prev": self.prev, "body": body})
        row = {"seq": self.seq, "hash": digest, "payload": body}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self.prev = digest
        return digest

    def _project(self, charge: Charge) -> dict[str, float]:
        next_usage = dict(self.usage)
        next_usage["actions"] += charge.actions
        next_usage["seconds"] += charge.seconds
        next_usage["external_requests"] += charge.external_requests
        next_usage["input_tokens"] += charge.input_tokens
        next_usage["output_tokens"] += charge.output_tokens
        next_usage["cost_usd"] = round(
            self.budget.cost(int(next_usage["input_tokens"]), int(next_usage["output_tokens"])), 6
        )
        return next_usage

    @staticmethod
    def _first_exceeded(usage: dict[str, float], budget: Budget) -> str | None:
        checks = (
            ("actions", usage["actions"], budget.max_actions),
            ("seconds", usage["seconds"], budget.max_seconds),
            ("external_requests", usage["external_requests"], budget.max_external_requests),
            ("cost_usd", usage["cost_usd"], budget.max_cost_usd),
        )
        for name, value, limit in checks:
            if value > limit:
                return f"{name}={value:.6g} > {limit}"
        return None

    def _trip(self, reason: str, projected: dict[str, float]) -> None:
        self.tripped = True
        self.trip_reason = reason
        digest = self._append({"kind": "kill_switch", "reason": reason,
                               "projected": projected, "usage_before": dict(self.usage)})
        raise BudgetExceeded(reason, digest)

    def check(self, label: str, charge: Charge | None = None) -> dict[str, Any]:
        """动作前的唯一入口；通过才记账，超限则写 kill_switch 并抛异常。"""
        if self.tripped:
            raise BudgetExceeded(f"KILL_SWITCH 已生效（{self.trip_reason}）", self.prev)
        charge = charge or Charge()
        projected = self._project(charge)
        reason = self._first_exceeded(projected, self.budget)
        if reason:
            self._trip(reason, projected)
        digest = self._append({"kind": "charge", "label": label,
                               "charge": charge.to_dict(), "usage_after": projected})
        self.usage = projected
        return {
            "accepted": True, "label": label, "usage": dict(projected),
            "remaining": {
                "actions": self.budget.max_actions - projected["actions"],
                "cost_usd": round(self.budget.max_cost_usd - projected["cost_usd"], 6),
            },
            "ledger_hash": digest,
        }

    @property
    def kill_switch_engaged(self) -> bool:
        return self.tripped

    def ledger_head(self) -> str:
        return self.prev

    def replay(self) -> dict[str, Any]:
        """只读账本重算；不重新测量、不调模型。用于证明裁决可复现。"""
        usage = _initial_usage()
        prev = GENESIS
        tripped = False
        reason: str | None = None
        count = 0
        ok = True
        if self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                if not ln:
                    continue
                row = json.loads(ln)
                body = row["payload"]
                if content_hash({"prev": prev, "body": body}) != row["hash"]:
                    ok = False
                prev = row["hash"]
                count += 1
                if body["kind"] == "charge":
                    usage = dict(body["usage_after"])
                else:
                    tripped, reason = True, body["reason"]
        return {
            "ok": ok, "events": count, "head_hash": prev,
            "usage": usage, "tripped": tripped, "reason": reason,
            "matches_live": ok and prev == self.prev and tripped == self.tripped
            and usage == self.usage,
        }
