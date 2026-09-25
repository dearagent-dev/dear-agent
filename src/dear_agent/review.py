from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# The instruction that turns any OpenAI-compatible chat model into an adversarial reviewer
# (ADR 0010). It is a judgment about a diff, never a second implementation: it approves or asks
# for a revision with concrete notes.
SYSTEM_PROMPT = (
    "You are a strict code reviewer. You receive the INSTRUCTIONS a coding agent was given and "
    "the DIFF it produced. Decide whether the change should become a pull request as-is. Reply "
    "with ONE JSON object and nothing else: "
    '{"verdict": "approve" | "revise", "summary": "<one line>", '
    '"notes": "<what to fix, or empty>"}. '
    "Approve when the change satisfies the instructions and looks correct; otherwise revise and "
    "list concrete, actionable fixes. Never rewrite the code."
)


class ReviewError(RuntimeError):
    """A review could not be obtained from the model."""


@dataclass(slots=True, frozen=True)
class ReviewVerdict:
    """The reviewer's judgment: approve, or a bounded request to revise with notes."""

    approved: bool
    summary: str = ""
    notes: str = ""


@runtime_checkable
class Reviewer(Protocol):
    """Reviews a diff before it becomes a pull request (advisory, ADR 0010)."""

    def review(self, *, diff: str, instructions: str) -> ReviewVerdict: ...


@dataclass(slots=True)
class NoReviewer:
    """The default: approve everything, so behavior is unchanged without a reviewer."""

    def review(self, *, diff: str, instructions: str) -> ReviewVerdict:
        return ReviewVerdict(approved=True, summary="no reviewer configured")


ChatFn = Callable[..., str]


def _http_chat(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float,
    messages: list[dict[str, str]],
) -> str:
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": 0, "stream": False}
    ).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReviewError(f"reviewer {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ReviewError(f"reviewer unreachable: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewError("reviewer returned non-JSON") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewError("reviewer response has no content") from exc
    if not isinstance(content, str):
        raise ReviewError("reviewer content is not a string")
    return content


@dataclass(slots=True)
class ModelReviewer:
    """A reviewer over any OpenAI-compatible chat endpoint (stdlib-only).

    Point it at a different provider/model than the harness to get an independent opinion; a
    local endpoint keeps the diff on the box. Callers catch its :class:`ReviewError` and fall
    back to publishing (fail-open, ADR 0010).
    """

    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    _chat: ChatFn = field(default=_http_chat, repr=False)

    def review(self, *, diff: str, instructions: str) -> ReviewVerdict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._prompt(diff, instructions)},
        ]
        content = self._chat(
            api_key=self.api_key,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
            messages=messages,
        )
        return _parse_verdict(content)

    @staticmethod
    def _prompt(diff: str, instructions: str) -> str:
        return f"INSTRUCTIONS:\n{instructions}\n\nDIFF:\n{diff}\n\nReply with one JSON object."


def _parse_verdict(content: str) -> ReviewVerdict:
    try:
        data = json.loads(_strip_fence(content))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"reviewer content is not JSON: {content[:200]!r}") from exc
    if not isinstance(data, dict):
        raise ReviewError("reviewer content is not a JSON object")
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("approve", "revise"):
        raise ReviewError(f"reviewer verdict {verdict!r} is not approve|revise")
    return ReviewVerdict(
        approved=verdict == "approve",
        summary=str(data.get("summary", "")),
        notes=str(data.get("notes", "")),
    )


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def build_reviewer(
    env: Mapping[str, str] | None = None, *, chat: ChatFn | None = None
) -> Reviewer | None:
    """Build the configured reviewer, or ``None`` when disabled (ADR 0010).

    ``DEAR_AGENT_REVIEWER=none|openai-compat`` (default ``none``); ``openai-compat`` needs
    ``DEAR_AGENT_REVIEWER_MODEL`` and reads ``DEAR_AGENT_REVIEWER_BASE_URL`` /
    ``DEAR_AGENT_REVIEWER_API_KEY`` (falling back to ``OPENROUTER_API_KEY``).
    """
    source = env if env is not None else os.environ
    choice = (source.get("DEAR_AGENT_REVIEWER") or "none").strip().lower()
    if choice in ("", "none", "off"):
        return None
    if choice not in ("openai-compat", "openai", "model"):
        raise ReviewError(f"unknown DEAR_AGENT_REVIEWER {choice!r}; expected none|openai-compat")
    model = (source.get("DEAR_AGENT_REVIEWER_MODEL") or "").strip()
    if not model:
        raise ReviewError("DEAR_AGENT_REVIEWER_MODEL is required")
    return ModelReviewer(
        api_key=source.get("DEAR_AGENT_REVIEWER_API_KEY")
        or source.get("OPENROUTER_API_KEY")
        or "local",
        model=model,
        base_url=source.get("DEAR_AGENT_REVIEWER_BASE_URL") or DEFAULT_BASE_URL,
        timeout=float(source.get("DEAR_AGENT_REVIEWER_TIMEOUT", str(int(DEFAULT_TIMEOUT_SECONDS)))),
        _chat=chat or _http_chat,
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "NoReviewer",
    "ModelReviewer",
    "ReviewError",
    "ReviewVerdict",
    "Reviewer",
    "build_reviewer",
]
