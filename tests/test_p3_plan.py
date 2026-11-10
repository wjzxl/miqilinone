"""P3 计划与访谈的契约测试（轻量，默认回归必跑）。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.verification.generic_skill import (  # noqa: E402
    FEATURE_H2, SIX_PHASES, generate_generic_skill,
)
from michelin.verification.interview import (  # noqa: E402
    describe, ground_plan, interview,
)
from michelin.verification.plan import (  # noqa: E402
    Command, RuleMutation, VerificationPlan, dump_plan, load_plan,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "plans" / "michelin-self.json"


class TestPlanContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = load_plan(PLAN_PATH)

    def test_fingerprint_is_deterministic_and_content_bound(self) -> None:
        self.assertEqual(self.plan.fingerprint(), load_plan(PLAN_PATH).fingerprint())
        changed = replace(self.plan, surface="another surface")
        self.assertNotEqual(self.plan.fingerprint(), changed.fingerprint())

    def test_json_roundtrip_preserves_every_field(self) -> None:
        raw = json.loads(dump_plan(self.plan))
        self.assertEqual(VerificationPlan.from_dict(raw).fingerprint(),
                         self.plan.fingerprint())

    def test_every_rule_has_a_mutation(self) -> None:
        self.assertEqual({m.rule_id for m in self.plan.mutations},
                         {r.id for r in self.plan.rules})

    def test_validate_rejects_rule_without_mutation(self) -> None:
        broken = replace(self.plan, mutations=self.plan.mutations[:-1])
        with self.assertRaisesRegex(ValueError, "没有变异证明"):
            VerificationPlan.from_dict(broken.to_dict())

    def test_validate_rejects_shell_metacharacter(self) -> None:
        broken = replace(self.plan, launch=Command(("python3", "&&", "rm -rf /")))
        with self.assertRaisesRegex(ValueError, "shell 元字符"):
            VerificationPlan.from_dict(broken.to_dict())

    def test_validate_rejects_unknown_rule_kind_and_empty_journeys(self) -> None:
        raw = self.plan.to_dict()
        raw["rules"][0]["kind"] = "regex_contains"
        with self.assertRaisesRegex(ValueError, "未知规则类型"):
            VerificationPlan.from_dict(raw)
        raw = self.plan.to_dict()
        raw["journeys"] = []
        with self.assertRaisesRegex(ValueError, "至少要有一条 journey"):
            VerificationPlan.from_dict(raw)


class TestRepoInterview(unittest.TestCase):
    def test_interview_is_deterministic_and_ignores_mtime(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="mw-p3-intv-"))
        try:
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            first = interview(root)
            self.assertEqual(first.languages, {"python": 1})
            os.utime(root / "pkg" / "a.py", (1_000_000_000, 1_000_000_000))
            self.assertEqual(interview(root).fingerprint, first.fingerprint)
            self.assertEqual(first.modules[0].classes, ("A",))
            (root / "pkg" / "a.py").write_text("class B:\n    pass\n", encoding="utf-8")
            self.assertNotEqual(interview(root).fingerprint, first.fingerprint)
            self.assertIn("fingerprint", describe(first))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_interview_ignores_venv_and_generated_data(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="mw-p3-ign-"))
        try:
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            for noisy in (".venv", "data", "__pycache__"):
                (root / noisy).mkdir()
                (root / noisy / "noise.py").write_text("y = 2\n", encoding="utf-8")
            profile = interview(root)
            self.assertEqual(profile.tracked_files, ("src/a.py",))
            self.assertEqual(profile.languages, {"python": 1})
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_ground_plan_detects_missing_targets(self) -> None:
        profile = interview(ROOT)
        plan = load_plan(PLAN_PATH)
        self.assertEqual(ground_plan(profile, plan), ())
        broken = replace(plan, mutations=(
            RuleMutation(plan.rules[0].id, "tests/__missing__.py", "eval('1')"),
            *plan.mutations[1:],
        ))
        problems = ground_plan(profile, broken)
        self.assertTrue(any("不存在" in p for p in problems), problems)


class TestGenericGeneration(unittest.TestCase):
    def test_generated_skill_has_no_target_app_hardcoding(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="mw-p3-gen-"))
        try:
            profile = interview(ROOT)
            plan = load_plan(PLAN_PATH)
            skill = generate_generic_skill(root, plan, profile)
            helper = (skill.root / "helpers" / "verify_helper.py").read_text(encoding="utf-8")
            md = (skill.root / "SKILL.md").read_text(encoding="utf-8")
            for forbidden in ("shop.cli", "TOTAL=31.80", "TAX_RATE = 0.06", "receipt.json"):
                self.assertNotIn(forbidden, helper)
            for phase in SIX_PHASES:
                self.assertIn(f"## {phase.capitalize()}", md)
                self.assertIn("verify_helper.py " + phase, md)
            for h2 in FEATURE_H2:
                self.assertIn("## " + h2, (skill.root / "features" /
                                           f"{plan.features[0].name}.md").read_text(
                                               encoding="utf-8"))
            self.assertEqual(len(list((skill.root / "rules").glob("*.yml"))), len(plan.rules))
            embedded = json.loads((skill.root / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(VerificationPlan.from_dict(embedded).fingerprint(),
                             plan.fingerprint())
            self.assertIn("plan.json", skill.files)
            self.assertEqual(skill.skill_hash,
                             generate_generic_skill(root, plan, profile).skill_hash)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
