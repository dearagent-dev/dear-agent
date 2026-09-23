from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class InjectionKind(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_IMPERSONATION = "role_impersonation"
    CREDENTIAL_PROBE = "credential_probe"
    SHELL_COMMAND = "shell_command"
    EXFILTRATION = "exfiltration"


# These are heuristics for **audit and escalation**, not a security boundary. A determined
# attacker evades any pattern list, which is why the real containment is the sandbox, the
# worktree, the protected-branch guard and the human review of the PR.
PATTERNS: list[tuple[InjectionKind, re.Pattern[str]]] = [
    (
        InjectionKind.INSTRUCTION_OVERRIDE,
        re.compile(
            r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier)\b"
            r"[^.\n]{0,40}\b(instruction|prompt|rule)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.ROLE_IMPERSONATION,
        re.compile(
            r"(^|\n)\s*(system|assistant|developer)\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.CREDENTIAL_PROBE,
        re.compile(
            r"\b(reveal|show|print|send|exfiltrate|dump|leak)\b[^.\n]{0,40}"
            r"\b(api[_-]?key|secret|token|password|credential)s?\b"
            r"|\b(api[_-]?key|secret|token|password|credential)s?\b[^.\n]{0,30}"
            r"\b(reveal|show|print|send|exfiltrate|dump|leak)\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.SHELL_COMMAND,
        re.compile(r"(curl|wget|nc|ncat|base64\s+-d|/bin/sh|\$\(|`[^`]+`)", re.IGNORECASE),
    ),
    (
        InjectionKind.EXFILTRATION,
        re.compile(r"https?://(?!github\.com|example\.com)[^\s]+", re.IGNORECASE),
    ),
]


@dataclass(slots=True, frozen=True)
class Finding:
    kind: InjectionKind
    excerpt: str


@dataclass(slots=True)
class ScanResult:
    """The outcome of scanning untrusted text."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.findings)

    def kinds(self) -> set[InjectionKind]:
        return {finding.kind for finding in self.findings}


class InjectionScanner:
    """Flags likely prompt-injection patterns in untrusted text.

    The scanner is advisory: it records findings so a run can be escalated or a human can
    look, and it never executes anything. Its absence of findings is not a guarantee of
    safety, and its presence is not proof of an attack.
    """

    def __init__(self, *, max_findings: int = 20) -> None:
        self._max_findings = max_findings

    def scan(self, text: str) -> ScanResult:
        findings: list[Finding] = []
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(text):
                findings.append(Finding(kind=kind, excerpt=_excerpt(text, match)))
                if len(findings) >= self._max_findings:
                    return ScanResult(findings)
        return ScanResult(findings)

    def should_require_approval(self, text: str) -> bool:
        """True when findings are severe enough to gate the task behind a human."""
        return bool(
            self.scan(text).kinds()
            & {
                InjectionKind.INSTRUCTION_OVERRIDE,
                InjectionKind.CREDENTIAL_PROBE,
                InjectionKind.EXFILTRATION,
            }
        )


def _excerpt(text: str, match: re.Match[str]) -> str:
    start = max(match.start() - 20, 0)
    end = min(match.end() + 20, len(text))
    return " ".join(text[start:end].split())[:160]


__all__ = [
    "Finding",
    "InjectionKind",
    "InjectionScanner",
    "ScanResult",
]
