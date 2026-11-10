"""P1 集成测试：真实 Postgres 脊 + DBOS 崩溃恢复。

系统 python3（无 psycopg / dbos）运行时整组自动 skip；
用 `.venv/bin/python -m unittest discover -s tests -v` 且 Postgres 在跑时实跑。

为避免污染开发库，测试会创建一个临时数据库，结束时删除。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import _path  # noqa: F401
from _path import SRC

from michelin.chief.policy import TaskIntent
from michelin.chief.workflow import ChiefOfStaff
from michelin.context.sources import SourcePlane
from michelin.delivery.replay import replay
from michelin.models import ScriptedModel
from michelin.platform.contract import assert_store_contract
from michelin.platform.postgres import DEFAULT_DSN

HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None
HAS_DBOS = importlib.util.find_spec("dbos") is not None
BASE_DSN = os.getenv("MW_TEST_DATABASE_URL", DEFAULT_DSN)
ROOT = SRC.parent


def _admin_dsn() -> str:
    return BASE_DSN.rsplit("/", 1)[0] + "/postgres"


def _create_temp_db() -> tuple[str, str]:
    import psycopg
    name = f"mw_p1_test_{uuid.uuid4().hex[:8]}"
    conn = psycopg.connect(_admin_dsn(), autocommit=True)
    conn.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    return BASE_DSN.rsplit("/", 1)[0] + "/" + name, name


def _drop_temp_db(name: str) -> None:
    import psycopg
    conn = psycopg.connect(_admin_dsn(), autocommit=True)
    conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (name,)
    )
    conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    conn.close()


def _chief(store, script):
    return ChiefOfStaff(
        store, SourcePlane(store),
        ScriptedModel("anthropic", "claude-stub-pg", script),
        ScriptedModel("openai", "gpt-stub-pg", ["PASS"]),
    )


@unittest.skipUnless(HAS_PSYCOPG, "需要 psycopg3（在 .venv 中运行）")
class TestPostgresStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.dsn, cls.dbname = _create_temp_db()
            from michelin.platform.postgres import PostgresEventStore
            cls.store = PostgresEventStore(cls.dsn)
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres 不可用: {type(exc).__name__}: {exc}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.close()
        finally:
            _drop_temp_db(cls.dbname)

    def test_satisfies_store_contract(self):
        assert_store_contract(self.store)

    def test_p0_pipeline_runs_unchanged_on_postgres(self):
        chief = _chief(self.store, ["EVIDENCE:ok 只读分析完成。"])
        result = chief.handle(TaskIntent(
            intent="Postgres 实库验证", source_bodies=("一手原文 A。", "一手原文 B。")
        ))
        view = replay(self.store, result.run_id)
        self.assertEqual(result.state.value, "ACCEPTED")
        self.assertTrue(view.complete, view.anchors)
        self.assertTrue(view.chain_ok)
        self.assertEqual(view.decision["author_family"], "anthropic")
        self.assertEqual(view.decision["verifier_family"], "openai")

    def test_all_four_scenarios_on_postgres(self):
        cases = [
            (["EVIDENCE:ok 完成。"], "ACCEPTED", 1, ""),
            (["草稿缺证据。", "EVIDENCE:ok 补齐。"], "ACCEPTED", 2, ""),
            (["始终缺证据。"], "NEEDS_HUMAN", 3, ""),
            (["EVIDENCE:ok 完成。"], "RULE_UPDATED", 1, "missing_evidence_marker"),
        ]
        for script, state, attempts, root_cause in cases:
            with self.subTest(state=state):
                outcome = "REJECT" if root_cause else "ACCEPT"
                r = _chief(self.store, script).handle(
                    TaskIntent(intent="PG 场景", source_bodies=("原文。",)),
                    sampling_outcome=outcome, sampling_root_cause=root_cause,
                )
                self.assertEqual(r.state.value, state)
                self.assertEqual(r.attempts, attempts)
        rules = self.store.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertTrue(bool(rules[0]["enabled"]))

    def test_append_only_trigger_rejects_update(self):
        from michelin.platform.envelope import make_envelope
        self.store.emit(
            make_envelope(run_id="r-append", task_id="t-append", actor="a",
                          role="r", event_type="probe"),
            {"k": "v"},
        )
        import psycopg
        with self.assertRaises(psycopg.errors.IntegrityError):
            with self.store.conn.cursor() as cur:
                cur.execute("UPDATE event SET payload_json='{}' WHERE run_id='r-append'")

    def test_idempotency_and_hash_chain(self):
        from michelin.platform.envelope import make_envelope
        env = make_envelope(run_id="r-idem", task_id="t", actor="a", role="r", event_type="x")
        a = self.store.emit(env, {"k": "v"})
        b = self.store.emit(env, {"k": "v"})
        self.assertEqual(a, b)
        ok, count, bad = self.store.verify_chain()
        self.assertTrue(ok, f"哈希链损坏于 seq={bad}")
        self.assertGreater(count, 10)

    def test_delivery_dedupe_and_approval_gate(self):
        d = {
            "delivery_id": "pg-delivery-1", "source": "github", "signature_ok": True,
            "timestamp_ok": True, "payload_hash": "abc", "received_at": "now",
            "status": "PENDING", "raw_ref": None,
        }
        self.assertTrue(self.store.put_delivery(d))
        self.assertFalse(self.store.put_delivery(d))
        self.assertEqual(self.store.get_delivery("pg-delivery-1")["replay_count"], 1)
        self.store.mark_delivery("pg-delivery-1", status="DONE", workflow_id="wf-1")
        self.assertEqual(self.store.get_delivery("pg-delivery-1")["status"], "DONE")

        self.store.put_approval_gate({
            "gate_id": "gate-1", "run_id": "r-1", "topic": "approval:wf-1",
            "status": "WAITING", "created_at": "now",
        })
        self.store.decide_approval_gate("gate-1", "APPROVED", "human")
        gate = self.store.get_approval_gate("gate-1")
        self.assertEqual(gate["status"], "DECIDED")
        self.assertEqual(gate["decision"], "APPROVED")


@unittest.skipUnless(HAS_PSYCOPG and HAS_DBOS, "需要 psycopg3 + dbos + 运行中的 Postgres")
class TestDBOSDurable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.dsn, cls.dbname = _create_temp_db()
            from michelin.platform.postgres import PostgresEventStore
            cls.store = PostgresEventStore(cls.dsn)  # 先建业务表
            cls.store.close()
        except Exception as exc:
            raise unittest.SkipTest(f"Postgres 不可用: {type(exc).__name__}: {exc}")
        cls.app_version = f"mw-test-{uuid.uuid4().hex[:8]}"
        cls.env = os.environ.copy()
        cls.env.update({
            "PYTHONPATH": str(SRC),
            "MW_APP_VERSION": cls.app_version,
            "MW_DATABASE_URL": cls.dsn,
        })
        cls.env.pop("MW_CRASH_STEP", None)

    @classmethod
    def tearDownClass(cls):
        _drop_temp_db(cls.dbname)

    def _cli(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--store", "postgres",
             "--dsn", self.dsn, "p1-durable", *args],
            capture_output=True, text=True, timeout=timeout, env=self.env,
            cwd=str(ROOT),
        )

    def test_approval_gate_completes(self):
        wfid = f"wf-ok-{uuid.uuid4().hex[:6]}"
        p = self._cli("--phase", "start", "--workflow-id", wfid)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("state=ACCEPTED", p.stdout)
        self.assertIn("六锚点 齐全", p.stdout)
        self.assertIn("APPROVED", p.stdout)

    def test_crash_recovery_skips_completed_steps(self):
        wfid = f"wf-crash-{uuid.uuid4().hex[:6]}"
        start = self._cli("--phase", "start", "--workflow-id", wfid,
                          "--crash-at", "execute")
        self.assertEqual(start.returncode, 137, start.stdout + start.stderr)
        self.assertIn("CRASH_SIMULATED", start.stdout)

        resume = self._cli("--phase", "resume", "--workflow-id", wfid)
        self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)
        self.assertIn("state=ACCEPTED", resume.stdout)
        self.assertIn("六锚点 齐全", resume.stdout)

        from michelin.platform.postgres import PostgresEventStore
        store = PostgresEventStore(self.dsn)
        match = re.search(r"run_id\s*:\s*(run_\w+)", resume.stdout)
        self.assertIsNotNone(match, resume.stdout)
        types = [e["type"] for e in store.query_events(match.group(1))]
        self.assertEqual(types.count("intent.received"), 1, "step_ingest 被重跑了")
        self.assertEqual(types.count("task.scoped"), 1, "step_scope 被重跑了")
        self.assertEqual(types.count("worker.model_response"), 1, "模型被重复调用")
        self.assertEqual(types.count("verdict.issued"), 1)
        self.assertTrue(store.verify_chain()[0])
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
