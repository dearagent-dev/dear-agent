from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any


class EventSourceError(RuntimeError):
    """The event stream could not be read or parsed."""


def parse_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Parse a Server-Sent Events stream into JSON payloads.

    Per the SSE spec, fields are ``event:``, ``data:`` and ``id:``; a blank line dispatches
    the event. We only surface events whose data parses as JSON (JMAP ``StateChange``
    objects); comment lines (``:``) and unknown fields are ignored. A multi-line ``data:``
    payload is joined with newlines as the spec requires.

    Pure and socket-free so it can be tested against a literal stream.
    """

    event = ""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                joined = "\n".join(data)
                try:
                    payload = json.loads(joined)
                except json.JSONDecodeError as exc:
                    raise EventSourceError(f"event data is not JSON: {joined!r}") from exc
                if event:
                    payload.setdefault("_event", event)
                yield payload
            event = ""
            data = []
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            data.append(value)
        elif field_name == "event":
            event = value


@dataclass(slots=True)
class EventSourceListener:
    """Reads a JMAP EventSource and hands each JSON event to ``on_event``.

    Authentication is a bearer token on the request. The stream is finite for tests (inject
    ``lines``) and long-lived in production (``lines`` opens the socket). ``on_event``
    returns ``False`` to stop early (e.g. shutdown); Redispatched events are the caller's
    concern — a ``StateChange`` only tells us *something* changed, so the caller re-polls.
    """

    url: str
    on_event: Callable[[dict[str, Any]], bool | None]
    token: str | None = None
    timeout: float = 60.0
    lines: Iterable[str] | None = None

    def run(self) -> list[dict[str, Any]]:
        source = self.lines if self.lines is not None else self._open()
        delivered: list[dict[str, Any]] = []
        for event in parse_events(source):
            delivered.append(event)
            if self.on_event(event) is False:
                break
        return delivered

    def _open(self) -> Iterator[str]:
        import urllib.request

        headers = {"Accept": "text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, headers=headers)
        response = urllib.request.urlopen(request, timeout=self.timeout)
        try:
            for line in response:
                yield line.decode("utf-8", errors="replace")
        finally:
            response.close()


__all__ = ["EventSourceError", "EventSourceListener", "parse_events"]
