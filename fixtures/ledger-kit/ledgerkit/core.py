"""记账核心：只做纯计算，不碰 IO。"""

from __future__ import annotations


def total(amounts) -> int:
    return sum(int(a) for a in amounts)


def balance(entries) -> dict:
    """entries: [(label, amount), ...] -> 按 label 聚合。"""
    out: dict[str, int] = {}
    for label, amount in entries:
        out[str(label)] = out.get(str(label), 0) + int(amount)
    return out
