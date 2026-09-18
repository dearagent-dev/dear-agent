from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herald.http_server import HealthServer
from herald.queue.memory import MemoryQueue
from herald.queue.models import Task


@pytest.fixture()
def server() -> HealthServer:
    queue = MemoryQueue()
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    instance = HealthServer(queue=queue, host="127.0.0.1", port=0)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def get(server: HealthServer, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_health_returns_ok_and_counts(server: HealthServer) -> None:
    status, payload = get(server, "/health")

    assert status == 200
    assert payload["ok"] is True
    assert payload["counts"]["queued"] == 1


def test_unknown_path_is_404(server: HealthServer) -> None:
    status, payload = get(server, "/nope")

    assert status == 404
    assert payload["error"] == "not found"


def test_health_payload_is_serializable() -> None:
    payload = HealthServer(queue=MemoryQueue()).health_payload(now=datetime(2026, 1, 1, tzinfo=UTC))

    assert payload["checked_at"] == "2026-01-01T00:00:00+00:00"
    assert json.dumps(payload)


def test_starting_twice_is_an_error() -> None:
    instance = HealthServer(queue=MemoryQueue(), host="127.0.0.1", port=0)
    instance.start()
    try:
        with pytest.raises(RuntimeError):
            instance.start()
    finally:
        instance.stop()


def post(server: HealthServer, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{server.bound_port}{path}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_inbound_without_a_handler_is_503() -> None:
    instance = HealthServer(queue=MemoryQueue(), host="127.0.0.1", port=0)
    instance.start()
    try:
        status, payload = post(instance, "/inbound", b"{}", {})
    finally:
        instance.stop()

    assert status == 503


def test_inbound_route_delegates_to_the_handler() -> None:
    seen: dict[str, object] = {}

    def handler(body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        seen["body"] = body
        return 202, {"accepted": ["e1"]}

    instance = HealthServer(queue=MemoryQueue(), host="127.0.0.1", port=0, inbound=handler)
    instance.start()
    try:
        status, payload = post(instance, "/inbound", b'{"id": "e1"}', {"X-Herald-Signature": "x"})
    finally:
        instance.stop()

    assert status == 202
    assert payload == {"accepted": ["e1"]}
    assert seen["body"] == b'{"id": "e1"}'


def test_oversized_body_is_413_and_not_read() -> None:
    called = False

    def handler(body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        nonlocal called
        called = True
        return 202, {}

    instance = HealthServer(
        queue=MemoryQueue(), host="127.0.0.1", port=0, inbound=handler, max_body_bytes=16
    )
    instance.start()
    try:
        status, payload = post(instance, "/inbound", b"x" * 17, {})
    finally:
        instance.stop()

    assert status == 413
    assert payload["error"] == "request body too large"
    assert called is False
