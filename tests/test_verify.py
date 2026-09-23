from __future__ import annotations

from pathlib import Path

from herald.verify import CommandVerifier


def test_from_env_parses_the_allowlist() -> None:
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "pytest, make test ; cargo test"})

    assert verifier.allowed == (("pytest",), ("make", "test"), ("cargo", "test"))


def test_empty_allowlist_denies_everything() -> None:
    verifier = CommandVerifier.from_env({})

    assert verifier.allows("pytest") is False
    assert verifier.run("pytest", "/tmp").blocked is True


def test_allows_prefix_commands() -> None:
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "pytest,make test"})

    assert verifier.allows("pytest -q tests/") is True
    assert verifier.allows("make test -v") is True
    assert verifier.allows("make deploy") is False
    assert verifier.allows("rm -rf /") is False


def test_run_executes_an_allowed_command(tmp_path: Path) -> None:
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "python"})

    result = verifier.run('python -c "print(1)"', str(tmp_path))

    assert result.ok is True
    assert result.exit_code == 0


def test_run_reports_a_nonzero_exit(tmp_path: Path) -> None:
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "python"})

    result = verifier.run('python -c "import sys; sys.exit(3)"', str(tmp_path))

    assert result.ok is False
    assert result.exit_code == 3


def test_run_blocks_a_disallowed_command(tmp_path: Path) -> None:
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "pytest"})

    result = verifier.run("rm -rf /", str(tmp_path))

    assert result.blocked is True
    assert result.ok is False


def test_run_never_uses_a_shell(tmp_path: Path) -> None:
    # The command is executed as argv, so a shell metacharacter is a literal argument.
    verifier = CommandVerifier.from_env({"HERALD_VERIFY_ALLOW": "echo"})

    verifier.run("echo hi; touch pwned", str(tmp_path))

    assert not (tmp_path / "pwned").exists()
