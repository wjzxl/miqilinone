"""P3 规则层测试：双后端对拍、生成物内嵌检查器的一致性、规则变异门禁。

本文件同时是 plans/michelin-self.json 里四条 mutation 的**注入靶子**：
变异测试会往这里追加一段真实违规代码，规则必须因此开火。
因此本文件自身不得含 eval / os.system / requests 导入 / TAX_RATE = 0.10
这样的 Python 语句（字符串字面量不算 AST 节点，可以出现）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403  — 把 src 加进 sys.path

from michelin.verification.generic_skill import (  # noqa: E402
    PY_AST_CHECKER, GeneratedGenericSkill, generate_generic_skill,
)
from michelin.verification.interview import RepoProfile  # noqa: E402
from michelin.verification.plan import (  # noqa: E402
    StructuredRule, VerificationPlan, load_plan,
)
from michelin.verification.rules import (  # noqa: E402
    compare_backends, find_ast_grep, rule_to_yaml, scan,
)

ROOT = Path(__file__).resolve().parents[1]
AST_GREP = find_ast_grep()
FULL = os.getenv("MW_P3_FULL") == "1"

RULES = (
    StructuredRule("no-eval", "forbidden_call", "eval", "no dynamic evaluation",
                   ("src/**/*.py",)),
    StructuredRule("no-shell", "forbidden_call", "os.system", "no shell out",
                   ("src/**/*.py",)),
    StructuredRule("no-http", "forbidden_import", "requests", "no http client",
                   ("src/**/*.py",)),
    StructuredRule("no-bad-rate", "forbidden_literal", "TAX_RATE", "no wrong rate",
                   ("src/**/*.py",), "0.10"),
)

BAD_SOURCE = """import os
import requests
from requests import get

def f(x):
    return eval(x)

def g():
    os.system("true")

TAX_RATE = 0.10
"""

CLEAN_SOURCE = """import json

def f(x):
    return x + 1
