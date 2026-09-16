"""Piece vocabulary and the Discord message chunker."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

LIMIT = 1800

# Headroom kept free in an oversized split so the reopened fence prefix and
# the closing fence always fit.
RESERVE = 24

MAX_FENCE_LANG = 12


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


def _close_suffix(body: str) -> str:
    """The text needed to close this body's open fence, or "" if balanced."""
    if fence_state(body) is None:
        return ""
    return "```" if body.endswith("\n") else "\n```"


def sealed_len(body: str) -> int:
    """Length this body would have once sealed, fence closure included.

    The seal decision uses this rather than len(), so a body that still owes
    a closing fence reserves room for it instead of overflowing when sealed.
    """
    return len(body) + len(_close_suffix(body))


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
            if sealed_len(self._body + piece) > self._limit:
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
        if lang is None:
            self._body = ""
        else:
            # An over-long tag is dropped rather than truncated: an unlabelled
            # block renders better than a mislabelled one, and bounding the
            # prefix keeps the reopened body inside RESERVE.
            tag = lang if len(lang) <= MAX_FENCE_LANG else ""
            self._body = f"```{tag}\n"
        return body

    @staticmethod
    def _close_fence(body: str) -> str:
        return body + _close_suffix(body)


SendFn = Callable[[str], Awaitable[Any]]
EditFn = Callable[[Any, str], Awaitable[None]]

PLACEHOLDER = "🤔 …"
STDERR_TAIL = 300


def fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


class Sink:
    """Streams pieces into a Discord thread as live-edited messages.

    Only the most recent message is ever edited; sealed messages are final.
    Edit cost is therefore flat in the length of the turn.
    """

    def __init__(self, send: SendFn, edit: EditFn,
                 limit: int = LIMIT, interval: float = 1.5) -> None:
        self._send = send
        self._edit = edit
        self._interval = interval
        self._chunker = Chunker(limit)
        self._handle: Any = None
        self._dirty = False
        self._writer: asyncio.Task | None = None
        self._started = 0.0
        self._tools = 0
        self.conversation_id: str | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._started = time.monotonic()
        self._handle = await self._send(PLACEHOLDER)
        self._writer = asyncio.create_task(self._pump())

    async def feed(self, piece: Piece) -> None:
        if isinstance(piece, Meta):
            self.conversation_id = piece.conversation_id
            return

        if isinstance(piece, Tool):
            # Counted for the footer but not rendered: the per-call subtext
            # lines were noise in the channel. The footer's tool count still
            # shows that work happened.
            self._tools += 1
            return

        text = piece.s

        # The chunker advance and the handle repoint must be atomic with
        # respect to the pump, which reads both. Splitting them lets the pump
        # write the next body onto the previous, already-sealed message.
        async with self._lock:
            for sealed in self._chunker.feed(text):
                await self._edit(self._handle, sealed)
                self._handle = await self._send(self._chunker.current or "…")
            self._dirty = True

    async def finish(self, returncode: int, stderr_tail: str = "",
                     cancelled: bool = False, timed_out: bool = False) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
            # Awaiting the cancelled task guarantees no pump edit is still in
            # flight; otherwise one can land after the footer and clobber it.
            with contextlib.suppress(asyncio.CancelledError):
                await writer

        async with self._lock:
            footer = self._footer(returncode, stderr_tail, cancelled, timed_out)
            bodies = self._chunker.flush()
            body = bodies[0] if bodies else ""

            # flush() yields at most one body, so the footer joins it whenever
            # the pair fits inside Discord's hard 2000-character message cap.
            if len(body) + len(footer) <= 2000:
                await self._edit(self._handle, (body + footer) or PLACEHOLDER)
            else:
                await self._edit(self._handle, body)
                self._handle = await self._send(footer)

    def _footer(self, returncode: int, stderr_tail: str,
                cancelled: bool, timed_out: bool = False) -> str:
        elapsed = fmt_elapsed(time.monotonic() - self._started)
        cid = self.conversation_id or "unknown"
        if timed_out:
            head = "-# ⏱️ timed out"
        elif cancelled:
            head = "-# 🛑 cancelled"
        elif returncode == 0:
            head = "-# ✅"
        else:
            head = f"-# ❌ exit {returncode}"
        out = f"\n{head} · {elapsed} · {self._tools} tools · conv {cid}"
        if returncode != 0 and stderr_tail:
            out += f"\n```\n{stderr_tail[-STDERR_TAIL:]}\n```"
        return out

    async def _pump(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                async with self._lock:
                    if self._dirty:
                        self._dirty = False
                        await self._edit(self._handle,
                                         self._chunker.current or PLACEHOLDER)
        except asyncio.CancelledError:
            pass
