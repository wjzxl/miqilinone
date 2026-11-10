"""L4 独立验证者：丢弃自陈、独立重跑门禁、跨模型家族、verdict 绑定产物哈希。

三条硬约束，全部有运行时断言 + 单测锁死：
1. verifier 与作者**不同模型家族**（pstack: "Each PR gets a verifier that isn't its author,
   on a different model family."）
2. worker 的 self_check / CI 结果只是**输入**，裁决来自 verifier 独立重跑
   （pstack: "Green CI is an input to a verdict, not a verdict."）
3. verdict 绑定 head_sha（P0 无 git，用 artifact_hash 顶替，字段契约不变）；
   一次新 push 作废旧 verdict，且作废只能追加记录（append-only）。

另有一条 P0 就落地的纪律：每条门禁都要用「故意破坏的变体」证明它真的会开火，
否则规则形同虚设（架构文档 §L6 / 风险清单「门禁形同虚设」）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from ..chief.policy import EvidenceSpec
from ..models import IndependenceWaiver, ModelAdapter
from ..platform.envelope import SEMCONV_VERSION, make_envelope, new_id, utcnow
from ..platform.store import EventStore
from .holdout import HoldoutBundle, HoldoutViolation, assert_holdout_shape


@dataclass(frozen=True, slots=True)
class CheckReport:
    independent_test_output: str
    checks: list[dict]
    passed: bool
    self_reported_test_output: str


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict_id: str
    run_id: str
    verifier_run_id: str
    head_sha: str
    checks: list[dict]
    evidence_refs: list[str]
    result: str  # PASS | FAIL | INVALIDATED
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def independent_rerun(bundle: HoldoutBundle, spec: EvidenceSpec) -> CheckReport:
    """把 worker 自陈的 test_output 整个丢掉，重新跑一遍确定性门禁。"""
    assert_holdout_shape(bundle)
    baseline = replace(bundle, test_output="")
    prelim = [
        (p, p.check(baseline))
        for p in spec.predicates
        if p.kind != "test_output_contains"
    ]
    ok_count = sum(ok for _, (ok, _) in prelim)
    marker = "INDEPENDENT_PASS" if ok_count == len(prelim) and prelim else "INDEPENDENT_FAIL"
    fresh_output = f"{marker} {ok_count}/{len(prelim)} | " + "; ".join(
        detail for _, (_, detail) in prelim
    )
    rerun_bundle = replace(bundle, test_output=fresh_output)
    checks = []
    for p in spec.predicates:
        ok, detail = p.check(rerun_bundle)
        checks.append({"name": p.name, "kind": p.kind, "passed": ok, "detail": detail})
    return CheckReport(
        independent_test_output=fresh_output,
        checks=checks,
        passed=all(c["passed"] for c in checks),
        self_reported_test_output=bundle.test_output,
    )


def mutation_test(spec: EvidenceSpec, reference: HoldoutBundle) -> list[dict]:
    """每条规则造一个故意破坏的变体，验证 property 真的会失败。"""
    results = []
    for p in spec.predicates:
        mutated = p.mutate(reference)
        ok, detail = p.check(mutated)
        results.append({
            "rule": p.name,
            "fired": not ok,  # True = 门禁在该变体上确实开火
            "detail": detail,
        })
    return results


class IndependentVerifier:
    def __init__(self, store: EventStore, model: ModelAdapter):
        self.store = store
        self.model = model

    def verify(
        self,
        bundle: HoldoutBundle,
        *,
        run_id: str,
        task_id: str,
        author_run_id: str,
        author_family: str,
        head_sha: str,
        evidence_spec: EvidenceSpec,
        source_hashes: tuple[str, ...] = (),
        independence_waiver: IndependenceWaiver | None = None,
    ) -> Verdict:
        """唯一位置参数就是 holdout 结构体；其余全为 keyword-only 元数据。

        tests/test_holdout.py 用 AST 锁住这个签名：不允许新增任何作者侧叙述参数。
        """
        assert_holdout_shape(bundle)
        if self.model.family == author_family and independence_waiver is None:
            raise HoldoutViolation(
                f"verifier 与作者同属 {author_family} 模型家族，裁决无效"
            )

        verifier_run_id = new_id("run")
        report = independent_rerun(bundle, evidence_spec)
        mutations = mutation_test(evidence_spec, bundle)

        # LLM-as-judge 只作补充输入：prompt 仅由 holdout 四字段 + 独立重跑结果构成，
        # 永远看不到作者的 plan / transcript。
        judge_prompt = (
            "你是独立验证者。只依据以下材料给出意见，不要臆测作者意图。\n"
            f"issue: {bundle.issue_body}\n"
            f"diff: {bundle.diff}\n"
            f"independent test output: {report.independent_test_output}\n"
            f"governance: {bundle.governance_docs}\n"
        )
        judge = self.model.complete(judge_prompt)

        all_gates_fired = all(m["fired"] for m in mutations)
        checks = report.checks + [
            {"name": "mutation_test", "kind": "meta", "passed": all_gates_fired,
             "detail": f"{sum(m['fired'] for m in mutations)}/{len(mutations)} 条门禁在破坏变体上开火"},
            {"name": "llm_judge_opinion", "kind": "advisory", "passed": True,
             "detail": f"{self.model.version}: {judge.text[:120]}"},
            {"name": "llm_judge_usage", "kind": "meta", "passed": True,
             "detail": f"judge 用量 {judge.usage.to_dict()}"},
            {"name": "self_report_discarded", "kind": "meta", "passed": True,
             "detail": f"已丢弃自陈: {report.self_reported_test_output[:100]}"},
        ]
        if independence_waiver is not None:
            # 降级必须出现在裁决记录里，不能只存在于某个人的脑子里。
            checks.append({
                "name": "model_independence_waived", "kind": "meta", "passed": True,
                "detail": (f"家族级独立性已豁免：verifier({independence_waiver.verifier_model}) "
                           f"与 author({independence_waiver.worker_model}) 同属 "
                           f"{independence_waiver.verifier_family}；{independence_waiver.reason}"),
            })
        result = "PASS" if (report.passed and all_gates_fired) else "FAIL"

        verdict = Verdict(
            verdict_id=new_id("vrd"),
            run_id=run_id,
            verifier_run_id=verifier_run_id,
            head_sha=head_sha,
            checks=checks,
            evidence_refs=[*source_hashes, head_sha],
            result=result,
            created_at=utcnow(),
        )
        self.store.put_agent_run({
            "run_id": verifier_run_id,
            "task_id": task_id,
            "actor": "independent-verifier",
            "role": "verifier",
            "model_family": self.model.family,
            "model_version": self.model.version,
            "prompt_hash": judge.prompt_hash,
            "semconv_version": SEMCONV_VERSION,
            "tool_policy": {"readonly": True, "holdout": True},
            "started_at": utcnow(),
        })
        self.store.put_verdict(verdict.to_dict())
        self.store.emit(
            make_envelope(
                run_id=run_id, task_id=task_id, actor="independent-verifier",
                role="verifier", event_type="verdict.issued",
                parent_id=author_run_id,
                model_family=self.model.family,
                model_version=self.model.version,
                prompt_hash=judge.prompt_hash,
                artifact_hash=head_sha,
            ),
            {
                "verdict_id": verdict.verdict_id,
                "result": result,
                "head_sha": head_sha,
                "checks": checks,
                "independent_test_output": report.independent_test_output,
                "self_reported_test_output": report.self_reported_test_output,
                "author_family": author_family,
                "verifier_family": self.model.family,
                "independence_waiver": (
                    independence_waiver.to_dict() if independence_waiver else None
                ),
                "judge_usage": judge.usage.to_dict(),
                "provider_request_id": judge.provider_request_id,
                "mutations": mutations,
            },
        )
        return verdict

    def invalidate(self, head_sha: str, run_id: str, reason: str) -> dict:
        """新 push → 旧 verdict 自动作废（追加 INVALIDATED 记录，不改历史）。"""
        record = self.store.invalidate_verdict(head_sha, run_id, reason)
        self.store.emit(
            make_envelope(
                run_id=run_id, task_id=run_id, actor="independent-verifier",
                role="verifier", event_type="verdict.invalidated",
                artifact_hash=head_sha,
            ),
            {"reason": reason, "head_sha": head_sha},
        )
        return record
