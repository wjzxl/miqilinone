"""L2 总厨（Chief of Staff）编排 —— P0 的垂直切片。

铁律：**总厨不下场**。它不写交付物、不改代码、不「顺手帮忙修一下」；
只做拆解、写清可验证证据规格、派发、收裁决、决定返工或升级。
对应 pstack orchestrate.md 原文 "You never author or edit code"。

一次 handle() 走完整条状态机，并把每一步以统一信封写进 append-only events，
使得 `replay(run_id)` 能还原这次决策（P0 的退出判据）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chief.policy import (
    EvidenceSpec, Kickoff, Predicate, TaskIntent, TaskState, assert_transition,
)
from ..context.sources import SourcePlane
from ..delivery.sampler import Sampler
from ..models import ModelAdapter
from ..platform.envelope import SEMCONV_VERSION, content_hash, make_envelope, new_id, utcnow
from ..platform.store import EventStore
from ..verification.holdout import build_holdout
from ..verification.verifier import IndependentVerifier
from ..workers.base import ReadonlyWorker

GOVERNANCE_DOCS = (
    "只读任务：不写任何文件、不调用写工具",
    "证据规格必须可检查，禁止以模型自述作为完成判据",
    "独立验证者不得看到作者 plan / transcript",
)


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    task_id: str
    state: TaskState
    verdict: str | None
    artifact_hash: str | None
    attempts: int
    rule_ref: str | None = None


class DuplicateIntent(RuntimeError):
    pass


class ChiefOfStaff:
    """只读调度者。P0 单 worker + 单 verifier，不 fan-out（P1 才扩）。"""

    def __init__(
        self,
        store: EventStore,
        source_plane: SourcePlane,
        worker_model: ModelAdapter,
        verifier_model: ModelAdapter,
        *,
        max_rework: int = 2,
    ):
        if worker_model.family == verifier_model.family:
            raise RuntimeError("worker 与 verifier 必须分属不同模型家族")
        self.store = store
        self.source_plane = source_plane
        self.worker_model = worker_model
        self.verifier = IndependentVerifier(store, verifier_model)
        self.max_rework = max_rework

    # ---------- 内部工具 ----------
    def _emit(self, run_id: str, task_id: str, event_type: str, payload: dict, **env_fields: object) -> int:
        return self.store.emit(
            make_envelope(
                run_id=run_id,
                task_id=task_id,
                actor=str(env_fields.pop("actor", "chief-of-staff")),
                role=str(env_fields.pop("role", "chief")),
                event_type=event_type,
                **env_fields,
            ),
            payload,
        )

    def _transition(
        self, run_id: str, task_id: str, src: TaskState, dst: TaskState, why: str
    ) -> None:
        assert_transition(src, dst)
        self.store.set_task_status(task_id, dst.value)
        self._emit(run_id, task_id, "state.transition", {"from": src.value, "to": dst.value, "why": why})

    @staticmethod
    def _evidence_spec(marker: str) -> EvidenceSpec:
        """kickoff 必须写明可验证证据规格；为空不得派发（policy.py 会抛错）。"""
        return EvidenceSpec((
            Predicate("evidence_marker_present", "diff_contains", marker),
            Predicate("independent_gate_green", "test_output_contains", "INDEPENDENT_PASS"),
            Predicate("no_author_plan_leak", "diff_absent", "AUTHOR_PLAN"),
            Predicate("readonly_governance", "governance_contains", "只读"),
        ))

    # ---------- 主链路 ----------
    def handle(
        self,
        intent: TaskIntent,
        *,
        sampling_outcome: str = "ACCEPT",
        sampling_root_cause: str = "",
    ) -> RunResult:
        run_id, task_id = new_id("run"), new_id("task")
        inbox_id = new_id("inbox")
        if not self.store.put_inbox_event(
            inbox_id, intent.source_system, True,
            {"intent": intent.intent, "sources": list(intent.source_bodies)},
        ):
            raise DuplicateIntent(inbox_id)

        self.store.put_task({
            "task_id": task_id,
            "parent_id": None,
            "intent": intent.intent,
            "source_refs": [],
            "evidence_spec": {},
            "risk_level": intent.risk_level,
            "budget": intent.budget,
            "status": TaskState.INBOX_RECEIVED.value,
            "created_at": utcnow(),
        })
        self._emit(run_id, task_id, "intent.received", {
            "intent": intent.intent,
            "inbox_event_id": inbox_id,
            "risk_level": intent.risk_level,
            "budget": intent.budget,
        })

        # L0 → L1：一手原文入库，worker 拿原文 + hash，不是摘要。
        docs = [
            self.source_plane.ingest(
                body,
                source_system=intent.source_system,
                source_uri=f"inbox://{inbox_id}/{i}",
            )
            for i, body in enumerate(intent.source_bodies)
        ]
        source_hashes = tuple(d.source_hash for d in docs)
        self._transition(run_id, task_id, TaskState.INBOX_RECEIVED, TaskState.NORMALIZED, "需求已标准化")
        self._emit(run_id, task_id, "context.sourced", {
            "source_hashes": list(source_hashes),
            "source_system": intent.source_system,
            "kind": "一手原文（append-only，非摘要）",
        })

        spec = self._evidence_spec(intent.evidence_marker)
        kickoff_base = {
            "task_id": task_id,
            "intent": intent.intent,
            "source_hashes": source_hashes,
            "writable_paths": (),          # P0 只读
            "forbidden_actions": ("write_file", "network_egress", "shell_exec"),
            "evidence_spec": spec,
            "risk_level": intent.risk_level,
            "budget": intent.budget,
        }
        self._transition(run_id, task_id, TaskState.NORMALIZED, TaskState.SCOPED, "已写出可验证证据规格")
        self._emit(run_id, task_id, "task.scoped", {
            "evidence_spec": spec.to_dict(),
            "writable_paths": [],
            "forbidden_actions": list(kickoff_base["forbidden_actions"]),
            "unit": "一个可验证单元（P0 不 fan-out）",
        })
        self.store.put_task({
            "task_id": task_id, "parent_id": None, "intent": intent.intent,
            "source_refs": list(source_hashes), "evidence_spec": spec.to_dict(),
            "risk_level": intent.risk_level, "budget": intent.budget,
            "status": TaskState.SCOPED.value, "created_at": utcnow(),
        })

        verdict_result: str | None = None
        artifact_hash: str | None = None
        attempts = 0
        state = TaskState.SCOPED
        sampler = Sampler(self.store, spec, intent.evidence_marker)

        for attempt in range(1, self.max_rework + 2):
            attempts = attempt
            kickoff = Kickoff(**kickoff_base, attempt=attempt)
            worker_run_id = new_id("run")  # 每次返工都是全新 worker、全新上下文
            self._transition(run_id, task_id, state, TaskState.DISPATCHED, f"派发第 {attempt} 次")
            self._emit(run_id, task_id, "worker.dispatched", {
                "attempt": attempt, "worker_run_id": worker_run_id,
                "kickoff": kickoff.to_dict(),
            })
            self._transition(run_id, task_id, TaskState.DISPATCHED, TaskState.IN_PROGRESS, "worker 开始执行")

            worker = ReadonlyWorker(self.worker_model, evidence_marker=intent.evidence_marker)
            output = worker.run(kickoff, docs)
            self.store.put_agent_run({
                "run_id": worker_run_id, "task_id": task_id, "actor": "readonly-worker",
                "role": "author", "model_family": output.model_response.model_family,
                "model_version": output.model_response.model_version,
                "prompt_hash": output.model_response.prompt_hash,
                "semconv_version": SEMCONV_VERSION,
                "tool_policy": {"readonly": True, "writable_paths": []},
                "started_at": utcnow(),
            })
            self._emit(
                run_id, task_id, "worker.model_response",
                {
                    "attempt": attempt,
                    "model_family": output.model_response.model_family,
                    "model_version": output.model_response.model_version,
                    "prompt_hash": output.model_response.prompt_hash,
                    "tool_calls": list(output.tool_calls),
                    "self_check_output": output.self_check_output,
                },
                actor="readonly-worker", role="author", parent_id=worker_run_id,
                model_family=output.model_response.model_family,
                model_version=output.model_response.model_version,
                prompt_hash=output.model_response.prompt_hash,
            )
            self._transition(run_id, task_id, TaskState.IN_PROGRESS, TaskState.SELF_CHECKED, "worker 自检完毕")
            self._emit(run_id, task_id, "worker.self_checked", {
                "self_check_output": output.self_check_output,
                "note": "自证不构成 verdict，verifier 将丢弃并独立重跑",
            })

            artifact_hash = content_hash(output.artifact_body)
            self.store.put_artifact(artifact_hash, worker_run_id, f"artifact://{run_id}/{attempt}", "inline:events")
            self._emit(run_id, task_id, "artifact.produced", {
                "artifact_hash": artifact_hash, "attempt": attempt,
                "size_bytes": len(output.artifact_body.encode("utf-8")),
            }, artifact_hash=artifact_hash)

            self._transition(run_id, task_id, TaskState.SELF_CHECKED, TaskState.VERIFYING, "交给独立验证者")
            bundle = build_holdout(
                issue_body=intent.intent,
                artifact_text=output.artifact_body,
                self_reported_test_output=output.self_check_output,
                governance_docs=GOVERNANCE_DOCS,
            )
            verdict = self.verifier.verify(
                bundle,
                run_id=run_id,
                task_id=task_id,
                author_run_id=worker_run_id,
                author_family=output.model_response.model_family,
                head_sha=artifact_hash,
                evidence_spec=spec,
                source_hashes=source_hashes,
            )
            verdict_result = verdict.result
            new_state = TaskState.VERDICT_PASS if verdict.result == "PASS" else TaskState.VERDICT_FAIL
            self._transition(run_id, task_id, TaskState.VERIFYING, new_state, verdict.result)
            if verdict.result == "PASS":
                break

            state = TaskState.VERDICT_FAIL
            if attempt <= self.max_rework:
                self._emit(run_id, task_id, "rework.requested", {
                    "attempt": attempt, "next": "新 worker、全新上下文", "max_rework": self.max_rework,
                })
                continue
            self._emit(run_id, task_id, "human.escalated", {
                "reason": f"连续 {attempt} 次验证失败，返工上限 {self.max_rework}",
            })
            self._transition(run_id, task_id, TaskState.VERDICT_FAIL, TaskState.NEEDS_HUMAN, "返工耗尽")
            return RunResult(run_id, task_id, TaskState.NEEDS_HUMAN, verdict_result, artifact_hash, attempts)

        # L5 交付 → 抽样 → 接受或回流成环境规则
        self._transition(run_id, task_id, TaskState.VERDICT_PASS, TaskState.DELIVERED, "裁决通过")
        self._emit(run_id, task_id, "artifact.delivered", {
            "artifact_hash": artifact_hash,
            "active_verdict": bool(self.store.active_verdict(artifact_hash or "")),
        })
        self._transition(run_id, task_id, TaskState.DELIVERED, TaskState.SAMPLED, "进入抽样验收")
        review = sampler.review(
            run_id=run_id, task_id=task_id, outcome=sampling_outcome,
            root_cause=sampling_root_cause, head_sha=artifact_hash or "",
        )
        final = TaskState.ACCEPTED if sampling_outcome == "ACCEPT" else TaskState.RULE_UPDATED
        self._transition(run_id, task_id, TaskState.SAMPLED, final, sampling_outcome)
        if review["rule_ref"]:
            self._emit(run_id, task_id, "guardrail.rule_added", {
                "rule_ref": review["rule_ref"],
                "root_cause": sampling_root_cause,
                "note": "回流到 L6 环境规则，不改提示词",
            })
        return RunResult(
            run_id, task_id, final, verdict_result, artifact_hash, attempts, review["rule_ref"]
        )
