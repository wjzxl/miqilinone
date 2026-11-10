"""P2 垂直切片：六段式 skill 生成 → 真跑证明 → 代码级变异门禁 → 真实 diff → 行为裁决。

与 P0 的 ChiefOfStaff 并列，而不是替换它：P0/P1 的零依赖链路一行不改，
新增的行为验证作为另一条可跑通的路径，事件形状完全一致，replay 仍只读 events。

两个「先于派发」的阻断条件是 P2 的关键：
1. skill 没被端到端执行过（DRAFT）或 cleanup 吃掉证据 → 不许派发 worker；
2. 变异测试未全数开火（门禁形同虚设、被弱化的 Drive）→ 不许派发 worker。
否则「先让模型干活、再拿一个可能根本不会失败的门禁去验」，等于没有验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..context.sources import SourcePlane
from ..delivery.sampler import Sampler
from ..guardrails.rules import derive_rule  # noqa: F401  (规则回流路径复用)
from ..models import IndependenceWaiver, ModelAdapter
from ..platform.envelope import SEMCONV_VERSION, make_envelope, new_id, utcnow
from ..platform.store import EventStore
from ..workspace.seed import CLI_SOURCE, PRICING_CORRECT, GitWorkspace
from ..verification.behavior import BehaviorVerifier
from ..verification.generator import generate_skill
from ..verification.holdout import build_holdout
from ..verification.mutants import MutationReport, run_mutation_suite
from ..verification.runner import prove_skill
from ..verification.skill_spec import SkillProof
from ..chief.policy import (
    EvidenceSpec, Kickoff, Predicate, TaskIntent, TaskState, assert_transition,
)
from ..workers.git_worker import GitCodeWorker, WorkerOutputError

GOVERNANCE_DOCS = (
    "六段式 verify skill：Launch / Doctor / Drive / Evidence / Cleanup / Helpers",
    "cleanup 绝不删证据；未执行过的 skill 只是 draft，不得用于裁决",
    "Drive 必须走真实用户路径 python3 -m shop.cli，禁止内部 setter 与 test-only 端点",
    "每条门禁必须经代码级变异测试证明会开火；改环境规则，不改提示词",
)


@dataclass(frozen=True, slots=True)
class P2Scenario:
    worker_rates: tuple[str, ...]
    weaken_drive: bool = False
    cleanup_eats_evidence: bool = False
    sampling_outcome: str = "ACCEPT"
    sampling_root_cause: str = ""


@dataclass(frozen=True, slots=True)
class P2Result:
    run_id: str
    task_id: str
    state: TaskState
    verdict: str | None
    attempts: int
    skill_hash: str
    skill_status: str
    mutants_all_fired: bool
    head_sha: str | None
    blocked_reason: str
    workdir: Path
    rule_ref: str | None = None


class P2Pipeline:
    def __init__(
        self,
        store: EventStore,
        source_plane: SourcePlane,
        worker_model: ModelAdapter,
        verifier_model: ModelAdapter,
        workdir: Path,
        *,
        max_rework: int = 2,
        waiver: IndependenceWaiver | None = None,
    ):
        if worker_model.family == verifier_model.family and waiver is None:
            raise RuntimeError(
                "worker 与 verifier 必须分属不同模型家族"
                "（确需同家族请显式声明豁免，豁免会被写进证据链）"
            )
        self.store = store
        self.source_plane = source_plane
        self.worker_model = worker_model
        self.verifier_model = verifier_model
        self.workdir = Path(workdir)
        self.max_rework = max_rework
        self.waiver = waiver

    def _emit(self, run_id: str, task_id: str, event_type: str, payload: dict,
              **fields: object) -> int:
        return self.store.emit(
            make_envelope(
                run_id=run_id, task_id=task_id,
                actor=str(fields.pop("actor", "chief-of-staff")),
                role=str(fields.pop("role", "chief")),
                event_type=event_type, **fields,
            ), payload,
        )

    def _transition(self, run_id: str, task_id: str, src: TaskState, dst: TaskState, why: str):
        assert_transition(src, dst)
        self.store.set_task_status(task_id, dst.value)
        self._emit(run_id, task_id, "state.transition",
                   {"from": src.value, "to": dst.value, "why": why})

    @staticmethod
    def _evidence_spec() -> EvidenceSpec:
        """声明式证据规格；实际裁决由六段式 skill + 变异测试强制，不是字符串谓词。"""
        return EvidenceSpec((
            Predicate("patch_is_unified_diff", "diff_contains", "diff --git"),
            Predicate("independent_six_phase_run", "test_output_contains", "INDEPENDENT_PASS"),
            Predicate("evidence_survived_cleanup", "governance_contains", "cleanup"),
            Predicate("mutation_gate_active", "governance_contains", "变异"),
        ))

    def _run_worker(self, worker: GitCodeWorker, kickoff: Kickoff, docs,
                    run_id: str, task_id: str):
        """跑一次 worker；产出不可机械检查时返回 None（记事件，不崩流水线）。"""
        try:
            return worker.run(kickoff, docs)
        except WorkerOutputError as exc:
            self._emit(run_id, task_id, "worker.output_rejected", {
                "attempt": kickoff.attempt, "reason": str(exc),
                "note": "交付物必须可机械检查；本次 attempt 作废，换新 worker 重来",
            })
            return None

    def handle(self, intent: TaskIntent, scenario: P2Scenario) -> P2Result:
        run_id, task_id = new_id("run"), new_id("task")
        root = (self.workdir / run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        workspace = GitWorkspace(root / "repo")
        baseline_sha = workspace.seed_baseline()
        baseline_fingerprint = workspace.baseline_fingerprint()

        inbox_id = new_id("inbox")
        self.store.put_inbox_event(
            inbox_id, intent.source_system, True,
            {"intent": intent.intent, "sources": list(intent.source_bodies)},
        )
        self.store.put_task({
            "task_id": task_id, "parent_id": None, "intent": intent.intent,
            "source_refs": [], "evidence_spec": {}, "risk_level": intent.risk_level,
            "budget": intent.budget, "status": TaskState.INBOX_RECEIVED.value,
            "created_at": utcnow(),
        })
        self._emit(run_id, task_id, "intent.received", {
            "intent": intent.intent, "inbox_event_id": inbox_id,
            "risk_level": intent.risk_level, "budget": intent.budget,
            "baseline_sha": baseline_sha,
            "baseline_fingerprint": baseline_fingerprint, "phase": "P2",
        })
        if self.waiver is not None:
            # 独立性降级必须第一时间入链：谁在什么时候、用什么理由降级，一目了然。
            self._emit(run_id, task_id, "model.independence_waived", {
                **self.waiver.to_dict(),
                "note": "本次行为裁决没有跨模型家族独立性；结构性 holdout（输入隔离）仍然成立",
            })

        docs = [
            self.source_plane.ingest(
                body, source_system=intent.source_system,
                source_uri=f"inbox://{inbox_id}/{i}",
            )
            for i, body in enumerate(intent.source_bodies)
        ]
        source_hashes = tuple(d.source_hash for d in docs)
        self._transition(run_id, task_id, TaskState.INBOX_RECEIVED, TaskState.NORMALIZED,
                         "需求已标准化")
        self._emit(run_id, task_id, "context.sourced", {
            "source_hashes": list(source_hashes),
            "kind": "一手原文（append-only，非摘要）",
        })

        spec = self._evidence_spec()
        self._transition(run_id, task_id, TaskState.NORMALIZED, TaskState.SCOPED,
                         "已写出行为证据规格")
        self._emit(run_id, task_id, "task.scoped", {
            "evidence_spec": spec.to_dict(),
            "spec_kind": "behavior",
            "enforced_by": "six_phase_skill + code_mutation_suite",
            "writable_paths": ["shop/pricing.py"],
            "unit": "一个可验证单元：修对税率并驱动真实 CLI",
        })
        self.store.put_task({
            "task_id": task_id, "parent_id": None, "intent": intent.intent,
            "source_refs": list(source_hashes), "evidence_spec": spec.to_dict(),
            "risk_level": intent.risk_level, "budget": intent.budget,
            "status": TaskState.SCOPED.value, "created_at": utcnow(),
        })

        # ---- 生成 skill（此刻还是 DRAFT） ----
        skill = generate_skill(
            root / "skills", weaken_drive=scenario.weaken_drive,
            cleanup_eats_evidence=scenario.cleanup_eats_evidence,
        )
        self._emit(run_id, task_id, "skill.generated", {
            "skill_root": str(skill.root.relative_to(root)),
            "skill_hash": skill.skill_hash,
            "files": list(skill.files),
            "status": "DRAFT",
            "six_phases": ["launch", "doctor", "drive", "evidence", "cleanup", "helpers"],
            "weaken_drive": skill.weakened_drive,
            "note": "A generated skill that was never executed is a draft, not a deliverable.",
        })

        # ---- §4 端到端证明（在 golden stand-in 上；stand-in 步骤必须如实列出） ----
        golden = workspace.worktree("golden")
        golden.write_file("shop/pricing.py", PRICING_CORRECT)
        golden.write_file("shop/cli.py", CLI_SOURCE)
        stand_in = (
            "golden worktree 写入 PRICING_CORRECT 作为已知正确基线（目标应用本身仍需作者修复）",
            "未使用远程/生产环境，全部在本机临时 git worktree 内",
        )
        proof: SkillProof = prove_skill(skill, golden.path, root / "prove-run",
                                        stand_in_steps=stand_in)
        self._emit(run_id, task_id, "skill.proven", {
            **proof.to_dict(),
            "note": "cleanup 后证据必须存活；失败迭代也跑了 cleanup",
        })

        # ---- 代码级变异测试 ----
        mutations: MutationReport = run_mutation_suite(skill, workspace, root / "mutants")
        self._emit(run_id, task_id, "skill.mutants_tested", {
            **mutations.to_dict(),
            "note": "每个坏版本都必须让 Drive 断言开火，否则门禁形同虚设",
        })

        blocked = ""
        if not proof.usable:
            blocked = f"skill 未证明可用（status={proof.status}）: {proof.failure_reason}"
        elif not mutations.all_fired:
            fired = [c.name for c in mutations.cases if c.fired]
            blocked = (f"变异测试未全数开火（{len(fired)}/{len(mutations.cases)}），"
                       "门禁形同虚设，禁止用于裁决")
        if blocked:
            self._emit(run_id, task_id, "human.escalated", {
                "reason": blocked, "stage": "pre-dispatch",
                "note": "在派发 worker 之前阻断，避免用不会失败的门禁去验收",
            })
            self._transition(run_id, task_id, TaskState.SCOPED,
                             TaskState.BLOCKED_NEEDS_HUMAN, blocked)
            return P2Result(
                run_id, task_id, TaskState.BLOCKED_NEEDS_HUMAN, None, 0,
                skill.skill_hash, proof.status, mutations.all_fired, None, blocked, root,
            )

        # ---- 派发 → 真实 diff → 独立行为裁决 ----
        worker = GitCodeWorker(self.worker_model, workspace)
        verifier = BehaviorVerifier(self.store, self.verifier_model, skill, workspace, root)
        kickoff_base = {
            "task_id": task_id, "intent": intent.intent,
            "source_hashes": source_hashes,
            "writable_paths": ("shop/pricing.py",),
            "forbidden_actions": ("network_egress", "git_push", "shell_exec"),
            "evidence_spec": spec, "risk_level": intent.risk_level, "budget": intent.budget,
        }
        state = TaskState.SCOPED
        verdict_result: str | None = None
        head_sha: str | None = None
        attempts = 0

        for attempt in range(1, self.max_rework + 2):
            attempts = attempt
            # Scene 用第 attempt 个速率脚本控制 worker 这次「修成什么」。
            rate = scenario.worker_rates[min(attempt - 1, len(scenario.worker_rates) - 1)]
            self.worker_model = _with_rate(self.worker_model, rate)
            worker = GitCodeWorker(self.worker_model, workspace)

            kickoff = Kickoff(**kickoff_base, attempt=attempt)
            worker_run_id = new_id("run")
            self._transition(run_id, task_id, state, TaskState.DISPATCHED,
                             f"派发第 {attempt} 次（全新 worktree）")
            self._emit(run_id, task_id, "worker.dispatched", {
                "attempt": attempt, "worker_run_id": worker_run_id,
                "kickoff": kickoff.to_dict(),
            })
            self._transition(run_id, task_id, TaskState.DISPATCHED, TaskState.IN_PROGRESS,
                             "worker 开始修改真实文件")

            output = self._run_worker(worker, kickoff, docs, run_id, task_id)
            if output is None:
                # 产出不可机械检查 → 本次 attempt 作废，换全新 worker 重来。
                if attempt <= self.max_rework:
                    self._emit(run_id, task_id, "rework.requested", {
                        "attempt": attempt, "next": "新 worker + 新 worktree + 全新上下文",
                        "max_rework": self.max_rework,
                        "reason": "worker 产出不可机械检查",
                    })
                    continue
                self._emit(run_id, task_id, "human.escalated", {
                    "reason": f"连续 {attempt} 次 worker 产出不可机械检查，返工上限 {self.max_rework}",
                })
                self._transition(run_id, task_id, TaskState.IN_PROGRESS,
                                 TaskState.NEEDS_HUMAN, "返工耗尽")
                return P2Result(
                    run_id, task_id, TaskState.NEEDS_HUMAN, None, attempts,
                    skill.skill_hash, proof.status, mutations.all_fired, None, "", root,
                )
            self.store.put_agent_run({
                "run_id": worker_run_id, "task_id": task_id, "actor": "git-code-worker",
                "role": "author", "model_family": output.model_response.model_family,
                "model_version": output.model_response.model_version,
                "prompt_hash": output.model_response.prompt_hash,
                "semconv_version": SEMCONV_VERSION,
                "tool_policy": {"writable_paths": ["shop/pricing.py"], "git_diff": True},
                "started_at": utcnow(),
            })
            self._emit(run_id, task_id, "worker.model_response", {
                "attempt": attempt,
                "model_family": output.model_response.model_family,
                "model_version": output.model_response.model_version,
                "prompt_hash": output.model_response.prompt_hash,
                "usage": output.model_response.usage.to_dict(),
                "provider_request_id": output.model_response.provider_request_id,
                "finish_reason": output.model_response.finish_reason,
                "tool_calls": list(output.tool_calls),
                "self_check_output": output.self_check_output,
            }, actor="git-code-worker", role="author", parent_id=worker_run_id,
                model_family=output.model_response.model_family,
                model_version=output.model_response.model_version,
                prompt_hash=output.model_response.prompt_hash)
            self._transition(run_id, task_id, TaskState.IN_PROGRESS, TaskState.SELF_CHECKED,
                             "worker 自检完毕（自证不构成裁决）")
            self._emit(run_id, task_id, "worker.self_checked", {
                "self_check_output": output.self_check_output,
                "note": "自证不构成 verdict，verifier 将丢弃并在干净 worktree 重放 diff",
            })

            artifact_hash = output.diff_hash
            self.store.put_artifact(artifact_hash, worker_run_id,
                                    f"git-diff://{run_id}/{attempt}", "inline:events")
            self._emit(run_id, task_id, "artifact.produced", {
                "artifact_hash": artifact_hash, "attempt": attempt,
                "kind": "unified diff", "size_bytes": len(output.patch.encode("utf-8")),
                "patch": output.patch, "baseline_sha": baseline_sha,
            }, artifact_hash=artifact_hash)
            self._transition(run_id, task_id, TaskState.SELF_CHECKED, TaskState.VERIFYING,
                             "交给独立行为验证者")

            bundle = build_holdout(
                issue_body=intent.intent,
                artifact_text=output.patch,
                self_reported_test_output=output.self_check_output,
                governance_docs=GOVERNANCE_DOCS,
            )
            verdict = verifier.verify(
                bundle, run_id=run_id, task_id=task_id,
                author_run_id=worker_run_id,
                author_family=output.model_response.model_family,
                skill_proof=proof, mutation_report=mutations,
                source_hashes=source_hashes, attempt=attempt,
                independence_waiver=self.waiver,
            )
            verdict_result = verdict.result
            head_sha = verdict.head_sha
            new_state = (TaskState.VERDICT_PASS if verdict.result == "PASS"
                         else TaskState.VERDICT_FAIL)
            self._transition(run_id, task_id, TaskState.VERIFYING, new_state, verdict.result)
            if verdict.result == "PASS":
                break
            state = TaskState.VERDICT_FAIL
            if attempt <= self.max_rework:
                self._emit(run_id, task_id, "rework.requested", {
                    "attempt": attempt, "next": "新 worker + 新 worktree + 全新上下文",
                    "max_rework": self.max_rework,
                })
                continue
            self._emit(run_id, task_id, "human.escalated", {
                "reason": f"连续 {attempt} 次行为验证失败，返工上限 {self.max_rework}",
            })
            self._transition(run_id, task_id, TaskState.VERDICT_FAIL, TaskState.NEEDS_HUMAN,
                             "返工耗尽")
            return P2Result(
                run_id, task_id, TaskState.NEEDS_HUMAN, verdict_result, attempts,
                skill.skill_hash, proof.status, mutations.all_fired, head_sha, "", root,
            )

        self._transition(run_id, task_id, TaskState.VERDICT_PASS, TaskState.DELIVERED,
                         "行为裁决通过")
        self._emit(run_id, task_id, "artifact.delivered", {
            "artifact_hash": head_sha,
            "active_verdict": bool(self.store.active_verdict(head_sha or "")),
        })
        self._transition(run_id, task_id, TaskState.DELIVERED, TaskState.SAMPLED,
                         "进入抽样验收")
        sampler = Sampler(self.store, spec, intent.evidence_marker)
        review = sampler.review(
            run_id=run_id, task_id=task_id, outcome=scenario.sampling_outcome,
            root_cause=scenario.sampling_root_cause, head_sha=head_sha or "",
        )
        final = (TaskState.ACCEPTED if scenario.sampling_outcome == "ACCEPT"
                 else TaskState.RULE_UPDATED)
        self._transition(run_id, task_id, TaskState.SAMPLED, final, scenario.sampling_outcome)
        if review["rule_ref"]:
            self._emit(run_id, task_id, "guardrail.rule_added", {
                "rule_ref": review["rule_ref"],
                "root_cause": scenario.sampling_root_cause,
                "note": "回流到 L6 环境规则，不改提示词",
            })
        return P2Result(
            run_id, task_id, final, verdict_result, attempts,
            skill.skill_hash, proof.status, mutations.all_fired, head_sha, "", root,
            review["rule_ref"],
        )


def _with_rate(model: ModelAdapter, rate: str) -> ModelAdapter:
    """给 ScriptedModel 换本轮脚本；**真实模型不做任何注入**。

    这是有意为之：真实模型必须自己从「意图 + 一手原文」推导税率（6% → 0.06）。
    把目标值塞进 prompt 会让「模型真的在改代码」变成「照抄答案」，那这条路径
    就没有验证价值了。`scenario.worker_rates` 只服务于确定性测试脚本。
    """
    from ..models import ScriptedModel
    if isinstance(model, ScriptedModel):
        model.script = [f"将税率改为 TAX_RATE = {rate}，并说明依据的 source_hash。"]
        model.calls = 0
    return model