"""


def _tiny_repo(source: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="mw-p3-rules-"))
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text(source, encoding="utf-8")
    return root


class TestRuleYaml(unittest.TestCase):
    def test_each_kind_compiles_to_verified_ast_grep_yaml(self) -> None:
        for rule in RULES:
            yml = rule_to_yaml(rule)
            self.assertIn("id: " + rule.id, yml)
            self.assertIn("language: Python", yml)
            self.assertIn("severity: error", yml)
            self.assertIn("message: ", yml)
        self.assertIn("pattern: eval($$$ARGS)", rule_to_yaml(RULES[0]))
        self.assertIn("any:", rule_to_yaml(RULES[2]))
        self.assertIn("pattern: TAX_RATE = 0.10", rule_to_yaml(RULES[3]))

    def test_python_ast_backend_finds_every_kind(self) -> None:
        root = _tiny_repo(BAD_SOURCE)
        try:
            result = scan(RULES, root, "python-ast")
            self.assertEqual({f.rule_id for f in result.findings},
                             {"no-eval", "no-shell", "no-http", "no-bad-rate"})
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_clean_repo_has_no_findings(self) -> None:
        root = _tiny_repo(CLEAN_SOURCE)
        try:
            self.assertTrue(scan(RULES, root, "python-ast").clean)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestDualBackend(unittest.TestCase):
    @unittest.skipUnless(AST_GREP, "需要 .venv 里的 ast-grep")
    def test_two_backends_agree_on_bad_and_clean(self) -> None:
        for source, expected in ((BAD_SOURCE, 5), (CLEAN_SOURCE, 0)):
            root = _tiny_repo(source)
            try:
                a = scan(RULES, root, "python-ast")
                b = scan(RULES, root, "ast-grep", executable=AST_GREP)
                self.assertEqual(len(b.findings), expected)
                agreed, diff = compare_backends(a, b)
                self.assertTrue(agreed, f"后端分歧：{diff}")
            finally:
                shutil.rmtree(root, ignore_errors=True)


class TestGeneratedChecker(unittest.TestCase):
    """生成物内嵌的 python-ast 检查器必须与主流程 rules.py 同源。"""

    @staticmethod
    def _skill(root: Path) -> GeneratedGenericSkill:
        from dataclasses import replace

        profile = RepoProfile(
            root=str(root), languages={"python": 1}, entrypoints=(),
            test_targets=(), test_framework="none", governance_files=(),
            modules=(), tracked_files=("src/a.py",), fingerprint="test-fingerprint",
        )
        real = load_plan(ROOT / "plans" / "michelin-self.json")
        tiny = replace(real, app_name="p3-probe", rules=RULES, mutations=())
        return _build_skill_without_validation(root, profile, tiny)

    def test_helper_embeds_the_same_checker_source(self) -> None:
        root = _tiny_repo(CLEAN_SOURCE)
        try:
            skill = self._skill(root)
            helper = (skill.root / "helpers" / "verify_helper.py").read_text(encoding="utf-8")
            for fragment in ("def _dotted(node):", "def structured_findings(rules):",
                             'rule["kind"] == "forbidden_call"',
                             'rule["kind"] == "forbidden_import"',
                             'rule["kind"] == "forbidden_literal"'):
                self.assertIn(fragment, helper)
            self.assertIn("structured_findings(rules):", PY_AST_CHECKER)
            self.assertTrue(os.access(str(skill.root / "helpers" / "verify_helper.py"),
                                      os.X_OK))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @unittest.skipUnless(FULL, "重型：要跑生成的 helper 子进程，用 MW_P3_FULL=1 打开")
    def test_helper_drive_rejects_violation_and_accepts_clean(self) -> None:
        plan = load_plan(ROOT / "plans" / "michelin-self.json")
        root = _tiny_repo(CLEAN_SOURCE)
        try:
            profile = RepoProfile(
                root=str(root), languages={"python": 1}, entrypoints=(),
                test_targets=(), test_framework="none", governance_files=(),
                modules=(), tracked_files=("src/a.py",), fingerprint="probe",
            )
            skill = _build_skill_without_validation(root, profile, plan)
            helper = skill.root / "helpers" / "verify_helper.py"
            # 把 plan.json 里的 globs 指向这个 tiny repo，journeys 换成一行只读命令。
            payload = json.loads((skill.root / "plan.json").read_text(encoding="utf-8"))
            for r in payload["rules"]:
                r["globs"] = ["src/**/*.py"]
            payload["journeys"] = [{
                "name": "probe",
                "argv": [sys.executable, "-c", "print('JOURNEY_OK')"],
                "expect_stdout_contains": "JOURNEY_OK", "expect_exit_code": 0,
                "timeout_seconds": 30,
            }]
            (skill.root / "plan.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            run_dir = root / "run-good"
            env = {**os.environ, "VERIFY_APP_ROOT": str(root),
                   "VERIFY_RUN_DIR": str(run_dir)}
            good = subprocess.run([sys.executable, "-S", str(helper), "drive"],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertIn("DRIVE_OK", good.stdout, good.stdout + good.stderr)

            (root / "src" / "a.py").write_text(BAD_SOURCE, encoding="utf-8")
            bad = subprocess.run([sys.executable, "-S", str(helper), "drive"],
                                 capture_output=True, text=True, env=env, timeout=60)
            self.assertIn("DRIVE_FAIL", bad.stdout, bad.stdout + bad.stderr)
            self.assertIn("structured rules fired", bad.stdout)
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _build_skill_without_validation(root: Path, profile: RepoProfile,
                                    plan: VerificationPlan) -> GeneratedGenericSkill:
    """生成器要求 mutations 覆盖所有规则；探针场景没有变异，直接写最小生成物。

    只为测试 helper 的内嵌检查器，绕过的是「计划完整性」校验，不是六段式语义。
    """
    from michelin.verification.generic_skill import SIX_PHASES

    target = root / "skills"
    # 用真实生成器，但给 plan 补上覆盖每条规则的占位 mutation（不会被执行）。
    from dataclasses import replace
    from michelin.verification.plan import RuleMutation
    patched = replace(plan, mutations=tuple(
        RuleMutation(r.id, "src/a.py", "# probe placeholder") for r in plan.rules
    ))
    skill = generate_generic_skill(target, patched, profile)
    assert all(p in (skill.root / "SKILL.md").read_text(encoding="utf-8")
               for p in ("verify_helper.py " + x for x in SIX_PHASES))
    return skill


if __name__ == "__main__":
    unittest.main()
