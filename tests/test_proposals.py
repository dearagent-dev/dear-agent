from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from herald.idle.proposals import ProposalGenerator
from herald.idle.signals import Signal, SignalKind, Signals

REPO = Path("/repos/herald")


def make_signals(items: list[Signal]) -> Signals:
    return Signals(repo_path=REPO, since=datetime(2026, 1, 1, tzinfo=UTC), items=items)


def todo(marker: str, path: str = "app.py") -> Signal:
    return Signal(SignalKind.TODO, marker, evidence=path)


def commit(subject: str, sha: str = "abc1234") -> Signal:
    return Signal(SignalKind.COMMIT, subject, evidence=sha)


def test_todo_markers_become_proposals() -> None:
    signals = make_signals([todo("TODO: handle errors"), todo("FIXME: retry logic")])

    proposals = ProposalGenerator().generate(signals)

    assert len(proposals) == 2
    assert "handle errors" in proposals[0].title
    assert "app.py" in proposals[0].instructions


def test_commit_history_becomes_one_follow_up_proposal() -> None:
    signals = make_signals([commit("add health endpoint"), commit("fix build")])

    proposals = ProposalGenerator().generate(signals)

    assert len(proposals) == 1
    assert "Follow up" in proposals[0].title
    assert "add health endpoint" in proposals[0].instructions


def test_todos_are_preferred_over_commits() -> None:
    signals = make_signals([commit("a change"), todo("TODO: something")])

    proposals = ProposalGenerator().generate(signals)

    assert "something" in proposals[0].title


def test_proposals_are_bounded() -> None:
    signals = make_signals([todo(f"TODO: item {n}") for n in range(20)])

    proposals = ProposalGenerator(max_proposals=3).generate(signals)

    assert len(proposals) == 3


def test_no_signals_means_no_proposals() -> None:
    assert ProposalGenerator().generate(make_signals([])) == []


def test_marker_text_is_sanitized_to_one_line() -> None:
    signals = make_signals([todo("TODO: line one\nline two\tstill here")])

    proposal = ProposalGenerator().generate(signals)[0]

    assert "\n" not in proposal.instructions
    assert "\t" not in proposal.instructions


def test_repo_name_defaults_to_the_directory() -> None:
    signals = make_signals([todo("TODO: x")])

    proposal = ProposalGenerator().generate(signals)[0]

    assert proposal.repo_path == REPO


def test_evidence_is_attached_to_every_proposal() -> None:
    signals = make_signals([todo("TODO: x", path="src/a.py")])

    proposal = ProposalGenerator().generate(signals)[0]

    assert proposal.evidence == ["src/a.py: TODO: x"]
