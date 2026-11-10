"""P6 真实模型：用量解析、密钥处理、模型家族独立性豁免必须被记录。

这一组测试全部**不联网**：真实调用的连通性由 `ci/gate.py` 的 real_model_smoke
在有密钥时单独验；这里锁死的是契约 —— 免得以后有人把「用量不记」「豁免不记」
「密钥入仓」这类事悄悄做掉。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.chief.policy import TaskIntent
from michelin.chief.workflow import ChiefOfStaff
from michelin.context.sources import SourcePlane
from michelin.models import (
    IndependenceWaiver, MeteredModel, ModelUsage, ScriptedModel, build_models,
    check_family_independence, family_of, resolve_api_key,
)
from michelin.platform.store import EventStore
from michelin.verification.holdout import HoldoutViolation

ROOT = Path(__file__).resolve().parents[1]


class TestUsageParsing(unittest.TestCase):
    def test_deepseek_style_usage_is_fully_parsed(self) -> None:
        """DeepSeek 的 usage 带缓存命中与推理 token，必须都解析出来。"""
        usage = ModelUsage.from_api({
            "prompt_tokens": 278,
            "completion_tokens": 879,
            "total_tokens": 1157,
            "completion_tokens_details": {"reasoning_tokens": 516},
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 238,
        })
        self.assertEqual(usage.input_tokens, 278)
        self.assertEqual(usage.output_tokens, 879)
        self.assertEqual(usage.reasoning_tokens, 516)
        self.assertEqual(usage.cached_tokens, 40)
        self.assertEqual(usage.total_tokens, 1157)
        self.assertEqual(usage.external_requests, 1)

    def test_cached_tokens_are_not_billed_twice(self) -> None:
        usage = ModelUsage(input_tokens=100, cached_tokens=40)
        self.assertEqual(usage.billable_input_tokens, 60)

    def test_missing_usage_block_does_not_crash(self) -> None:
        usage = ModelUsage.from_api(None)
        self.assertEqual(usage.total_tokens, 0)
        self.assertEqual(usage.external_requests, 1)


class TestKeyHandling(unittest.TestCase):
    def test_provider_slug_ignores_api_prefix(self) -> None:
        from michelin.models import _provider_slug
        self.assertEqual(_provider_slug("https://api.deepseek.com/v1"), "deepseek")
        self.assertEqual(_provider_slug("https://api.openai.com/v1"), "openai")

    def test_key_is_read_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.txt"
            path.write_text("sk-test-value\n", encoding="utf-8")
            old = os.environ.get("MW_API_KEY")
            os.environ["MW_API_KEY_FILE"] = str(path)
            try:
                self.assertEqual(resolve_api_key("https://api.deepseek.com/v1"), "sk-test-value")
            finally:
                os.environ.pop("MW_API_KEY_FILE", None)
                if old is not None:
                    os.environ["MW_API_KEY"] = old

    def test_missing_key_raises_with_actionable_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_env = os.environ.pop("MW_API_KEY", None)
            old_file = os.environ.pop("MW_API_KEY_FILE", None)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = tmp  # 让默认路径指向空目录
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    resolve_api_key("https://api.deepseek.com/v1")
                self.assertIn("MW_API_KEY", str(ctx.exception))
            finally:
                os.environ["HOME"] = old_home or tmp
                if old_env is not None:
                    os.environ["MW_API_KEY"] = old_env
                if old_file is not None:
                    os.environ["MW_API_KEY_FILE"] = old_file


class TestFamilyIndependence(unittest.TestCase):
    def test_same_family_is_rejected_by_default(self) -> None:
        """两个 DeepSeek 模型同属 open-source-cn，默认必须拒绝。"""
        worker = ScriptedModel("open-source-cn", "deepseek-flash", ["x"])
        verifier = ScriptedModel("open-source-cn", "deepseek-v4-pro", ["PASS"])
        with self.assertRaises(RuntimeError) as ctx:
            check_family_independence(worker, verifier)
        message = str(ctx.exception)
        self.assertIn("模型家族相同", message)
        self.assertIn("MW_ALLOW_SAME_MODEL_FAMILY", message)

    def test_different_family_passes_without_waiver(self) -> None:
        worker = ScriptedModel("anthropic", "claude", ["x"])
        verifier = ScriptedModel("openai", "gpt", ["PASS"])
        self.assertIsNone(check_family_independence(worker, verifier))

    def test_same_family_returns_waiver_when_explicitly_allowed(self) -> None:
        worker = ScriptedModel("open-source-cn", "deepseek-flash", ["x"])
        verifier = ScriptedModel("open-source-cn", "deepseek-v4-pro", ["PASS"])
        waiver = check_family_independence(worker, verifier, allow_same_family=True)
        self.assertIsNotNone(waiver)
        self.assertEqual(waiver.worker_model, "deepseek-flash")
        self.assertEqual(waiver.verifier_model, "deepseek-v4-pro")
        self.assertTrue(waiver.reason)

    def test_deepseek_family_classification(self) -> None:
        self.assertEqual(family_of("deepseek-flash"), "open-source-cn")
        self.assertEqual(family_of("deepseek-v4-pro"), "open-source-cn")

    def test_verifier_still_rejects_same_family_without_waiver(self) -> None:
        """加了豁免参数不代表默认放宽：没有豁免时仍必须抛 HoldoutViolation。"""
        from michelin.verification.verifier import IndependentVerifier
        from michelin.verification.holdout import build_holdout
        from michelin.chief.policy import EvidenceSpec, Predicate

        spec = EvidenceSpec((Predicate("m", "diff_contains", "EVIDENCE:ok"),))
        store = EventStore(":memory:")
        verifier = IndependentVerifier(store, ScriptedModel("open-source-cn", "deepseek-flash", ["PASS"]))
        bundle = build_holdout(
            issue_body="i", artifact_text="EVIDENCE:ok",
            self_reported_test_output="t", governance_docs=("只读",),
        )
        with self.assertRaises(HoldoutViolation):
            verifier.verify(bundle, run_id="r", task_id="t", author_run_id="a",
                            author_family="open-source-cn", head_sha="h", evidence_spec=spec)
        store.close()


class TestWaiverIsRecorded(unittest.TestCase):
    """豁免必须**出现在证据链里**：降级不能只存在于某个人的命令行历史。"""

    def _run(self) -> tuple[EventStore, str]:
        store = EventStore(":memory:")
        worker = ScriptedModel("open-source-cn", "deepseek-flash", ["EVIDENCE:ok 分析完成。"])
        verifier = ScriptedModel("open-source-cn", "deepseek-v4-pro", ["PASS"])
        waiver = IndependenceWaiver(
            reason="测试用豁免", worker_family="open-source-cn",
            verifier_family="open-source-cn",
            worker_model="deepseek-flash", verifier_model="deepseek-v4-pro",
        )
        chief = ChiefOfStaff(store, SourcePlane(store), worker, verifier, waiver=waiver)
        intent = TaskIntent(
            intent="只读分析：结论必须可追溯",
            source_bodies=("【需求原文】证据不足就不许派发。",),
            source_system="test",
        )
        chief.handle(intent)
        return store, "run"

    def test_waiver_event_is_in_the_chain(self) -> None:
        store, _ = self._run()
        try:
            rows = store.conn.execute(  # 测试直接查事件表
                "select type, payload_json from event where type='model.independence_waived'"
            ).fetchall()
            self.assertEqual(len(rows), 1, "豁免事件必须且只能出现一次")
            payload = json.loads(rows[0][1])
            self.assertEqual(payload["worker_model"], "deepseek-flash")
            self.assertEqual(payload["verifier_model"], "deepseek-v4-pro")
            self.assertEqual(payload["verifier_family"], "open-source-cn")
        finally:
            store.close()

    def test_verdict_carries_the_waiver(self) -> None:
        store, _ = self._run()
        try:
            rows = store.conn.execute(
                "select payload_json from event where type='verdict.issued'"
            ).fetchall()
            self.assertTrue(rows)
            payload = json.loads(rows[0][0])
            self.assertIsNotNone(payload.get("independence_waiver"))
            names = {c["name"] for c in payload["checks"]}
            self.assertIn("model_independence_waived", names)
            self.assertIn("llm_judge_usage", names)
        finally:
            store.close()

    def test_chief_without_waiver_refuses_same_family(self) -> None:
        store = EventStore(":memory:")
        try:
            with self.assertRaises(RuntimeError):
                ChiefOfStaff(
                    store, SourcePlane(store),
                    ScriptedModel("open-source-cn", "a", ["x"]),
                    ScriptedModel("open-source-cn", "b", ["PASS"]),
                )
        finally:
            store.close()


class TestMeteredModel(unittest.TestCase):
    def test_usage_is_accumulated_and_charged(self) -> None:
        charged: list[ModelUsage] = []
        inner = ScriptedModel("anthropic", "stub", ["a", "b"])
        metered = MeteredModel(inner, charged.append)
        metered.complete("p1")
        metered.complete("p2")
        self.assertEqual(len(charged), 2)
        self.assertEqual(metered.total_usage.external_requests, 2)


class TestNoSecretInRepo(unittest.TestCase):
    """密钥必须只在本机文件里，绝不能进仓库。"""

    PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    )

    def test_no_api_key_literal_in_tracked_files(self) -> None:
        offenders: list[str] = []
        files = subprocess.run(
            ["git", "-C", str(ROOT), "-c", "core.quotepath=false", "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        for rel in files:
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in self.PATTERNS:
                if pattern.search(text):
                    offenders.append(rel)
                    break
        self.assertEqual(offenders, [], f"仓库里出现疑似密钥：{offenders}")

    def test_env_example_has_no_real_key(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for pattern in self.PATTERNS:
            self.assertIsNone(pattern.search(text), ".env.example 里不应出现真实密钥")


if __name__ == "__main__":
    unittest.main(verbosity=2)
