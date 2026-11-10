"""L5 抽样验收与「不合格 → 回流成环境规则」闭环。

架构文档 §L5：抽样不合格必须归因到「上下文没给透 / 验证规则有洞 / 环境约束缺失」，
并写回 L6 一条**带变异测试证明**的规则。sample_review.rule_ref 不得为空。
这里不改任何提示词、不重训模型 —— 改的是环境。
"""

from __future__ import annotations

from ..chief.policy import EvidenceSpec
from ..guardrails.rules import ROOT_CAUSES, derive_rule
from ..platform.envelope import make_envelope, new_id, utcnow
from ..platform.store import EventStore


class Sampler:
    def __init__(self, store: EventStore, evidence_spec: EvidenceSpec, marker: str = "EVIDENCE:ok"):
        self.store = store
        self.evidence_spec = evidence_spec
        self.marker = marker

    def review(
        self,
        *,
        run_id: str,
        task_id: str,
        outcome: str,
        root_cause: str = "",
        reviewer: str = "human-sampler",
        head_sha: str = "",
    ) -> dict:
        if outcome not in {"ACCEPT", "REJECT"}:
            raise ValueError("outcome 必须是 ACCEPT 或 REJECT")
        if outcome == "REJECT" and root_cause not in ROOT_CAUSES:
            raise ValueError(
                f"抽样不合格必须归因到 {sorted(ROOT_CAUSES)}，不能只写「模型不行」"
            )

        review_id = new_id("rev")
        rule_ref = None
        if outcome == "REJECT":
            rule = derive_rule(
                root_cause,
                evidence_spec=self.evidence_spec,
                review_id=review_id,
                marker=self.marker,
            )
            self.store.put_guardrail_rule(rule.to_dict())
            rule_ref = rule.rule_id

        self.store.put_sample_review({
            "review_id": review_id,
            "run_id": run_id,
            "reviewer": reviewer,
            "outcome": outcome,
            "root_cause": root_cause,
            "rule_ref": rule_ref,
            "created_at": utcnow(),
        })
        self.store.emit(
            make_envelope(
                run_id=run_id, task_id=task_id, actor=reviewer,
                role="sampler", event_type="sample.reviewed",
                artifact_hash=head_sha or None,
            ),
            {
                "review_id": review_id,
                "outcome": outcome,
                "root_cause": root_cause,
                "rule_ref": rule_ref,
            },
        )
        return {"review_id": review_id, "outcome": outcome, "rule_ref": rule_ref}
