"""P0 事件存储：append-only events + 全局哈希链 + 八张核心表。

架构文档 §3「核心表」的实现。设计要点：

* `event` / `source_document` / `artifact` / `verdict` 四张表由 SQLite trigger 强制
  **禁 UPDATE / 禁 DELETE**，撤销/作废只能追加新事件表达，不能改历史。
* 每条 event 的 `hash_chain = SHA256(prev_hash || canonical(envelope) || canonical(payload))`，
  形成全局单链；`verify_chain()` 可检测任何事后篡改（P1 再升级为 WORM / 外部签名）。
* `idempotency_key` UNIQUE：崩溃恢复重放同一步不会写入第二条事件。
* **执行日志与观测 trace 是两套东西**：这里只存完整、不可变、长期保留的执行日志；
  OTel / Langfuse 那条可采样可脱敏的链路不要拿来替代它。

P0 用 SQLite（标准库、零运维、今天就能跑）。Postgres + DBOS 是 P1 的接缝：
`EventStore` 的方法签名即适配器契约，换实现时上层不动。compose.yaml 已备好 Postgres。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .envelope import Envelope, canonical, content_hash, sha256_text, utcnow

GENESIS = "GENESIS"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_event (
    event_id       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    signature_ok   INTEGER NOT NULL,
    payload_hash   TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_document (
    source_hash    TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    source_uri     TEXT NOT NULL,
    raw_bytes      BLOB NOT NULL,
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task (
    task_id        TEXT PRIMARY KEY,
    parent_id      TEXT,
    intent         TEXT NOT NULL,
    source_refs    TEXT NOT NULL,
    evidence_spec  TEXT NOT NULL,
    risk_level     TEXT NOT NULL,
    budget         REAL NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run (
    run_id          TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    actor           TEXT NOT NULL,
    role            TEXT NOT NULL,
    model_family    TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_hash     TEXT,
    semconv_version TEXT,
    tool_policy     TEXT,
    started_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    seq                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL,
    task_id              TEXT NOT NULL,
    type                 TEXT NOT NULL,
    actor                TEXT NOT NULL,
    envelope_json        TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    idempotency_key      TEXT NOT NULL UNIQUE,
    side_effect_receipt  TEXT,
    prev_hash            TEXT NOT NULL,
    hash_chain           TEXT NOT NULL,
    occurred_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id, seq);

CREATE TABLE IF NOT EXISTS artifact (
    artifact_hash  TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    path           TEXT NOT NULL,
    content_ref    TEXT NOT NULL,
    produced_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdict (
    verdict_id       TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    verifier_run_id  TEXT NOT NULL,
    head_sha         TEXT NOT NULL,
    checks_json      TEXT NOT NULL,
    evidence_refs    TEXT NOT NULL,
    result           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    seq              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_verdict_head ON verdict(head_sha, seq);

CREATE TABLE IF NOT EXISTS sample_review (
    review_id   TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    reviewer    TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    rule_ref    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardrail_rule (
    rule_id            TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    target             TEXT NOT NULL,
    body               TEXT NOT NULL,
    mutation_test_ref  TEXT NOT NULL,
    version            INTEGER NOT NULL,
    enabled            INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    source_review_id   TEXT
);

-- P1 新增：webhook 投递台账（可变，与 append-only 事实表分开）与人工审批门
CREATE TABLE IF NOT EXISTS inbox_delivery (
    delivery_id     TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    signature_ok    INTEGER NOT NULL,
    timestamp_ok    INTEGER NOT NULL,
    payload_hash    TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL,
    failure_reason  TEXT,
    workflow_id     TEXT,
    replay_count    INTEGER NOT NULL DEFAULT 0,
    raw_ref         TEXT
);

CREATE TABLE IF NOT EXISTS approval_gate (
    gate_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    topic       TEXT NOT NULL,
    status      TEXT NOT NULL,
    decision    TEXT,
    decided_by  TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
"""

# append-only：四张事实表不允许改、不允许删。作废/纠正一律追加。
_APPEND_ONLY_TABLES = ("source_document", "event", "artifact", "verdict")


def _triggers() -> str:
    parts = []
    for table in _APPEND_ONLY_TABLES:
        for op in ("UPDATE", "DELETE"):
            parts.append(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()} "
                f"BEFORE {op} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, 'append-only: {table} forbids {op}'); END;"
            )
    return "\n".join(parts)


class AppendOnlyViolation(RuntimeError):
    pass


