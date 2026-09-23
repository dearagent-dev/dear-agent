from __future__ import annotations

from dear_agent.notify.escalate import Escalator, FailureKind, classify_failure
from dear_agent.queue.models import Task
from dear_agent.runners.worktree import RunResult
from dear_agent.transports.memory import MemoryTransport


def make_task() -> Task:
    return Task(id="e1", transport_id="<m1@x>", thread_id="t1", subject="add healthz")


def test_classify_missing_binary() -> None:
    assert classify_failure(RunResult(exit_code=127)) is FailureKind.HARNESS_MISSING


def test_classify_timeout() -> None:
    assert classify_failure(RunResult(exit_code=124)) is FailureKind.TIMEOUT


def test_classify_generic_failure() -> None:
    assert classify_failure(RunResult(exit_code=2)) is FailureKind.HARNESS_FAILED


def test_escalation_is_threaded_and_explains_the_reason() -> None:
    transport = MemoryTransport()

    escalation = Escalator(transport).escalate(
        make_task(), RunResult(exit_code=127, stderr="opencode: not found"), recipient="ops@x.com"
    )

    assert escalation.kind is FailureKind.HARNESS_MISSING
    sent = transport.outbox[0]
    assert sent.thread_id == "t1"
    assert sent.headers["to"] == "ops@x.com"
    assert "needs attention" in sent.subject
    assert "not available" in sent.body


def test_escalation_does_not_include_raw_harness_output() -> None:
    transport = MemoryTransport()

    Escalator(transport).escalate(
        make_task(),
        RunResult(exit_code=1, stdout="SECRET_SOURCE_CODE", stderr="boom"),
        recipient="ops@x.com",
    )

    assert "SECRET_SOURCE_CODE" not in transport.outbox[0].body


def test_escalation_includes_the_branch_when_given() -> None:
    transport = MemoryTransport()

    Escalator(transport).escalate(
        make_task(), RunResult(exit_code=2), recipient="ops@x.com", branch="dear-agent/add-healthz"
    )

    assert "dear-agent/add-healthz" in transport.outbox[0].body


def test_escalation_honors_an_explicit_kind_without_an_exit_code() -> None:
    transport = MemoryTransport()

    Escalator(transport).escalate(
        make_task(),
        RunResult(exit_code=0),
        recipient="ops@x.com",
        kind=FailureKind.NO_CHANGES,
    )

    body = transport.outbox[0].body
    assert "produced no changes" in body
    assert "exit" not in body
