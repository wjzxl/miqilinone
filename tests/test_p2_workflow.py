"""P2 全链路测试：阻断条件必须在派发前生效；裁决必须绑定 diff 与门禁版本。"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from _path import SRC  # noqa: F401

from michelin.chief.p2_workflow import P2Pipeline, P2Scenario
from michelin.chief.policy import TaskIntent
from michelin.context.sources import SourcePlane
from michelin.delivery.replay import replay
from michelin.models import ScriptedModel
from michelin.platform.envelope import content_hash
from michelin.platform.store import EventStore
from michelin.verification.behavior import BehaviorVerifier
from michelin.verification.holdout import FORBIDDEN_FIELDS

GIT_AVAILABLE = shutil.which("git") is not None


def _scenario(rates=("0.06",), **kw) -> P2Scenario:
    return P2Scenario(worker_rates=rates, **kw)


# 每个场景要真跑六段式 + 5 个代码变体 + git apply，几十个子进程。
# 默认回归跳过；完整验证：MW_P2_FULL=1 python3 -m unittest discover -s tests -v
@unittest.skipUnless(GIT_AVAILABLE, "需要本机 git")
@unittest.skipUnless(os.getenv("MW_P2_FULL"), "重型端到端测试，设 MW_P2_FULL=1 启用")
class TestP2Pipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mw-p2-pipe-")
        self.root = Path(self.tmp.name)
        self.store = EventStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _run(self, scenario: P2Scenario):
        pipeline = P2Pipeline(
            self.store, SourcePlane(self.store),
            ScriptedModel("anthropic", "claude-p2-test", ["TAX_RATE = 0.06"]),
            ScriptedModel("openai", "gpt-p2-test", ["PASS"]),
            self.root / "runs", max_rework=2,
        )
        intent = TaskIntent(
            intent="修复税率：total 10 20 应输出 TOTAL=31.80，receipt 与 stdout 一致",
            source_bodies=("一手原文：税率 6%。", "治理原文：门禁必须经变异证明。"),
            source_system="unittest-p2",
        )
        return pipeline.handle(intent, scenario)

    def _types(self, run_id: str) -> list[str]:
        return [r["type"] for r in self.store.query_events(run_id)]

    def _payload(self, run_id: str, event_type: str) -> dict:
        for row in self.store.query_events(run_id):
            if row["type"] == event_type:
                return json.loads(row["payload_json"])
        return {}

    def test_ok_path_is_accepted_with_full_evidence(self) -> None:
        r = self._run(_scenario())
        self.assertEqual(r.state.value, "ACCEPTED")
        self.assertEqual(r.verdict, "PASS")
        self.assertEqual(r.attempts, 1)
        self.assertEqual(r.skill_status, "PROVEN")
        self.assertTrue(r.mutants_all_fired)
        self.assertEqual(r.blocked_reason, "")

        view = replay(self.store, r.run_id)
        self.assertTrue(view.complete)
        self.assertTrue(view.chain_ok)
        types = self._types(r.run_id)
        for t in ("skill.generated", "skill.proven", "skill.mutants_tested",
                  "artifact.produced", "verdict.issued", "artifact.delivered"):
            self.assertIn(t, types)

        patch = self._payload(r.run_id, "artifact.produced")["patch"]
        self.assertIn("diff --git", patch)
        self.assertIn("+TAX_RATE = 0.06", patch)
        verdict_payload = self._payload(r.run_id, "verdict.issued")
        self.assertEqual(verdict_payload["skill_hash"], r.skill_hash)
        self.assertTrue(verdict_payload["mutations"]["all_fired"])
        self.assertIn("INDEPENDENT_PASS", verdict_payload["independent_test_output"])
        self.assertTrue(verdict_payload["evidence_hash"])

    def test_blind_gate_blocks_before_dispatch(self) -> None:
        r = self._run(_scenario(weaken_drive=True))
        self.assertEqual(r.state.value, "BLOCKED_NEEDS_HUMAN")
        self.assertEqual(r.attempts, 0)
        self.assertFalse(r.mutants_all_fired)
        self.assertIn("门禁形同虚设", r.blocked_reason)
        types = self._types(r.run_id)
        self.assertNotIn("worker.dispatched", types)
        self.assertNotIn("artifact.produced", types)
        self.assertNotIn("verdict.issued", types)
        self.assertIn("human.escalated", types)
        self.assertEqual(self._payload(r.run_id, "human.escalated")["stage"], "pre-dispatch")
        self.assertTrue(self.store.verify_chain()[0])

    def test_cleanup_eating_evidence_blocks_before_dispatch(self) -> None:
        r = self._run(_scenario(cleanup_eats_evidence=True))
        self.assertEqual(r.state.value, "BLOCKED_NEEDS_HUMAN")
        self.assertEqual(r.skill_status, "FAILED")
        self.assertIn("cleanup", r.blocked_reason)
        self.assertNotIn("worker.dispatched", self._types(r.run_id))
        prove = self._payload(r.run_id, "skill.proven")
        self.assertFalse(prove["evidence_survived"])

    def test_wrong_fix_reworks_with_a_fresh_worker(self) -> None:
        r = self._run(_scenario(("0.08", "0.06")))
        self.assertEqual(r.state.value, "ACCEPTED")
        self.assertEqual(r.verdict, "PASS")
        self.assertEqual(r.attempts, 2)
        rows = self.store.query_events(r.run_id)
        dispatched = [json.loads(x["payload_json"])["worker_run_id"]
                      for x in rows if x["type"] == "worker.dispatched"]
        self.assertEqual(len(dispatched), 2)
        self.assertNotEqual(dispatched[0], dispatched[1])
        self.assertIn("rework.requested", self._types(r.run_id))
        self.assertEqual(sum(1 for x in rows if x["type"] == "verdict.issued"), 2)

    def test_always_fail_escalates_without_delivery(self) -> None:
        r = self._run(_scenario(("0.09",)))
        self.assertEqual(r.state.value, "NEEDS_HUMAN")
        self.assertEqual(r.verdict, "FAIL")
        self.assertEqual(r.attempts, 3)
        types = self._types(r.run_id)
        self.assertIn("human.escalated", types)
        self.assertNotIn("artifact.delivered", types)
        self.assertTrue(replay(self.store, r.run_id).complete)

    def test_sampling_reject_produces_an_enabled_rule(self) -> None:
        r = self._run(_scenario(sampling_outcome="REJECT",
                                sampling_root_cause="verification_rule_gap"))
        self.assertEqual(r.state.value, "RULE_UPDATED")
        self.assertIsNotNone(r.rule_ref)
        rules = self.store.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertTrue(bool(rules[0]["enabled"]))
        self.assertTrue(rules[0]["mutation_test_ref"])

    def test_head_sha_binds_both_diff_and_skill_hash(self) -> None:
        """head_sha = hash(diff + skill_hash + 基线内容指纹)，改任一项都要重裁。"""
        r1 = self._run(_scenario())
        recomputed = content_hash({
            "diff": self._payload(r1.run_id, "artifact.produced")["patch"],
            "skill_hash": r1.skill_hash,
            "baseline": self._payload(r1.run_id, "intent.received")["baseline_fingerprint"],
        })
        self.assertEqual(recomputed, r1.head_sha)
        from michelin.verification.generator import generate_skill
        other_skill_hash = generate_skill(self.root / "other-skills",
                                          weaken_drive=True).skill_hash
        changed = content_hash({
            "diff": self._payload(r1.run_id, "artifact.produced")["patch"],
            "skill_hash": other_skill_hash,
            "baseline": self._payload(r1.run_id, "intent.received")["baseline_fingerprint"],
        })
        self.assertNotEqual(changed, r1.head_sha)
        changed_diff = content_hash({
            "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
            "skill_hash": r1.skill_hash,
            "baseline": self._payload(r1.run_id, "intent.received")["baseline_fingerprint"],
        })
        self.assertNotEqual(changed_diff, r1.head_sha)


class TestBehaviorVerifierShape(unittest.TestCase):
    def test_only_bundle_is_positional_and_no_author_narration(self) -> None:
        tree = ast.parse(textwrap.dedent(inspect.getsource(BehaviorVerifier.verify)))
        func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
        self.assertEqual([a.arg for a in func.args.args], ["self", "bundle"])
        self.assertIsNone(func.args.vararg)
        self.assertIsNone(func.args.kwarg)
        names = {a.arg for a in func.args.args + func.args.kwonlyargs}
        self.assertTrue(names.isdisjoint(FORBIDDEN_FIELDS))
        for banned in ("plan", "transcript", "description", "notes", "title"):
            self.assertFalse(any(banned in n for n in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
