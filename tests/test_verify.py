from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from dear_agent.verify import CommandVerifier, _argv


def test_from_env_parses_the_allowlist() -> None:
    verifier = CommandVerifier.from_env(
        {"DEAR_AGENT_VERIFY_ALLOW": "pytest, make test ; cargo test"}
    )

    assert verifier.allowed == (("pytest",), ("make", "test"), ("cargo", "test"))


def test_empty_allowlist_denies_everything() -> None:
    verifier = CommandVerifier.from_env({})

    assert verifier.allows("pytest") is False
    assert verifier.run("pytest", "/tmp").blocked is True


def test_allows_exact_commands_only() -> None:
    verifier = CommandVerifier.from_env({"DEAR_AGENT_VERIFY_ALLOW": "pytest,make test"})

    assert verifier.allows("pytest") is True
    assert verifier.allows("make test") is True
    # exact match: extra args are not allowed (no prefix matching)
    assert verifier.allows("pytest -q tests/") is False
    assert verifier.allows("make test -v") is False
    assert verifier.allows("make deploy") is False
    assert verifier.allows("rm -rf /") is False


def test_run_executes_an_allowed_command(tmp_path: Path) -> None:
    verifier = CommandVerifier(allowed=(("python", "-c", "print(1)"),))

    result = verifier.run('python -c "print(1)"', str(tmp_path))

    assert result.ok is True
    assert result.exit_code == 0


def test_run_reports_a_nonzero_exit(tmp_path: Path) -> None:
    command = 'python -c "raise SystemExit(3)"'
    verifier = CommandVerifier(allowed=(_argv(command),))  # type: ignore[arg-type]

    result = verifier.run(command, str(tmp_path))

    assert result.ok is False
    assert result.exit_code == 3


def test_run_blocks_a_disallowed_command(tmp_path: Path) -> None:
    verifier = CommandVerifier.from_env({"DEAR_AGENT_VERIFY_ALLOW": "pytest"})

    result = verifier.run("rm -rf /", str(tmp_path))

    assert result.blocked is True
    assert result.ok is False


def test_run_never_uses_a_shell(tmp_path: Path) -> None:
    # argv is executed directly, so a shell metacharacter is a literal argument.
    verifier = CommandVerifier(allowed=(("echo", "hi;", "touch", "pwned"),))

    verifier.run("echo hi; touch pwned", str(tmp_path))

    assert not (tmp_path / "pwned").exists()


def test_run_wraps_the_command_in_the_sandbox(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Recording:
        @property
        def available(self) -> bool:
            return True

        def wrap(self, argv: list[str], *, worktree: Path) -> list[str]:
            return ["sandbox", *argv]

    def execute(argv: Any, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    verifier = CommandVerifier(
        allowed=(("echo",),),
        sandbox=Recording(),
        _execute=execute,  # type: ignore[arg-type]
    )

    verifier.run("echo", str(tmp_path))

    assert calls == [["sandbox", "echo"]]


def test_run_env_has_no_secrets(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "leak-me")
    command = "python -c \"import os; print(os.environ.get('SECRET_TOKEN', 'MISSING'))\""
    verifier = CommandVerifier(allowed=(_argv(command),))  # type: ignore[arg-type]

    result = verifier.run(command, str(tmp_path))

    assert "MISSING" in result.output
    assert "leak-me" not in result.output
