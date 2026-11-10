"""P4 自动起草的轻量契约测试（默认回归跑，不启动端到端流水线）。

端到端部分在 `ci/gate.py` 的 autodraft / external_plan 检查里，按 CI 或
`python3 ci/gate.py` 运行；这里只锁死「草稿必须确定、接地、可编译」的契约。
"""

from __future__ import annotations

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.verification.autodraft import (  # noqa: E402
    _find_package, _mutation_snippet, _pick_inject_file, _rules_for, draft_plan,
)
from michelin.verification.interview import ground_plan, interview  # noqa: E402
from michelin.verification.plan import StructuredRule, load_plan  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "ledger-kit"


class TestAutodraftContract(unittest.TestCase):
    def test_draft_is_deterministic_grounded_and_complete(self) -> None:
        first = draft_plan(FIXTURE, app_name="ledger-kit-auto")
        second = draft_plan(FIXTURE, app_name="ledger-kit-auto")
        self.assertEqual(first.plan.fingerprint(), second.plan.fingerprint(),
                         "同一仓库的自动起草不得含时间戳或随机数")
        self.assertEqual(first.package, "ledgerkit")
        self.assertEqual(first.layout, "flat")
        self.assertEqual(ground_plan(first.profile, first.plan), ())
        self.assertTrue(first.plan.journeys)
        self.assertEqual(len(first.plan.mutations), len(first.plan.rules))
        self.assertEqual({m.rule_id for m in first.plan.mutations},
                         {r.id for r in first.plan.rules})
        self.assertTrue(all(p.passed for p in first.probes
                            if p.label in ("launch", "doctor")))
        for feature in first.plan.features:
            self.assertTrue(all((feature.sub_features, feature.user_pov,
                                feature.driving, feature.gotchas)))

    def test_draft_differs_from_handwritten_plan(self) -> None:
        draft = draft_plan(FIXTURE, app_name="ledger-kit-auto")
        hand = load_plan(ROOT / "plans" / "ledger-kit.json")
        self.assertNotEqual(draft.plan.fingerprint(), hand.fingerprint())
        self.assertNotEqual(tuple(r.id for r in draft.plan.rules),
                            tuple(r.id for r in hand.rules))

    def test_baseline_violating_rule_is_dropped(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mw-p4-draft-"))
        try:
            (tmp / "bad.py").write_text("value = eval('1 + 1')\n", encoding="utf-8")
            (tmp / "clean.py").write_text("value = 2\n", encoding="utf-8")
            kept, dropped = _rules_for(tmp, "python-ast")
            self.assertNotIn("no-eval", [r.id for r in kept])
            self.assertTrue(any("no-eval" in d for d in dropped))
            self.assertTrue(kept, "不能因为一条规则命中就丢弃整份基线")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mutation_snippets_are_valid_python(self) -> None:
        rules = (
            StructuredRule("no-call", "forbidden_call", "pickle.loads", "msg"),
            StructuredRule("no-import", "forbidden_import", "yaml", "msg"),
            StructuredRule("no-literal", "forbidden_literal", "DEBUG", "msg", literal="True"),
        )
        for rule in rules:
            snippet = _mutation_snippet(rule)
            tree = ast.parse(snippet)
            self.assertTrue(any(isinstance(n, ast.FunctionDef) for n in tree.body),
                            "变异必须包在函数体里，避免 import 时执行")
            self.assertIn(rule.selector, snippet)

    def test_inject_target_is_real_and_not_a_test_module(self) -> None:
        profile = interview(FIXTURE)
        target = _pick_inject_file(profile, FIXTURE)
        self.assertIn(target, profile.tracked_files)
        self.assertTrue(target.endswith(".py"))
        self.assertFalse(Path(target).name.startswith("test_"))

    def test_src_layout_is_detected(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mw-p4-src-"))
        try:
            pkg = tmp / "src" / "demo"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("__version__ = '1'\n", encoding="utf-8")
            name, layout = _find_package(interview(tmp), tmp)
            self.assertEqual((name, layout), ("demo", "src"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
