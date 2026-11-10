"""L0 接入层：webhook 验签（raw bytes / 常量时间 / 时间戳窗口）。

架构文档 §L0 的硬机制，三条一条都不能省：
1. 验签用 **raw bytes**，绝不把 JSON 解析后重新序列化（字段顺序/空格一变签名就废）
2. `hmac.compare_digest` 常量时间比较，防时序侧信道
3. 时间戳窗口防重放；没有时间戳头的 provider 明确标注，靠 delivery_id 去重兜底

支持三种签名方案：GitHub `X-Hub-Signature-256`、Slack `v0`、以及自建
`X-Michelin-Signature`（飞书/企业微信/自建群机器人走这个）。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignatureResult:
    ok: bool
    scheme: str
    timestamp_ok: bool
    reason: str


def _lower(headers: dict) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _digest(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _extract(provided: str, prefix: str) -> str | None:
    if not provided.startswith(prefix):
        return None
    return provided[len(prefix):].strip()


def verify_signature(
    raw: bytes,
    headers: dict,
    secret: str,
    *,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> SignatureResult:
    """只接受原始字节；返回结构化结果，调用方据此决定 200/401 与台账状态。"""
    h = _lower(headers)
    now = time.time() if now is None else now

    if "x-hub-signature-256" in h:
        scheme = "github"
        signed_payload = raw
        provided = _extract(h["x-hub-signature-256"], "sha256=")
        ts_raw = h.get("x-michelin-timestamp")
    elif "x-slack-signature" in h:
        scheme = "slack"
        ts_raw = h.get("x-slack-request-timestamp")
        if not ts_raw:
            return SignatureResult(False, "slack", False, "缺少 X-Slack-Request-Timestamp")
        signed_payload = b"v0:" + ts_raw.encode() + b":" + raw
        provided = _extract(h["x-slack-signature"], "v0=")
    elif "x-michelin-signature" in h:
        scheme = "michelin"
        ts_raw = h.get("x-michelin-timestamp")
        if not ts_raw:
            return SignatureResult(False, "michelin", False, "缺少 X-Michelin-Timestamp")
        signed_payload = ts_raw.encode() + b":" + raw
        provided = _extract(h["x-michelin-signature"], "sha256=")
    else:
        return SignatureResult(False, "none", False, "没有可识别的签名头")

    if not provided:
        return SignatureResult(False, scheme, False, "签名头格式不正确")

    # 时间戳窗口：存在的头必须合法且在窗口内；缺失时只依赖 delivery_id 去重。
    if ts_raw:
        try:
            delta = abs(now - int(ts_raw))
        except ValueError:
            return SignatureResult(False, scheme, False, "时间戳不是整数")
        if delta > tolerance_seconds:
            return SignatureResult(
                False, scheme, False,
                f"时间戳超出 {tolerance_seconds}s 窗口（delta={int(delta)}s），疑似重放",
            )
        timestamp_ok = True
        ts_note = "时间戳窗口内"
    else:
        timestamp_ok = True
        ts_note = "provider 无时间戳头，依赖 delivery_id 去重防重放"

    expected = _digest(secret, signed_payload)
    if not hmac.compare_digest(provided, expected):
        return SignatureResult(False, scheme, timestamp_ok, "签名不匹配")
    return SignatureResult(True, scheme, timestamp_ok, f"验签通过（{ts_note}）")


def sign(secret: str, raw: bytes, *, scheme: str = "michelin", timestamp: int | None = None) -> dict:
    """测试与自建客户端用的签名构造器。"""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    if scheme == "github":
        return {"X-Hub-Signature-256": "sha256=" + _digest(secret, raw)}
    if scheme == "slack":
        return {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": "v0=" + _digest(secret, b"v0:" + ts.encode() + b":" + raw),
        }
    return {
        "X-Michelin-Timestamp": ts,
        "X-Michelin-Signature": "sha256=" + _digest(secret, ts.encode() + b":" + raw),
    }
