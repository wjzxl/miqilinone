"""统一事件信封与内容哈希。

架构文档 §3「统一事件信封（自研契约）」的代码落点。
所有事件无论来自 L0 接入、L2 调度、L3 执行还是 L4 裁决，都必须带同一个信封，
否则按 run_id 无法重建因果链。

注意：prompt_hash 不是 OTel 标准字段，是我们自己算的 SHA-256；
semconv_version 必须 pin 到具体版本/提交，否则半年后的回放读不懂 gen_ai.* 埋点。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

# OTel GenAI semantic conventions 仍处于 Development、无 tagged release。
# 审计重放依赖这个戳来解释 span 属性；升级约定时必须显式改这里并保留旧值可读。
SEMCONV_VERSION = "otel-genai-semconv@dev-pin-2026-11-09"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(obj: object) -> str:
    """确定性序列化：哈希链对键顺序敏感，必须 sort_keys。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj: object) -> str:
    return sha256_text(canonical(obj))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class Envelope:
    run_id: str
    task_id: str
    actor: str
    role: str
    event_type: str
    idempotency_key: str
    occurred_at: str
    parent_id: str | None = None
    tool: str | None = None
    args_hash: str | None = None
    model_family: str | None = None
    model_version: str | None = None
    prompt_hash: str | None = None
    artifact_hash: str | None = None
    approval_principal: str | None = None
    side_effect_receipt: str | None = None
    semconv_version: str = SEMCONV_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def make_envelope(
    *,
    run_id: str,
    task_id: str,
    actor: str,
    role: str,
    event_type: str,
    occurred_at: str | None = None,
    idempotency_key: str | None = None,
    **fields: object,
) -> Envelope:
    """构造信封；未给幂等键时按「run + type + actor + 业务字段」自动派生。

    自动派生的意义：崩溃恢复重放到同一步时不会产生第二条事件（跨引擎幂等键自研契约）。
    """
    key = idempotency_key or content_hash(
        {"run": run_id, "type": event_type, "actor": actor, "fields": fields}
    )
    allowed = {f for f in Envelope.__dataclass_fields__ if f not in {
        "run_id", "task_id", "actor", "role", "event_type", "idempotency_key", "occurred_at"
    }}
    unexpected = set(fields) - allowed
    if unexpected:
        raise ValueError(f"信封字段不在契约内: {sorted(unexpected)}")
    return Envelope(
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        role=role,
        event_type=event_type,
        idempotency_key=key,
        occurred_at=occurred_at or utcnow(),
        **fields,  # type: ignore[arg-type]
    )
