"""只读回放：证明「能从 events 表按 run_id 还原这次决策」这个 P0 退出判据。"""

from __future__ import annotations

import unittest

import _path  # noqa: F401

from michelin.chief.policy import TaskIntent
from michelin.chief.workflow import ChiefOfStaff
from michelin.context.sources import SourcePlane
from michelin.delivery.replay import ANCHORS, format_replay, replay
from michelin.models import ScriptedModel
from michelin.platform.store import EventStore


class TestReplay(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore(":memory:")
        self.worker_model = ScriptedModel("anthropic", "claude-stub-p0", [
            "EVIDENCE:ok 只读分析完成，引用 source_hash。"
        ])
        self.verifier_model = ScriptedModel("openai", "gpt-stub-p0", ["PASS"])
        self.chief = ChiefOfStaff(
            self.store, SourcePlane(self.store), self.worker_model, self.verifier_model
        )
        self.result = self.chief.handle(TaskIntent(
            intent="只读分析：归纳一手材料结论",
            source_bodies=("原文 A：验证循环要做成工程门禁。", "原文 B：出错改环境规则。"),
        ))

    def tearDown(self) -> None:
        self.store.close()

    def test_all_six_anchors_present(self) -> None:
        view = replay(self.store, self.result.run_id)
        self.assertTrue(view.complete, f"缺失: {[k for k, v in view.anchors.items() if not v]}")
        self.assertEqual(set(view.anchors), set(ANCHORS))

    def test_decision_carries_full_causal_chain(self) -> None:
        d = replay(self.store, self.result.run_id).decision
        for key in (
            "intent", "source_hashes", "evidence_spec", "author_model", "author_family",
            "prompt_hash", "artifact_hash", "result", "verifier_family", "checks",
            "independent_test_output", "self_reported_discarded", "sampling", "state_path",
        ):
            self.assertIn(key, d, f"因果链缺字段: {key}")
        self.assertEqual(len(d["source_hashes"]), 2)
        self.assertEqual(d["result"], "PASS")
        self.assertNotEqual(d["author_family"], d["verifier_family"])
        self.assertEqual(d["state_path"][0], "NORMALIZED")
        self.assertEqual(d["state_path"][-1], "ACCEPTED")

    def test_replay_is_readonly_and_never_calls_a_model(self) -> None:
        worker_calls = self.worker_model.calls
        verifier_calls = self.verifier_model.calls
        events_before = self.store.counts()["event"]
        replay(self.store, self.result.run_id)
        replay(self.store, self.result.run_id)
        self.assertEqual(self.worker_model.calls, worker_calls)
        self.assertEqual(self.verifier_model.calls, verifier_calls)
        self.assertEqual(self.store.counts()["event"], events_before)
        self.assertTrue(self.store.verify_chain()[0])

    def test_source_documents_are_raw_and_immutable(self) -> None:
        view = replay(self.store, self.result.run_id)
        for h in view.decision["source_hashes"]:
            doc = self.store.get_source_document(h)
            self.assertIsNotNone(doc)
            self.assertTrue(doc["raw_bytes"].startswith("原文".encode("utf-8")))
            self.assertIn("occurred_at", doc)
            self.assertIn("ingested_at", doc)

    def test_formatted_replay_is_human_readable(self) -> None:
        text = format_replay(replay(self.store, self.result.run_id))
        for needle in ("因果链回放", "锚点", "时间线", "决策摘要", "verdict.issued", "INDEPENDENT_PASS"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
