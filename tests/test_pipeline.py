"""端到端：四个场景 + append-only / 幂等 / 状态机 / 裁决作废的结构性保证。"""

from __future__ import annotations

import json
import sqlite3
import unittest

import _path  # noqa: F401

from michelin.chief.policy import (
    EvidenceSpec, StateError, TaskIntent, TaskState, assert_transition,
)
from michelin.chief.workflow import ChiefOfStaff
from michelin.context.sources import SourcePlane
from michelin.delivery.replay import replay
from michelin.models import ScriptedModel
from michelin.platform.envelope import make_envelope
from michelin.platform.store import EventStore


def make_chief(script: list[str], *, max_rework: int = 2):
    store = EventStore(":memory:")
    worker_model = ScriptedModel("anthropic", "claude-stub-p0", script)
    verifier_model = ScriptedModel("openai", "gpt-stub-p0", ["PASS"])
    chief = ChiefOfStaff(
        store, SourcePlane(store), worker_model, verifier_model, max_rework=max_rework
    )
    return store, chief, worker_model, verifier_model


def sample_intent() -> TaskIntent:
    return TaskIntent(
        intent="只读分析：归纳一手材料结论，结论必须可追溯到 source_hash",
        source_bodies=("原文 A：验证循环要做成工程门禁。", "原文 B：出错改环境规则，不改提示词。"),
    )


class TestEndToEnd(unittest.TestCase):
    def test_ok_one_shot_pass_and_replayable(self) -> None:
        store, chief, w, v = make_chief(["EVIDENCE:ok 只读分析完成，引用 source_hash。"])
        result = chief.handle(sample_intent())
        self.assertEqual(result.state, TaskState.ACCEPTED)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.attempts, 1)
        self.assertNotEqual(w.family, v.family)

        view = replay(store, result.run_id)
        self.assertTrue(view.complete, f"锚点缺失: {view.anchors}")
        self.assertTrue(view.chain_ok)
        self.assertEqual(view.decision["author_family"], "anthropic")
        self.assertEqual(view.decision["verifier_family"], "openai")
        self.assertEqual(view.decision["result"], "PASS")
        self.assertIn("INDEPENDENT_PASS", view.decision["independent_test_output"])
        self.assertIn("WORKER_SELF_REPORT", view.decision["self_reported_discarded"])
        store.close()

    def test_flaky_rework_uses_fresh_worker(self) -> None:
        store, chief, _, _ = make_chief([
            "草稿：缺少证据标记。",
            "EVIDENCE:ok 返工后补齐证据标记。",
        ])
        result = chief.handle(sample_intent())
        self.assertEqual(result.state, TaskState.ACCEPTED)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.attempts, 2)
        types = [e["type"] for e in store.query_events(result.run_id)]
        self.assertIn("rework.requested", types)
        self.assertEqual(types.count("verdict.issued"), 2)
        # 两次派发必须是两个不同的 worker_run_id（全新上下文，不复用失败会话）
        dispatched = [
            json.loads(e["payload_json"])["worker_run_id"]
            for e in store.query_events(result.run_id) if e["type"] == "worker.dispatched"
        ]
        self.assertEqual(len(set(dispatched)), 2)
        store.close()

    def test_rework_budget_exhaustion_escalates_to_human(self) -> None:
        store, chief, _, _ = make_chief(["草稿：始终不写证据标记。"])
        result = chief.handle(sample_intent())
        self.assertEqual(result.state, TaskState.NEEDS_HUMAN)
        self.assertEqual(result.verdict, "FAIL")
        self.assertEqual(result.attempts, 3)  # 1 次首跑 + 2 次返工
        types = [e["type"] for e in store.query_events(result.run_id)]
        self.assertIn("human.escalated", types)
        self.assertNotIn("artifact.delivered", types)
        store.close()

    def test_sampling_reject_produces_an_enabled_guardrail_rule(self) -> None:
        store, chief, _, _ = make_chief(["EVIDENCE:ok 只读分析完成。"])
        result = chief.handle(
            sample_intent(), sampling_outcome="REJECT",
            sampling_root_cause="missing_evidence_marker",
        )
        self.assertEqual(result.state, TaskState.RULE_UPDATED)
        self.assertIsNotNone(result.rule_ref)
        rules = store.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(bool(rules[0]["enabled"]), True)
        self.assertTrue(rules[0]["mutation_test_ref"])
        types = [e["type"] for e in store.query_events(result.run_id)]
        self.assertIn("guardrail.rule_added", types)
        store.close()

    def test_reject_without_root_cause_is_refused(self) -> None:
        store, chief, _, _ = make_chief(["EVIDENCE:ok 只读分析完成。"])
        with self.assertRaises(ValueError):
            chief.handle(sample_intent(), sampling_outcome="REJECT", sampling_root_cause="模型不行")
        store.close()


