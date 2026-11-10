"""P1-A 测试：验签（raw bytes / 防重放）、原子去重、DLQ 重试、对账、一手原文。

零外部依赖，系统 python3 即可运行。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest

import _path  # noqa: F401

from michelin.chief.workflow import ChiefOfStaff
from michelin.context.sources import SourcePlane
from michelin.inbox.dlq import reconcile, retry_dlq
from michelin.inbox.handler import InlineRunner, receive_webhook
from michelin.inbox.normalize import delivery_id_from, normalize, parse_payload
from michelin.inbox.objects import InboxObjectStore
from michelin.inbox.signature import sign, verify_signature
from michelin.models import ScriptedModel
from michelin.platform.store import EventStore

SECRET = "p1-test-secret"


def make_chief(store):
    return ChiefOfStaff(
        store, SourcePlane(store),
        ScriptedModel("anthropic", "claude-stub", ["EVIDENCE:ok 只读分析完成。"]),
        ScriptedModel("openai", "gpt-stub", ["PASS"]),
    )


def payload_bytes(intent: str = "外部只读分析需求") -> bytes:
    return json.dumps({
        "intent": intent,
        "sources": ["原文一：验证循环要工程化。", "原文二：出错改环境规则。"],
    }, ensure_ascii=False).encode("utf-8")


class TestSignature(unittest.TestCase):
    def test_three_schemes_verify(self):
        raw = payload_bytes()
        for scheme in ("github", "slack", "michelin"):
            with self.subTest(scheme=scheme):
                r = verify_signature(raw, sign(SECRET, raw, scheme=scheme), SECRET)
                self.assertTrue(r.ok, r.reason)
                self.assertEqual(r.scheme, scheme)

    def test_raw_bytes_not_reserialized(self):
        """改一个空格就应验签失败 —— 证明用的是原始字节而非重新序列化的 JSON。"""
        raw = b'{"a": 1, "b": [1, 2]}'
        headers = sign(SECRET, raw)
        self.assertTrue(verify_signature(raw, headers, SECRET).ok)
        tampered = raw.replace(b'"a": 1', b'"a":1')
        self.assertFalse(verify_signature(tampered, headers, SECRET).ok)

    def test_wrong_secret_and_missing_header(self):
        raw = payload_bytes()
        self.assertFalse(verify_signature(raw, sign("other", raw), SECRET).ok)
        self.assertFalse(verify_signature(raw, {}, SECRET).ok)

    def test_timestamp_window_blocks_replay(self):
        raw = payload_bytes()
        now = time.time()
        fresh = sign(SECRET, raw, timestamp=int(now))
        self.assertTrue(verify_signature(raw, fresh, SECRET, now=now).ok)
        stale = sign(SECRET, raw, timestamp=int(now) - 9999)
        r = verify_signature(raw, stale, SECRET, now=now)
        self.assertFalse(r.ok)
        self.assertIn("重放", r.reason)

    def test_slack_requires_timestamp(self):
        raw = payload_bytes()
        r = verify_signature(raw, {"X-Slack-Signature": "v0=abc"}, SECRET)
        self.assertFalse(r.ok)


class TestDedupeAndDelivery(unittest.TestCase):
    def setUp(self):
        self.store = EventStore(":memory:")
        self.objects = InboxObjectStore(tempfile.mkdtemp(prefix="mw-test-inbox-"))
        self.runner = InlineRunner(self.store, lambda: make_chief(self.store))
        self.raw = payload_bytes()

    def tearDown(self):
        self.runner.shutdown()
        self.store.close()

    def _receive(self, delivery_id="delivery-001", headers=None, raw=None, secret=SECRET):
        raw = raw or self.raw
        h = {"X-Delivery-Id": delivery_id, **(headers or sign(secret, raw))}
        return receive_webhook(
            self.store, raw, h, secret=secret, runner=self.runner,
            objects=self.objects, source="generic",
        )

    def test_redelivery_does_not_create_second_task(self):
        r1 = self._receive()
        r2 = self._receive()
        self.runner.wait()
        self.assertTrue(r1.accepted)
        self.assertTrue(r2.duplicate)
        self.assertEqual(self.store.counts()["task"], 1)
        row = self.store.get_delivery("delivery-001")
        self.assertEqual(row["status"], "DONE")
        self.assertEqual(row["replay_count"], 1)
        self.assertEqual(len(self.runner.results), 1)
        linked = [e for e in self.store.all_events() if e["type"] == "inbox.linked"]
        self.assertEqual(len(linked), 1)

    def test_bad_signature_does_not_block_later_valid_delivery(self):
        bad = self._receive(
            "delivery-001",
            headers={"X-Michelin-Timestamp": str(int(time.time())),
                     "X-Michelin-Signature": "sha256=deadbeef"},
        )
        self.assertEqual(bad.status_code, 401)
        good = self._receive("delivery-001")
        self.assertTrue(good.accepted)
        self.runner.wait()
        rejected = [d for d in self.store.list_deliveries("REJECTED")]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.store.counts()["task"], 1)

    def test_dlq_retry_recovers_from_downstream_failure(self):
        calls = {"n": 0}

        def failing():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟下游故障")
            return make_chief(self.store)

        fail_runner = InlineRunner(self.store, failing)
        h = {"X-Delivery-Id": "dlq-001", **sign(SECRET, self.raw)}
        receive_webhook(self.store, self.raw, h, secret=SECRET, runner=fail_runner,
                        objects=self.objects, source="generic")
        fail_runner.wait()
        fail_runner.shutdown()
        self.assertEqual(self.store.get_delivery("dlq-001")["status"], "DLQ")

        retried = retry_dlq(self.store, lambda: make_chief(self.store), self.objects)
        self.assertEqual(retried[0]["status"], "RETRIED")
        self.assertEqual(self.store.get_delivery("dlq-001")["status"], "DONE")
        self.assertTrue(self.store.verify_chain()[0])

    def test_reconcile_finds_missing_delivery(self):
        self._receive()
        self.runner.wait()
        r = reconcile(self.store, {"delivery-001", "never-arrived"})
        self.assertEqual(r["missing"], ["never-arrived"])
        self.assertTrue(r["needs_backfill"])


class TestNormalize(unittest.TestCase):
    def test_source_bodies_contain_raw_full_text_not_summary(self):
        raw = payload_bytes()
        intent = normalize(parse_payload(raw), raw, source="generic")
        self.assertTrue(any("原始投递全文" in b and "原文一" in b for b in intent.source_bodies))
        self.assertTrue(any("原文一：验证循环要工程化" in b for b in intent.source_bodies))

    def test_github_issue_normalization(self):
        raw = json.dumps({
            "issue": {"number": 42, "title": "加一条门禁", "body": "原文正文，不许摘要"},
            "repository": {"full_name": "acme/repo"},
        }, ensure_ascii=False).encode("utf-8")
        intent = normalize(parse_payload(raw), raw, source="github")
        self.assertIn("#42", intent.intent)
        self.assertTrue(any("原文正文，不许摘要" in b for b in intent.source_bodies))

    def test_delivery_id_prefers_provider_header(self):
        raw = payload_bytes()
        h = {"X-GitHub-Delivery": "gh-123", "X-Delivery-Id": "generic-456"}
        self.assertEqual(delivery_id_from(h, parse_payload(raw), raw)[0], "gh-123")
        self.assertEqual(
            delivery_id_from({"X-Delivery-Id": "generic-456"}, parse_payload(raw), raw)[0],
            "generic-456",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
