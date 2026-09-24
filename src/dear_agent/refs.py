from __future__ import annotations

_REF_FORBIDDEN = set(" ~^:?*[\\")


def is_valid_ref(name: str) -> bool:
    """Whether ``name`` is a safe, plausible git ref (never option-like).

    Rejects a leading ``-`` (so a message-supplied ref cannot be read as a git option —
    argument injection), whitespace, the characters git forbids in a ref name, and ``..`` /
    ``@{``.
    """
    if not name or name.startswith(("-", "/")) or name.endswith("/"):
        return False
    if name.endswith(".") or ".." in name or "@{" in name:
        return False
    return not any(char in _REF_FORBIDDEN for char in name)


__all__ = ["is_valid_ref"]
