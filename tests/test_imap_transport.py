from __future__ import annotations

from email.message import EmailMessage

from dear_agent.transports.base import OutboundMessage
from dear_agent.transports.imap import ImapSmtpTransport
from dear_agent.transports.port import Transport


class FakeImapClient:
    def __init__(self, messages: dict[bytes, bytes] | None = None) -> None:
        self._messages = messages or {}
        self.connected = False
        self.selected: list[str] = []
        self.moved: list[tuple[bytes, str]] = []

    def connect(self) -> None:
        self.connected = True

    def select(self, mailbox: str, *, create: bool = True) -> None:
        self.selected.append(mailbox)

    def unread_uids(self) -> list[bytes]:
        return list(self._messages)

    def fetch_raw(self, uid: bytes) -> bytes:
        return self._messages[uid]

    def move(self, uid: bytes, target: str) -> None:
        self.moved.append((uid, target))

    def close(self) -> None:
        self.connected = False


class FakeSmtpClient:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, str, list[str]]] = []

    def send(self, raw: bytes, *, sender: str, recipients: list[str]) -> None:
        self.sent.append((raw, sender, recipients))


def build_raw(
    *,
    sender: str = "ricardo.arguello@gmail.com",
    subject: str = "a task",
    body: str = "repo: https://github.com/dearagent-dev/dear-agent-lab-python\n\nfix it",
    message_id: str = "<m1@x>",
    in_reply_to: str | None = None,
    auth_results: str | None = None,
    attachment: bool = False,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "dear-agent@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if auth_results:
        message["Authentication-Results"] = auth_results
    message.set_content(body)
    if attachment:
        message.add_attachment(b"patch", maintype="text", subtype="x-patch", filename="patch.diff")
    return message.as_bytes()


def make_transport(
    messages: dict[bytes, bytes] | None = None, *, done: str = "Dear-Agent-Done"
) -> tuple[ImapSmtpTransport, FakeImapClient, FakeSmtpClient]:
    imap = FakeImapClient(messages)
    smtp = FakeSmtpClient()
    transport = ImapSmtpTransport(
        imap=imap, smtp=smtp, done_mailbox=done, sender="dear-agent@arguello.ec"
    )
    return transport, imap, smtp


def test_imap_transport_satisfies_the_port() -> None:
    transport, _, _ = make_transport()

    assert isinstance(transport, Transport)


def test_poll_parses_a_message() -> None:
    transport, imap, _ = make_transport(
        {b"1": build_raw(subject="do it", auth_results="mx; dmarc=pass header.from=gmail.com")}
    )

    messages = transport.poll()

    assert len(messages) == 1
    message = messages[0]
    assert message.transport_id == "<m1@x>"
    assert message.sender == "ricardo.arguello@gmail.com"
    assert message.subject == "do it"
    assert "fix it" in message.body
    assert "dmarc=pass" in message.headers["Authentication-Results"]
    assert imap.selected == ["INBOX"]


def test_poll_without_messages_closes_the_connection() -> None:
    transport, imap, _ = make_transport()

    assert transport.poll() == []
    assert imap.connected is False


def test_poll_reports_attachments_so_the_normalizer_can_reject() -> None:
    transport, _, _ = make_transport({b"1": build_raw(attachment=True)})

    messages = transport.poll()

    assert messages[0].has_attachments is True
    assert messages[0].attachments[0].name == "patch.diff"


def test_poll_derives_the_thread_from_in_reply_to() -> None:
    transport, _, _ = make_transport({b"1": build_raw(message_id="<m2@x>", in_reply_to="<m1@x>")})

    assert transport.poll()[0].thread_id == "<m1@x>"


def test_ack_moves_processed_messages_to_the_done_mailbox() -> None:
    transport, imap, _ = make_transport({b"1": build_raw()})
    messages = transport.poll()

    transport.ack(messages)

    assert imap.moved == [(b"1", "Dear-Agent-Done")]


def test_send_threads_the_reply_and_sets_the_sender() -> None:
    transport, _, smtp = make_transport()
    outbound = OutboundMessage(
        thread_id="<m1@x>",
        subject="re: a task",
        body="queued",
        headers={"to": "ricardo.arguello@gmail.com", "in-reply-to": "<m1@x>"},
    )

    transport.send(outbound)

    raw, sender, recipients = smtp.sent[0]
    assert sender == "dear-agent@arguello.ec"
    assert recipients == ["ricardo.arguello@gmail.com"]
    text = raw.decode()
    assert "In-Reply-To: <m1@x>" in text
    assert "queued" in text


def test_send_requires_a_recipient() -> None:
    transport, _, _ = make_transport()

    try:
        transport.send(OutboundMessage(thread_id=None, subject="s", body="b"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")
