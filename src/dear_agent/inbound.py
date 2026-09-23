from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dear_agent.auth import AuthError, InboundGate
from dear_agent.control_plane import ControlPlane
from dear_agent.transports.base import Attachment, RawMessage


class InboundError(ValueError):
    """The webhook payload was not a valid message."""


def parse_message(payload: bytes) -> RawMessage:
    """Parse a webhook JSON body into a :class:`RawMessage`.

    The payload is attacker-controlled: we require a stable ``id`` (the dedupe key) and a
    string ``body``, and we never execute anything from it. Unknown fields are ignored.
    """

    try:
        data: Any = json.loads(payload or b"{}")
    except json.JSONDecodeError as exc:
        raise InboundError("body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InboundError("body must be a JSON object")
    transport_id = data.get("id")
    if not isinstance(transport_id, str) or not transport_id:
        raise InboundError("'id' is required and must be a non-empty string")
    body = data.get("body", "")
    if not isinstance(body, str):
        raise InboundError("'body' must be a string")
    headers = data.get("headers", {})
    if not isinstance(headers, dict):
        raise InboundError("'headers' must be an object")
    attachments = [
        Attachment(
            name=str(item.get("name", "")),
            content_type=str(item.get("content_type", "")),
            size=int(item.get("size", 0) or 0),
        )
        for item in data.get("attachments", [])
        if isinstance(item, dict)
    ]
    return RawMessage(
        transport_id=transport_id,
        thread_id=data.get("thread_id"),
        sender=data.get("sender"),
        subject=data.get("subject"),
        body=body,
        attachments=attachments,
        headers={str(key): str(value) for key, value in headers.items()},
    )


@dataclass(slots=True)
class InboundWebhook:
    """Turns one HTTP request into an ingest pass.

    Auth and rate limiting run first (the gate); an unauthorized request is answered 401
    and never normalized (no backscatter). A malformed payload is a 400. A well-formed,
    authenticated message is ingested and answered 202 with what happened to its task id.
    """

    control_plane: ControlPlane
    gate: InboundGate | None = None
    recipient: str | None = None

    def __call__(self, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        try:
            message = parse_message(body)
        except InboundError as exc:
            return 400, {"error": str(exc)}

        if self.gate is not None:
            try:
                # The signature covers the raw request body (with the sender bound in), so a
                # verified payload cannot be reshaped in transit.
                raw = body.decode("utf-8", errors="replace")
                self.gate.admit(body=raw, headers=headers, sender=message.sender)
            except AuthError as exc:
                return 401, {"error": str(exc)}

        report = self.control_plane.ingest([message], recipient=self.recipient)
        if report.denied:
            return 403, {"error": "sender is not allowed"}
        return 202, {
            "accepted": report.accepted,
            "rejected": report.rejected,
            "decided": report.decided,
            "suspicious": report.suspicious,
        }


__all__ = ["InboundError", "InboundWebhook", "parse_message"]
