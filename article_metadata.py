"""Mechanical metadata extraction from the standard HLS article header."""

from __future__ import annotations

import re


_BYLINE_RE = re.compile(
    r"\A[ \t]*Autorin/Autor:[ \t]*(?:\r?\n[ \t]*)?"
    r"(?P<author>[^\r\n]+)(?:\r?\n)?",
    re.IGNORECASE,
)


def split_author_byline(text: str) -> tuple[str | None, str]:
    """Return the HLS byline author and the article body without the byline."""
    value = text or ""
    match = _BYLINE_RE.match(value)
    if not match:
        return None, value
    author = match.group("author").strip() or None
    return author, value[match.end():].lstrip()
