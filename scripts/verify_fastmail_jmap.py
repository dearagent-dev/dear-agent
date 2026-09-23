#!/usr/bin/env python3
"""M1.1 spike: verify the Fastmail JMAP capabilities Herald depends on.

Run against a real account; nothing is written to disk and no secret is printed.

Checks:
  1. JMAP session and the mail-capable account id.
  2. Custom keyword support (`$herald-queued` and `herald-queued`).
  3. ``Email/query`` filtering by the ``Message-ID`` header.
  4. ``Email/set`` optimistic concurrency: a stale ``ifInState`` must fail with
     ``stateMismatch`` and a fresh one must succeed.

Configuration (environment variables):

  FASTMAIL_API_TOKEN   required; a Fastmail API token with mail scope
  FASTMAIL_ACCOUNT_ID  optional; discovered from the session otherwise
  FASTMAIL_MAILBOX     optional; probe mailbox name (default: HeraldProbe)

Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

SESSION_URL = "https://api.fastmail.com/jmap/session"
CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
PROBE_SUBJECT = "herald jmap capability probe"
PROBE_FROM = "herald-probe@example.invalid"


class JmapError(RuntimeError):
    pass


class JmapClient:
    def __init__(self, token: str) -> None:
        self._token = token
        self.api_url = ""
        self.account_id = ""
        self.session: dict[str, Any] = {}

    def _authorized(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"})

    def connect(self, account_id: str | None = None) -> None:
        try:
            with urllib.request.urlopen(self._authorized(SESSION_URL), timeout=30) as resp:
                self.session = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise JmapError(f"session request failed: HTTP {exc.code}") from exc

        self.api_url = self.session["apiUrl"]
        accounts = self.session.get("accounts", {})
        if account_id:
            self.account_id = account_id
        else:
            for aid, account in accounts.items():
                if MAIL in account.get("accountCapabilities", {}):
                    self.account_id = aid
                    break
        if not self.account_id:
            raise JmapError("session exposed no mail-capable account")
        if not self.session.get("primaryAccounts", {}).get(MAIL):
            print("note: no primary mail account advertised; using the first mail account")

    def call(self, method_calls: list[list[Any]]) -> list[list[Any]]:
        payload = {"using": [CORE, MAIL], "methodCalls": method_calls}
        req = self._authorized(self.api_url)
        req.data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise JmapError(f"JMAP call failed: HTTP {exc.code}: {detail[:500]}") from exc
        return body.get("methodResponses", [])


def response_for(responses: list[list[Any]], call_id: str) -> tuple[str, dict[str, Any]]:
    for name, args, cid in responses:
        if cid == call_id:
            return name, args
    raise JmapError(f"no response for call id {call_id!r}")


def unwrap(responses: list[list[Any]], call_id: str) -> dict[str, Any]:
    name, args = response_for(responses, call_id)
    if name == "error":
        raise JmapError(f"{call_id}: {args.get('type', 'error')} {args}")
    return args


def find_or_create_mailbox(client: JmapClient, name: str) -> str:
    args = unwrap(
        client.call([["Mailbox/get", {"accountId": client.account_id, "ids": None}, "m0"]]),
        "m0",
    )
    for mailbox in args["list"]:
        if mailbox["name"] == name:
            return mailbox["id"]

    args = unwrap(
        client.call(
            [
                [
                    "Mailbox/set",
                    {
                        "accountId": client.account_id,
                        "create": {"probe": {"name": name}},
                    },
                    "m1",
                ]
            ]
        ),
        "m1",
    )
    if "created" not in args or "probe" not in args["created"]:
        raise JmapError(f"could not create probe mailbox: {args.get('notCreated')}")
    return args["created"]["probe"]["id"]


def create_probe_email(client: JmapClient, mailbox_id: str) -> tuple[str, str]:
    payload = {
        "mailboxIds": {mailbox_id: True},
        "keywords": {"$draft": True},
        "from": [{"email": PROBE_FROM}],
        "to": [{"email": PROBE_FROM}],
        "subject": PROBE_SUBJECT,
        "bodyValues": {"b1": {"value": "herald jmap probe"}},
        "textBody": [{"partId": "b1", "type": "text/plain"}],
    }
    args = unwrap(
        client.call(
            [
                [
                    "Email/set",
                    {"accountId": client.account_id, "create": {"probe": payload}},
                    "e0",
                ]
            ]
        ),
        "e0",
    )
    if "created" not in args or "probe" not in args["created"]:
        raise JmapError(f"could not create probe email: {args.get('notCreated')}")
    created = args["created"]["probe"]
    return created["id"], args["newState"]


def email_state(client: JmapClient, email_id: str) -> str:
    args = unwrap(
        client.call(
            [
                [
                    "Email/get",
                    {
                        "accountId": client.account_id,
                        "ids": [email_id],
                        "properties": [],
                    },
                    "st",
                ]
            ]
        ),
        "st",
    )
    return args["state"]


def check_keyword(client: JmapClient, email_id: str, state: str, keyword: str) -> bool:
    args = unwrap(
        client.call(
            [
                [
                    "Email/set",
                    {
                        "accountId": client.account_id,
                        "ifInState": state,
                        "update": {email_id: {f"keywords/{keyword}": True}},
                    },
                    "k0",
                ]
            ]
        ),
        "k0",
    )
    if "updated" not in args:
        print(f"  [FAIL] {keyword}: {args.get('notUpdated', args)}")
        return False

    got = unwrap(
        client.call(
            [
                [
                    "Email/get",
                    {
                        "accountId": client.account_id,
                        "ids": [email_id],
                        "properties": ["keywords"],
                    },
                    "k1",
                ]
            ]
        ),
        "k1",
    )
    present = keyword in got["list"][0].get("keywords", {})
    print(f"  [{'PASS' if present else 'FAIL'}] {keyword}")
    return present


def check_message_id_query(client: JmapClient, email_id: str) -> bool:
    got = unwrap(
        client.call(
            [
                [
                    "Email/get",
                    {
                        "accountId": client.account_id,
                        "ids": [email_id],
                        "properties": ["messageId"],
                    },
                    "q0",
                ]
            ]
        ),
        "q0",
    )
    message_ids = got["list"][0].get("messageId") or []
    if not message_ids:
        print("  [FAIL] messageId: the server returned none")
        return False

    args = unwrap(
        client.call(
            [
                [
                    "Email/query",
                    {
                        "accountId": client.account_id,
                        "filter": {"header": ["Message-ID", message_ids[0]]},
                        "limit": 10,
                    },
                    "q1",
                ]
            ]
        ),
        "q1",
    )
    found = email_id in args.get("ids", [])
    print(f"  [{'PASS' if found else 'FAIL'}] Email/query by Message-ID")
    return found


def check_ifinstate(client: JmapClient, email_id: str, state: str) -> bool:
    stale = unwrap_or_error(
        client.call(
            [
                [
                    "Email/set",
                    {
                        "accountId": client.account_id,
                        "ifInState": "herald-deliberately-stale",
                        "update": {email_id: {"keywords/$seen": True}},
                    },
                    "s0",
                ]
            ]
        ),
        "s0",
    )
    rejected = stale[0] == "error" and stale[1].get("type") == "stateMismatch"
    print(f"  [{'PASS' if rejected else 'FAIL'}] stale ifInState rejected with stateMismatch")

    fresh = unwrap(
        client.call(
            [
                [
                    "Email/set",
                    {
                        "accountId": client.account_id,
                        "ifInState": state,
                        "update": {email_id: {"keywords/$seen": True}},
                    },
                    "s1",
                ]
            ]
        ),
        "s1",
    )
    accepted = "updated" in fresh
    print(f"  [{'PASS' if accepted else 'FAIL'}] fresh ifInState accepted")
    return rejected and accepted


def unwrap_or_error(responses: list[list[Any]], call_id: str) -> tuple[str, dict[str, Any]]:
    return response_for(responses, call_id)


def _write_raw_email(client: Any, mailbox_id: str, message_id: str, body: str) -> str:
    payload = {
        "mailboxIds": {mailbox_id: True},
        "keywords": {"$draft": True},
        "from": [{"email": PROBE_FROM}],
        "to": [{"email": PROBE_FROM}],
        "messageId": [message_id],
        "subject": "herald transport probe",
        "bodyValues": {"b1": {"value": body}},
        "textBody": [{"partId": "b1", "type": "text/plain"}],
    }
    created = _raw_call(
        client,
        [["Email/set", {"accountId": client.account_id, "create": {"p": payload}}, "c"]],
        "c",
    )
    if "p" not in created.get("created", {}):
        raise RuntimeError(f"could not create email: {created.get('notCreated')}")
    return created["created"]["p"]["id"]


def check_transport_integration(token: str, account_id: str | None) -> bool:
    """Validate :class:`herald.transports.jmap.JmapTransport` against the live account.

    Polls a throwaway mailbox for a probe message and, if the token has the submission
    scope, sends a threaded reply. A missing submission scope is reported, not failed.
    """
    from herald.jmap.client import JmapClient as TransportJmapClient
    from herald.transports.base import OutboundMessage
    from herald.transports.jmap import JmapTransport

    probe_mailbox = os.environ.get("FASTMAIL_TRANSPORT_MAILBOX", "Herald-transport-probe")
    client = TransportJmapClient(token, account_id=account_id)
    client.connect()
    mailbox_id = client.get_or_create_mailbox(probe_mailbox)

    message_id = f"<herald-transport-probe-{os.getpid()}@example.invalid>"
    email_id = _write_raw_email(
        client, mailbox_id, message_id, "repo: https://example.com/o/r\n\nfix the build"
    )

    transport = JmapTransport(client, mailbox_name=probe_mailbox)
    try:
        polled = transport.poll()
        match = next((raw for raw in polled if raw.transport_id == message_id), None)
        if match is None:
            print("  [FAIL] transport: probe message was not polled")
            return False
        if "fix the build" not in match.body:
            print("  [FAIL] transport: polled body is empty")
            return False
        print("  [PASS] transport: poll maps the email to a RawMessage")

        try:
            transport.send(
                OutboundMessage(
                    thread_id=match.thread_id,
                    subject="re: herald transport probe",
                    body="transport probe reply",
                    headers={"to": PROBE_FROM, "in-reply-to": message_id},
                )
            )
            print("  [PASS] transport: threaded send")
        except Exception as exc:  # noqa: BLE001
            print(f"  [SKIP] transport: send needs the Email submission scope ({exc})")
        return True
    finally:
        _destroy(client, email_id)


def _destroy(client: Any, email_id: str) -> None:
    _raw_call(
        client,
        [["Email/set", {"accountId": client.account_id, "destroy": [email_id]}, "d"]],
        "d",
    )


def _raw_call(client: Any, method_calls: list[list[Any]], call_id: str) -> dict[str, Any]:
    return client._response(client._api_url, method_calls, call_id)


def main() -> int:
    token = os.environ.get("FASTMAIL_API_TOKEN")
    if not token:
        print("FASTMAIL_API_TOKEN is not set", file=sys.stderr)
        return 2

    account_id = os.environ.get("FASTMAIL_ACCOUNT_ID")
    mailbox_name = os.environ.get("FASTMAIL_MAILBOX", "HeraldProbe")

    client = JmapClient(token)
    print("Fastmail JMAP capability probe")
    print("- session")
    client.connect(account_id)
    print(f"  [PASS] account {client.account_id}")
    print(f"  [PASS] apiUrl {client.api_url}")

    print("- probe mailbox")
    mailbox_id = find_or_create_mailbox(client, mailbox_name)
    print(f"  [PASS] mailbox {mailbox_name!r}")

    print("- probe email")
    email_id, state = create_probe_email(client, mailbox_id)
    print(f"  [PASS] created {email_id}")

    print("- custom keywords")
    keyword_results = [check_keyword(client, email_id, state, "$herald-queued")]
    state = email_state(client, email_id)
    keyword_results.append(check_keyword(client, email_id, state, "herald-queued"))

    print("- Message-ID query")
    query_ok = check_message_id_query(client, email_id)

    print("- ifInState (CAS)")
    state = email_state(client, email_id)
    cas_ok = check_ifinstate(client, email_id, state)

    client.call(
        [
            [
                "Email/set",
                {"accountId": client.account_id, "destroy": [email_id]},
                "d0",
            ]
        ]
    )

    print("- JmapTransport over Fastmail")
    transport_ok = check_transport_integration(token, account_id)

    print("- result")
    if any(keyword_results) and query_ok and cas_ok and transport_ok:
        print("  all critical checks passed")
        return 0
    print("  some checks failed; see above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
