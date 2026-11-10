"""P1 持久化脊：PostgreSQL 实现的 EventStore。

与 SQLite 版**同一套方法契约**（见 contract.py），上层 ChiefOfStaff / verifier /
replay / sampler 一行不改即可切换。差异只在这几处：

* append-only 用 PL/pgSQL trigger（不是 SQLite RAISE）
* 哈希链的串行化用 `pg_advisory_xact_lock`，支持多进程并发 emit
* `seq` 用 BIGSERIAL，幂等靠 `idempotency_key UNIQUE` + `ON CONFLICT DO NOTHING`
* `pgvector` 已启用，但**向量只用于检索排序，不进 worker 上下文**（架构文档 §L1）

额外提供 P1 的 inbox_delivery（DLQ/对账）与 approval_gate（人工审批门）台账。
psycopg3 是可选依赖：缺失时只有本模块报错，P0 的零依赖路径完全不受影响。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from .envelope import Envelope, canonical, content_hash, sha256_text, utcnow

GENESIS = "GENESIS"
SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
DEFAULT_DSN = "postgresql://michelin:michelin@127.0.0.1:55432/michelin"
_ADVISORY_LOCK_KEY = 918273645  # 全局 emit 串行化，保证哈希链无分叉

_TABLES = ("inbox_event", "source_document", "task", "agent_run", "event",
           "artifact", "verdict", "sample_review", "guardrail_rule")


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "PostgresEventStore 需要 psycopg3：在 .venv 里执行 pip install 'psycopg[binary]'"
        ) from exc
    return psycopg, dict_row


class PostgresEventStore:
    def __init__(self, dsn: str | None = None):
        psycopg, dict_row = _psycopg()
        self.dsn = dsn or DEFAULT_DSN
        self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.conn.close()

    # ---------- L0 接入：原子占位去重 ----------
    def put_inbox_event(self, event_id: str, source: str, signature_ok: bool, payload: dict) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbox_event VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                (event_id, source, bool(signature_ok), content_hash(payload), utcnow(), "RECEIVED"),
            )
            return cur.fetchone() is not None

    # ---------- L1 上下文 ----------
    def put_source_document(self, source_hash: str, source_system: str, source_uri: str,
                            raw_bytes: bytes, occurred_at: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_document VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (source_hash) DO NOTHING",
                (source_hash, source_system, source_uri, raw_bytes, occurred_at, utcnow()),
            )

    def get_source_document(self, source_hash: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM source_document WHERE source_hash=%s", (source_hash,))
            row = cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        out["raw_bytes"] = bytes(out["raw_bytes"])  # psycopg 返回 memoryview
        return out

    # ---------- 任务与运行 ----------
    def put_task(self, task: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (task_id) DO UPDATE SET parent_id=EXCLUDED.parent_id, "
                "intent=EXCLUDED.intent, source_refs=EXCLUDED.source_refs, "
                "evidence_spec=EXCLUDED.evidence_spec, risk_level=EXCLUDED.risk_level, "
                "budget=EXCLUDED.budget, status=EXCLUDED.status, created_at=EXCLUDED.created_at",
                (
                    task["task_id"], task.get("parent_id"), task["intent"],
                    json.dumps(task["source_refs"], ensure_ascii=False),
                    json.dumps(task["evidence_spec"], ensure_ascii=False),
                    task["risk_level"], task["budget"], task["status"], utcnow(),
                ),
            )

    def set_task_status(self, task_id: str, status: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE task SET status=%s WHERE task_id=%s", (status, task_id))

    def get_task(self, task_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM task WHERE task_id=%s", (task_id,))
            return cur.fetchone()

    def put_agent_run(self, run: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_run VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (run_id) DO UPDATE SET prompt_hash=EXCLUDED.prompt_hash",
                (
                    run["run_id"], run["task_id"], run["actor"], run["role"],
                    run["model_family"], run["model_version"], run.get("prompt_hash"),
                    run.get("semconv_version"),
                    json.dumps(run.get("tool_policy", {}), ensure_ascii=False),
                    run.get("started_at", utcnow()),
                ),
            )

    def get_agent_run(self, run_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_run WHERE run_id=%s", (run_id,))
            return cur.fetchone()

    # ---------- append-only 事件与全局哈希链 ----------
    def emit(self, envelope: Envelope, payload: dict | None = None) -> int:
        payload = payload or {}
        payload_json = canonical(payload)
        effective_key = (
            sha256_text(envelope.idempotency_key + payload_json)
            if payload else envelope.idempotency_key
        )
        effective_env = (
            dataclasses.replace(envelope, idempotency_key=effective_key)
            if effective_key != envelope.idempotency_key else envelope
        )
        env_json = canonical(effective_env.to_dict())
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                cur.execute("SELECT seq FROM event WHERE idempotency_key=%s", (effective_key,))
                existing = cur.fetchone()
                if existing:
                    return int(existing["seq"])
                cur.execute("SELECT hash_chain FROM event ORDER BY seq DESC LIMIT 1")
                row = cur.fetchone()
                prev_hash = row["hash_chain"] if row else GENESIS
                chain = sha256_text(prev_hash + env_json + payload_json)
                cur.execute(
                    "INSERT INTO event "
                    "(run_id,task_id,type,actor,envelope_json,payload_json,idempotency_key,"
                    " side_effect_receipt,prev_hash,hash_chain,occurred_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING RETURNING seq",
                    (
                        effective_env.run_id, effective_env.task_id, effective_env.event_type,
                        effective_env.actor, env_json, payload_json, effective_key,
                        effective_env.side_effect_receipt, prev_hash, chain,
                        effective_env.occurred_at,
                    ),
                )
                inserted = cur.fetchone()
                if inserted:
                    return int(inserted["seq"])
                cur.execute("SELECT seq FROM event WHERE idempotency_key=%s", (effective_key,))
                return int(cur.fetchone()["seq"])

    def query_events(self, run_id: str) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM event WHERE run_id=%s ORDER BY seq", (run_id,))
            return [dict(r) for r in cur.fetchall()]

    def all_events(self) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM event ORDER BY seq")
            return [dict(r) for r in cur.fetchall()]

    def verify_chain(self) -> tuple[bool, int, int | None]:
        prev, count = GENESIS, 0
        for row in self.all_events():
            expected = sha256_text(prev + row["envelope_json"] + row["payload_json"])
            if expected != row["hash_chain"] or row["prev_hash"] != prev:
                return False, count, int(row["seq"])
            prev, count = row["hash_chain"], count + 1
        return True, count, None

    def put_artifact(self, artifact_hash: str, run_id: str, path: str, content_ref: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifact VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (artifact_hash, run_id, path, content_ref, utcnow()),
            )

    def put_verdict(self, verdict: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO verdict "
                "(verdict_id,run_id,verifier_run_id,head_sha,checks_json,evidence_refs,result,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (verdict_id) DO NOTHING",
                (
                    verdict["verdict_id"], verdict["run_id"], verdict["verifier_run_id"],
                    verdict["head_sha"], json.dumps(verdict["checks"], ensure_ascii=False),
                    json.dumps(verdict["evidence_refs"], ensure_ascii=False),
                    verdict["result"], verdict.get("created_at", utcnow()),
                ),
            )

    def active_verdict(self, head_sha: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM verdict WHERE head_sha=%s ORDER BY seq DESC LIMIT 1", (head_sha,)
            )
            row = cur.fetchone()
        if not row:
            return None
        out = dict(row)
        return None if out["result"] == "INVALIDATED" else out

    def invalidate_verdict(self, head_sha: str, run_id: str, reason: str) -> dict:
        from .envelope import new_id

        record = {
            "verdict_id": new_id("vrd"), "run_id": run_id, "verifier_run_id": "system",
            "head_sha": head_sha,
            "checks": [{"name": "invalidation", "passed": True, "detail": reason}],
            "evidence_refs": [], "result": "INVALIDATED",
        }
        self.put_verdict(record)
        return record

    def put_sample_review(self, review: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sample_review VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (review_id) DO UPDATE SET rule_ref=EXCLUDED.rule_ref",
                (
                    review["review_id"], review["run_id"], review["reviewer"],
                    review["outcome"], review["root_cause"], review.get("rule_ref"),
                    review.get("created_at", utcnow()),
                ),
            )

    def put_guardrail_rule(self, rule: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guardrail_rule VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (rule_id) DO UPDATE SET enabled=EXCLUDED.enabled",
                (
                    rule["rule_id"], rule["kind"], rule["target"],
                    json.dumps(rule["body"], ensure_ascii=False),
                    rule["mutation_test_ref"], rule["version"], bool(rule["enabled"]),
                    rule.get("created_at", utcnow()), rule.get("source_review_id"),
                ),
            )

    def list_rules(self) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM guardrail_rule ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]

    def counts(self) -> dict[str, int]:
        out = {}
        with self.conn.cursor() as cur:
            for t in _TABLES:
                cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
                out[t] = int(cur.fetchone()["n"])
        return out

    # ---------- P1：投递台账（DLQ / 对账） ----------
    def put_delivery(self, delivery: dict) -> bool:
        """返回 True 表示首次占位成功；False 表示重发。"""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbox_delivery "
                "(delivery_id,source,signature_ok,timestamp_ok,payload_hash,received_at,status,"
                " failure_reason,workflow_id,replay_count,raw_ref) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s) "
                "ON CONFLICT (delivery_id) DO UPDATE SET replay_count=inbox_delivery.replay_count+1 "
                "RETURNING delivery_id",
                (
                    delivery["delivery_id"], delivery["source"],
                    bool(delivery["signature_ok"]), bool(delivery["timestamp_ok"]),
                    delivery["payload_hash"], delivery["received_at"], delivery["status"],
                    delivery.get("failure_reason"), delivery.get("workflow_id"),
                    delivery.get("raw_ref"),
                ),
            )
            # 用 replay_count 区分首次插入与重发
            cur.execute(
                "SELECT replay_count FROM inbox_delivery WHERE delivery_id=%s",
                (delivery["delivery_id"],),
            )
            return int(cur.fetchone()["replay_count"]) == 0

    def mark_delivery(self, delivery_id: str, *, status: str, workflow_id: str | None = None,
                      failure_reason: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE inbox_delivery SET status=%s, workflow_id=COALESCE(%s,workflow_id), "
                "failure_reason=COALESCE(%s,failure_reason) WHERE delivery_id=%s",
                (status, workflow_id, failure_reason, delivery_id),
            )

    def get_delivery(self, delivery_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM inbox_delivery WHERE delivery_id=%s", (delivery_id,))
            return cur.fetchone()

    def list_deliveries(self, status: str | None = None) -> list[dict]:
        with self.conn.cursor() as cur:
            if status:
                cur.execute("SELECT * FROM inbox_delivery WHERE status=%s ORDER BY received_at", (status,))
            else:
                cur.execute("SELECT * FROM inbox_delivery ORDER BY received_at")
            return [dict(r) for r in cur.fetchall()]

    # ---------- P1：人工审批门 ----------
    def put_approval_gate(self, gate: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approval_gate VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (gate_id) DO NOTHING",
                (
                    gate["gate_id"], gate["run_id"], gate["topic"], gate["status"],
                    gate.get("decision"), gate.get("decided_by"),
                    gate.get("created_at", utcnow()), gate.get("decided_at"),
                ),
            )

    def decide_approval_gate(self, gate_id: str, decision: str, decided_by: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE approval_gate SET status='DECIDED', decision=%s, decided_by=%s, "
                "decided_at=%s WHERE gate_id=%s",
                (decision, decided_by, utcnow(), gate_id),
            )

    def get_approval_gate(self, gate_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM approval_gate WHERE gate_id=%s", (gate_id,))
            return cur.fetchone()
