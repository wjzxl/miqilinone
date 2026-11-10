"""可观测层：append-only 哈希链的 span 日志 + OTel GenAI 语义属性。

三条纪律：
1. **观测 trace ≠ 执行日志**。P0 事件脊是完整、不可变、长期保留的因果链；这里是可采样、
   可脱敏、可导出 Langfuse/OTel 的链路，绝不拿它替代事件脊做裁决。
2. **semconv 版本必须 pin**。OTel GenAI semantic conventions 仍处 Development、无 tagged
   release；每条 span 都带 `semconv_version`，回放时才知道 `gen_ai.*` 该怎么解释。
3. **链本身可被证伪**。篡改任意一行 -> `verify_chain()` 必须报错；门禁里真的改一次。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..platform.envelope import SEMCONV_VERSION, canonical, content_hash, new_id, utcnow

GENESIS = "GENESIS"


def _line_hash(prev: str, payload: dict) -> str:
    return content_hash({"prev": prev, "payload": payload})


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    ok: bool
    count: int
    bad_index: int | None
    head_hash: str

    def to_dict(self) -> dict:
        return {"ok": self.ok, "count": self.count,
               "bad_index": self.bad_index, "head_hash": self.head_hash}


class SpanLog:
    """一行一条 span，每行携带前一行哈希；只追加，崩溃恢复可续写。"""

    def __init__(self, path: Path, *, semconv_version: str = SEMCONV_VERSION):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.semconv_version = semconv_version
        self.prev = GENESIS
        self.seq = 0
        self.corrupt = False
        if self.path.exists():
            lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln]
            for ln in lines:
                row = json.loads(ln)
                self.prev = row["hash"]
                self.seq = row["seq"]
            self.corrupt = not self.verify_chain().ok

    def append(self, span: dict[str, Any]) -> str:
        if self.corrupt:
            raise RuntimeError("span 链已损坏，拒绝在损坏的链上续写（应先人工核查）")
        self.seq += 1
        payload = {"seq": self.seq, "span": span,
                   "semconv_version": self.semconv_version}
        digest = _line_hash(self.prev, payload)
        row = {"seq": self.seq, "hash": digest, "payload": payload}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self.prev = digest
        return digest

    def verify_chain(self) -> ChainVerdict:
        prev = GENESIS
        count = 0
        if not self.path.exists():
            return ChainVerdict(True, 0, None, prev)
        for idx, ln in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not ln:
                continue
            try:
                row = json.loads(ln)
                expected = _line_hash(prev, row["payload"])
                if row["hash"] != expected or row["seq"] != count + 1:
                    return ChainVerdict(False, count, idx, prev)
                prev = row["hash"]
                count += 1
            except (json.JSONDecodeError, KeyError, TypeError):
                return ChainVerdict(False, count, idx, prev)
        return ChainVerdict(True, count, None, prev)

    def spans(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(ln)["payload"]["span"]
                for ln in self.path.read_text(encoding="utf-8").splitlines() if ln]

    def export_otel(self, *, service: str = "michelin-workbench") -> dict:
        """最小 OTel 导出形状；刻意不引 opentelemetry-sdk（CI 零依赖）。"""
        return {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": service}},
                    {"key": "otel.genai.semconv_pin", "value": {"stringValue": self.semconv_version}},
                ]},
                "scopeSpans": [{
                    "scope": {"name": "michelin.runtime", "version": "p5"},
                    "spans": self.spans(),
                }],
            }],
        }


@dataclass
class _SpanHandle:
    span: dict[str, Any]
    start: float = field(default_factory=time.monotonic)
    closed: bool = False

    def set(self, **attrs: Any) -> None:
        self.span.setdefault("attributes", {}).update(attrs)


class Tracer:
    def __init__(self, path: Path, run_id: str, *, service: str = "michelin-workbench"):
        self.log = SpanLog(path)
        self.run_id = run_id
        self.trace_id = content_hash({"trace": run_id})[:32]
        self.service = service
        self._stack: list[str] = []

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[_SpanHandle]:
        span_id = new_id("span")
        parent_id = self._stack[-1] if self._stack else None
        record: dict[str, Any] = {
            "traceId": self.trace_id, "spanId": span_id,
            "parentSpanId": parent_id, "name": name,
            "startTimeUnixNano": str(time.time_ns()),
            "status": {"code": "UNSET"},
            "attributes": {
                "gen_ai.operation.name": name,
                "gen_ai.provider.name": "michelin.local",
                "michelin.run_id": self.run_id,
                "semconv_version": self.log.semconv_version,
                **{str(k): v for k, v in attrs.items()},
            },
        }
        self._stack.append(span_id)
        handle = _SpanHandle(record)
        try:
            yield handle
            record["status"] = {"code": "OK"}
        except Exception as exc:
            record["status"] = {"code": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
            handle.set(error_type=type(exc).__name__)
            raise
        finally:
            record["endTimeUnixNano"] = str(time.time_ns())
            record["durationMs"] = round((time.monotonic() - handle.start) * 1000, 2)
            record["attributes"]["michelin.duration_ms"] = record["durationMs"]
            self.log.append(record)
            handle.closed = True
            self._stack.pop()

    def summary(self) -> dict:
        verdict = self.log.verify_chain()
        spans = self.log.spans()
        return {
            "trace_id": self.trace_id,
            "span_count": len(spans),
            "span_names": [s["name"] for s in spans],
            "statuses": [s["status"]["code"] for s in spans],
            "semconv_version": self.log.semconv_version,
            "chain": verdict.to_dict(),
            "log_path": str(self.log.path),
        }
