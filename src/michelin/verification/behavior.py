"""L4 行为验证者：在干净 worktree 上重放作者的 unified diff，自己驱动真实应用。

与 P0/P1 的 IndependentVerifier 保持同一份 holdout 契约：
唯一位置参数是 HoldoutBundle（issue_body / diff / test_output / governance_docs），
作者的 plan / kickoff / transcript 结构性进不来。差别只在证据来源：
P0 是确定性字符串谓词，P2 是 `git apply` + 六段式真实 CLI 驱动。

三条硬约束不变：
* 与作者不同模型家族；
* worker 的 self_check 整个丢弃，裁决来自独立重跑；
* verdict 绑定 head_sha = hash(diff + skill_hash)，改 diff 或改门禁都要重裁。
"""

from __future__ import annotations

from ..models import ModelAdapter
from ..platform.envelope import (
    SEMCONV_VERSION, content_hash, make_envelope, new_id, utcnow,
)
from ..platform.store import EventStore
from ..workspace.seed import GitWorkspace
from .generator import GeneratedSkill
from .holdout import HoldoutBundle, HoldoutViolation, assert_holdout_shape
from .mutants import MutationReport
from .runner import prove_skill
from .skill_spec import SkillProof
from .verifier import Verdict


class BehaviorVerifier:
    def __init__(
        self,
        store: EventStore,
        model: ModelAdapter,
        skill: GeneratedSkill,
        workspace: GitWorkspace,
        run_root,
    ):
        self.store = store
        self.model = model
        self.skill = skill
        self.workspace = workspace
        self.run_root = run_root

    def verify(
        self,
        bundle: HoldoutBundle,
        *,
        run_id: str,
        task_id: str,
        author_run_id: str,
        author_family: str,
        skill_proof: SkillProof,
        mutation_report: MutationReport,
        source_hashes: tuple[str, ...] = (),
        attempt: int = 1,
    ) -> Verdict:
        assert_holdout_shape(bundle)
        if self.model.family == author_family:
            raise HoldoutViolation(
                f"verifier 与作者同属 {author_family} 模型家族，裁决无效"
            )

        # head_sha 同时绑定产物与门禁版本：改 diff 或改 skill 都必须重裁。
        head_sha = content_hash({
            "diff": bundle.diff,
            "skill_hash": self.skill.skill_hash,
            "baseline": self.workspace.baseline_fingerprint(),
        })

        tree = self.workspace.worktree("verifier")
        independent_parts: list[str] = []
        checks: list[dict] = []
        apply_ok = True
        try:
            tree.apply_patch(bundle.diff)
        except (ValueError, RuntimeError) as exc:
            apply_ok = False
            independent_parts.append(f"PATCH_REJECTED {exc}")
        checks.append({"name": "patch_applies_cleanly", "kind": "gate",
                       "passed": apply_ok,
                       "detail": independent_parts[-1] if not apply_ok else "git apply --check + apply 通过"})

        proof = None
        if apply_ok:
            proof = prove_skill(self.skill, tree.path, self.run_root / f"verifier-run-a{attempt}",
                                stand_in_steps=("在独立 worktree 上 git apply 作者 diff",))
            independent_parts.append(
                f"INDEPENDENT_{'PASS' if proof.usable else 'FAIL'} "
                f"status={proof.status} evidence_survived={proof.evidence_survived}"
            )
        else:
            independent_parts.append("INDEPENDENT_FAIL patch 未应用，六段式未执行")

        independent_output = " | ".join(independent_parts)
        checks.extend([
            {"name": "skill_proven", "kind": "meta", "passed": bool(skill_proof.usable),
             "detail": f"skill status={skill_proof.status} hash={self.skill.skill_hash[:16]}"},
            {"name": "independent_six_phase_run", "kind": "gate",
             "passed": bool(proof and proof.usable),
             "detail": independent_output},
            {"name": "evidence_survived_cleanup", "kind": "gate",
             "passed": bool(proof and proof.evidence_survived),
             "detail": "cleanup 后 evidence/proof.json 仍存活" if proof else "未执行"},
            {"name": "mutation_test_all_fired", "kind": "meta",
             "passed": mutation_report.all_fired,
             "detail": (f"{sum(c.fired for c in mutation_report.cases)}/"
                        f"{len(mutation_report.cases)} 个坏版本上门禁开火；"
                        f"baseline_green={mutation_report.baseline_green}")},
            {"name": "self_report_discarded", "kind": "meta", "passed": True,
             "detail": f"已丢弃自陈: {bundle.test_output[:120]}"},
        ])

        # LLM-as-judge 只作补充输入，prompt 仅由 holdout 四字段 + 独立重跑结果构成。
        judge_prompt = (
            "你是独立验证者。只依据以下材料给出意见，不要臆测作者意图。\n"
            f"issue: {bundle.issue_body}\n"
            f"diff: {bundle.diff}\n"
            f"independent run: {independent_output}\n"
            f"governance: {bundle.governance_docs}\n"
        )
        judge = self.model.complete(judge_prompt)
        checks.append({"name": "llm_judge_opinion", "kind": "advisory", "passed": True,
                       "detail": f"{self.model.version}: {judge.text[:120]}"})

        result = "PASS" if all(c["passed"] for c in checks if c["kind"] != "advisory") else "FAIL"
        verifier_run_id = new_id("run")
        verdict = Verdict(
            verdict_id=new_id("vrd"), run_id=run_id, verifier_run_id=verifier_run_id,
            head_sha=head_sha, checks=checks,
            evidence_refs=[*source_hashes, head_sha, self.skill.skill_hash],
            result=result, created_at=utcnow(),
        )
        self.store.put_agent_run({
            "run_id": verifier_run_id, "task_id": task_id,
            "actor": "behavior-verifier", "role": "verifier",
            "model_family": self.model.family, "model_version": self.model.version,
            "prompt_hash": judge.prompt_hash, "semconv_version": SEMCONV_VERSION,
            "tool_policy": {"readonly": True, "holdout": True, "git_apply": True},
            "started_at": utcnow(),
        })
        self.store.put_verdict(verdict.to_dict())
        self.store.emit(
            make_envelope(
                run_id=run_id, task_id=task_id, actor="behavior-verifier",
                role="verifier", event_type="verdict.issued",
                parent_id=author_run_id, model_family=self.model.family,
                model_version=self.model.version, prompt_hash=judge.prompt_hash,
                artifact_hash=head_sha,
            ),
            {
                "verdict_id": verdict.verdict_id, "result": result,
                "head_sha": head_sha, "checks": checks,
                "independent_test_output": independent_output,
                "self_reported_test_output": bundle.test_output,
                "author_family": author_family, "verifier_family": self.model.family,
                "skill_hash": self.skill.skill_hash,
                "skill_proof": skill_proof.to_dict(),
                "mutations": mutation_report.to_dict(),
                "evidence_hash": proof.evidence_hash if proof else None,
            },
        )
        return verdict
