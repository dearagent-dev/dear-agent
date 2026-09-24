from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Thread
from typing import Any

from dear_agent.observability.health import Health
from dear_agent.queue.port import Queue

HealthPayload = Callable[[], dict[str, Any]]
InboundHandler = Callable[[bytes, dict[str, str]], tuple[int, dict[str, Any]]]
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_MAX_WORKERS = 32


class _DearAgentHTTPServer(ThreadingHTTPServer):
    health_payload: HealthPayload
    inbound_handler: InboundHandler | None
    max_body_bytes: int
    timeout: float
    max_workers: int

    def __init__(self, *args: Any, max_workers: int = DEFAULT_MAX_WORKERS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_workers = max_workers
        self._slots = BoundedSemaphore(max_workers)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Bound concurrency so a flood cannot exhaust threads.
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that disconnects or times out is normal; never print a traceback.
        return


class _Handler(BaseHTTPRequestHandler):
    server_version = "Dear Agent/0.0"

    def setup(self) -> None:
        # Apply the server's socket timeout so an idle/slow client cannot hold a thread open.
        self.timeout = self.server.timeout  # type: ignore[attr-defined]
        super().setup()

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
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length else 0
        except (TypeError, ValueError):
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length > self.server.max_body_bytes:  # type: ignore[attr-defined]
            self._json(413, {"error": "request body too large"})
            return
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

    Deliberately stdlib-only: Dear Agent's HTTP surface is small (health now, the inbound
    webhook later), so an ASGI stack is not worth the dependency yet. The business logic
    stays in the ``ControlPlane``; this is a thin adapter.
    """

    queue: Queue
    host: str = "0.0.0.0"
    port: int = 8080
    max_running: int = 1
    inbound: InboundHandler | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    timeout_seconds: float = 15.0
    max_workers: int = DEFAULT_MAX_WORKERS
    _httpd: _DearAgentHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)

    def health_payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        status = Health(max_running=self.max_running).check(self.queue, now=now)
        return status.to_dict()

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("server already started")
        self._httpd = _DearAgentHTTPServer(
            (self.host, self.port), _Handler, max_workers=self.max_workers
        )
        self._httpd.health_payload = self.health_payload
        self._httpd.inbound_handler = self.inbound
        self._httpd.max_body_bytes = self.max_body_bytes
        self._httpd.timeout = self.timeout_seconds
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
