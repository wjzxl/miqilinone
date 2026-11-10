"""P2 六段式 skill、真实工作区、代码级变异门禁的单元测试。

全部零 pip 依赖，只依赖本机 git 与 python3；跑在临时目录里，不碰工作区数据。
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from _path import SRC  # noqa: F401

from michelin.verification.generator import generate_skill
from michelin.verification.mutants import run_mutation_suite
from michelin.verification.runner import drive_probe, prove_skill
from michelin.verification.skill_spec import FEATURE_MAP_H2, SIX_PHASES, SkillProof
from michelin.workspace.seed import CLI_SOURCE, PRICING_CORRECT, GitWorkspace

GIT_AVAILABLE = shutil.which("git") is not None


@unittest.skipUnless(GIT_AVAILABLE, "需要本机 git")
class TestSkillGenerator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mw-p2-skill-")
        self.root = Path(self.tmp.name)
        self.skill = generate_skill(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_skill_md_has_frontmatter_and_six_sections(self) -> None:
        md = (self.skill.root / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith("---\n"))
        self.assertIn("name: verify-shop", md)
        self.assertIn("disable-model-invocation: true", md)
        for phase in ("Launch", "Doctor", "Drive", "Evidence", "Cleanup", "Helpers"):
            self.assertIn(f"## {phase}", md)
        self.assertNotIn("TODO", md)
        self.assertNotIn("PLACEHOLDER", md)

    def test_helper_is_executable_compilable_and_documented(self) -> None:
        helper = self.skill.root / "helpers" / "verify_helper.py"
        self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
        ast.parse(helper.read_text(encoding="utf-8"))
        md = (self.skill.root / "SKILL.md").read_text(encoding="utf-8")
        for phase in SIX_PHASES:
            self.assertIn(f"verify_helper.py {phase}", md)

    def test_feature_map_shape(self) -> None:
        readme = (self.skill.root / "features" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Never weaken the journey", readme)
        files = sorted(p.name for p in (self.skill.root / "features").glob("*.md"))
        self.assertEqual(len(files), 4)  # README + 3 features
        for f in (self.skill.root / "features").glob("*.md"):
            if f.name == "README.md":
                continue
            body = f.read_text(encoding="utf-8")
            for h2 in FEATURE_MAP_H2:
                self.assertIn(f"## {h2}", body, f"{f.name} 缺 {h2}")

    def test_skill_hash_is_deterministic_and_sensitive(self) -> None:
        again = generate_skill(self.root / "again").skill_hash
        self.assertEqual(again, self.skill.skill_hash)
        weakened = generate_skill(self.root / "weak", weaken_drive=True).skill_hash
        eaten = generate_skill(self.root / "eaten", cleanup_eats_evidence=True).skill_hash
        self.assertNotEqual(weakened, self.skill.skill_hash)
        self.assertNotEqual(eaten, self.skill.skill_hash)
        self.assertNotEqual(weakened, eaten)

    def test_draft_proof_is_not_usable(self) -> None:
        draft = SkillProof(
            skill_hash=self.skill.skill_hash, executed=False, status="DRAFT",
            phases=(), evidence_survived=False, evidence_hash=None,
            stand_in_steps=(), failure_reason="never executed",
        )
        self.assertFalse(draft.usable)


@unittest.skipUnless(GIT_AVAILABLE, "需要本机 git")
class TestWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mw-p2-ws-")
        self.ws = GitWorkspace(Path(self.tmp.name) / "repo")

    def tearDown(self) -> None:
        self.ws.close()
        self.tmp.cleanup()

    def test_baseline_and_worktree_isolation(self) -> None:
        sha = self.ws.seed_baseline()
        self.assertEqual(len(sha), 40)
        a = self.ws.worktree("a")
        b = self.ws.worktree("b")
        self.assertIn("TAX_RATE = 0.10", a.read_file("shop/pricing.py"))
        a.write_file("shop/pricing.py", PRICING_CORRECT)
        self.assertIn("TAX_RATE = 0.10", b.read_file("shop/pricing.py"))
        self.assertEqual(a.head_sha(), b.head_sha())

    def test_diff_is_a_real_unified_patch_that_applies_cleanly(self) -> None:
        self.ws.seed_baseline()
        author = self.ws.worktree("author")
        author.write_file("shop/pricing.py", PRICING_CORRECT)
        patch = author.diff()
        for token in ("diff --git", "--- a/shop/pricing.py", "+++ b/shop/pricing.py",
                      "-TAX_RATE = 0.10", "+TAX_RATE = 0.06"):
            self.assertIn(token, patch)
        verifier = self.ws.worktree("verifier")
        verifier.apply_patch(patch)
        self.assertIn("TAX_RATE = 0.06", verifier.read_file("shop/pricing.py"))

    def test_bad_patch_is_rejected_before_apply(self) -> None:
        self.ws.seed_baseline()
        tree = self.ws.worktree("bad")
        with self.assertRaises(ValueError):
            tree.apply_patch("this is not a patch")
        self.assertIn("TAX_RATE = 0.10", tree.read_file("shop/pricing.py"))


# 这些测试要真跑几十个子进程（本机每个 Python 启动 ~0.3s），默认回归里跳过。
# 完整验证：MW_P2_FULL=1 python3 -m unittest discover -s tests -v
@unittest.skipUnless(GIT_AVAILABLE, "需要本机 git")
@unittest.skipUnless(os.getenv("MW_P2_FULL"), "重型子进程测试，设 MW_P2_FULL=1 启用")
class TestSixPhaseRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mw-p2-run-")
        self.root = Path(self.tmp.name)
        self.ws = GitWorkspace(self.root / "repo")
        self.ws.seed_baseline()
        self.golden = self.ws.worktree("golden")
        self.golden.write_file("shop/pricing.py", PRICING_CORRECT)
        self.golden.write_file("shop/cli.py", CLI_SOURCE)
        self.skill = generate_skill(self.root / "skills")

    def tearDown(self) -> None:
        self.ws.close()
        self.tmp.cleanup()

    def test_prove_is_proven_and_evidence_survives_cleanup(self) -> None:
        proof = prove_skill(self.skill, self.golden.path, self.root / "prove")
        self.assertEqual(proof.status, "PROVEN", proof.failure_reason)
        self.assertTrue(proof.executed)
        self.assertEqual([p.phase for p in proof.phases], list(SIX_PHASES))
        self.assertTrue(all(p.returncode == 0 for p in proof.phases))
        self.assertTrue(proof.evidence_survived)
        self.assertIsNotNone(proof.evidence_hash)
        self.assertTrue((self.root / "prove" / "evidence" / "proof.json").exists())
        self.assertFalse((self.root / "prove" / "instance").exists())

    def test_cleanup_that_eats_evidence_fails_the_proof(self) -> None:
        bad = generate_skill(self.root / "bad-skill", cleanup_eats_evidence=True)
        proof = prove_skill(bad, self.golden.path, self.root / "bad-prove")
        self.assertEqual(proof.status, "FAILED")
        self.assertFalse(proof.evidence_survived)
        self.assertIn("cleanup", proof.failure_reason)
        self.assertFalse(proof.usable)

    def test_failed_iteration_still_runs_cleanup(self) -> None:
        broken = self.ws.worktree("broken")
        broken.write_file("shop/cli.py", CLI_SOURCE.replace('line = f"TOTAL=', 'line = f"BROKEN='))
        ok, phases, executed = drive_probe(self.skill, broken.path, self.root / "broken-run")
        self.assertFalse(ok)
        self.assertTrue(executed)
        self.assertIn("cleanup", [p.phase for p in phases])
        self.assertFalse((self.root / "broken-run" / "instance").exists())


@unittest.skipUnless(GIT_AVAILABLE, "需要本机 git")
@unittest.skipUnless(os.getenv("MW_P2_FULL"), "重型子进程测试，设 MW_P2_FULL=1 启用")
class TestMutationSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mw-p2-mut-")
        self.root = Path(self.tmp.name)
        self.ws = GitWorkspace(self.root / "repo")
        self.ws.seed_baseline()

    def tearDown(self) -> None:
        self.ws.close()
        self.tmp.cleanup()

    def test_every_mutant_fires_the_drive_gate(self) -> None:
        skill = generate_skill(self.root / "skills")
        report = run_mutation_suite(skill, self.ws, self.root / "mut")
        self.assertTrue(report.baseline_green)
        self.assertEqual(len(report.cases), 5)
        self.assertTrue(all(c.drove for c in report.cases))
        self.assertTrue(report.all_fired, report.to_dict())
        for c in report.cases:
            self.assertIn("DRIVE_FAIL", c.stdout)

    def test_weakened_gate_is_caught_by_mutation_suite(self) -> None:
        skill = generate_skill(self.root / "skills-weak", weaken_drive=True)
        report = run_mutation_suite(skill, self.ws, self.root / "mut-weak")
        self.assertTrue(report.baseline_green)
        self.assertFalse(report.all_fired)
        self.assertEqual(sum(c.fired for c in report.cases), 0)
        self.assertTrue(all("DRIVE_OK" in c.stdout for c in report.cases))


if __name__ == "__main__":
    unittest.main(verbosity=2)
