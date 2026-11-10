"""P5 真实 git PR diff 的轻量契约测试。

这里用的是真的 `git init` / commit / branch，不是伪造的 diff 文本：NUL 文件名、
重命名、删除、head tree 判定、固定时间戳带来的可复现 commit SHA 都要锁住。
完整的「增量扫描 + 违规判红」由 ci/gate.py 的 pr_diff_gate 端到端验证。
"""

from __future__ import annotations

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
from pr_diff import export_tree, is_git_repo, read_pr_diff, resolve_base  # noqa: E402
from pr_fixture import BRANCHES, ensure_demo_repo  # noqa: E402
from michelin.verification.rules import find_ast_grep  # noqa: E402

RULE_YAML = """id: no-eval-in-spine
language: Python
severity: error
message: dynamic eval is forbidden
rule:
  pattern: eval($$$ARGS)
"""


class TestPRDiff(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p5-prdiff-"))
        self.repo = ensure_demo_repo(self.tmp / "demo")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_is_a_real_git_repo_with_four_branches(self) -> None:
        self.assertTrue(is_git_repo(self.repo.root))
        self.assertEqual(set(self.repo.shas), set(BRANCHES))
        self.assertTrue(all(len(sha) == 40 for sha in self.repo.shas.values()))

    def test_fixed_metadata_makes_commits_reproducible(self) -> None:
        second = ensure_demo_repo(self.tmp / "demo-copy")
        self.assertEqual(self.repo.shas, second.shas)

    def test_three_way_diff_handles_rename_delete_and_ignores_non_python(self) -> None:
        clean = read_pr_diff(self.repo.root, base="main", head="pr-clean")
        self.assertEqual(clean.python_files, ("feature.py",))
        self.assertIn("README.md", [c.path for c in clean.changed])

        renamed = read_pr_diff(self.repo.root, base="main", head="pr-rename")
        self.assertEqual(renamed.python_files, ("renamed.py",))
        self.assertTrue(any(c.status.startswith("R") and c.old_path == "app.py"
                            for c in renamed.changed))
        self.assertTrue(any(c.status.startswith("D") and c.path == "legacy.py"
                            for c in renamed.changed))

    def test_head_tree_not_worktree_decides_file_existence(self) -> None:
        """checkout 停在 main 时，pr-violation 的 evil.py 不在磁盘上，但必须在 diff 里。"""
        diff = read_pr_diff(self.repo.root, base="main", head="pr-violation")
        self.assertEqual(diff.python_files, ("evil.py",))
        self.assertFalse((self.repo.root / "evil.py").exists())
        out = diff.materialize(self.tmp / "export")
        self.assertIn("eval(", (out / "evil.py").read_text(encoding="utf-8"))

    def test_base_resolution_via_environment_and_missing_repo(self) -> None:
        self.assertIsNone(read_pr_diff(self.tmp, base="main"))
        old = os.environ.get("MW_PR_BASE")
        os.environ["MW_PR_BASE"] = "main"
        try:
            diff = read_pr_diff(self.repo.root, head="pr-violation")
            self.assertEqual(diff.base_ref, "main")
            self.assertEqual(resolve_base(self.repo.root, "main"), "main")
        finally:
            if old is None:
                os.environ.pop("MW_PR_BASE", None)
            else:
                os.environ["MW_PR_BASE"] = old

    def test_github_actions_base_ref_resolves_origin_remote(self) -> None:
        """workflow 用显式 refspec 造出 origin/<base>；解析器必须认得这条路径。"""
        import subprocess
        subprocess.run(
            ["git", "-C", str(self.repo.root), "update-ref",
             "refs/remotes/origin/main", self.repo.shas["main"]],
            check=True, capture_output=True, timeout=30,
        )
        old_base = os.environ.pop("MW_PR_BASE", None)
        old_gh = os.environ.get("GITHUB_BASE_REF")
        os.environ["GITHUB_BASE_REF"] = "main"
        try:
            diff = read_pr_diff(self.repo.root, head="pr-violation")
            self.assertEqual(diff.base_ref, "origin/main")
            self.assertEqual(diff.python_files, ("evil.py",))
        finally:
            os.environ.pop("GITHUB_BASE_REF", None)
            if old_gh is not None:
                os.environ["GITHUB_BASE_REF"] = old_gh
            if old_base is not None:
                os.environ["MW_PR_BASE"] = old_base

    def test_zero_python_changes_does_not_fall_back_to_full_tree(self) -> None:
        """只改文档的 PR 也应走增量路径（0 文件），不能被误当成「没有 PR」。"""
        old = os.environ.get("MW_PR_BASE")
        os.environ["MW_PR_BASE"] = "main"
        try:
            files, source = gate._pr_files(self.repo.root, ("**/*.py",))
            self.assertEqual(files, ())
            self.assertIn("git-diff", source)
        finally:
            if old is None:
                os.environ.pop("MW_PR_BASE", None)
            else:
                os.environ["MW_PR_BASE"] = old

    @unittest.skipUnless(find_ast_grep(), "需要 ast-grep CLI")
    def test_incremental_scan_red_while_full_scan_also_finds_legacy(self) -> None:
        exe = find_ast_grep()
        rules = self.tmp / "rules"
        rules.mkdir()
        (rules / "no-eval-in-spine.yml").write_text(RULE_YAML, encoding="utf-8")
        diff = read_pr_diff(self.repo.root, base="main", head="pr-violation")
        out = diff.materialize(self.tmp / "export")
        incremental = gate.scan_generated_rules(exe, rules, out, diff.python_files)
        self.assertFalse(incremental["clean"])
        self.assertTrue(any(f["file"] == "evil.py" for f in incremental["findings"]))

        full_files, full_root = export_tree(self.repo.root, "main", self.tmp / "full")
        full = gate.scan_generated_rules(exe, rules, full_root, full_files)
        self.assertTrue(any(f["file"] == "legacy.py" for f in full["findings"]))

        clean = read_pr_diff(self.repo.root, base="main", head="pr-clean")
        clean_out = clean.materialize(self.tmp / "clean")
        self.assertTrue(gate.scan_generated_rules(
            exe, rules, clean_out, clean.python_files)["clean"])


if __name__ == "__main__":
    unittest.main()
