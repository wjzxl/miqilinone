"""锁死 holdout 形状：这是整套系统最容易被后人「不小心」放宽的地方。

架构文档 §L4 / 风险清单「LLM-as-judge 被作者叙述污染」：
用函数签名 + AST guard + 单测把 verifier 输入结构固定住，
防止有人为了「让 verifier 更聪明」把作者的 PR 描述 / plan / transcript 塞回去。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
import unittest

from _path import SRC  # noqa: F401

from michelin.chief.policy import EvidenceSpec, Predicate
from michelin.models import ScriptedModel
from michelin.platform.store import EventStore
from michelin.verification.holdout import (
    ALLOWED_FIELDS, FORBIDDEN_FIELDS, HoldoutBundle, HoldoutViolation, build_holdout,
)
from michelin.verification.verifier import IndependentVerifier, independent_rerun


def _function_node(source: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(source))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))


class TestHoldoutShape(unittest.TestCase):
    def test_fields_are_exactly_the_whitelist(self) -> None:
        fields = {f.name for f in dataclasses.fields(HoldoutBundle)}
        self.assertEqual(fields, set(ALLOWED_FIELDS))
        self.assertTrue(fields.isdisjoint(FORBIDDEN_FIELDS))

    def test_source_whitelist_matches_dataclass(self) -> None:
        """源码里白名单常量与 dataclass 定义必须一致，不能只改一处。"""
        import michelin.verification.holdout as holdout_module

        source = inspect.getsource(holdout_module)
        tree = ast.parse(source)
        assigned = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ALLOWED_FIELDS" for t in node.targets
            ):
                assigned = ast.literal_eval(node.value.args[0])  # frozenset({...})
        self.assertEqual(set(assigned or ()), set(ALLOWED_FIELDS))

    def test_slots_prevent_dynamic_author_fields(self) -> None:
        bundle = build_holdout(
            issue_body="i", artifact_text="d",
            self_reported_test_output="t", governance_docs=("只读",),
        )
        with self.assertRaises((TypeError, AttributeError)):
            object.__setattr__(bundle, "author_plan", "帮我判过")
        with self.assertRaises(TypeError):
            HoldoutBundle(  # type: ignore[call-arg]
                issue_body="i", diff="d", test_output="t",
                governance_docs=("只读",), author_plan="帮我判过",
            )

    def test_shape_guard_rejects_a_lookalike(self) -> None:
        from michelin.verification.holdout import assert_holdout_shape

        @dataclasses.dataclass(frozen=True)
        class Smuggled:
            issue_body: str
            diff: str
            test_output: str
            governance_docs: tuple
            tool_transcript: str = ""

        with self.assertRaises(HoldoutViolation):
            assert_holdout_shape(Smuggled("i", "d", "t", ()))


class TestVerifierSignature(unittest.TestCase):
    def test_verify_only_accepts_bundle_positionally(self) -> None:
        node = _function_node(inspect.getsource(IndependentVerifier.verify))
        args = node.args
        self.assertEqual([a.arg for a in args.args], ["self", "bundle"])
        self.assertIsNone(args.vararg)
        self.assertIsNone(args.kwarg)
        self.assertEqual(args.posonlyargs, [])
        self.assertGreaterEqual(len(args.kwonlyargs), 5)

    def test_no_author_narration_in_signature(self) -> None:
        node = _function_node(inspect.getsource(IndependentVerifier.verify))
        names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        self.assertTrue(names.isdisjoint(FORBIDDEN_FIELDS))
        for banned in ("plan", "transcript", "description", "notes", "title"):
            self.assertFalse(any(banned in n for n in names), f"签名里出现作者侧叙述: {banned}")

    def test_build_holdout_signature_has_no_kickoff(self) -> None:
        node = _function_node(inspect.getsource(build_holdout))
        names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        self.assertEqual(names, {
            "issue_body", "artifact_text", "self_reported_test_output", "governance_docs",
        })
        self.assertTrue(names.isdisjoint(FORBIDDEN_FIELDS))


class TestChiefNeverAuthors(unittest.TestCase):
    def test_chief_source_has_no_file_writes(self) -> None:
        """总厨不下场：源码中不得出现任何写文件动作。"""
        path = SRC / "michelin" / "chief" / "workflow.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        banned = {"open", "write_text", "write_bytes", "mkdir", "unlink"}
        offenders = [
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id in banned)
                or (isinstance(n.func, ast.Attribute) and n.func.attr in banned)
            )
        ]
        self.assertEqual(offenders, [], f"总厨源码出现写动作: {offenders}")


class TestIndependentRerun(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = EvidenceSpec((
            Predicate("marker", "diff_contains", "EVIDENCE:ok"),
            Predicate("gate", "test_output_contains", "INDEPENDENT_PASS"),
        ))

    def test_self_report_cannot_buy_a_pass(self) -> None:
        """worker 自称 PASS 但 diff 缺证据 → 独立重跑必须 FAIL。"""
        bundle = build_holdout(
            issue_body="只读分析",
            artifact_text="这里没有证据标记",
            self_reported_test_output="PASS: 99/99 checks (self-reported, trust me)",
            governance_docs=("只读任务",),
        )
        report = independent_rerun(bundle, self.spec)
        self.assertFalse(report.passed)
        self.assertIn("INDEPENDENT_FAIL", report.independent_test_output)
        self.assertEqual(report.self_reported_test_output, bundle.test_output)

    def test_same_family_verifier_is_rejected(self) -> None:
        store = EventStore(":memory:")
        verifier = IndependentVerifier(store, ScriptedModel("anthropic", "claude-same", ["PASS"]))
        bundle = build_holdout(
            issue_body="i", artifact_text="EVIDENCE:ok",
            self_reported_test_output="t", governance_docs=("只读",),
        )
        with self.assertRaises(HoldoutViolation):
            verifier.verify(
                bundle, run_id="r", task_id="t", author_run_id="a",
                author_family="anthropic", head_sha="h", evidence_spec=self.spec,
            )
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
