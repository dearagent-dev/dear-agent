from __future__ import annotations

import pytest

from dear_agent.runners.catalog import (
    EnvHarnessCatalog,
    HarnessCatalog,
    build_runner_for,
    select_harness,
)
from dear_agent.runners.claude import ClaudeCodeRunner
from dear_agent.runners.codex import CodexRunner
from dear_agent.runners.opencode import OpenCodeRunner
from dear_agent.sandbox import NoSandbox


def test_catalog_satisfies_the_port() -> None:
    assert isinstance(EnvHarnessCatalog(env={}), HarnessCatalog)


def test_catalog_lists_the_first_class_harnesses() -> None:
    infos = {info.id: info for info in EnvHarnessCatalog(env={}).list_harnesses()}

    assert set(infos) == {"opencode", "claude", "codex"}
    assert infos["opencode"].accepts_model is True
    assert infos["claude"].accepts_model is False
    assert infos["codex"].accepts_model is False


def test_catalog_lists_command_only_when_configured() -> None:
    assert "command" not in {i.id for i in EnvHarnessCatalog(env={}).list_harnesses()}

    catalog = EnvHarnessCatalog(env={"DEAR_AGENT_HARNESS_COMMAND": "my-agent --flag"})

    assert "command" in {i.id for i in catalog.list_harnesses()}


def test_select_harness_by_id() -> None:
    info = select_harness(EnvHarnessCatalog(env={}), "codex")

    assert info.id == "codex"


def test_select_harness_auto_picks_the_first_available() -> None:
    def which(name: str) -> str | None:
        return "/usr/bin/claude" if name == "claude" else None

    info = select_harness(EnvHarnessCatalog(env={}), "local-agent", which=which)

    assert info.id == "claude"


def test_select_harness_auto_raises_when_nothing_is_available() -> None:
    with pytest.raises(RuntimeError):
        select_harness(EnvHarnessCatalog(env={}), "auto", which=lambda name: None)


def test_select_harness_rejects_an_unknown_id() -> None:
    with pytest.raises(RuntimeError):
        select_harness(EnvHarnessCatalog(env={}), "nope")


def test_select_harness_command_without_a_command_raises() -> None:
    with pytest.raises(RuntimeError):
        select_harness(EnvHarnessCatalog(env={}), "command")


def test_the_model_is_passed_only_to_harnesses_that_accept_one() -> None:
    catalog = {info.id: info for info in EnvHarnessCatalog(env={}).list_harnesses()}
    sandbox = NoSandbox()

    opencode = build_runner_for(catalog["opencode"], "deepseek/v4", sandbox)
    claude = build_runner_for(catalog["claude"], "deepseek/v4", sandbox)
    codex = build_runner_for(catalog["codex"], "deepseek/v4", sandbox)

    assert isinstance(opencode, OpenCodeRunner) and opencode.model == "deepseek/v4"
    assert isinstance(claude, ClaudeCodeRunner) and claude.model is None
    assert isinstance(codex, CodexRunner) and codex.model is None


def test_build_runner_honors_the_binary_override(monkeypatch) -> None:
    from dear_agent.worker_factory import build_runner

    monkeypatch.setenv("DEAR_AGENT_HARNESS", "opencode")
    monkeypatch.setenv("DEAR_AGENT_HARNESS_BINARY", "/opt/oc")
    monkeypatch.setenv("DEAR_AGENT_SANDBOX", "none")

    runner = build_runner(None, sandbox=NoSandbox())

    assert isinstance(runner, OpenCodeRunner)
    assert runner.binary == "/opt/oc"


def test_build_runner_auto_selects_an_available_agent(monkeypatch) -> None:
    from dear_agent.worker_factory import build_runner

    monkeypatch.setenv("DEAR_AGENT_HARNESS", "auto")
    monkeypatch.setenv("DEAR_AGENT_SANDBOX", "none")
    monkeypatch.setattr(
        "dear_agent.runners.catalog.shutil.which",
        lambda name: "/usr/bin/opencode" if name == "opencode" else None,
    )

    runner = build_runner(None, sandbox=NoSandbox())

    assert isinstance(runner, OpenCodeRunner)
