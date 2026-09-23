from __future__ import annotations

from typing import Any

import pytest

from dear_agent.jmap.client import JmapClient, JmapError

SESSION = {
    "apiUrl": "https://example.com/jmap/api/",
    "eventSourceUrl": "https://example.com/jmap/eventsource/?types=*",
    "accounts": {"u1": {"accountCapabilities": {"urn:ietf:params:jmap:mail": {}}}},
}


class StubClient(JmapClient):
    def __init__(self) -> None:
        super().__init__("token")
        self.requests: list[Any] = []
        self.responses: list[Any] = []

    def _request(self, url: str, *, data: bytes | None = None, method: str = "GET") -> Any:
        return SESSION

    def _response(self, url: str, payload: Any, call_id: str) -> Any:
        self.requests.append(payload)
        return self.responses.pop(0)


def test_connect_captures_the_event_source_url() -> None:
    client = StubClient()

    client.connect()

    assert client.event_source_url == SESSION["eventSourceUrl"]


def test_event_source_url_requires_connect() -> None:
    with pytest.raises(JmapError):
        _ = StubClient().event_source_url


def test_push_create_returns_the_subscription_id() -> None:
    client = StubClient()
    client.connect()
    client.responses.append({"created": {"p": {"id": "sub-1"}}})

    subscription_id = client.push_create()

    assert subscription_id == "sub-1"
    payload = client.requests[0]
    args = payload[0][1]
    assert args["create"]["p"]["types"] == ["Email"]


def test_push_create_passes_a_webhook_url() -> None:
    client = StubClient()
    client.connect()
    client.responses.append({"created": {"p": {"id": "sub-1"}}})

    client.push_create(url="https://dear_agent.example/inbound")

    args = client.requests[0][0][1]
    assert args["create"]["p"]["url"] == "https://dear_agent.example/inbound"


def test_push_create_surfaces_a_failure() -> None:
    client = StubClient()
    client.connect()
    client.responses.append({"notCreated": {"p": {"type": "invalidProperties"}}})

    with pytest.raises(JmapError):
        client.push_create()


def test_push_destroy_accepts_a_destroyed_id() -> None:
    client = StubClient()
    client.connect()
    client.responses.append({"destroyed": ["sub-1"]})

    client.push_destroy("sub-1")

    args = client.requests[0][0][1]
    assert args["destroy"] == ["sub-1"]


def test_push_destroy_rejects_an_unknown_id() -> None:
    client = StubClient()
    client.connect()
    client.responses.append({"notDestroyed": {"sub-1": {"type": "notFound"}}})

    with pytest.raises(JmapError):
        client.push_destroy("sub-1")