class EventStore:
    """P0 的脊。方法签名即 P1 换 Postgres/DBOS 时的适配器契约。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(_triggers())
        # 轻量迁移：旧库可能没有 P1 新增的 raw_ref 列
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(inbox_delivery)")}
        if "raw_ref" not in cols:
            self.conn.execute("ALTER TABLE inbox_delivery ADD COLUMN raw_ref TEXT")
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ---------- L0 接入：原子占位去重 ----------
    def put_inbox_event(
        self, event_id: str, source: str, signature_ok: bool, payload: dict
    ) -> bool:
        """返回 True 表示首次落库；False 表示重发（at-least-once 语义下不得建第二个任务）。"""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO inbox_event VALUES (?,?,?,?,?,?)",
                (event_id, source, int(signature_ok), content_hash(payload), utcnow(), "RECEIVED"),
            )
            return cur.rowcount == 1

    # ---------- L1 上下文：append-only 一手原文 ----------
    def put_source_document(
        self,
        source_hash: str,
        source_system: str,
        source_uri: str,
        raw_bytes: bytes,
        occurred_at: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO source_document VALUES (?,?,?,?,?,?)",
                (source_hash, source_system, source_uri, raw_bytes, occurred_at, utcnow()),
            )

    def get_source_document(self, source_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM source_document WHERE source_hash=?", (source_hash,)
        ).fetchone()
        return dict(row) if row else None

    # ---------- 任务与运行 ----------
    def put_task(self, task: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO task VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task["task_id"], task.get("parent_id"), task["intent"],
                    json.dumps(task["source_refs"], ensure_ascii=False),
                    json.dumps(task["evidence_spec"], ensure_ascii=False),
                    task["risk_level"], task["budget"], task["status"], utcnow(),
                ),
            )

    def set_task_status(self, task_id: str, status: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE task SET status=? WHERE task_id=?", (status, task_id))

    def get_task(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM task WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def put_agent_run(self, run: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO agent_run VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run["run_id"], run["task_id"], run["actor"], run["role"],
                    run["model_family"], run["model_version"], run.get("prompt_hash"),
                    run.get("semconv_version"), json.dumps(run.get("tool_policy", {}), ensure_ascii=False),
                    run.get("started_at", utcnow()),
                ),
            )

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM agent_run WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ---------- 不可变事实 ----------
    def emit(self, envelope: Envelope, payload: dict | None = None) -> int:
        """追加一条事件，返回 seq。幂等键已存在时返回原 seq，不写第二条。

        跨引擎幂等键分配是**自研契约**：最终键 = SHA256(调用方给的 key || 本次 payload)。
        只看信封不看 payload 会把「第 2 次派发」「下一次状态迁移」误判成崩溃重放而吞掉，
        那等于证据链凭空缺了一段。信封里存的也是最终生效键，保证审计与存储一致。
        """
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
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.conn.execute(
                    "SELECT seq FROM event WHERE idempotency_key=?", (effective_key,)
                ).fetchone()
                if existing:
                    self.conn.execute("COMMIT")
                    return int(existing["seq"])
                row = self.conn.execute(
                    "SELECT hash_chain FROM event ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                prev_hash = row["hash_chain"] if row else GENESIS
                chain = sha256_text(prev_hash + env_json + payload_json)
                cur = self.conn.execute(
                    "INSERT INTO event "
                    "(run_id,task_id,type,actor,envelope_json,payload_json,"
                    " idempotency_key,side_effect_receipt,prev_hash,hash_chain,occurred_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        effective_env.run_id, effective_env.task_id, effective_env.event_type,
                        effective_env.actor, env_json, payload_json, effective_key,
                        effective_env.side_effect_receipt, prev_hash, chain,
                        effective_env.occurred_at,
                    ),
                )
                seq = int(cur.lastrowid)
                self.conn.execute("COMMIT")
                return seq
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def query_events(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_events(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM event ORDER BY seq")]

    def verify_chain(self) -> tuple[bool, int, int | None]:
        """重算整条哈希链。返回 (是否完好, 事件数, 首个不一致的 seq)。"""
        prev = GENESIS
        count = 0
        for row in self.conn.execute("SELECT * FROM event ORDER BY seq"):
            expected = sha256_text(prev + row["envelope_json"] + row["payload_json"])
            if expected != row["hash_chain"] or row["prev_hash"] != prev:
                return False, count, int(row["seq"])
            prev = row["hash_chain"]
            count += 1
        return True, count, None

    def put_artifact(self, artifact_hash: str, run_id: str, path: str, content_ref: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO artifact VALUES (?,?,?,?,?)",
                (artifact_hash, run_id, path, content_ref, utcnow()),
            )

    def put_verdict(self, verdict: dict) -> None:
        with self._lock:
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM verdict").fetchone()["n"]
            self.conn.execute(
                "INSERT INTO verdict VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    verdict["verdict_id"], verdict["run_id"], verdict["verifier_run_id"],
                    verdict["head_sha"], json.dumps(verdict["checks"], ensure_ascii=False),
                    json.dumps(verdict["evidence_refs"], ensure_ascii=False),
                    verdict["result"], verdict.get("created_at", utcnow()), seq,
                ),
            )

    def active_verdict(self, head_sha: str) -> dict | None:
        """最新一条即当前有效裁决；作废也是追加一条 INVALIDATED 记录。"""
        row = self.conn.execute(
            "SELECT * FROM verdict WHERE head_sha=? ORDER BY seq DESC LIMIT 1", (head_sha,)
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        return None if out["result"] == "INVALIDATED" else out

    def invalidate_verdict(self, head_sha: str, run_id: str, reason: str) -> dict:
        """新 push 作废旧 verdict：只追加，不改历史。"""
        from .envelope import new_id

        record = {
            "verdict_id": new_id("vrd"),
            "run_id": run_id,
            "verifier_run_id": "system",
            "head_sha": head_sha,
            "checks": [{"name": "invalidation", "passed": True, "detail": reason}],
            "evidence_refs": [],
            "result": "INVALIDATED",
        }
        self.put_verdict(record)
        return record

    def put_sample_review(self, review: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sample_review VALUES (?,?,?,?,?,?,?)",
                (
                    review["review_id"], review["run_id"], review["reviewer"],
                    review["outcome"], review["root_cause"], review.get("rule_ref"),
                    review.get("created_at", utcnow()),
                ),
            )

    def put_guardrail_rule(self, rule: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO guardrail_rule VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rule["rule_id"], rule["kind"], rule["target"],
                    json.dumps(rule["body"], ensure_ascii=False),
                    rule["mutation_test_ref"], rule["version"], int(rule["enabled"]),
                    rule.get("created_at", utcnow()), rule.get("source_review_id"),
                ),
            )

    def list_rules(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM guardrail_rule ORDER BY created_at")]

    # ---------- P1：投递台账（DLQ / 对账） ----------
    def put_delivery(self, delivery: dict) -> bool:
        """返回 True 表示首次占位成功；False 表示重发（只累加 replay_count）。"""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO inbox_delivery "
                "(delivery_id,source,signature_ok,timestamp_ok,payload_hash,received_at,"
                " status,failure_reason,workflow_id,replay_count,raw_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                (
                    delivery["delivery_id"], delivery["source"],
                    int(delivery["signature_ok"]), int(delivery["timestamp_ok"]),
                    delivery["payload_hash"], delivery["received_at"], delivery["status"],
                    delivery.get("failure_reason"), delivery.get("workflow_id"),
                    delivery.get("raw_ref"),
                ),
            )
            if cur.rowcount == 1:
                return True
            self.conn.execute(
                "UPDATE inbox_delivery SET replay_count=replay_count+1 WHERE delivery_id=?",
                (delivery["delivery_id"],),
            )
            return False

    def mark_delivery(self, delivery_id: str, *, status: str,
                      workflow_id: str | None = None,
                      failure_reason: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE inbox_delivery SET status=?, workflow_id=COALESCE(?,workflow_id), "
                "failure_reason=COALESCE(?,failure_reason) WHERE delivery_id=?",
                (status, workflow_id, failure_reason, delivery_id),
            )

    def get_delivery(self, delivery_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM inbox_delivery WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_deliveries(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM inbox_delivery WHERE status=? ORDER BY received_at", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM inbox_delivery ORDER BY received_at").fetchall()
        return [dict(r) for r in rows]

    # ---------- P1：人工审批门 ----------
    def put_approval_gate(self, gate: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO approval_gate VALUES (?,?,?,?,?,?,?,?)",
                (
                    gate["gate_id"], gate["run_id"], gate["topic"], gate["status"],
                    gate.get("decision"), gate.get("decided_by"),
                    gate.get("created_at", utcnow()), gate.get("decided_at"),
                ),
            )

    def decide_approval_gate(self, gate_id: str, decision: str, decided_by: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE approval_gate SET status='DECIDED', decision=?, decided_by=?, "
                "decided_at=? WHERE gate_id=?",
                (decision, decided_by, utcnow(), gate_id),
            )

    def get_approval_gate(self, gate_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM approval_gate WHERE gate_id=?", (gate_id,)
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        tables = ("inbox_event", "source_document", "task", "agent_run", "event",
                  "artifact", "verdict", "sample_review", "guardrail_rule")
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
            for t in tables
        }
