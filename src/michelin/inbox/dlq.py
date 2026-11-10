"""L0 接入层：DLQ 与对账。

架构文档 §L0：webhook 是 at-least-once，把它当「提示」而非「真相」；
去重键保留期长于最长重试窗口，定期按 provider API 回补。
"""

from __future__ import annotations

from typing import Any

from .handler import run_delivery
from .normalize import parse_payload


def reconcile(store: Any, remote_ids: set[str], *, source: str | None = None) -> dict:
    """拿 provider 的投递 ID 列表与本地台账对账，找出漏投递与本地多出的记录。"""
    local_rows = store.list_deliveries()
    if source:
        local_rows = [r for r in local_rows if r["source"] == source]
    local = {r["delivery_id"] for r in local_rows}
    missing = sorted(remote_ids - local)
    extra = sorted(i for i in local - remote_ids if not i.startswith("rejected:"))
    by_status: dict[str, int] = {}
    for row in local_rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "remote": len(remote_ids),
        "local": len(local),
        "missing": missing,
        "local_extra": extra,
        "by_status": by_status,
        "needs_backfill": bool(missing),
    }


def retry_dlq(
    store: Any,
    chief_factory: Any,
    objects: Any,
    *,
    limit: int = 10,
) -> list[dict]:
    """重试死信队列；原始字节从对象存储取回，不依赖任何内存状态。"""
    out = []
    for row in store.list_deliveries("DLQ")[:limit]:
        try:
            raw = objects.get(row["raw_ref"] or "")
            payload = parse_payload(raw)
            result = run_delivery(chief_factory(), store, row, raw, payload, row["source"])
            out.append({"delivery_id": row["delivery_id"], "status": "RETRIED", **result})
        except Exception as exc:
            out.append({
                "delivery_id": row["delivery_id"],
                "status": "STILL_FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
            })
    return out
