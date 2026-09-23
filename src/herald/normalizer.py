from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from herald.queue.models import Task, TaskSpec
from herald.transports.base import RawMessage

METADATA_RE = re.compile(r"^\s*(repo|base|branch|model)\s*:\s*(\S.*)$", re.IGNORECASE)
REPO_URL_RE = re.compile(r"^(?:https?://|git@)[^\s]+$|^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ROUTING_RE = re.compile(r"^(?P<owner>[A-Za-z0-9._-]+)-(?P<repo>[A-Za-z0-9._-]+)@")
DEFAULT_BASE_BRANCH = "main"


class RejectReason(StrEnum):
    """Why a message was not turned into a task."""

    ATTACHMENTS = "attachments"
    NO_REPO = "no_repo"
    EMPTY = "empty"


@dataclass(slots=True)
class NormalizedTask:
    """A message successfully mapped to a queue record and its content."""

    task: Task
    spec: TaskSpec


@dataclass(slots=True)
class Rejected:
    """A message that must not run, with a reason the notifier can explain."""

    transport_id: str
    reason: RejectReason
    detail: str


NormalizationResult = NormalizedTask | Rejected


def normalize(message: RawMessage, *, recipient: str | None = None) -> NormalizationResult:
    """Map a raw inbound message to a task or a rejection.

    The message is untrusted: Herald extracts a repo and instructions, and never turns
    message text into a command. A message carrying attachments, or with no resolvable
    repo, is rejected with an explanation rather than guessed at.
    """
    if message.attachments:
        return Rejected(
            transport_id=message.transport_id,
            reason=RejectReason.ATTACHMENTS,
            detail="code travels over Git only; resend without attachments or patches",
        )

    fields, instructions = _parse_body(message.body)
    repo_url = fields.get("repo") or _repo_from_recipient(recipient)
    if not repo_url:
        return Rejected(
            transport_id=message.transport_id,
            reason=RejectReason.NO_REPO,
            detail="no repo found; add a 'repo: <url>' line or use an owner-repo@ routing address",
        )
    if not instructions.strip():
        return Rejected(
            transport_id=message.transport_id,
            reason=RejectReason.EMPTY,
            detail="no instructions found; describe the task in the message body",
        )

    spec = TaskSpec(
        repo_url=repo_url,
        base_branch=fields.get("base", DEFAULT_BASE_BRANCH),
        instructions=instructions.strip(),
        model_request=fields.get("model"),
    )
    task = Task(
        id=message.transport_id,
        transport_id=message.transport_id,
        thread_id=message.thread_id,
        sender=message.sender,
        subject=message.subject,
        spec=spec,
    )
    return NormalizedTask(task=task, spec=spec)


def _parse_body(body: str) -> tuple[dict[str, str], str]:
    fields: dict[str, str] = {}
    instructions: list[str] = []
    for line in body.splitlines():
        match = METADATA_RE.match(line)
        if match is not None:
            fields[match.group(1).lower()] = match.group(2).strip()
        else:
            instructions.append(line)
    return fields, "\n".join(instructions)


def _repo_from_recipient(recipient: str | None) -> str | None:
    if not recipient:
        return None
    match = ROUTING_RE.match(recipient)
    if match is None:
        return None
    # Routing addresses only hint at a repo; the owner/repo pair still has to be a URL or
    # slug the Git plane can resolve. Never trust the address alone for authorization.
    slug = f"{match.group('owner')}/{match.group('repo')}"
    return slug if REPO_URL_RE.match(slug) else None
