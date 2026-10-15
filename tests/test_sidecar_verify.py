"""The two-container ``verify`` gate must not run where the credentials live (ADR 0007).

Review finding 1: in the default sidecar runner, ``build_worker`` left the session unset, so
``CommandVerifier`` fell back to ``NoSandbox`` and the task's ``verify`` command — which runs on
files the harness just wrote — executed in the **control** container, with the database DSN, the
Git keys and the forge token in its environment.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from dear_agent.queue.memory import MemoryQueue
from dear_agent.queue.models import Task, TaskSpec
from dear_agent.runners.command import CommandRunner
from dear_agent.runners.shared import DelegatingRunner, SidecarSession, serve
from dear_agent.runners.worktree import Worktree
from dear_agent.verify import CommandVerifier

# The probe exits 1 when it can read the control container's environment and 0 when it cannot.
# Same argv, opposite outcome on the two sides: that is the whole property under test.
PROBE = "python probe.py"
PROBE_SOURCE = """\
import os
import sys

sys.exit(1 if os.environ.get("DEAR_AGENT_DATABASE_URL") else 0)
"""


def _worktree(tmp_path: Path) -> Worktree:
    workdir = tmp_path / "wt"
    workdir.mkdir(exist_ok=True)
    (workdir / "probe.py").write_text(PROBE_SOURCE)
    return Worktree(repo_path=workdir, path=workdir, branch="dear-agent/x")


def _start_harness(shared: Path) -> threading.Thread:
    thread = threading.Thread(
        target=serve,
        args=(shared,),
        kwargs={
            "runner": CommandRunner(command=["python", "-c", "print('ok')"]),
            "once": False,
            "poll": 0.02,
            "idle_timeout": 30,
        },
        daemon=True,
    )
    thread.start()
    return thread


def _delegating_verifier(session: SidecarSession) -> CommandVerifier:
    verifier = CommandVerifier.from_env({"DEAR_AGENT_VERIFY_ALLOW": PROBE}, sandbox=None)
    assert verifier.allows(PROBE)
    return replace(verifier, _execute=session.run_command)


def test_verify_passes_because_it_cannot_see_the_control_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DEAR_AGENT_DATABASE_URL", "postgresql://control:secret@db/dear")
    shared = tmp_path / "shared"
    thread = _start_harness(shared)
    session = SidecarSession(shared, timeout=10, poll=0.02)

    try:
        result = _delegating_verifier(session).run(PROBE, str(_worktree(tmp_path).path))
    finally:
        session.close()
        thread.join(timeout=10)

    # A failure here means the command ran in this process (the control side) instead.
    assert result.ok is True, result.output


def test_verify_fails_cleanly_when_the_harness_container_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DEAR_AGENT_DATABASE_URL", "postgresql://control:secret@db/dear")
    session = SidecarSession(tmp_path / "shared", timeout=0.3, poll=0.05)

    result = _delegating_verifier(session).run(PROBE, str(_worktree(tmp_path).path))

    assert result.ok is False
    assert result.exit_code == 124


def test_the_executor_verifies_through_the_harness_container(tmp_path: Path, monkeypatch) -> None:
    """The harness run and the verify gate share one handshake, as ADR 0008 intends."""
    monkeypatch.setenv("DEAR_AGENT_DATABASE_URL", "postgresql://control:secret@db/dear")
    shared = tmp_path / "shared"
    thread = _start_harness(shared)
    session = SidecarSession(shared, timeout=10, poll=0.02)
    worktree = _worktree(tmp_path)

    try:
        run = DelegatingRunner(session).run(
            Task(id="t1", transport_id="<m1@x>"),
            TaskSpec(repo_url="", instructions="do nothing"),
            worktree,
        )
        result = _delegating_verifier(session).run(PROBE, str(worktree.path))
    finally:
        session.close()
        thread.join(timeout=10)

    assert run.ok is True
    assert result.ok is True, result.output


def test_build_worker_routes_the_verify_gate_through_the_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    """The wiring itself: with ``DEAR_AGENT_HARNESS_DIR`` set, the worker delegates."""
    from dear_agent.transports.memory import MemoryTransport
    from dear_agent.worker_factory import build_worker

    monkeypatch.setenv("DEAR_AGENT_DATABASE_URL", "postgresql://control:secret@db/dear")
    monkeypatch.setenv("DEAR_AGENT_VERIFY_ALLOW", PROBE)
    shared = tmp_path / "shared"
    monkeypatch.setenv("DEAR_AGENT_HARNESS_DIR", str(shared))
    thread = _start_harness(shared)
    worker = build_worker(
        queue=MemoryQueue(),
        transport=MemoryTransport(),
        repo_path=str(tmp_path),
        recipient=None,
        provider_model=None,
    )

    try:
        result = worker.verifier.run(PROBE, str(_worktree(tmp_path).path))
    finally:
        SidecarSession(shared).close()
        thread.join(timeout=10)

    assert result.ok is True, result.output
