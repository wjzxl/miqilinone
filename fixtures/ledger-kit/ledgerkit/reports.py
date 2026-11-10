"""报表渲染：结构化数据 -> 稳定的文本输出。"""

from __future__ import annotations

from .core import total


def render(entries) -> str:
    lines = [f"{label}: {amount}" for label, amount in sorted(entries.items())]
    lines.append(f"TOTAL={total(entries.values())}")
    return "\n".join(lines)
