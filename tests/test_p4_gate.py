"""P4 CI 门禁的轻量自检：门禁里的关键函数本身要能被测试、能被证明会红。

完整 10 项门禁由 `python3 ci/gate.py` 运行；这里只覆盖不跑流水线的部分，
保证默认 unittest discover 仍然在十几秒内结束。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "ci"
if str(CI) not in sys.path:
    sys.path.insert(0, str(CI))

import gate  # noqa: E402

from michelin.verification.generic_mutants import ShadowRepo  # noqa: E402
from michelin.verification.generic_skill import generate_generic_skill  # noqa: E402
from michelin.verification.interview import interview  # noqa: E402
from michelin.verification.plan import load_plan  # noqa: E402
from michelin.verification.rules import collect_files, find_ast_grep  # noqa: E402

FIXTURE = ROOT / "fixtures" / "ledger-kit"
HAND_PLAN = ROOT / "plans" / "ledger-kit.json"
GLOBS = ("ledgerkit/**/*.py", "spec/**/*.py")


class TestGateAssets(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p4-gate-"))
        self.plan = load_plan(HAND_PLAN)
        self.skill = generate_generic_skill(
            self.tmp, self.plan, interview(FIXTURE)
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generated_skill_leaks_no_host_knowledge(self) -> None:
        for f in sorted(self.skill.root.rglob("*")):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            for marker in gate.HOST_ONLY_MARKERS:
                self.assertNotIn(marker, text,
                                 f"{f.relative_to(self.skill.root)} 泄漏了 {marker}")

    @unittest.skipUnless(find_ast_grep(), "需要 ast-grep CLI")
    def test_yaml_rules_are_clean_on_baseline_and_fire_on_injection(self) -> None:
        exe = find_ast_grep()
        files = tuple(collect_files(FIXTURE, GLOBS))
        baseline = gate.scan_generated_rules(
            exe, self.skill.root / "rules", FIXTURE, files
        )
        self.assertTrue(baseline["clean"], baseline["findings"])
        self.assertEqual(len(baseline["scanned"]), len(self.plan.rules))

        with ShadowRepo(FIXTURE) as shadow:
            mutation = self.plan.mutations[0]
            shadow.inject(mutation.inject_file, mutation.snippet)
            shadow_files = tuple(collect_files(shadow.root, GLOBS))
            hit = gate.scan_generated_rules(
                exe, self.skill.root / "rules", shadow.root, shadow_files
            )
        self.assertFalse(hit["clean"])
        self.assertTrue(any(f["rule"] == mutation.rule_id for f in hit["findings"]),
                        hit["findings"])

    def test_pr_files_falls_back_to_full_tree_without_git(self) -> None:
        old = os.environ.pop("GITHUB_BASE_REF", None)
        try:
            files, source = gate._pr_files(ROOT, ("src/michelin/**/*.py",))
            self.assertEqual(source, "full-tree")
            self.assertTrue(files)
            self.assertTrue(all(f.endswith(".py") for f in files))
        finally:
            if old is not None:
                os.environ["GITHUB_BASE_REF"] = old

    def test_gate_records_a_failing_check_instead_of_hiding_it(self) -> None:
        g = gate.Gate(fast=True, require_ast_grep=False)

        def broken():
            raise ValueError("deliberate gate failure")

        with contextlib.redirect_stdout(io.StringIO()):
            g._check("deliberate_failure", broken)
        self.assertEqual(len(g.checks), 1)
        self.assertFalse(g.checks[0]["ok"])
        self.assertIn("deliberate gate failure", g.checks[0]["evidence"]["error"])


if __name__ == "__main__":
    unittest.main()
