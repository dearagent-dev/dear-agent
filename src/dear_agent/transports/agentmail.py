from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any, Protocol

from dear_agent.transports.base import Attachment, OutboundMessage, RawMessage

DEFAULT_BASE_URL = "https://api.agentmail.to/v0"
RECEIVED_LABEL = "received"
DEFAULT_DONE_LABEL = "dear-agent-done"


class AgentMailError(Exception):
    """An AgentMail API call failed."""


class _AgentMailClient(Protocol):
    """The slice of the AgentMail API the transport depends on."""

    def default_inbox_id(self) -> str: ...
    def list_messages(
        self, inbox_id: str, *, labels: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...
    def get_message(self, inbox_id: str, message_id: str) -> dict[str, Any]: ...
    def update_labels(
        self,
        inbox_id: str,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None: ...
    def reply(
        self, inbox_id: str, message_id: str, *, text: str, to: str | None = None
    ) -> None: ...
    def send(self, inbox_id: str, *, to: str, subject: str, text: str) -> None: ...


class AgentMailClient:
    """Thin AgentMail REST client (stdlib only).

    Credentials live in ``AGENTMAIL_API_TOKEN`` (ADR 0003); the transport never sees the
    token. AgentMail is Bearer-authenticated over HTTPS.
    """

    def __init__(
        self, token: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 60.0
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200]
            raise AgentMailError(f"{method} {path} -> {exc.code}: {detail!r}") from exc
        except urllib.error.URLError as exc:
            raise AgentMailError(f"{method} {path} failed: {exc.reason}") from exc
        return json.loads(raw) if raw else {}

    def default_inbox_id(self) -> str:
        inboxes = self._request("GET", "/inboxes").get("inboxes") or []
        if not inboxes:
            raise AgentMailError("the AgentMail account has no inbox")
        return str(inboxes[0]["inbox_id"])

    def list_messages(
        self, inbox_id: str, *, labels: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if labels:
            params["labels"] = list(labels)
        data = self._request("GET", f"/inboxes/{_seg(inbox_id)}/messages", params=params)
        return list(data.get("messages") or [])

    def get_message(self, inbox_id: str, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}")

    def update_labels(
        self,
        inbox_id: str,
        message_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if add:
            body["add_labels"] = list(add)
        if remove:
            body["remove_labels"] = list(remove)
        self._request("PATCH", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}", body=body)

    def reply(self, inbox_id: str, message_id: str, *, text: str, to: str | None = None) -> None:
        body: dict[str, Any] = {"text": text}
        if to:
            body["to"] = to
        self._request(
            "POST", f"/inboxes/{_seg(inbox_id)}/messages/{_seg(message_id)}/reply", body=body
        )

    def send(self, inbox_id: str, *, to: str, subject: str, text: str) -> None:
        self._request(
            "POST",
            f"/inboxes/{_seg(inbox_id)}/messages/send",
            body={"to": to, "subject": subject, "text": text},
        )


class AgentMailTransport:
    """Transport over AgentMail's inbox API.

    Inbound pulls unprocessed ``received`` messages as :class:`RawMessage`; outbound replies
    in-thread (or sends when there is nothing to reply to). It carries no business logic: the
    Normalizer maps a message to a task, and the queue's dedupe on ``message_id`` makes a
    redelivery a no-op.
    """

    def __init__(
        self,
        client: _AgentMailClient,
        *,
        inbox_id: str | None = None,
        done_label: str = DEFAULT_DONE_LABEL,
    ) -> None:
        self._client = client
        self._inbox_id = inbox_id or client.default_inbox_id()
        self._done_label = done_label

    def poll(self, *, limit: int = 50) -> list[RawMessage]:
        messages = self._client.list_messages(self._inbox_id, labels=[RECEIVED_LABEL], limit=limit)
        fresh = [m for m in messages if self._done_label not in (m.get("labels") or [])]
        fresh.sort(key=lambda m: str(m.get("timestamp") or m.get("created_at") or ""))
        # The list endpoint omits the body; fetch each message's full record.
        return [self._to_raw(self._full(message)) for message in fresh]

    def _full(self, message: dict[str, Any]) -> dict[str, Any]:
        message_id = message.get("message_id")
        if not message_id:
            return message
        try:
            return self._client.get_message(self._inbox_id, str(message_id))
        except Exception:  # noqa: BLE001 - fall back to the list record
            return message

    def ack(self, messages: list[RawMessage]) -> None:
        """Label handled messages so the next poll skips them (best effort)."""
        for message in messages:
            try:
                self._client.update_labels(
                    self._inbox_id, message.transport_id, add=[self._done_label]
                )
            except Exception:  # noqa: BLE001 - acknowledging is best effort
                continue

    def receive(self, message: RawMessage) -> None:
        raise NotImplementedError("AgentMail is a pull transport; use poll()")

    def send(self, message: OutboundMessage) -> None:
        recipient = message.headers.get("to")
        if not recipient:
            raise ValueError("outbound message needs a 'to' header")
        in_reply_to = message.headers.get("in-reply-to")
        if in_reply_to:
            self._client.reply(self._inbox_id, in_reply_to, text=message.body, to=recipient)
        else:
            self._client.send(
                self._inbox_id, to=recipient, subject=message.subject, text=message.body
            )

    @staticmethod
    def _to_raw(message: dict[str, Any]) -> RawMessage:
        sender = _first_address(message.get("from_") or message.get("from"))
        attachments = [
            Attachment(
                name=str(a.get("filename") or "attachment"),
                content_type=str(a.get("content_type") or "application/octet-stream"),
                size=int(a.get("size") or 0),
            )
            for a in (message.get("attachments") or [])
        ]
        body = message.get("text") or message.get("extracted_text") or ""
        return RawMessage(
            transport_id=str(message.get("message_id") or message.get("id")),
            thread_id=message.get("thread_id"),
            sender=sender,
            subject=message.get("subject"),
            body=body,
            attachments=attachments,
            headers={str(k): str(v) for k, v in (message.get("headers") or {}).items()},
            received_at=_parse_time(message.get("timestamp") or message.get("created_at")),
        )


def _seg(value: str) -> str:
    """Quote a path segment (message ids contain ``<...>@...``)."""
    return urllib.parse.quote(str(value), safe="")


def _first_address(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    # AgentMail may return ``Display Name <addr>`` or a bare address; keep the address.
    return parseaddr(str(value))[1] or str(value)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


__all__ = [
    "AgentMailClient",
    "AgentMailError",
    "AgentMailTransport",
    "DEFAULT_BASE_URL",
]
