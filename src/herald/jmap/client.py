from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
DEFAULT_SESSION_URL = "https://api.fastmail.com/jmap/session"

_EMAIL_PROPERTIES = [
    "messageId",
    "threadId",
    "from",
    "subject",
    "receivedAt",
    "keywords",
    "mailboxIds",
    "bodyValues",
    "hasAttachment",
]

_INBOUND_PROPERTIES = _EMAIL_PROPERTIES
_TEXT_BODY_PROPERTIES = "textBody,bodyValues"


class JmapError(RuntimeError):
    """Base class for JMAP client errors."""


def _extract_text_body(item: dict[str, Any]) -> str:
    body_values = item.get("bodyValues") or {}
    parts = item.get("textBody") or []
    chunks: list[str] = []
    for part in parts:
        part_id = part.get("partId")
        value = body_values.get(part_id, {}).get("value")
        if value:
            chunks.append(value)
    return "\n".join(chunks)


class StateMismatchError(JmapError):
    """Raised when an ``ifInState``-guarded write loses the optimistic-concurrency race."""


@dataclass(slots=True)
class EmailRecord:
    """The subset of a JMAP ``Email`` the queue needs."""

    id: str
    message_id: str
    thread_id: str | None
    sender: str | None
    subject: str | None
    received_at: datetime
    keywords: set[str]
    mailbox_ids: set[str]
    body: str = ""
    has_attachment: bool = False


