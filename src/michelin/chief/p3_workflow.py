"""P3 编排：仓库访谈 → 计划接地 → 通用 skill 生成/证明 → 双后端规则复核 → 变异门禁 → 裁决。

P3 验证的不是「某个 app 改对了没有」，而是「这套验证装置本身能不能交付」：
生成的 skill 是否真的可执行、规则是否真的会开火、两个 AST 后端是否一致、
计划里的命令是否真的存在于被验证的仓库。四道门任一不过，在产出 artifact
之前就 BLOCKED_NEEDS_HUMAN，绝不带着不会失败的门禁往下走。

与 P0/P1/P2 的关系：并列，不替换。事件形状完全一致，`replay` 只读 events
即可还原这次决策；P3 没有 LLM 作者，裁决者身份只用于审计记录，判定本身是
确定性的（真实命令 + 结构化 AST），这比 LLM 自评更可靠。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..context.sources import SourcePlane
from ..delivery.sampler import Sampler
from ..models import ModelAdapter
from ..platform.envelope import SEMCONV_VERSION, content_hash, make_envelope, new_id, utcnow
from ..platform.store import EventStore
from ..verification.generic_mutants import RuleMutationReport, run_rule_mutations
from ..verification.generic_skill import GeneratedGenericSkill, generate_generic_skill
from ..verification.interview import RepoProfile, ground_plan, interview
from ..verification.plan import VerificationPlan
from ..verification.rules import (
    RuleScan, compare_backends, find_ast_grep, rules_digest, scan,
)
from ..verification.runner import prove_skill
from ..verification.skill_spec import SkillProof
from .policy import (
    EvidenceSpec, Predicate, TaskIntent, TaskState, assert_transition,
)

GOVERNANCE_DOCS = (
    "六段式 verify skill：Launch / Doctor / Drive / Evidence / Cleanup / Helpers",
    "生成物不含目标应用知识，所有命令来自 plan.json 与仓库访谈",
    "结构化规则双后端（python-ast / ast-grep）判定必须一致",
    "每条规则必须经变异证明会开火；出错改环境规则，不改提示词",
)


@dataclass(frozen=True, slots=True)
class P3Scenario:
    cleanup_eats_evidence: bool = False
    weaken_rules: bool = False
    unbacked_plan: bool = False
    force_backend_disagreement: bool = False
    sampling_outcome: str = "ACCEPT"
    sampling_root_cause: str = ""


@dataclass(frozen=True, slots=True)
class P3Result:
    run_id: str
    task_id: str
    state: TaskState
    verdict: str | None
    skill_hash: str
    skill_status: str
    repo_fingerprint: str
    plan_fingerprint: str
    backend: str
    cross_checked: bool
    rules_all_fired: bool
    head_sha: str | None
    blocked_reason: str
    workdir: Path
    rule_ref: str | None = None
    skill_root: Path | None = None


class P3Pipeline:
    def __init__(
        self,
        store: EventStore,
        source_plane: SourcePlane,
        verifier_model: ModelAdapter,
        repo_root: Path,
        plan: VerificationPlan,
        workdir: Path,
        *,
        backend: str = "python-ast",
        ast_grep: str | None = None,
        probe_drive: bool = True,
    ):
        self.store = store
        self.source_plane = source_plane
        self.verifier_model = verifier_model
        self.repo_root = Path(repo_root).resolve()
        self.plan = plan
        self.workdir = Path(workdir)
        self.backend = backend
        self.ast_grep = ast_grep
        self.probe_drive = probe_drive

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
    def _evidence_spec(plan: VerificationPlan) -> EvidenceSpec:
        return EvidenceSpec((
            Predicate("skill_six_phases_proven", "test_output_contains", "PROVEN"),
            Predicate("structured_rules_clean", "governance_contains", "结构化规则"),
            Predicate("rule_mutations_all_fired", "governance_contains", "变异"),
            Predicate("plan_grounded_in_repo", "diff_contains", "plan.json"),
        ))

    def _blocked(self, run_id, task_id, reason: str, *, stage: str,
                 skill_hash: str, proof_status: str, profile: RepoProfile,
                 plan: VerificationPlan, root: Path, cross_checked: bool,
                 fired: bool, head: str | None = None) -> P3Result:
        self._emit(run_id, task_id, "human.escalated", {
            "reason": reason, "stage": stage,
            "note": "在产出 artifact / 裁决之前阻断，避免交付不会失败的门禁",
        })
        self._transition(run_id, task_id, TaskState.SCOPED,
                         TaskState.BLOCKED_NEEDS_HUMAN, reason)
        return P3Result(
            run_id, task_id, TaskState.BLOCKED_NEEDS_HUMAN, None, skill_hash, proof_status,
            profile.fingerprint, plan.fingerprint(), self.backend, cross_checked, fired,
            head, reason, root,
        )

    def handle(self, intent: TaskIntent, scenario: P3Scenario) -> P3Result:
        run_id, task_id = new_id("run"), new_id("task")
        root = (self.workdir / run_id).resolve()
        root.mkdir(parents=True, exist_ok=True)

        # 场景注入：改的是「计划/生成物」，不是真实仓库。
        plan = self.plan
        if scenario.unbacked_plan:
            m = replace(plan.mutations[0], inject_file="tests/__never_exists__.py")
            plan = replace(plan, mutations=(m, *plan.mutations[1:]))
        if scenario.weaken_rules:
            # 用合法 Python 标识符弱化规则：规则仍可解析，但永远不会命中 ——
            # 这才是「门禁形同虚设」的准确演示；非法 selector 只会让规则加载失败。
            plan = replace(plan, rules=tuple(
                replace(r, selector="_never_matches_gate") for r in plan.rules
            ))
        plan.validate()

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
            "phase": "P3", "repo_root": str(self.repo_root),
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

        # ---- L1 访谈：只读扫描真实仓库 ----
        profile = interview(self.repo_root)
        self._emit(run_id, task_id, "repo.interviewed", {
            "root": profile.root, "languages": profile.languages,
            "entrypoints": list(profile.entrypoints),
            "test_framework": profile.test_framework,
            "test_targets": list(profile.test_targets)[:20],
            "governance_files": list(profile.governance_files),
            "python_modules": len(profile.modules),
            "tracked_files": len(profile.tracked_files),
            "fingerprint": profile.fingerprint,
            "note": "确定性扫描，不调模型；指纹只吃文件内容，不吃时间戳与 git SHA",
        })

        spec = self._evidence_spec(plan)
        self._transition(run_id, task_id, TaskState.NORMALIZED, TaskState.SCOPED,
                         "已写出可验证单元契约")
        self._emit(run_id, task_id, "task.scoped", {
            "evidence_spec": spec.to_dict(),
            "spec_kind": "configurable-repo-verification",
            "enforced_by": "generic six-phase skill + structured rules + mutation gate",
            "plan_fingerprint": plan.fingerprint(),
            "rules": [r.id for r in plan.rules],
            "journeys": [j.name for j in plan.journeys],
            "unit": "一个可验证单元：为任意仓库生成并证明一套可执行门禁",
        })
        self.store.put_task({
            "task_id": task_id, "parent_id": None, "intent": intent.intent,
            "source_refs": list(source_hashes), "evidence_spec": spec.to_dict(),
            "risk_level": intent.risk_level, "budget": intent.budget,
            "status": TaskState.SCOPED.value, "created_at": utcnow(),
        })

        # ---- 门 1：计划必须 grounded 在访谈到的事实上 ----
        problems = ground_plan(profile, plan)
        self._emit(run_id, task_id, "plan.grounded", {
            "plan_fingerprint": plan.fingerprint(),
            "problems": list(problems),
            "grounded": not problems,
            "note": "计划里引用的测试目标与注入文件必须在仓库里真实存在",
        })
        if problems:
            return self._blocked(
                run_id, task_id, "计划未接地：" + "; ".join(problems[:3]),
                stage="pre-generation", skill_hash="", proof_status="DRAFT",
                profile=profile, plan=plan, root=root, cross_checked=False, fired=False,
            )

        # ---- 生成通用 skill（此刻还是 DRAFT） ----
        skill: GeneratedGenericSkill = generate_generic_skill(
            root / "skills", plan, profile,
            cleanup_eats_evidence=scenario.cleanup_eats_evidence,
        )
        self._emit(run_id, task_id, "skill.generated", {
            "skill_root": str(skill.root.relative_to(root)),
            "skill_hash": skill.skill_hash,
            "files": list(skill.files),
            "status": "DRAFT",
            "six_phases": ["launch", "doctor", "drive", "evidence", "cleanup", "helpers"],
            "app_specific_code": False,
            "note": "A generated skill that was never executed is a draft, not a deliverable.",
        })

        # ---- §4：端到端证明（在真实仓库副本上，不是 golden stand-in） ----
        proof: SkillProof = prove_skill(
            skill, self.repo_root, root / "prove-run",
            stand_in_steps=(
                "P3 直接在被验证的真实仓库上 prove，不需要 golden stand-in",
                "生成物不含应用知识，helper 只读 plan.json",
            ),
        )
        self._emit(run_id, task_id, "skill.proven", {
            **proof.to_dict(),
            "note": "cleanup 后证据必须存活；失败迭代也跑了 cleanup",
        })

        # ---- 门 2 / 3：双后端对拍 + 干净基线扫描 ----
        # 规则加载/解析失败（例如 ast-grep 拒绝非法 pattern）必须变成证据链里的一条
        # 阻断记录，绝不能让异常冒泡成「演示崩了」而看不到原因。
        exe = find_ast_grep(self.ast_grep)
        try:
            primary: RuleScan = scan(plan.rules, self.repo_root, self.backend,
                                     executable=self.ast_grep)
            cross_checked = False
            agreed, diff = True, ()
            secondary_payload = None
            if exe:
                other = "python-ast" if self.backend == "ast-grep" else "ast-grep"
                secondary = scan(plan.rules, self.repo_root, other, executable=exe)
                agreed, diff = compare_backends(primary, secondary)
                cross_checked = True
                secondary_payload = secondary.to_dict()
        except (RuntimeError, ValueError) as exc:
            self._emit(run_id, task_id, "rules.scan_error", {
                "backend": self.backend, "error": str(exc),
                "note": "结构化规则无法加载/执行，视为门禁不可用，直接阻断",
            })
            return self._blocked(
                run_id, task_id, f"结构化规则不可用：{str(exc)[:200]}",
                stage="pre-verdict", skill_hash=skill.skill_hash,
                proof_status=proof.status, profile=profile, plan=plan, root=root,
                cross_checked=False, fired=False,
            )
        if scenario.force_backend_disagreement:
            agreed, diff = False, ("SIMULATED_DISAGREEMENT::no-eval-in-spine",)
            cross_checked = True
        self._emit(run_id, task_id, "rules.scanned", {
            "backend": self.backend,
            "cross_checked": cross_checked,
            "agreed": agreed,
            "differences": list(diff),
            "primary": primary.to_dict(),
            "secondary": secondary_payload,
            "rules_digest": rules_digest(plan.rules),
            "simulated_disagreement": scenario.force_backend_disagreement,
        })

        # ---- 门 4：规则变异证明（规则命中 + 生成 skill 的 Drive 拒绝） ----
        mutations: RuleMutationReport = run_rule_mutations(
            skill, plan, self.repo_root, root / "mutants",
            backend=self.backend, executable=self.ast_grep,
            probe_drive=self.probe_drive,
        )
        self._emit(run_id, task_id, "rules.mutants_tested", {
            **mutations.to_dict(),
            "note": "每条规则都要注入真实违规代码；规则命中且 Drive 拒绝才算开火",
        })

        blocked = ""
        if not proof.usable:
            blocked = f"skill 未证明可用（status={proof.status}）: {proof.failure_reason}"
        elif not primary.clean:
            blocked = f"被验证仓库已有 {len(primary.findings)} 条规则命中，基线不干净"
        elif cross_checked and not agreed:
            blocked = f"双后端判定不一致：{', '.join(diff[:3])}"
        elif not mutations.all_fired:
            fired = [c.rule_id for c in mutations.cases if c.fired]
            blocked = (f"规则变异未全数开火（{len(fired)}/{len(mutations.cases)}），"
                       "门禁形同虚设，禁止用于裁决")
        if blocked:
            return self._blocked(
                run_id, task_id, blocked, stage="pre-verdict",
                skill_hash=skill.skill_hash, proof_status=proof.status,
                profile=profile, plan=plan, root=root, cross_checked=cross_checked,
                fired=mutations.all_fired,
            )

        # ---- 产出与裁决（确定性，不靠 LLM 自评） ----
        self._transition(run_id, task_id, TaskState.SCOPED, TaskState.DISPATCHED,
                         "派发可配置验证单元")
        self._emit(run_id, task_id, "worker.model_response", {
            "actor_kind": "deterministic-generator",
            "model_family": self.verifier_model.family,
            "model_version": self.verifier_model.version,
            "prompt_hash": content_hash({"plan": plan.fingerprint(),
                                         "repo": profile.fingerprint}),
            "tool_calls": ["interview", "generate_generic_skill", "prove_skill", "scan_rules"],
            "self_check_output": f"skill={proof.status} rules={len(plan.rules)}",
            "note": "P3 的生成者是确定性的，不调用 LLM；此事件仅为六锚点与审计身份留痕",
        }, actor="generic-skill-generator", role="author",
            model_family=self.verifier_model.family,
            model_version=self.verifier_model.version,
            prompt_hash=content_hash({"plan": plan.fingerprint()}))
        self._transition(run_id, task_id, TaskState.DISPATCHED, TaskState.IN_PROGRESS,
                         "生成物已落地")
        self._transition(run_id, task_id, TaskState.IN_PROGRESS, TaskState.SELF_CHECKED,
                         "生成物自检完毕（自证不构成裁决）")

        head_sha = content_hash({
            "repo": profile.fingerprint,
            "plan": plan.fingerprint(),
            "skill": skill.skill_hash,
            "backend": self.backend,
            "mutations": mutations.report_hash,
        })
        self.store.put_artifact(head_sha, run_id, f"skill://{run_id}", "inline:events")
        self._emit(run_id, task_id, "artifact.produced", {
            "artifact_hash": head_sha,
            "kind": "generated verification skill + rule set",
            "files": list(skill.files),
            "skill_hash": skill.skill_hash,
            "rules_digest": rules_digest(plan.rules),
            "repo_fingerprint": profile.fingerprint,
        }, artifact_hash=head_sha)
        self._transition(run_id, task_id, TaskState.SELF_CHECKED, TaskState.VERIFYING,
                         "交给独立裁决")

        verifier_run_id = new_id("run")
        self.store.put_agent_run({
            "run_id": verifier_run_id, "task_id": task_id, "actor": "rule-verifier",
            "role": "verifier", "model_family": self.verifier_model.family,
            "model_version": self.verifier_model.version,
            "prompt_hash": content_hash({"head_sha": head_sha}),
            "semconv_version": SEMCONV_VERSION,
            "tool_policy": {"readonly": True, "independent_replay": True},
            "started_at": utcnow(),
        })
        checks = [
            {"name": "six_phase_proven", "passed": proof.usable,
             "detail": f"status={proof.status} evidence_survived={proof.evidence_survived}"},
            {"name": "plan_grounded", "passed": not problems, "detail": repr(problems)},
            {"name": "structured_rules_clean", "passed": primary.clean,
             "detail": f"findings={len(primary.findings)} backend={self.backend}"},
            {"name": "backends_agree", "passed": agreed,
             "detail": f"cross_checked={cross_checked} diff={list(diff)}"},
            {"name": "rule_mutations_fired", "passed": mutations.all_fired,
             "detail": f"{sum(c.fired for c in mutations.cases)}/{len(mutations.cases)}"},
        ]
        result = "PASS" if all(c["passed"] for c in checks) else "FAIL"
        self.store.put_verdict({
            "verdict_id": new_id("vrd"), "run_id": verifier_run_id,
            "verifier_run_id": verifier_run_id, "head_sha": head_sha,
            "checks": checks,
            "evidence_refs": [proof.evidence_hash or "", mutations.report_hash,
                              primary.scan_hash],
            "result": result, "created_at": utcnow(),
        })
        self._emit(run_id, task_id, "verdict.issued", {
            "result": result, "head_sha": head_sha, "checks": checks,
            "verifier_family": self.verifier_model.family,
            "author_family": "deterministic-generator",
            "self_reported_test_output": "（已丢弃：不以生成者自陈为裁决依据）",
            "independent_test_output": f"primary.scan_hash={primary.scan_hash[:16]} "
                                       f"mutations={mutations.report_hash[:16]}",
        }, actor="rule-verifier", role="verifier", parent_id=verifier_run_id,
            model_family=self.verifier_model.family,
            model_version=self.verifier_model.version)
        self._transition(run_id, task_id, TaskState.VERIFYING, TaskState.VERDICT_PASS,
                         result)
        self._transition(run_id, task_id, TaskState.VERDICT_PASS, TaskState.DELIVERED,
                         "验证单元可交付")
        self._emit(run_id, task_id, "artifact.delivered", {
            "artifact_hash": head_sha,
            "active_verdict": bool(self.store.active_verdict(head_sha)),
            "skill_root": str(skill.root),
        })
        self._transition(run_id, task_id, TaskState.DELIVERED, TaskState.SAMPLED,
                         "进入抽样验收")

        sampler = Sampler(self.store, spec, intent.evidence_marker)
        review = sampler.review(
            run_id=run_id, task_id=task_id, outcome=scenario.sampling_outcome,
            root_cause=scenario.sampling_root_cause, head_sha=head_sha,
        )
        final = (TaskState.ACCEPTED if scenario.sampling_outcome == "ACCEPT"
                 else TaskState.RULE_UPDATED)
        self._transition(run_id, task_id, TaskState.SAMPLED, final, scenario.sampling_outcome)
        if review["rule_ref"]:
            self._emit(run_id, task_id, "guardrail.rule_added", {
                "rule_ref": review["rule_ref"],
                "root_cause": scenario.sampling_root_cause,
                "note": "回流到 L6 环境规则（rules/*.yml），不改提示词",
            })
        return P3Result(
            run_id, task_id, final, result, skill.skill_hash, proof.status,
            profile.fingerprint, plan.fingerprint(), self.backend, cross_checked,
            mutations.all_fired, head_sha, "", root, review["rule_ref"], skill.root,
        )
