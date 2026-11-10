"""L0 接入层：最小 HTTP 服务器（标准库，零 Web 框架依赖）。

生产环境应换成 FastAPI/ASGI + 反代；这里保留最小实现，
重点是把「立刻返回 200」的时序与验签/去重逻辑跑通。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .handler import receive_webhook


class WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, *, secret: str, runner, objects, source: str = "generic"):
        self.store = store
        self.secret = secret
        self.runner = runner
        self.objects = objects
        self.source = source
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: WebhookHTTPServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 压掉默认 access log；真实日志走事件表

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"ok": True, "counts": self.server.store.counts()})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "invalid Content-Length"})
            return
        raw = self.rfile.read(length)
        receipt = receive_webhook(
            self.server.store,
            raw,
            dict(self.headers.items()),
            secret=self.server.secret,
            runner=self.server.runner,
            objects=self.server.objects,
            source=self.server.source,
        )
        self._reply(receipt.status_code, receipt.to_dict())

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
