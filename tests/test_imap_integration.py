from __future__ import annotations

import os
import socket
import time
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from dear_agent.auth import EmailAuthGate
from dear_agent.imap.client import ImapClient
from dear_agent.smtp.client import SmtpClient
from dear_agent.transports.base import OutboundMessage
from dear_agent.transports.imap import ImapSmtpTransport

HOST = os.environ.get("DEAR_AGENT_TEST_IMAP_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="DEAR_AGENT_TEST_IMAP_HOST is not set")

IMAP_PORT = int(os.environ.get("DEAR_AGENT_TEST_IMAP_PORT", "3143"))
SMTP_PORT = int(os.environ.get("DEAR_AGENT_TEST_SMTP_PORT", "3025"))
USER = os.environ.get("DEAR_AGENT_TEST_IMAP_USER", "green")
PASSWORD = os.environ.get("DEAR_AGENT_TEST_IMAP_PASSWORD", "pwd")
ADDRESS = os.environ.get("DEAR_AGENT_TEST_IMAP_TO", "green@example.com")
USE_SSL = os.environ.get("DEAR_AGENT_TEST_IMAP_SSL", "0") == "1"

AUTH_RESULTS = "mx.test; dkim=pass header.d=gmail.com; dmarc=pass header.from=gmail.com"


def _wait_for(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    pytest.skip(f"mail server on {HOST}:{port} is not reachable")


@pytest.fixture()
def transport() -> ImapSmtpTransport:
    _wait_for(SMTP_PORT)
    _wait_for(IMAP_PORT)
    imap = ImapClient(host=HOST, user=USER, password=PASSWORD, port=IMAP_PORT, ssl=USE_SSL)
    smtp = SmtpClient(
        host=HOST, port=SMTP_PORT, user=USER, password=PASSWORD, starttls=False, ssl=False
    )
    return ImapSmtpTransport(
        imap=imap, smtp=smtp, mailbox="INBOX", done_mailbox="Dear-Agent-Done", sender=ADDRESS
    )


def _poll_until(transport: ImapSmtpTransport, predicate, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages = transport.poll()
        if any(predicate(message) for message in messages):
            return messages
        time.sleep(1)
    raise AssertionError("message did not arrive")


def test_receive_the_dmarc_gate_and_ack(transport: ImapSmtpTransport) -> None:
    raw = EmailMessage()
    raw["From"] = "ricardo.arguello@gmail.com"
    raw["To"] = ADDRESS
    raw["Subject"] = "integration task"
    message_id = make_msgid()
    raw["Message-ID"] = message_id
    raw["Authentication-Results"] = AUTH_RESULTS
    raw.set_content("repo: https://github.com/dearagent-dev/dear-agent-lab-python\n\nfix the tests")
    transport.smtp.send(raw.as_bytes(), sender="ricardo.arguello@gmail.com", recipients=[ADDRESS])

    messages = _poll_until(transport, lambda message: message.transport_id == message_id)
    received = next(message for message in messages if message.transport_id == message_id)

    assert received.sender == "ricardo.arguello@gmail.com"
    assert "fix the tests" in received.body
    assert "dmarc=pass" in received.headers.get("Authentication-Results", "")

    # The real gate accepts an authenticated, aligned sender.
    EmailAuthGate(allowed_domains=frozenset({"gmail.com"}), auth_serv_id="mx.test").admit(
        body=received.body, headers=received.headers, sender=received.sender
    )

    transport.ack(messages)
    assert all(message.transport_id != message_id for message in transport.poll())


def test_send_delivers_a_threaded_reply(transport: ImapSmtpTransport) -> None:
    transport.send(
        OutboundMessage(
            thread_id=None,
            subject="re: integration task",
            body="queued",
            headers={"to": ADDRESS, "in-reply-to": "<original@x>"},
        )
    )

    messages = _poll_until(transport, lambda message: message.subject == "re: integration task")
    reply = next(message for message in messages if message.subject == "re: integration task")

    assert reply.sender == ADDRESS
    transport.ack(messages)
