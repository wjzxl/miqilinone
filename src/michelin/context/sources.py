"""L1 上下文层：一手原文 append-only 引用平面。

这一层刻意**没有** summarize() / distill() 方法。架构文档 §L1 的判断是：
mem0 / Cognee / Letta 这类「记忆层」天生在做压缩摘要，与「给源头信息、不给二手信息」
正面冲突。worker 拿到的永远是原始字节 + SHA-256，摘要（若将来引入）只用于检索排序，
绝不进入 worker 或 verifier 上下文。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..platform.envelope import utcnow
from ..platform.store import EventStore


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_hash: str
    source_system: str
    source_uri: str
    raw_bytes: bytes
    occurred_at: str
    ingested_at: str

    @property
    def text(self) -> str:
        return self.raw_bytes.decode("utf-8", errors="replace")


class SourcePlane:
    def __init__(self, store: EventStore):
        self.store = store

    def ingest(
        self,
        raw: bytes | str,
        *,
        source_system: str,
        source_uri: str,
        occurred_at: str | None = None,
    ) -> SourceDocument:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        source_hash = hashlib.sha256(raw_bytes).hexdigest()
        when = occurred_at or utcnow()
        self.store.put_source_document(
            source_hash, source_system, source_uri, raw_bytes, when
        )
        return self.fetch(source_hash)  # type: ignore[return-value]

    def fetch(self, source_hash: str) -> SourceDocument:
        row = self.store.get_source_document(source_hash)
        if row is None:
            raise KeyError(f"一手原文不存在: {source_hash}")
        return SourceDocument(
            source_hash=row["source_hash"],
            source_system=row["source_system"],
            source_uri=row["source_uri"],
            raw_bytes=row["raw_bytes"],
            occurred_at=row["occurred_at"],
            ingested_at=row["ingested_at"],
        )

    def retrieve_for_kickoff(self, source_hashes: list[str]) -> list[SourceDocument]:
        """检索工具只回「带引用 + 原文」，不回改写段落。"""
        return [self.fetch(h) for h in source_hashes]