class TestStateMachine(unittest.TestCase):
    def test_illegal_transition_is_blocked(self) -> None:
        with self.assertRaises(StateError):
            assert_transition(TaskState.SCOPED, TaskState.DELIVERED)
        assert_transition(TaskState.VERIFYING, TaskState.VERDICT_PASS)  # 合法路径不抛错

    def test_empty_evidence_spec_cannot_be_dispatched(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceSpec(())


class TestInvariants(unittest.TestCase):
    def test_inbox_dedupe_is_atomic(self) -> None:
        store = EventStore(":memory:")
        self.assertTrue(store.put_inbox_event("evt-1", "github", True, {"a": 1}))
        self.assertFalse(store.put_inbox_event("evt-1", "github", True, {"a": 1}))
        self.assertEqual(store.counts()["inbox_event"], 1)
        store.close()

    def test_append_only_tables_reject_update_and_delete(self) -> None:
        store = EventStore(":memory:")
        store.emit(make_envelope(
            run_id="r", task_id="t", actor="a", role="r", event_type="x",
        ), {"k": "v"})
        store.put_source_document("h1", "sys", "uri", b"raw", "2026-11-09T00:00:00Z")
        store.put_artifact("a1", "r", "p", "c")
        store.put_verdict({
            "verdict_id": "v1", "run_id": "r", "verifier_run_id": "rv",
            "head_sha": "a1", "checks": [], "evidence_refs": [], "result": "PASS",
        })
        for sql in (
            "UPDATE event SET payload_json='{}' WHERE seq=1",
            "DELETE FROM event WHERE seq=1",
            "UPDATE verdict SET result='FAIL' WHERE verdict_id='v1'",
            "DELETE FROM source_document WHERE source_hash='h1'",
            "UPDATE artifact SET path='x' WHERE artifact_hash='a1'",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                store.conn.execute(sql)
        store.close()

    def test_idempotency_key_does_not_duplicate_events(self) -> None:
        store = EventStore(":memory:")
        env = make_envelope(run_id="r", task_id="t", actor="a", role="r", event_type="x")
        first = store.emit(env, {"k": "v"})
        second = store.emit(env, {"k": "v"})
        self.assertEqual(first, second)
        self.assertEqual(store.counts()["event"], 1)
        self.assertTrue(store.verify_chain()[0])
        store.close()

    def test_hash_chain_detects_tampering_even_if_trigger_is_dropped(self) -> None:
        store, chief, _, _ = make_chief(["EVIDENCE:ok 只读分析完成。"])
        result = chief.handle(sample_intent())
        self.assertTrue(store.verify_chain()[0])
        # 模拟攻击者拿到 DB 权限并绕过了 append-only trigger
        store.conn.execute("DROP TRIGGER trg_event_no_update")
        store.conn.execute(
            "UPDATE event SET hash_chain='deadbeef' WHERE seq=1"
        )
        ok, _checked, bad = store.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, 1)
        self.assertGreater(store.counts()["event"], 10)
        self.assertIsNotNone(result.run_id)
        store.close()

    def test_new_push_invalidates_old_verdict_by_append_only(self) -> None:
        store, chief, _, _ = make_chief(["EVIDENCE:ok 只读分析完成。"])
        result = chief.handle(sample_intent())
        head = result.artifact_hash or ""
        before = store.active_verdict(head)
        self.assertIsNotNone(before)
        count_before = store.counts()["verdict"]
        chief.verifier.invalidate(head, result.run_id, "新 push，head_sha 已变")
        self.assertIsNone(store.active_verdict(head))
        self.assertEqual(store.counts()["verdict"], count_before + 1)
        self.assertTrue(store.verify_chain()[0])
        store.close()

    def test_same_model_family_cannot_build_a_chief(self) -> None:
        store = EventStore(":memory:")
        with self.assertRaises(RuntimeError):
            ChiefOfStaff(
                store, SourcePlane(store),
                ScriptedModel("anthropic", "a", ["x"]),
                ScriptedModel("anthropic", "b", ["PASS"]),
            )
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
