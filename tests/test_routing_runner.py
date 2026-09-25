from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from dear_agent.decision.port import Answer, Decision, DecisionError, DecisionKind
from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.routing import RoutingRunner
from dear_agent.runners.worktree import RunResult, Worktree


@dataclass
class RecordingRunner:
    name: str
    calls: list[str]

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        self.calls.append(self.name)
        return RunResult(exit_code=0, branch=self.name)


class ScriptedDecider:
    def __init__(self, choice: str | None, *, error: bool = False) -> None:
        self._choice = choice
        self._error = error

    def decide(self, state, questions):
        if self._error:
            raise DecisionError("boom")
        return Decision(
            answers={
                "harness": Answer(kind=DecisionKind.CHOICE, choice=self._choice, confidence=0.9)
            }
        )


def _task() -> tuple[Task, TaskSpec, Worktree]:
    return (
        Task(id="e1", transport_id="<m1@x>", subject="fix the typo"),
        TaskSpec(repo_url="https://example.com/o/r", instructions="fix the typo"),
        Worktree(repo_path=Path("/tmp"), path=Path("/tmp"), branch="dear-agent/x"),
    )


def _runners(record: list[str]) -> dict[str, RecordingRunner]:
    return {"local": RecordingRunner("local", record), "hosted": RecordingRunner("hosted", record)}


def test_routing_runner_dispatches_to_the_chosen_harness() -> None:
    record: list[str] = []
    runner = RoutingRunner(runners=_runners(record), decider=ScriptedDecider("local"))

    runner.run(*_task())

    assert record == ["local"]


def test_routing_runner_falls_back_to_default_without_a_decider() -> None:
    record: list[str] = []
    runner = RoutingRunner(runners=_runners(record), decider=None, default="hosted")

    runner.run(*_task())

    assert record == ["hosted"]


def test_routing_runner_falls_back_when_the_decider_errors() -> None:
    record: list[str] = []
    runner = RoutingRunner(
        runners=_runners(record), decider=ScriptedDecider("local", error=True), default="hosted"
    )

    runner.run(*_task())

    assert record == ["hosted"]


def test_routing_runner_falls_back_when_the_class_is_unknown() -> None:
    record: list[str] = []
    runner = RoutingRunner(
        runners=_runners(record), decider=ScriptedDecider("banana"), default="hosted"
    )

    runner.run(*_task())

    assert record == ["hosted"]


def test_routing_runner_raises_without_the_default_runner() -> None:
    runner = RoutingRunner(runners={}, decider=None, default="hosted")

    with pytest.raises(KeyError):
        runner.run(*_task())


class SessionRunner:
    def __init__(self, name: str, sandbox: object) -> None:
        self.name = name
        self.sandbox = sandbox

    def run(self, task: Task, spec: TaskSpec, worktree: Worktree) -> RunResult:
        return RunResult(exit_code=0, branch=self.name)


def test_routing_session_for_returns_the_chosen_runners_sandbox() -> None:
    router = RoutingRunner(
        runners={
            "local": SessionRunner("local", "local-session"),
            "hosted": SessionRunner("hosted", "hosted-session"),
        },
        decider=ScriptedDecider("local"),
        default="hosted",
    )

    task, spec, _ = _task()

    assert router.session_for(task, spec) == "local-session"


def test_routing_asks_the_decider_once_for_the_session_and_the_run() -> None:
    calls = {"n": 0}

    class CountingDecider:
        def decide(self, state, questions):
            calls["n"] += 1
            return Decision(
                answers={
                    "harness": Answer(kind=DecisionKind.CHOICE, choice="local", confidence=0.9)
                }
            )

    record: list[str] = []
    router = RoutingRunner(
        runners={
            "local": SessionRunner("local", object()),
            "hosted": SessionRunner("hosted", object()),
        },
        decider=CountingDecider(),
        default="hosted",
    )
    task, spec, worktree = _task()

    router.session_for(task, spec)
    router.run(task, spec, worktree)

    assert calls["n"] == 1
    assert record == []
