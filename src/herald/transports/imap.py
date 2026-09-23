from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage, Message
from email.utils import make_msgid, parseaddr, parsedate_to_datetime
from typing import Protocol

from herald.transports.base import Attachment, OutboundMessage, RawMessage


class _ImapClient(Protocol):
    """The slice of an IMAP client the transport depends on."""

    def connect(self) -> None: ...
    def select(self, mailbox: str, *, create: bool = True) -> None: ...
    def unread_uids(self) -> list[bytes]: ...
    def fetch_raw(self, uid: bytes) -> bytes: ...
    def move(self, uid: bytes, target: str) -> None: ...
    def close(self) -> None: ...


class _SmtpClient(Protocol):
    """The slice of an SMTP client the transport depends on."""

    def send(self, raw: bytes, *, sender: str, recipients: list[str]) -> None: ...


@dataclass(slots=True)
class ImapSmtpTransport:
    """Email transport over IMAP (receive) and SMTP (send).

    A mail channel has two directions, so this adapter binds both protocols: IMAP lists and
    fetches unread messages and moves processed ones aside, SMTP submits replies threaded to
    the original ``Message-ID``. It carries no business logic — the wire format is parsed
    here so everything upstream sees the same :class:`RawMessage`.
    """

    imap: _ImapClient
    smtp: _SmtpClient
    mailbox: str = "INBOX"
    done_mailbox: str = "Herald-Done"
    sender: str = ""
    _pending: dict[str, bytes] = field(default_factory=dict, init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def poll(self, *, limit: int = 50) -> list[RawMessage]:
        self._connect()
        try:
            self.imap.select(self.mailbox)
            messages: list[RawMessage] = []
            for uid in self.imap.unread_uids()[:limit]:
                raw = self.imap.fetch_raw(uid)
                message = _parse_message(raw)
                self._pending[message.transport_id] = uid
                messages.append(message)
            messages.sort(key=lambda message: message.received_at)
            if not messages:
                self._disconnect()
            return messages
        except Exception:
            self._disconnect()
            raise

    def ack(self, messages: list[RawMessage]) -> None:
        """File handled messages into ``done_mailbox`` (best effort)."""
        if not messages or not self._pending:
            self._disconnect()
            return
        try:
            self.imap.select(self.mailbox)
            for message in messages:
                uid = self._pending.pop(message.transport_id, None)
                if uid is None:
                    continue
                try:
                    self.imap.move(uid, self.done_mailbox)
                except Exception:  # noqa: BLE001 - acknowledging is best effort
                    continue
        finally:
            self._disconnect()

    def receive(self, message: RawMessage) -> None:
        raise NotImplementedError("IMAP is a pull transport; use poll()")

    def send(self, message: OutboundMessage) -> None:
        recipient = message.headers.get("to")
        if not recipient:
            raise ValueError("outbound message needs a 'to' header")
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = recipient
        email["Subject"] = message.subject
        email["Message-ID"] = make_msgid()
        in_reply_to = message.headers.get("in-reply-to")
        if in_reply_to:
            email["In-Reply-To"] = in_reply_to
            email["References"] = in_reply_to
        email.set_content(message.body)
        self.smtp.send(email.as_bytes(), sender=self.sender, recipients=[recipient])

    def _connect(self) -> None:
        if not self._connected:
            self.imap.connect()
            self._connected = True

    def _disconnect(self) -> None:
        if self._connected:
            self.imap.close()
            self._connected = False


def _parse_message(raw: bytes) -> RawMessage:
    message = message_from_bytes(raw, policy=policy.default)
    message_id = (message.get("Message-ID") or "").strip()
    in_reply_to = (message.get("In-Reply-To") or "").strip()
    references = (message.get("References") or "").strip()
    thread_id = in_reply_to or (references.split()[-1] if references else None)
    body, attachments = _extract_body(message)
    return RawMessage(
        transport_id=message_id or _fallback_id(raw),
        thread_id=thread_id,
        sender=parseaddr(message.get("From", ""))[1] or None,
        subject=message.get("Subject"),
        body=body,
        attachments=attachments,
        headers=_collect_headers(message),
        received_at=_received_at(message),
    )


def _collect_headers(message: Message) -> dict[str, str]:
    """Collect headers, joining duplicates with a newline.

    Duplicates matter: Fastmail (and others) emit one ``Authentication-Results`` header per
    mechanism, and the SPF/DKIM/DMARC gate needs all of them.
    """
    headers: dict[str, str] = {}
    for name, value in message.items():
        existing = next((key for key in headers if key.lower() == name.lower()), None)
        if existing is None:
            headers[name] = value
        else:
            headers[existing] = f"{headers[existing]}\n{value}"
    return headers


def _extract_body(message: Message) -> tuple[str, list[Attachment]]:
    attachments: list[Attachment] = []
    text: str | None = None
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if (part.get_content_disposition() or "").lower() == "attachment" or filename:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                Attachment(
                    name=filename or "attachment",
                    content_type=part.get_content_type(),
                    size=len(payload),
                )
            )
            continue
        if text is None and part.get_content_type() == "text/plain":
            text = _part_text(part)
    if text is None:
        text = _part_text(message)
    return (text or ""), attachments


def _part_text(part: Message) -> str | None:
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True)
        return payload.decode(errors="replace") if payload else None
    return content if isinstance(content, str) else None


def _received_at(message: Message) -> datetime:
    value = message.get("Date")
    if value:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def _fallback_id(raw: bytes) -> str:
    return f"<imap-{hashlib.sha256(raw).hexdigest()[:32]}@herald>"


__all__ = ["ImapSmtpTransport"]
