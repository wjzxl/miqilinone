"""P5 运行时层：隔离证据 / span 链可证伪 / 预算熔断黏性 / 账本可重放。

刻意不做「看起来都对」的断言：macOS 会忽略 RLIMIT_AS，测试要求这个缺口被如实
记录；文件系统与网络边界必须标成 declared-only，不许把声明冒充强制。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _path import *  # noqa: F401,F403

from michelin.platform.envelope import SEMCONV_VERSION  # noqa: E402
from michelin.runtime import (  # noqa: E402
    Budget, BudgetExceeded, BudgetGuard, Charge, RunHarness, SandboxPolicy,
    SpanLog, Tracer, container_command, isolated_run,
)

PY = sys.executable


class TestSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p5-sandbox-"))
        self.src = self.tmp / "source"
        self.src.mkdir()
        (self.src / "context.py").write_text("VALUE = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_whitelist_drops_secrets_and_isolates_home(self) -> None:
        secret = "MW_P5_TEST_SECRET"
        os.environ[secret] = "must-not-leak"
        try:
            rec = isolated_run(
                [PY, "-S", "-c", "import os,json;print(json.dumps(dict(os.environ)))"],
                label="env", workdir=self.tmp / "run",
            )
        finally:
            os.environ.pop(secret, None)
        self.assertTrue(rec.ok)
        env = json.loads(rec.stdout)
        self.assertNotIn(secret, env)
        self.assertNotIn("must-not-leak", rec.stdout)
        self.assertIn(secret, rec.sensitive_dropped)
        self.assertTrue(env["HOME"].endswith("/box/home"))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")

    def test_rlimits_are_measured_not_claimed(self) -> None:
        rec = isolated_run([PY, "-S", "-c", "print(1)"], label="limits",
                          workdir=self.tmp / "run")
        self.assertEqual(rec.rlimits_effective.get("RLIMIT_CPU"),
                         rec.rlimits_requested["RLIMIT_CPU"])
        self.assertEqual(rec.rlimits_effective.get("RLIMIT_FSIZE"),
                         rec.rlimits_requested["RLIMIT_FSIZE"])
        self.assertTrue(rec.rlimits_ignored, "本平台应当至少暴露一个被忽略的 rlimit")
        self.assertIn("platform ignored", rec.enforcement["rlimits"])

    def test_source_snapshot_is_read_only(self) -> None:
        rec = isolated_run(
            [PY, "-S", "-c",
             "import os;p=os.environ['MW_SOURCE_ROOT']+'/context.py';open(p,'a').write('x')"],
            label="write", workdir=self.tmp / "run", source_root=self.src,
        )
        self.assertNotEqual(rec.exit_code, 0)
        self.assertIn("PermissionError", rec.stderr)
        self.assertTrue(rec.source_fingerprint)
        self.assertEqual((self.src / "context.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_timeout_kills_the_process_group(self) -> None:
        rec = isolated_run(
            [PY, "-S", "-c", "import time;time.sleep(10)"],
            label="slow", workdir=self.tmp / "run",
            policy=SandboxPolicy(timeout_seconds=0.5),
        )
        self.assertTrue(rec.timed_out)
        self.assertEqual(rec.exit_code, -9)
        self.assertLess(rec.duration_ms, 3000)

    def test_container_seam_turns_declared_into_enforced(self) -> None:
        cmd = container_command([PY, "-S", "-c", "pass"], policy=SandboxPolicy(),
                                source_root=self.src, workdir=self.tmp / "run")
        self.assertIn("--network=none", cmd)
        self.assertIn("--read-only", cmd)
        self.assertIn("--cap-drop=ALL", cmd)
        rec = isolated_run([PY, "-S", "-c", "print(1)"], label="plain",
                          workdir=self.tmp / "run")
        self.assertTrue(rec.enforcement["filesystem_boundary"].startswith("declared-only"))
        self.assertTrue(rec.enforcement["network_boundary"].startswith("declared-only"))


class TestTelemetry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p5-span-"))
        self.path = self.tmp / "spans.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hash_chain_parent_links_and_semconv_pin(self) -> None:
        tracer = Tracer(self.path, "run-1")
        with tracer.span("outer") as outer:
            outer.set(gen_ai_usage_input_tokens=42)
            with tracer.span("inner"):
                pass
        spans = tracer.log.spans()
        self.assertEqual([s["name"] for s in spans], ["inner", "outer"])
        self.assertEqual(spans[0]["parentSpanId"], spans[1]["spanId"])
        self.assertEqual(spans[1]["attributes"]["semconv_version"], SEMCONV_VERSION)
        self.assertTrue(tracer.log.verify_chain().ok)
        self.assertIn("resourceSpans", tracer.log.export_otel())

    def test_tampering_is_detected_and_corrupt_chain_rejects_writes(self) -> None:
        tracer = Tracer(self.path, "run-2")
        with tracer.span("one"):
            pass
        lines = self.path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0])
        row["payload"]["span"]["name"] = "TAMPERED"
        self.path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        reopened = SpanLog(self.path)
        self.assertFalse(reopened.verify_chain().ok)
        with self.assertRaises(RuntimeError):
            reopened.append({"name": "nope"})


class TestBudget(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mw-p5-budget-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sticky_kill_switch_and_replay(self) -> None:
        guard = BudgetGuard(Budget(max_actions=2), self.tmp / "b.jsonl", "r1")
        guard.check("a")
        guard.check("b")
        with self.assertRaises(BudgetExceeded) as ctx:
            guard.check("c")
        self.assertTrue(ctx.exception.reason.startswith("actions="))
        with self.assertRaises(BudgetExceeded) as ctx2:
            guard.check("d")
        self.assertIn("KILL_SWITCH", ctx2.exception.reason)
        self.assertTrue(guard.kill_switch_engaged)
        replay = guard.replay()
        self.assertTrue(replay["matches_live"])
        self.assertTrue(replay["tripped"])
        self.assertEqual(replay["head_hash"], guard.ledger_head())

    def test_cost_and_external_request_dimensions(self) -> None:
        cost = BudgetGuard(Budget(max_cost_usd=0.000001), self.tmp / "c.jsonl", "r2")
        with self.assertRaises(BudgetExceeded) as ctx:
            cost.check("big", Charge(input_tokens=100000, output_tokens=100000))
        self.assertTrue(ctx.exception.reason.startswith("cost_usd="))
        req = BudgetGuard(Budget(max_external_requests=1), self.tmp / "q.jsonl", "r3")
        with self.assertRaises(BudgetExceeded) as ctx2:
            req.check("many", Charge(external_requests=2))
        self.assertTrue(ctx2.exception.reason.startswith("external_requests="))

    def test_harness_records_error_span_without_running_the_command(self) -> None:
        harness = RunHarness(self.tmp / "run", "r4",
                            budget=Budget(max_actions=1),
                            policy=SandboxPolicy(timeout_seconds=3.0))
        self.assertTrue(harness.execute("one", [PY, "-S", "-c", "print(1)"]).ok)
        with self.assertRaises(BudgetExceeded):
            harness.execute("two", [PY, "-S", "-c", "print(2)"])
        report = harness.close()
        self.assertEqual(report["spans"]["statuses"], ["OK", "ERROR"])
        self.assertEqual(len(report["receipts"]), 1, "熔断发生在动作前，不应产生子进程")
        self.assertTrue(report["spans"]["chain"]["ok"])
        self.assertTrue(report["budget_journal"]["matches_live"])


if __name__ == "__main__":
    unittest.main()
