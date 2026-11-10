"""P3 端到端门控测试（重型：要真跑生成的 helper 与 ast-grep，按 MW_P3_FULL 门控）。

默认回归只跑 test_p3_plan.py / test_p3_rules.py 里的契约与规则测试；
完整验证用 `MW_P3_FULL=1 .venv/bin/python -m unittest discover -s tests -v`。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.chief.p3_workflow import P3Pipeline, P3Scenario  # noqa: E402
from michelin.chief.policy import TaskIntent  # noqa: E402
from michelin.context.sources import SourcePlane  # noqa: E402
from michelin.delivery.replay import replay  # noqa: E402
from michelin.models import ScriptedModel  # noqa: E402
from michelin.platform.envelope import content_hash  # noqa: E402
from michelin.platform.store import EventStore  # noqa: E402
from michelin.verification.plan import load_plan  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FULL = os.getenv("MW_P3_FULL") == "1"

INTENT = TaskIntent(
    intent="为指定仓库生成一套可配置、经变异证明的六段式验证门禁并独立裁决",
    source_bodies=(
        "【需求原文】验证装置不得内置目标应用知识；命令、旅程、规则全部来自仓库访谈与验证计划。",
        "【治理原文】结构化规则需双后端对拍；每条规则要经故意违规证明会开火。",
    ),
    source_system="test-p3",
)


@unittest.skipUnless(FULL, "重型：MW_P3_FULL=1 才跑生成的 helper / ast-grep 全链路")
class TestP3Pipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p3-wf-"))
        self.store = EventStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pipeline(self, *, backend: str = "python-ast", plan=None) -> P3Pipeline:
        return P3Pipeline(
            self.store, SourcePlane(self.store),
            ScriptedModel("openai", "gpt-stub-p3-test", ["PASS"]),
            ROOT, plan or load_plan(ROOT / "plans" / "michelin-self.json"),
            self.tmp, backend=backend,
        )

    def test_ok_delivers_with_complete_replay_chain(self) -> None:
        result = self._pipeline().handle(INTENT, P3Scenario())
        self.assertEqual(result.state.value, "ACCEPTED")
        self.assertEqual(result.verdict, "PASS")
        self.assertTrue(result.rules_all_fired)
        self.assertTrue(result.cross_checked)
        self.assertEqual(result.skill_status, "PROVEN")
        self.assertIsNotNone(result.skill_root)
        self.assertTrue(result.skill_root.is_dir())
        self.assertTrue((result.skill_root / "plan.json").is_file())
        view = replay(self.store, result.run_id)
        self.assertTrue(view.complete, view.anchors)
        self.assertTrue(view.chain_ok)
        self.assertTrue(self.store.active_verdict(result.head_sha))
        types = [t["type"] for t in view.timeline]
        for required in ("repo.interviewed", "plan.grounded", "skill.proven",
                         "rules.scanned", "rules.mutants_tested", "verdict.issued"):
            self.assertIn(required, types)
        # head_sha 的显式重算与「改仓库即失效」在 test_head_sha_binds_... 里验证。

    def test_four_gates_block_before_any_verdict(self) -> None:
        for name, scenario in (
            ("unproven", P3Scenario(cleanup_eats_evidence=True)),
            ("blind_gate", P3Scenario(weaken_rules=True)),
            ("unbacked_plan", P3Scenario(unbacked_plan=True)),
            ("backend_disagree", P3Scenario(force_backend_disagreement=True)),
        ):
            with self.subTest(gate=name):
                store = EventStore(":memory:")
                workdir = self.tmp / name
                workdir.mkdir(parents=True, exist_ok=True)
                pipeline = P3Pipeline(
                    store, SourcePlane(store),
                    ScriptedModel("openai", "gpt-stub-p3-test", ["PASS"]),
                    ROOT, load_plan(ROOT / "plans" / "michelin-self.json"), workdir,
                )
                result = pipeline.handle(INTENT, scenario)
                self.assertEqual(result.state.value, "BLOCKED_NEEDS_HUMAN")
                self.assertIsNone(result.verdict)
                self.assertTrue(result.blocked_reason)
                view = replay(store, result.run_id)
                types = [t["type"] for t in view.timeline]
                self.assertNotIn("verdict.issued", types)
                self.assertNotIn("artifact.produced", types)
                self.assertNotIn("artifact.delivered", types)
                self.assertIn("human.escalated", types)
                self.assertTrue(view.chain_ok)
                store.close()

    def test_head_sha_binds_repo_plan_skill_and_mutations(self) -> None:
        p = self._pipeline()
        r1 = p.handle(INTENT, P3Scenario())
        p2 = P3Pipeline(
            self.store, SourcePlane(self.store),
            ScriptedModel("openai", "gpt-stub-p3-test", ["PASS"]),
            ROOT, load_plan(ROOT / "plans" / "michelin-self.json"), self.tmp,
        )
        r2 = p2.handle(INTENT, P3Scenario())
        self.assertEqual(r1.head_sha, r2.head_sha)  # 确定性
        rows = self.store.query_events(r1.run_id)
        import json as _json
        mut = next(_json.loads(x["payload_json"])["report_hash"]
                   for x in rows if x["type"] == "rules.mutants_tested")
        recomputed = content_hash({
            "repo": r1.repo_fingerprint, "plan": r1.plan_fingerprint,
            "skill": r1.skill_hash, "backend": r1.backend, "mutations": mut,
        })
        self.assertEqual(r1.head_sha, recomputed)
        changed = content_hash({
            "repo": "DIFFERENT_REPO_CONTENT", "plan": r1.plan_fingerprint,
            "skill": r1.skill_hash, "backend": r1.backend, "mutations": mut,
        })
        self.assertNotEqual(r1.head_sha, changed)

    def test_sampling_reject_reflows_environment_rule(self) -> None:
        result = self._pipeline().handle(INTENT, P3Scenario(
            sampling_outcome="REJECT", sampling_root_cause="missing_evidence_marker",
        ))
        self.assertEqual(result.state.value, "RULE_UPDATED")
        self.assertEqual(result.verdict, "PASS")
        self.assertIsNotNone(result.rule_ref)
        self.assertGreater(self.store.counts()["guardrail_rule"], 0)


if __name__ == "__main__":
    unittest.main()
