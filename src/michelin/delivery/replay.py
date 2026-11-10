"""L5 只读回放视图：按 run_id 从 append-only events 重建完整因果链。

P0 的验收判据就是这里：能从 events 表按 run_id 还原「这次为什么这么决策」。
六类锚点缺一不可，缺任一类 `complete=False`，说明证据链断了，不能算交付完成。

三种重放严格区分（架构文档 §L5）：
* 恢复重放：读 journal，绝不重新调模型 —— 本模块只读事件表，不触发任何模型调用
* 审计重放：本模块，只读、带身份与版本戳
* 评测重放：新模型跑旧 case，diff 单独存，绝不覆盖原状态（P4，不在本模块）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..platform.store import EventStore

# 需求 → 一手上下文 → 证据规格 → 模型/提示 → 工具与产物 → 独立裁决 → 抽样结论
ANCHORS: dict[str, str] = {
    "需求事件": "intent.received",
    "一手上下文": "context.sourced",
    "证据规格": "task.scoped",
    "模型与提示": "worker.model_response",
    "产物哈希": "artifact.produced",
    "独立裁决": "verdict.issued",
}


@dataclass(frozen=True, slots=True)
class ReplayView:
    run_id: str
    timeline: list[dict]
    anchors: dict[str, bool]
    decision: dict
    complete: bool
    chain_ok: bool
    chain_bad_seq: int | None
    counts: dict[str, int] = field(default_factory=dict)


def replay(store: EventStore, run_id: str) -> ReplayView:
    rows = store.query_events(run_id)
    timeline = []
    for row in rows:
        env = json.loads(row["envelope_json"])
        payload = json.loads(row["payload_json"])
        timeline.append({
            "seq": row["seq"],
            "at": row["occurred_at"],
            "type": row["type"],
            "actor": row["actor"],
            "model_version": env.get("model_version"),
            "model_family": env.get("model_family"),
            "prompt_hash": (env.get("prompt_hash") or "")[:16] or None,
            "artifact_hash": (env.get("artifact_hash") or "")[:16] or None,
            "semconv_version": env.get("semconv_version"),
            "idempotency_key": row["idempotency_key"][:16],
            "hash_chain": row["hash_chain"][:16],
            "payload": payload,
        })

    seen_types = {t["type"] for t in timeline}
    anchors = {label: event_type in seen_types for label, event_type in ANCHORS.items()}

    decision: dict = {}
    for item in timeline:
        p = item["payload"]
        if item["type"] == "intent.received":
            decision["intent"] = p.get("intent")
        elif item["type"] == "context.sourced":
            decision["source_hashes"] = p.get("source_hashes")
        elif item["type"] == "task.scoped":
            decision["evidence_spec"] = p.get("evidence_spec")
        elif item["type"] == "worker.model_response":
            decision.update({
                "author_model": p.get("model_version"),
                "author_family": p.get("model_family"),
                "prompt_hash": p.get("prompt_hash"),
            })
        elif item["type"] == "artifact.produced":
            decision["artifact_hash"] = p.get("artifact_hash")
        elif item["type"] == "verdict.issued":
            decision.update({
                "result": p.get("result"),
                "verifier_family": p.get("verifier_family"),
                "author_family": p.get("author_family"),
                "checks": p.get("checks"),
                "self_reported_discarded": p.get("self_reported_test_output"),
                "independent_test_output": p.get("independent_test_output"),
            })
        elif item["type"] == "sample.reviewed":
            decision["sampling"] = p.get("outcome")
            decision["root_cause"] = p.get("root_cause")
            decision["rule_ref"] = p.get("rule_ref")
        elif item["type"] == "state.transition":
            decision.setdefault("state_path", []).append(p.get("to"))

    chain_ok, _count, bad_seq = store.verify_chain()
    return ReplayView(
        run_id=run_id,
        timeline=timeline,
        anchors=anchors,
        decision=decision,
        complete=all(anchors.values()),
        chain_ok=chain_ok,
        chain_bad_seq=bad_seq,
        counts=store.counts(),
    )


def format_replay(view: ReplayView) -> str:
    lines = [
        f"=== 因果链回放 run_id={view.run_id} ===",
        f"事件数 {len(view.timeline)} | 哈希链 {'完好' if view.chain_ok else 'BROKEN@seq=' + str(view.chain_bad_seq)}"
        f" | 证据链 {'完整' if view.complete else '不完整'}",
        "",
        "锚点：",
    ]
    for label, ok in view.anchors.items():
        lines.append(f"  [{'x' if ok else ' '}] {label}")
    lines += ["", "时间线："]
    for item in view.timeline:
        extra = ""
        if item["model_version"]:
            extra += f" model={item['model_version']}"
        if item["artifact_hash"]:
            extra += f" artifact={item['artifact_hash']}"
        lines.append(
            f"  {item['seq']:>3} {item['at']} {item['type']:<28} {item['actor']:<22}{extra}"
        )
    lines += ["", "决策摘要："]
    for key, value in view.decision.items():
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        if len(text) > 160:
            text = text[:160] + "…"
        lines.append(f"  {key}: {text}")
    return "\n".join(lines)
