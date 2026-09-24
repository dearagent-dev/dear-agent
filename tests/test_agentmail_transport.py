from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from dear_agent.transports.agentmail import AgentMailClient, AgentMailTransport
from dear_agent.transports.base import OutboundMessage

INBOX = "bot@agentmail.to"


class FakeClient:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.added: list[tuple[str, list[str]]] = []
        self.replies: list[tuple[str, str | None, str]] = []
        self.sent: list[tuple[str, str, str]] = []

    def default_inbox_id(self) -> str:
        return INBOX

    def list_messages(
        self, inbox_id: str, *, labels: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self.messages

    def update_labels(
        self,
        inbox_id: str,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        self.added.append((message_id, add or []))

    def reply(self, inbox_id: str, message_id: str, *, text: str, to: str | None = None) -> None:
        self.replies.append((message_id, to, text))

    def send(self, inbox_id: str, *, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))


def message(**overrides: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "message_id": "<m1@agentmail.to>",
        "thread_id": "t1",
        "from_": ["dev@example.com"],
        "subject": "add healthz",
        "text": "repo: https://github.com/o/r\n\nadd healthz",
        "labels": ["received"],
        "timestamp": "2026-01-01T00:00:00Z",
        "headers": {"Message-ID": "<m1@agentmail.to>"},
    }
    values.update(overrides)
    return values


def test_poll_maps_a_received_message() -> None:
    transport = AgentMailTransport(FakeClient([message()]))

    polled = transport.poll()

    assert len(polled) == 1
    raw = polled[0]
    assert raw.transport_id == "<m1@agentmail.to>"
    assert raw.thread_id == "t1"
    assert raw.sender == "dev@example.com"
    assert raw.subject == "add healthz"
    assert "add healthz" in raw.body
    assert raw.received_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_poll_skips_already_processed_messages() -> None:
    processed = message(message_id="<done@x>", labels=["received", "dear-agent-done"])
    transport = AgentMailTransport(FakeClient([message(), processed]))

    polled = transport.poll()

    assert [raw.transport_id for raw in polled] == ["<m1@agentmail.to>"]


def test_ack_labels_the_message() -> None:
    client = FakeClient([message()])
    transport = AgentMailTransport(client)
    raw = transport.poll()[0]

    transport.ack([raw])

    assert client.added == [("<m1@agentmail.to>", ["dear-agent-done"])]


def test_send_replies_in_thread_when_there_is_an_in_reply_to() -> None:
    client = FakeClient()
    transport = AgentMailTransport(client)

    transport.send(
        OutboundMessage(
            thread_id="t1",
            subject="done",
            body="PR: https://example/pr/1",
            headers={"to": "dev@example.com", "in-reply-to": "<m1@agentmail.to>"},
        )
    )

    assert client.replies == [("<m1@agentmail.to>", "dev@example.com", "PR: https://example/pr/1")]
    assert client.sent == []


def test_send_starts_a_new_message_without_a_thread() -> None:
    client = FakeClient()
    transport = AgentMailTransport(client)

    transport.send(
        OutboundMessage(
            thread_id=None, subject="hi", body="body", headers={"to": "dev@example.com"}
        )
    )

    assert client.sent == [("dev@example.com", "hi", "body")]


def test_the_default_inbox_is_discovered() -> None:
    transport = AgentMailTransport(FakeClient())

    assert transport._inbox_id == INBOX  # noqa: SLF001 - asserting discovery


def test_client_composes_the_send_request(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(request: Any, timeout: Any = None) -> Response:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.data
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    AgentMailClient("secret").send(INBOX, to="a@b.c", subject="s", text="t")

    assert captured["url"] == f"https://api.agentmail.to/v0/inboxes/{INBOX}/messages/send"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer secret"
    assert b'"subject": "s"' in captured["body"]
