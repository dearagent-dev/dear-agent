from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from herald.observability.health import Health
from herald.queue.port import Queue

HealthPayload = Callable[[], dict[str, Any]]
InboundHandler = Callable[[bytes, dict[str, str]], tuple[int, dict[str, Any]]]


class _HeraldHTTPServer(ThreadingHTTPServer):
    health_payload: HealthPayload
    inbound_handler: InboundHandler | None


class _Handler(BaseHTTPRequestHandler):
    server_version = "Herald/0.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Keep stdout clean; operators use metrics, not access logs.
        return

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            self._json(200, self.server.health_payload())  # type: ignore[attr-defined]
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/inbound":
            self._json(404, {"error": "not found"})
            return
        handler = self.server.inbound_handler  # type: ignore[attr-defined]
        if handler is None:
            self._json(503, {"error": "inbound webhook is not configured"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        headers = {key: value for key, value in self.headers.items()}
        status, payload = handler(body, headers)
        self._json(status, payload)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass(slots=True)
class HealthServer:
    """Minimal HTTP entrypoint exposing ``/health``.

    Deliberately stdlib-only: Herald's HTTP surface is small (health now, the inbound
    webhook later), so an ASGI stack is not worth the dependency yet. The business logic
    stays in the ``ControlPlane``; this is a thin adapter.
    """

    queue: Queue
    host: str = "0.0.0.0"
    port: int = 8080
    max_running: int = 1
    inbound: InboundHandler | None = None
    _httpd: _HeraldHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)

    def health_payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        status = Health(max_running=self.max_running).check(self.queue, now=now)
        return status.to_dict()

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("server already started")
        self._httpd = _HeraldHTTPServer((self.host, self.port), _Handler)
        self._httpd.health_payload = self.health_payload
        self._httpd.inbound_handler = self.inbound
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def bound_port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server is not running")
        return int(self._httpd.server_address[1])

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> HealthServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["HealthServer"]
