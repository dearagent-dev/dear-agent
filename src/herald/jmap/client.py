from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
DEFAULT_SESSION_URL = "https://api.fastmail.com/jmap/session"

_EMAIL_PROPERTIES = [
    "messageId",
    "threadId",
    "from",
    "subject",
    "receivedAt",
    "keywords",
    "mailboxIds",
]


class JmapError(RuntimeError):
    """Base class for JMAP client errors."""


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

    def get(self, ids: list[str]) -> tuple[str, list[EmailRecord]]:
        if not ids:
            return "", []
        args = self._response(
            self._api_url,
            [
                [
                    "Email/get",
                    {
                        "accountId": self.account_id,
                        "ids": ids,
                        "properties": _EMAIL_PROPERTIES,
                    },
                    "g",
                ]
            ],
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
        self, api_url: str, method_calls: list[list[Any]], call_id: str
    ) -> dict[str, Any]:
        body = self._request(
            api_url,
            data=json.dumps({"using": [CORE, MAIL], "methodCalls": method_calls}).encode(),
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
        )
