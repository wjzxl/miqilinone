"""L0 接入层：把外部事件标准化成 TaskIntent，并选出稳定的去重键。

一手信息保留原则：source_bodies 里放的是**原始 JSON 文本 + 原始正文字段**，
不是摘要、不是模型改写。worker 拿到的是原文 + source_hash（架构文档 §L1）。

去重键优先级（架构文档 §L0）：provider 自带的 delivery/event id > 自建 id > raw 内容哈希。
绝不拿业务对象 id 当去重键（Stripe 的 data.object.id 会复用）。
"""

from __future__ import annotations

import hashlib
import json

from ..chief.policy import TaskIntent


def parse_payload(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"payload 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("payload 顶层必须是对象")
    return data


def delivery_id_from(headers: dict, payload: dict, raw: bytes) -> tuple[str, str]:
    """返回 (delivery_id, 来源说明)。"""
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    for key, origin in (
        ("x-github-delivery", "X-GitHub-Delivery"),
        ("x-linear-delivery", "Linear-Delivery"),
        ("x-delivery-id", "X-Delivery-Id"),
    ):
        if h.get(key):
            return h[key], origin
    # Slack 的 event_id 在 body 里，且一次投递可能含多个外层事件
    if payload.get("event_id"):
        return str(payload["event_id"]), "Slack event_id"
    if payload.get("delivery_id"):
        return str(payload["delivery_id"]), "payload.delivery_id"
    return hashlib.sha256(raw).hexdigest(), "sha256(raw) 兜底（建议 provider 自带 delivery id）"


def normalize(payload: dict, raw: bytes, *, source: str = "generic") -> TaskIntent:
    """标准化为 TaskIntent。永远附带原始 JSON 全文，便于回放核对。"""
    raw_text = raw.decode("utf-8", errors="replace")
    bodies: list[str] = [f"【原始投递全文】\n{raw_text}"]

    if source == "github" and isinstance(payload.get("issue"), dict):
        issue = payload["issue"]
        intent = f"GitHub issue #{issue.get('number', '?')}: {issue.get('title', '')}"
        if issue.get("body"):
            bodies.append(f"【issue 正文原文】\n{issue['body']}")
        if payload.get("repository", {}).get("full_name"):
            bodies.append(f"【仓库】{payload['repository']['full_name']}")
    else:
        intent = str(payload.get("intent") or payload.get("text") or "外部投递的只读分析需求")
        for item in payload.get("sources", []) or []:
            bodies.append(f"【来源原文】\n{item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)}")

    return TaskIntent(
        intent=intent,
        source_bodies=tuple(bodies),
        source_system=source,
        risk_level=str(payload.get("risk_level", "low")),
        budget=float(payload.get("budget", 5.0)),
        evidence_marker=str(payload.get("evidence_marker", "EVIDENCE:ok")),
    )
