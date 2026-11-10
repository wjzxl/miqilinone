"""L6 环境护栏：错误 → 规则回流的落点。

这是视频里「改规则一劳永逸」的真实工程形态，也是与原始访谈一致、
而与视频口播（「把纠错聊天记录喂给 AI 让它自己立规矩」）不同的那一层：
规则必须是**确定性、可执行、带变异测试证明**的约束，不是 prose 提示词。

P0 生成的是谓词型规则（P2 换成 ast-grep YAML / JSON Schema / 类型约束），
但「无 mutation 证明不得启用」这条纪律现在就用代码锁死。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..chief.policy import EvidenceSpec, Predicate
from ..platform.envelope import content_hash, new_id, utcnow
from ..verification.holdout import build_holdout
from ..verification.verifier import mutation_test

# 归因分类来自架构文档 §L5：不合格先归因，再决定改哪一层。
ROOT_CAUSES = {
    "missing_evidence_marker": ("predicate", "artifact.diff", "环境约束缺失"),
    "verification_rule_gap": ("predicate", "verification.gates", "验证规则有洞"),
    "context_not_provided": ("context_ref", "context.source_document", "上下文没给透"),
    "environment_constraint_missing": ("lint_rule", "repo.guardrails", "环境约束缺失"),
}


@dataclass(frozen=True, slots=True)
class GuardrailRule:
    rule_id: str
    kind: str
    target: str
    body: dict
    mutation_test_ref: str
    version: int
    enabled: bool
    created_at: str
    source_review_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def derive_rule(
    root_cause: str,
    *,
    evidence_spec: EvidenceSpec,
    review_id: str,
    marker: str = "EVIDENCE:ok",
) -> GuardrailRule:
    """把一次抽样不合格转成一条环境规则，并用变异测试证明它会开火。"""
    if root_cause not in ROOT_CAUSES:
        raise ValueError(
            f"root_cause 必须归因到 {sorted(ROOT_CAUSES)}，不得写「模型不够聪明」"
        )
    kind, target, _category = ROOT_CAUSES[root_cause]

    new_predicate = Predicate(
        name=f"guardrail_{root_cause}",
        kind="diff_contains",
        value=marker,
    )
    strengthened = EvidenceSpec((*evidence_spec.predicates, new_predicate))

    # 真的跑一次变异测试，而不是只写一个引用链接。
    reference = build_holdout(
        issue_body="只读分析任务",
        artifact_text=f"合规产出 {marker}",
        self_reported_test_output="WORKER_SELF_REPORT PASS",
        governance_docs=("只读任务", "证据规格必须可检查"),
    )
    fired = mutation_test(strengthened, reference)
    proven = bool(fired) and all(m["fired"] for m in fired)

    return GuardrailRule(
        rule_id=new_id("rule"),
        kind=kind,
        target=target,
        body={
            "predicate": new_predicate.to_dict(),
            "why": ROOT_CAUSES[root_cause][2],
            "principle": "改环境，不改提示词",
        },
        mutation_test_ref=content_hash({
            "case_count": len(fired),
            "fired": sum(m["fired"] for m in fired),
            "cases": fired,
        }),
        version=1,
        enabled=proven,  # 没有变异证明就不启用
        created_at=utcnow(),
        source_review_id=review_id,
    )
