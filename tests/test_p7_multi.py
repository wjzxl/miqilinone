"""P7 多文件编辑：协议解析、范围强制、工具循环、多断言 Drive。

这组测试全部**不联网**：真实模型的连通性与协议服从由
`ci/gate.py` 的 real_model_smoke 在有密钥时单独验；这里锁死的是契约。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.chief.policy import TaskIntent
from michelin.chief.p2_workflow import P2Pipeline, P2Scenario
from michelin.context.sources import SourcePlane
from michelin.models import MeteredModel, ModelUsage, ScriptedModel
from michelin.platform.store import EventStore
from michelin.verification.mutants import MUTATIONS_MULTI, run_mutation_suite
from michelin.verification.skill_spec import build_multi_file_spec
from michelin.workers.editing_worker import (
    MultiFileCodeWorker, _in_scope, _safe_relpath, parse_actions,
)
from michelin.workers.git_worker import WorkerOutputError
from michelin.workspace.seed import CLI_SOURCE_MULTI, GitWorkspace, PRICING_CORRECT


class TestProtocolParsing(unittest.TestCase):
    def test_reads_and_edits_are_parsed(self) -> None:
        action = parse_actions(
            "READ shop/cli.py\n\n"
            "=== BEGIN FILE: shop/pricing.py ===\nX = 1\n=== END FILE ===\n"
            "=== BEGIN FILE: shop/cli.py ===\nY = 2\n=== END FILE ===\n"
        )
        self.assertEqual(action.reads, ("shop/cli.py",))
        self.assertEqual([e.path for e in action.edits], ["shop/pricing.py", "shop/cli.py"])
        self.assertEqual(action.edits[0].content, "X = 1\n")

    def test_unclosed_block_is_rejected(self) -> None:
        with self.assertRaises(WorkerOutputError) as ctx:
            parse_actions("=== BEGIN FILE: a.py ===\nX=1\n")
        self.assertIn("END FILE", str(ctx.exception))

    def test_prose_only_reply_is_rejected(self) -> None:
        with self.assertRaises(WorkerOutputError):
            parse_actions("我认为应该把税率改成 6%")


class TestScopeEnforcement(unittest.TestCase):
    def test_glob_matching(self) -> None:
        self.assertTrue(_in_scope("shop/pricing.py", ("shop/**",)))
        self.assertTrue(_in_scope("shop/deep/a.py", ("shop/**",)))
        self.assertFalse(_in_scope("README.md", ("shop/**",)))
        self.assertFalse(_in_scope("shop/pricing.py", ()))  # 空范围 = 全不许改

    def test_path_traversal_is_rejected(self) -> None:
        for bad in ("../etc/passwd", "/etc/passwd", "shop/../../x.py"):
            with self.assertRaises(WorkerOutputError):
                _safe_relpath(bad)


class TestStaleBytecodeTrap(unittest.TestCase):
    """锁死「陈旧字节码」陷阱：同长度改动必须真的生效。

    这是真实 CI（ubuntu）上 P7 变红的根因：`.pyc` 头部记的是源文件的
    **秒级** mtime + size，所以「同长度改动 + 同一秒内重写」会被 Python
    判成缓存仍然有效 —— **故意改坏的版本根本没被加载**，变异体静默失效，
    门禁于是误判「没开火」。

    注意 `-S` 只跳过 site，**照样写 .pyc**；真正管用的是
    PYTHONDONTWRITEBYTECODE=1。
    """

    def test_same_length_mutation_must_take_effect(self) -> None:
        import subprocess
        import sys
        import tempfile as tf

        from michelin.verification.generator import generate_skill

        tmp = Path(tf.mkdtemp(prefix="mw-stale-pyc-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        skill = generate_skill(tmp / "skills", spec=build_multi_file_spec())
        helper = (skill.root / "helpers" / "verify_helper.py").read_text(encoding="utf-8")
        self.assertIn("PYTHONDONTWRITEBYTECODE", helper,
                      "生成的 helper 必须禁止写 .pyc，否则同长度变异会静默失效")

        workspace = GitWorkspace(tmp / "repo")
        workspace.seed_baseline()
        tree = workspace.worktree("stale")
        tree.write_file("shop/pricing.py", PRICING_CORRECT)
        tree.write_file("shop/cli.py", CLI_SOURCE_MULTI)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(tree.path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = [sys.executable, "-S", "-m", "shop.cli", "total", "10", "20"]

        first = subprocess.run(argv, cwd=str(tree.path), env=env,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(first.stdout.strip(), "TOTAL=31.80")

        # 同长度改写：0.06 -> 0.10（长度完全相同）
        target = tree.path / "shop/pricing.py"
        target.write_text(
            target.read_text(encoding="utf-8").replace("TAX_RATE = 0.06", "TAX_RATE = 0.10"),
            encoding="utf-8",
        )
        second = subprocess.run(argv, cwd=str(tree.path), env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(second.stdout.strip(), "TOTAL=33.00",
                         "同长度改动必须真的生效，否则变异门禁形同虚设")


class TestMultiFileWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p7-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.workspace = GitWorkspace(self.tmp / "repo")
        self.workspace.seed_baseline()

    def _kickoff(self, scope=("shop/**",)):
        from michelin.chief.policy import EvidenceSpec, Kickoff, Predicate
        spec = EvidenceSpec((Predicate("m", "diff_contains", "diff --git"),))
        return Kickoff(
            task_id="t", intent="修税率并让 doctor 报税率", source_hashes=(),
            writable_paths=scope, forbidden_actions=(), evidence_spec=spec,
            risk_level="low", budget=1.0, attempt=1,
        )

    def test_tool_loop_reads_then_edits(self) -> None:
        model = ScriptedModel("anthropic", "stub", [
            "READ shop/pricing.py\nREAD shop/cli.py",
            "=== BEGIN FILE: shop/pricing.py ===\n" + PRICING_CORRECT + "=== END FILE ===\n",
        ])
        worker = MultiFileCodeWorker(model, self.workspace, max_rounds=4)
        out = worker.run(self._kickoff(), [])
        self.assertEqual(out.reads, ("shop/pricing.py", "shop/cli.py"))
        self.assertEqual(out.edited, ("shop/pricing.py",))
        self.assertIn("diff --git a/shop/pricing.py", out.patch)
        self.assertEqual(out.rounds, 2)

    def test_out_of_scope_edit_is_refused(self) -> None:
        """越界编辑必须被拒：文件不该被写，产出里也不该出现它。"""
        model = ScriptedModel("anthropic", "stub", [
            "=== BEGIN FILE: README.md ===\nHACKED\n=== END FILE ===\n",
        ])
        worker = MultiFileCodeWorker(model, self.workspace, max_rounds=2)
        with self.assertRaises(WorkerOutputError):
            worker.run(self._kickoff(), [])
        # README 必须在 seed 仓里保持原样
        original = (self.workspace.seed / "README.md").read_text(encoding="utf-8")
        self.assertIn("shop", original)
        self.assertNotIn("HACKED", original)

    def test_syntax_error_edit_is_refused(self) -> None:
        model = ScriptedModel("anthropic", "stub", [
            "=== BEGIN FILE: shop/pricing.py ===\ndef broken(:\n=== END FILE ===\n",
        ])
        worker = MultiFileCodeWorker(model, self.workspace, max_rounds=2)
        with self.assertRaises(WorkerOutputError):
            worker.run(self._kickoff(), [])


class TestMultiAssertionDrive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p7-spec-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_extra_check_is_rendered_into_helper(self) -> None:
        from michelin.verification.generator import generate_skill

        skill = generate_skill(self.tmp, spec=build_multi_file_spec())
        helper = (skill.root / "helpers" / "verify_helper.py").read_text(encoding="utf-8")
        self.assertIn("EXTRA_DRIVE_CHECKS", helper)
        self.assertIn("rate=0.06", helper)
        # 默认 spec 不应有额外断言
        plain = generate_skill(self.tmp / "plain")
        plain_helper = (plain.root / "helpers" / "verify_helper.py").read_text(encoding="utf-8")
        self.assertIn("EXTRA_DRIVE_CHECKS = []", plain_helper)

    def test_multi_mutation_set_covers_the_extra_assertion(self) -> None:
        """多出来的那条 Drive 断言必须也有变异体，否则它「写了没人验」。"""
        names = {m[0] for m in MUTATIONS_MULTI}
        self.assertIn("doctor_missing_rate", names)
        self.assertGreater(len(MUTATIONS_MULTI), 5)

    def test_mutation_suite_uses_the_given_baseline(self) -> None:
        from michelin.verification.generator import generate_skill

        skill = generate_skill(self.tmp, spec=build_multi_file_spec())
        workspace = GitWorkspace(self.tmp / "repo")
        workspace.seed_baseline()
        report = run_mutation_suite(
            skill, workspace, self.tmp / "mutants",
            baseline_files={"shop/pricing.py": PRICING_CORRECT,
                            "shop/cli.py": CLI_SOURCE_MULTI},
            mutations=MUTATIONS_MULTI,
        )
        self.assertTrue(report.baseline_green, "干净基线必须是绿的")
        self.assertTrue(report.all_fired, "每个坏版本都要让门禁开火")
        self.assertEqual(len(report.cases), len(MUTATIONS_MULTI))


class TestMultiFilePipeline(unittest.TestCase):
    def test_end_to_end_with_scripted_model(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="mw-p7-e2e-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = EventStore(":memory:")
        self.addCleanup(store.close)
        worker_script = [
            "READ shop/pricing.py\nREAD shop/cli.py",
            "=== BEGIN FILE: shop/pricing.py ===\n" + PRICING_CORRECT + "=== END FILE ===\n"
            "=== BEGIN FILE: shop/cli.py ===\n" + CLI_SOURCE_MULTI + "=== END FILE ===\n",
        ]
        pipeline = P2Pipeline(
            store, SourcePlane(store),
            ScriptedModel("anthropic", "stub-w", worker_script),
            ScriptedModel("openai", "stub-v", ["PASS"]),
            tmp / "runs",
            skill_spec=build_multi_file_spec(),
            golden_files={"shop/pricing.py": PRICING_CORRECT, "shop/cli.py": CLI_SOURCE_MULTI},
            writable_paths=("shop/**",),
            mutations=MUTATIONS_MULTI,
            worker_factory=lambda m, ws: MultiFileCodeWorker(m, ws, max_rounds=4),
        )
        intent = TaskIntent(
            intent="税率改成 6%，doctor 报出税率",
            source_bodies=("【需求原文 1】税率 6%。", "【需求原文 2】doctor 报 rate=0.06。"),
            source_system="test-multi",
        )
        result = pipeline.handle(intent, P2Scenario(worker_rates=("0.06",)))
        self.assertEqual(result.state.value, "ACCEPTED")
        self.assertEqual(result.verdict, "PASS")
        self.assertTrue(result.mutants_all_fired)
        self.assertEqual(result.attempts, 1)

    def test_default_pipeline_behaviour_is_unchanged(self) -> None:
        """默认参数必须还是 P2 的单文件行为，别把老路径带歪。"""
        tmp = Path(tempfile.mkdtemp(prefix="mw-p7-default-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = EventStore(":memory:")
        self.addCleanup(store.close)
        pipeline = P2Pipeline(
            store, SourcePlane(store),
            ScriptedModel("anthropic", "stub-w", ["将税率改为 TAX_RATE = 0.06。"]),
            ScriptedModel("openai", "stub-v", ["PASS"]),
            tmp / "runs",
        )
        self.assertEqual(pipeline.writable_paths, ("shop/pricing.py",))
        self.assertIsNone(pipeline.skill_spec)
        self.assertIsNone(pipeline.golden_files)
        self.assertIsNone(pipeline.worker_factory)


class TestMeteredChat(unittest.TestCase):
    def test_chat_is_also_metered(self) -> None:
        charged: list[ModelUsage] = []
        metered = MeteredModel(ScriptedModel("anthropic", "stub", ["a", "b"]), charged.append)
        metered.complete_chat([{"role": "user", "content": "x"}])
        metered.complete_chat([{"role": "user", "content": "y"}])
        self.assertEqual(len(charged), 2)
        self.assertEqual(metered.total_usage.external_requests, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
