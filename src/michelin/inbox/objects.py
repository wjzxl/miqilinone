"""L0 接入层：原始投递的对象存储（表内只存引用，不存大对象）。

架构文档 §L1：大对象存对象存储，表内只存引用。P1 用本地目录实现，
P4 换 S3/COS 时只要替换 put/get，上层不变。
文件名用 delivery_id 的哈希，避免外部 header 造成路径穿越。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class InboxObjectStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, delivery_id: str, raw: bytes) -> str:
        safe = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        path = self.root / f"{safe}.json"
        path.write_bytes(raw)
        return str(path)

    def get(self, ref: str) -> bytes:
        if not ref:
            raise KeyError("没有 raw_ref，无法取回原始投递")
        return Path(ref).read_bytes()
