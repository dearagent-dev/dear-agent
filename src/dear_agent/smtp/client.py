from __future__ import annotations

import contextlib
import smtplib
from dataclasses import dataclass


class SmtpError(RuntimeError):
    """An SMTP operation failed."""


@dataclass(slots=True)
class SmtpClient:
    """Thin wrapper over ``smtplib`` for submitting one raw message.

    It carries no business logic: the transport builds the message (with threading
    headers) and this adapter only talks to the wire.
    """

    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    starttls: bool = True
    ssl: bool = False
    timeout: float = 30.0

    def send(self, raw: bytes, *, sender: str, recipients: list[str]) -> None:
        client = self._open()
        try:
            if self.starttls and not self.ssl:
                client.starttls()
            if self.user and self.password:
                client.login(self.user, self.password)
            client.sendmail(sender, recipients, raw)
        except (smtplib.SMTPException, OSError) as exc:
            raise SmtpError(str(exc)) from exc
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                client.quit()

    def _open(self) -> smtplib.SMTP:
        if self.ssl:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)


__all__ = ["SmtpClient", "SmtpError"]