class JmapClient:
    """Minimal JMAP client covering the methods the queue needs."""

    def __init__(
        self,
        token: str,
        *,
        account_id: str | None = None,
        session_url: str = DEFAULT_SESSION_URL,
    ) -> None:
        self._token = token
        self._account_id = account_id
        self._session_url = session_url
        self._api_url = ""
        self._mailboxes: dict[str, str] = {}

    @property
    def account_id(self) -> str:
        if not self._account_id:
            raise JmapError("client is not connected; call connect() first")
        return self._account_id

    def connect(self) -> None:
        session = self._request(self._session_url)
        self._api_url = session["apiUrl"]
        if not self._account_id:
            for account_id, account in session.get("accounts", {}).items():
                if MAIL in account.get("accountCapabilities", {}):
                    self._account_id = account_id
                    break
        if not self._account_id:
            raise JmapError("session exposed no mail-capable account")

    def query_ids(self, *, filter: dict[str, Any], limit: int) -> list[str]:
        args = self._response(
            self._api_url,
            [
                [
                    "Email/query",
                    {"accountId": self.account_id, "filter": filter, "limit": limit},
                    "q",
                ]
            ],
            "q",
        )
        return args.get("ids", [])

    def get(self, ids: list[str], *, fetch_body: bool = False) -> tuple[str, list[EmailRecord]]:
        if not ids:
            return "", []
        properties = list(_EMAIL_PROPERTIES)
        arguments: dict[str, Any] = {
            "accountId": self.account_id,
            "ids": ids,
            "properties": properties + (["textBody"] if fetch_body else []),
        }
        if fetch_body:
            arguments["bodyProperties"] = ["partId", "type"]
            arguments["fetchTextBodyValues"] = True
        args = self._response(
            self._api_url,
            [["Email/get", arguments, "g"]],
            "g",
        )
        return args["state"], [self._to_record(item) for item in args.get("list", [])]

    def update(self, email_id: str, patch: dict[str, Any], *, if_in_state: str) -> str:
        args = self._response(
            self._api_url,
            [
                [
                    "Email/set",
                    {
                        "accountId": self.account_id,
                        "ifInState": if_in_state,
                        "update": {email_id: patch},
                    },
                    "u",
                ]
            ],
            "u",
        )
        if "updated" not in args:
            not_updated = args.get("notUpdated", {})
            if any(item.get("type") == "stateMismatch" for item in not_updated.values()):
                raise StateMismatchError(not_updated)
            raise JmapError(f"update failed: {not_updated}")
        return args["newState"]

    def get_or_create_mailbox(self, name: str) -> str:
        if name in self._mailboxes:
            return self._mailboxes[name]

        args = self._response(
            self._api_url,
            [["Mailbox/get", {"accountId": self.account_id, "ids": None}, "m"]],
            "m",
        )
        for mailbox in args.get("list", []):
            self._mailboxes[mailbox["name"]] = mailbox["id"]
        if name in self._mailboxes:
            return self._mailboxes[name]

        args = self._response(
            self._api_url,
            [
                [
                    "Mailbox/set",
                    {"accountId": self.account_id, "create": {"m": {"name": name}}},
                    "mc",
                ]
            ],
            "mc",
        )
        created = args.get("created", {})
        if "m" not in created:
            raise JmapError(f"could not create mailbox {name!r}: {args.get('notCreated')}")
        self._mailboxes[name] = created["m"]["id"]
        return self._mailboxes[name]

    def submit(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to_message_id: str | None = None,
    ) -> str:
        """Create a draft and submit it.

        When ``in_reply_to_message_id`` is given, the outgoing ``References``/
        ``In-Reply-To`` headers keep the conversation threaded. Only status/approval text
        travels here, never source.
        """
        email: dict[str, Any] = {
            "mailboxIds": {self.get_or_create_mailbox("Sent"): True},
            "keywords": {"$draft": True},
            "from": [{"email": self._identity_email()}],
            "to": [{"email": to}],
            "subject": subject,
            "bodyValues": {"b": {"value": body}},
            "textBody": [{"partId": "b", "type": "text/plain"}],
        }
        if in_reply_to_message_id is not None:
            email["inReplyTo"] = [in_reply_to_message_id]

        created = self._response(
            self._api_url,
            [["Email/set", {"accountId": self.account_id, "create": {"d": email}}, "d"]],
            "d",
        )
        if "d" not in created.get("created", {}):
            raise JmapError(f"could not create draft: {created.get('notCreated')}")
        email_id = created["created"]["d"]["id"]

        submission = self._response(
            self._api_url,
            [
                [
                    "EmailSubmission/set",
                    {
                        "accountId": self.account_id,
                        "create": {"s": {"emailId": email_id}},
                    },
                    "s",
                ]
            ],
            "s",
            using=[CORE, MAIL, SUBMISSION],
        )
        if "s" not in submission.get("created", {}):
            raise JmapError(f"could not submit: {submission.get('notCreated')}")
        return email_id

    def _identity_email(self) -> str:
        identities = self._response(
            self._api_url,
            [["Identity/get", {"accountId": self.account_id, "ids": None}, "i"]],
            "i",
            using=[CORE, SUBMISSION],
        )
        for identity in identities.get("list", []):
            if identity.get("email"):
                return identity["email"]
        raise JmapError("no sending identity found")

    def _request(self, url: str, *, data: bytes | None = None, method: str = "GET") -> Any:
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise JmapError(f"HTTP {exc.code}: {detail[:500]}") from exc

    def _response(
        self,
        api_url: str,
        method_calls: list[list[Any]],
        call_id: str,
        *,
        using: list[str] | None = None,
    ) -> dict[str, Any]:
        body = self._request(
            api_url,
            data=json.dumps({"using": using or [CORE, MAIL], "methodCalls": method_calls}).encode(),
            method="POST",
        )
        for name, args, response_id in body.get("methodResponses", []):
            if response_id != call_id:
                continue
            if name == "error":
                if args.get("type") == "stateMismatch":
                    raise StateMismatchError(args)
                raise JmapError(f"{call_id}: {args.get('type', 'error')}")
            return args
        raise JmapError(f"no response for call id {call_id!r}")

    @staticmethod
    def _to_record(item: dict[str, Any]) -> EmailRecord:
        message_ids = item.get("messageId") or []
        senders = item.get("from") or []
        return EmailRecord(
            id=item["id"],
            message_id=message_ids[0] if message_ids else "",
            thread_id=item.get("threadId"),
            sender=senders[0].get("email") if senders else None,
            subject=item.get("subject"),
            received_at=datetime.fromisoformat(item["receivedAt"].replace("Z", "+00:00")),
            keywords={kw for kw, on in (item.get("keywords") or {}).items() if on},
            mailbox_ids={mbx for mbx, on in (item.get("mailboxIds") or {}).items() if on},
            body=_extract_text_body(item),
            has_attachment=bool(item.get("hasAttachment")),
        )
