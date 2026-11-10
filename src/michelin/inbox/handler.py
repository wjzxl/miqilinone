"""L0 接入层：验签 → 原子占位 → 立刻 200 → 异步处理 → DLQ。

架构文档 §L0 的关键时序：**慢活绝不放在 HTTP handler 里**。
GitHub 要求 10s 内 2xx；这里 handler 只做验签、去重占位、投递线程池，
Agent 全链路在后台跑，结果回写台账。

webhook 是 at-least-once，把它当「提示」而非「真相」：
同一 delivery_id 重发只累加 replay_count，绝不建第二个任务。
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

from ..platform.envelope import make_envelope, utcnow
from .normalize import delivery_id_from, normalize, parse_payload
from .signature import verify_signature

MAX_BODY_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    status_code: int
    accepted: bool
    duplicate: bool
    delivery_id: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class DeliveryRunner(Protocol):
    def submit_async(self, delivery: dict, raw: bytes, payload: dict, source: str) -> None: ...
    def wait(self) -> None: ...


def run_delivery(
    chief: Any,
    store: Any,
    delivery: dict,
    raw: bytes,
    payload: dict,
    source: str,
) -> dict:
    """同步执行一次投递（供后台线程与 DLQ 重试共用）。"""
    intent = normalize(payload, raw, source=source)
    result = chief.handle(intent)
    # 把外部投递 ID 锚进证据链，回放时能从 webhook 追到 run
    store.emit(
        make_envelope(
            run_id=result.run_id,
            task_id=result.task_id,
            actor="inbox-listener",
            role="listener",
            event_type="inbox.linked",
        ),
        {
            "delivery_id": delivery["delivery_id"],
            "source": source,
            "payload_hash": delivery["payload_hash"],
            "raw_ref": delivery.get("raw_ref"),
            "run_id": result.run_id,
        },
    )
    store.mark_delivery(
        delivery["delivery_id"], status="DONE", workflow_id=result.run_id
    )
    return {
        "delivery_id": delivery["delivery_id"],
        "run_id": result.run_id,
        "state": result.state.value,
        "verdict": result.verdict,
    }


class InlineRunner:
    """默认 runner：进程内线程池。P1 换 DBOS 时实现同一协议即可。"""

    def __init__(self, store: Any, chief_factory: Callable[[], Any], max_workers: int = 2):
        self.store = store
        self.chief_factory = chief_factory
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="inbox-worker")
        self._futures: list[Future] = []
        self.results: list[dict] = []
        self.errors: list[dict] = []

    def submit_async(self, delivery: dict, raw: bytes, payload: dict, source: str) -> None:
        self._futures.append(self.pool.submit(self._run, delivery, raw, payload, source))

    def _run(self, delivery: dict, raw: bytes, payload: dict, source: str) -> None:
        try:
            chief = self.chief_factory()
            out = run_delivery(chief, self.store, delivery, raw, payload, source)
            self.results.append(out)
        except Exception as exc:  # 失败进 DLQ，不吞异常、不静默丢事件
            reason = f"{type(exc).__name__}: {exc}"
            self.store.mark_delivery(
                delivery["delivery_id"], status="DLQ", failure_reason=reason
            )
            self.errors.append({"delivery_id": delivery["delivery_id"], "reason": reason})

    def wait(self, timeout: float | None = 30) -> None:
        if self._futures:
            wait_futures(self._futures, timeout=timeout)
            self._futures.clear()

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True)


def receive_webhook(
    store: Any,
    raw: bytes,
    headers: dict,
    *,
    secret: str,
    runner: DeliveryRunner,
    objects: Any,
    source: str = "generic",
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> WebhookReceipt:
    """纯函数版 handler：不依赖 HTTP 服务器，便于单测与重放。"""
    now = time.time() if now is None else now
    if len(raw) > MAX_BODY_BYTES:
        return WebhookReceipt(413, False, False, "", "payload 超过 1 MiB")
    if not raw:
        return WebhookReceipt(400, False, False, "", "空 body")

    sig = verify_signature(raw, headers, secret, now=now, tolerance_seconds=tolerance_seconds)
    try:
        payload = parse_payload(raw)
    except ValueError as exc:
        return WebhookReceipt(400, False, False, "", str(exc))

    delivery_id, key_origin = delivery_id_from(headers, payload, raw)
    received_at = utcnow()
    payload_hash = hashlib.sha256(raw).hexdigest()

    if not sig.ok:
        # 签名失败不占用真实 delivery_id，避免攻击者用坏签名阻断合法投递；
        # 只写一条独立审计台账。
        audit_id = f"rejected:{delivery_id}:{int(now * 1000)}"
        store.put_delivery({
            "delivery_id": audit_id, "source": source,
            "signature_ok": False, "timestamp_ok": sig.timestamp_ok,
            "payload_hash": payload_hash, "received_at": received_at,
            "status": "REJECTED", "failure_reason": sig.reason,
            "workflow_id": None, "raw_ref": None,
        })
        return WebhookReceipt(401, False, False, delivery_id, sig.reason)

    raw_ref = objects.put(delivery_id, raw)
    delivery = {
        "delivery_id": delivery_id, "source": source,
        "signature_ok": True, "timestamp_ok": sig.timestamp_ok,
        "payload_hash": payload_hash, "received_at": received_at,
        "status": "PENDING", "failure_reason": None,
        "workflow_id": None, "raw_ref": raw_ref,
    }
    first = store.put_delivery(delivery)  # 单条 INSERT 原子占位
    if not first:
        return WebhookReceipt(
            200, False, True, delivery_id,
            f"重复投递（{key_origin}），replay_count 已累加，未建第二个任务",
        )

    runner.submit_async(delivery, raw, payload, source)
    return WebhookReceipt(200, True, False, delivery_id, f"已占位并异步处理（去重键 {key_origin}）")
