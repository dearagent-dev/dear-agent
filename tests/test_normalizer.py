from __future__ import annotations

from datetime import UTC, datetime

from herald.normalizer import NormalizedTask, Rejected, RejectReason, normalize
from herald.transports.base import Attachment, RawMessage

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_message(body: str, **overrides: object) -> RawMessage:
    values: dict[str, object] = {
        "transport_id": "<m1@x>",
        "thread_id": "t1",
        "sender": "dev@example.com",
        "subject": "[herald] owner/repo: add health endpoint",
        "body": body,
        "received_at": BASE,
    }
    values.update(overrides)
    return RawMessage(**values)  # type: ignore[arg-type]


def test_rejects_attachments() -> None:
    message = make_message(
        "repo: https://github.com/owner/repo\n\nadd healthz",
        attachments=[Attachment(name="patch.diff", content_type="text/x-patch", size=1)],
    )

    result = normalize(message)

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.ATTACHMENTS
    assert "Git" in result.detail


def test_rejects_when_no_repo_is_present() -> None:
    result = normalize(make_message("just do something nice"))

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.NO_REPO


def test_rejects_empty_instructions() -> None:
    result = normalize(make_message("repo: https://github.com/owner/repo\n"))

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.EMPTY


def test_parses_metadata_and_strips_it_from_instructions() -> None:
    message = make_message(
        "repo: https://github.com/owner/repo\n"
        "base: develop\n"
        "model: local:qwen\n\n"
        "add a health endpoint\nwith tests"
    )

    result = normalize(message)

    assert isinstance(result, NormalizedTask)
    assert result.spec.repo_url == "https://github.com/owner/repo"
    assert result.spec.base_branch == "develop"
    assert result.spec.model_request == "local:qwen"
    assert result.spec.instructions == "add a health endpoint\nwith tests"


def test_defaults_base_branch_to_main() -> None:
    result = normalize(make_message("repo: https://github.com/owner/repo\n\nfix tests"))

    assert isinstance(result, NormalizedTask)
    assert result.spec.base_branch == "main"


def test_task_carries_identity_from_the_message() -> None:
    message = make_message(
        "repo: https://github.com/owner/repo\n\nfix tests",
        transport_id="<msg-42@x>",
        thread_id="thread-9",
        sender="someone@example.com",
        subject="a task",
    )

    result = normalize(message)

    assert isinstance(result, NormalizedTask)
    assert result.task.id == "<msg-42@x>"
    assert result.task.transport_id == "<msg-42@x>"
    assert result.task.thread_id == "thread-9"
    assert result.task.sender == "someone@example.com"
    assert result.task.subject == "a task"


def test_infers_repo_from_recipient_owner_repo_address() -> None:
    message = make_message("add healthz", subject="[herald] add healthz")

    result = normalize(message, recipient="owner-repo@herald.example.com")

    assert isinstance(result, NormalizedTask)
    assert result.spec.repo_url == "owner/repo"


def test_header_repo_wins_over_the_routing_address() -> None:
    message = make_message("repo: https://github.com/actual/repo\n\nfix tests")

    result = normalize(message, recipient="other-repo@herald.example.com")

    assert isinstance(result, NormalizedTask)
    assert result.spec.repo_url == "https://github.com/actual/repo"


def test_metadata_keys_are_case_insensitive() -> None:
    result = normalize(make_message("REPO: https://github.com/owner/repo\nBase: dev\n\nfix"))

    assert isinstance(result, NormalizedTask)
    assert result.spec.repo_url == "https://github.com/owner/repo"
    assert result.spec.base_branch == "dev"


def test_metadata_is_not_treated_as_instructions() -> None:
    result = normalize(
        make_message("repo: https://github.com/owner/repo\nmodel: local:x\n\nfix the build")
    )

    assert isinstance(result, NormalizedTask)
    assert result.spec.instructions == "fix the build"
    assert result.spec.model_request == "local:x"


def test_only_metadata_and_no_instructions_is_rejected() -> None:
    result = normalize(make_message("repo: https://github.com/owner/repo\nmodel: local:x"))

    assert isinstance(result, Rejected)
    assert result.reason is RejectReason.EMPTY
