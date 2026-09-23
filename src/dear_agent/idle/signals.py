from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,120})", re.IGNORECASE)
DEFAULT_SINCE = timedelta(days=14)
MAX_COMMITS = 50
MAX_TODOS = 50


class SignalKind(StrEnum):
    """Kinds of recent repository activity the idle loop reasons over."""

    COMMIT = "commit"
    CHANGED_FILE = "changed_file"
    TODO = "todo"


@dataclass(slots=True, frozen=True)
class Signal:
    """One observed fact about the repository, with a short evidence string."""

    kind: SignalKind
    summary: str
    evidence: str = ""


@dataclass(slots=True)
class Signals:
    """A snapshot of recent activity used to propose new work."""

    repo_path: Path
    since: datetime
    items: list[Signal] = field(default_factory=list)

    def of_kind(self, kind: SignalKind) -> list[Signal]:
        return [signal for signal in self.items if signal.kind is kind]


class SignalCollector:
    """Reads recent repository activity with read-only git commands.

    Only git and file reads happen here; nothing derived from content is executed. The
    collector is deliberately local: the idle loop should reason about *this* checkout,
    not fetch remote state.
    """

    def __init__(
        self,
        *,
        since: timedelta = DEFAULT_SINCE,
        max_changes: int = 200,
    ) -> None:
        self._since = since
        self._max_changes = max_changes

    def collect(self, repo_path: str | Path, *, now: datetime | None = None) -> Signals:
        repo = Path(repo_path).resolve()
        since = (now or datetime.now(UTC)) - self._since
        signals = Signals(repo_path=repo, since=since)
        if not (repo / ".git").exists():
            return signals

        signals.items.extend(self._commits(repo, since))
        signals.items.extend(self._todos(repo))
        return signals

    def _commits(self, repo: Path, since: datetime) -> list[Signal]:
        output = _git(
            repo,
            "log",
            f"--since={since.isoformat()}",
            f"--max-count={MAX_COMMITS}",
            "--pretty=format:%H%x09%s",
        )
        signals: list[Signal] = []
        for line in output.splitlines():
            if "\t" not in line:
                continue
            sha, subject = line.split("\t", 1)
            signals.append(Signal(SignalKind.COMMIT, subject.strip(), evidence=sha[:12]))
        return signals

    def _todos(self, repo: Path) -> list[Signal]:
        output = _git(repo, "grep", "-n", "-I", "-E", "TODO|FIXME|HACK|XXX", "--", ".")
        signals: list[Signal] = []
        for line in output.splitlines()[:MAX_TODOS]:
            path, _, rest = line.partition(":")
            match = TODO_PATTERN.search(rest)
            detail = match.group(0).strip() if match else rest.strip()
            signals.append(Signal(SignalKind.TODO, detail[:140], evidence=path))
        return signals


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        # `git grep` returns 1 when there are no matches; anything else is an error.
        return ""
    return result.stdout


__all__ = ["Signal", "SignalCollector", "SignalKind", "Signals"]
