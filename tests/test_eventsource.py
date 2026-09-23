from __future__ import annotations

import pytest

from dear_agent.jmap.eventsource import EventSourceError, EventSourceListener, parse_events

STREAM = [
    ": keep-alive\n",
    "event: ping\n",
    'data: {"hello": 1}\n',
    "\n",
    "event: state\n",
    'data: {"@type": "StateChange", "changed": {"u1": {"Email": "s1"}}}\n',
    "\n",
]


def test_parse_events_ignores_comments_and_dispatches_on_blank_lines() -> None:
    events = list(parse_events(STREAM))

    assert events == [
        {"hello": 1, "_event": "ping"},
        {"@type": "StateChange", "changed": {"u1": {"Email": "s1"}}, "_event": "state"},
    ]


def test_parse_events_joins_multiline_data() -> None:
    events = list(parse_events(['data: {"a":\n', "data: 1}\n", "\n"]))

    assert events == [{"a": 1}]


def test_parse_events_rejects_non_json_data() -> None:
    with pytest.raises(EventSourceError):
        list(parse_events(["data: not-json\n", "\n"]))


def test_listener_calls_back_for_each_event() -> None:
    seen: list[dict] = []
    listener = EventSourceListener(url="x", on_event=seen.append, lines=STREAM)

    delivered = listener.run()

    assert delivered == seen
    assert seen[1]["@type"] == "StateChange"


def test_listener_stops_when_the_callback_returns_false() -> None:
    seen: list[dict] = []

    def stop_after_first(event: dict) -> bool:
        seen.append(event)
        return len(seen) < 1

    listener = EventSourceListener(url="x", on_event=stop_after_first, lines=STREAM)

    delivered = listener.run()

    assert len(delivered) == 1
