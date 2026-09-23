from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dear_agent.idle.loop import IdleBudget, IdleLoop
from dear_agent.idle.proposals import Proposal
from dear_agent.idle.signals import Signal, SignalKind, Signals
from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.models import Task, TaskState

REPO = Path("/repos/dear-agent")


def todo_signal(marker: str) -> Signal:
    return Signal(SignalKind.TODO, marker, evidence="app.py")


class FakeCollector:
    def __init__(self, signals: Signals) -> None:
        self._signals = signals

    def collect(self, repo_path: str) -> Signals:
        return self._signals


def make_signals(items: list[Signal]) -> Signals:
    return Signals(repo_path=REPO, since=datetime(2026, 1, 1, tzinfo=UTC), items=items)


def make_loop(queue: MemoryQueue, signals: Signals, *, max_per_run: int = 1, depth: int = 1):
    submitted: list[Proposal] = []

    def submit(proposal: Proposal) -> str:
        task = Task(
            id=f"idle-{len(submitted)}",
            transport_id=f"idle-{len(submitted)}",
            subject=proposal.title,
        )
        queue.enqueue(task)
        submitted.append(proposal)
        return task.id

    loop = IdleLoop(
        queue=queue,
        submit=submit,
        collector=FakeCollector(signals),
        budget=IdleBudget(max_per_run=max_per_run, min_queue_depth=depth),
    )
    return loop, submitted


def test_tick_submits_a_proposal_when_the_queue_is_empty() -> None:
    queue = MemoryQueue()
    loop, submitted = make_loop(queue, make_signals([todo_signal("TODO: handle errors")]))

    report = loop.tick("/repos/dear-agent")

    assert report.proposed == ["idle-0"]
    assert len(submitted) == 1


def test_tick_skips_when_real_work_is_waiting() -> None:
    queue = MemoryQueue()
    queue.enqueue(Task(id="e1", transport_id="<m1@x>"))
    loop, submitted = make_loop(queue, make_signals([todo_signal("TODO: x")]))

    report = loop.tick("/repos/dear-agent")

    assert report.skipped
    assert report.proposed == []
    assert submitted == []


def test_budget_caps_proposals_per_run() -> None:
    queue = MemoryQueue()
    signals = make_signals([todo_signal(f"TODO: item {n}") for n in range(5)])
    loop, submitted = make_loop(queue, signals, max_per_run=2)

    report = loop.tick("/repos/dear-agent")

    assert len(report.proposed) == 2
    assert len(submitted) == 2


def test_no_signals_means_no_proposals() -> None:
    queue = MemoryQueue()
    loop, submitted = make_loop(queue, make_signals([]))

    report = loop.tick("/repos/dear-agent")

    assert report.proposed == []
    assert submitted == []


def test_proposals_are_approval_gated_by_construction() -> None:
    queue = MemoryQueue()
    loop, submitted = make_loop(queue, make_signals([todo_signal("TODO: x")]))

    loop.tick("/repos/dear-agent")

    # The proposal is submitted as an ordinary task; it is never claimed/executed here.
    assert queue.list(TaskState.RUNNING) == []
    assert queue.list(TaskState.QUEUED)[0].id == "idle-0"
