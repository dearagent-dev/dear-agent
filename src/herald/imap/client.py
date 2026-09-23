from __future__ import annotations

import contextlib
import imaplib
from dataclasses import dataclass, field


class ImapError(RuntimeError):
    """An IMAP operation failed."""


@dataclass(slots=True)
class ImapClient:
    """Thin wrapper over ``imaplib`` using UIDs.

    Only what the transport needs: connect, select a mailbox, list unread UIDs, fetch the raw
    message, and move a processed message. It carries no business logic — parsing lives in the
    transport so the wire format stays in one place.
    """

    host: str
    user: str
    password: str
    port: int = 993
    ssl: bool = True
    timeout: float = 30.0
    _conn: imaplib.IMAP4 | None = field(default=None, init=False, repr=False)

    def connect(self) -> None:
        factory = imaplib.IMAP4_SSL if self.ssl else imaplib.IMAP4
        self._conn = factory(self.host, self.port, timeout=self.timeout)
        self._conn.login(self.user, self.password)

    def select(self, mailbox: str, *, create: bool = True) -> None:
        conn = self._require()
        status, _ = conn.select(mailbox)
        if status != "OK" and create:
            conn.create(mailbox)
            status, _ = conn.select(mailbox)
        if status != "OK":
            raise ImapError(f"cannot select mailbox {mailbox!r}")

    def unread_uids(self) -> list[bytes]:
        status, data = self._require().uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise ImapError("SEARCH UNSEEN failed")
        return data[0].split() if data and data[0] else []

    def fetch_raw(self, uid: bytes) -> bytes:
        status, data = self._require().uid("FETCH", uid, "(RFC822)")
        if status != "OK":
            raise ImapError(f"FETCH {uid!r} failed")
        for part in data or []:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                return part[1]
        raise ImapError(f"FETCH {uid!r} returned no body")

    def move(self, uid: bytes, target: str) -> None:
        """Copy ``uid`` to ``target`` and expunge it from the selected mailbox."""
        conn = self._require()
        status, _ = conn.uid("COPY", uid, target)
        if status != "OK":
            conn.create(target)
            status, _ = conn.uid("COPY", uid, target)
        if status != "OK":
            raise ImapError(f"COPY {uid!r} -> {target!r} failed")
        conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        conn.expunge()

    def close(self) -> None:
        if self._conn is None:
            return
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            self._conn.close()
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            self._conn.logout()
        self._conn = None

    def _require(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise ImapError("client is not connected; call connect() first")
        return self._conn


__all__ = ["ImapClient", "ImapError"]
