"""Piece vocabulary and the Discord message chunker."""
from __future__ import annotations

from dataclasses import dataclass

LIMIT = 1800

# Headroom kept free in an oversized split so the reopened fence prefix and
# the closing fence always fit.
RESERVE = 24


@dataclass(frozen=True)
class Text:
    s: str


@dataclass(frozen=True)
class Tool:
    name: str
    detail: str
    ok: bool | None


@dataclass(frozen=True)
class Meta:
    conversation_id: str


Piece = Text | Tool | Meta


def fence_state(text: str) -> str | None:
    """Return the language of the open code fence, or None if balanced.

    An empty string means a fence is open with no language given.
    Computed over the whole text rather than incrementally, so a fence
    marker arriving split across two stream deltas cannot be missed.
    """
    fence: str | None = None
    i = 0
    while (j := text.find("```", i)) != -1:
        if fence is None:
            k = text.find("\n", j + 3)
            raw = text[j + 3:k] if k != -1 else text[j + 3:]
            fence = raw.strip()
        else:
            fence = None
        i = j + 3
    return fence


def _split_oversized(s: str, maxlen: int) -> list[str]:
    """Break a single piece that cannot fit in one message.

    Prefers a newline boundary, then a space, then a hard cut.
    """
    if len(s) <= maxlen:
        return [s]

    parts: list[str] = []
    rest = s
    while len(rest) > maxlen:
        window = rest[:maxlen]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = maxlen
        else:
            cut += 1                 # keep the boundary character with the part
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


class Chunker:
    """Accumulates text into message bodies of at most `limit` characters.

    A piece is appended whole. If appending it would exceed the limit, the
    current body is sealed first and the piece opens the next one, so a body
    never exceeds the limit and a piece is never split across bodies. The one
    exception is a piece longer than the limit, which is split internally.
    """

    def __init__(self, limit: int = LIMIT) -> None:
        self._limit = limit
        self._body = ""

    @property
    def current(self) -> str:
        return self._body

    def feed(self, s: str) -> list[str]:
        sealed: list[str] = []
        for piece in _split_oversized(s, self._limit - RESERVE):
            if len(self._body) + len(piece) > self._limit:
                sealed.append(self._seal())
            self._body += piece
        return sealed

    def flush(self) -> list[str]:
        if not self._body:
            return []
        body = self._close_fence(self._body)
        self._body = ""
        return [body]

    def _seal(self) -> str:
        """Close the current body and start the next, carrying fence state."""
        lang = fence_state(self._body)
        body = self._close_fence(self._body)
        self._body = "" if lang is None else f"```{lang}\n"
        return body

    @staticmethod
    def _close_fence(body: str) -> str:
        if fence_state(body) is None:
            return body
        return body + ("```" if body.endswith("\n") else "\n```")
