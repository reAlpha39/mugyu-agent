import asyncio

import pytest

from agybot.render import Chunker, fence_state, LIMIT, Sink, Text, Tool, Meta, fmt_elapsed


def test_limit_is_1800():
    assert LIMIT == 1800


def test_short_feed_seals_nothing():
    c = Chunker()
    assert c.feed("hello") == []
    assert c.current == "hello"


def test_flush_returns_the_body():
    c = Chunker()
    c.feed("hello")
    assert c.flush() == ["hello"]
    assert c.current == ""


def test_flush_on_empty_body_returns_nothing():
    assert Chunker().flush() == []


def test_body_exactly_at_limit_is_not_sealed():
    c = Chunker()
    assert c.feed("a" * LIMIT) == []
    assert len(c.current) == LIMIT


def test_piece_crossing_limit_seals_before_appending():
    c = Chunker()
    c.feed("a" * (LIMIT - 5))
    sealed = c.feed("bbbbbbbbbb")
    assert sealed == ["a" * (LIMIT - 5)]
    assert c.current == "bbbbbbbbbb"


def test_no_body_ever_exceeds_the_limit():
    c = Chunker()
    bodies = []
    for _ in range(50):
        bodies += c.feed("word " * 20)
    bodies += c.flush()
    assert all(len(b) <= LIMIT for b in bodies)


def test_normal_piece_is_never_split():
    c = Chunker()
    c.feed("a" * (LIMIT - 3))
    c.feed("intact")
    assert "intact" in c.current
    assert c.current.count("intact") == 1


def test_oversized_piece_splits_at_a_newline():
    c = Chunker()
    piece = ("x" * 100 + "\n") * 40          # 4040 chars, newlines throughout
    bodies = c.feed(piece) + c.flush()
    assert len(bodies) > 1
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies).replace("\n", "") == piece.replace("\n", "")


def test_oversized_piece_without_newlines_splits_at_a_space():
    c = Chunker()
    piece = "word " * 500                    # 2500 chars, no newlines
    bodies = c.feed(piece) + c.flush()
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies).replace(" ", "") == piece.replace(" ", "")


def test_oversized_piece_without_whitespace_hard_splits():
    c = Chunker()
    piece = "z" * 4000
    bodies = c.feed(piece) + c.flush()
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies) == piece


def test_fence_state_detects_open_fence_with_language():
    assert fence_state("text\n```python\ncode") == "python"


def test_fence_state_detects_open_fence_without_language():
    assert fence_state("text\n```\ncode") == ""


def test_fence_state_is_none_when_balanced():
    assert fence_state("```python\ncode\n```\nafter") is None


def test_fence_state_is_none_for_plain_text():
    assert fence_state("no fences here") is None


def test_split_inside_fence_closes_and_reopens_with_language():
    c = Chunker()
    c.feed("```python\n")
    c.feed("y" * (LIMIT - 20))
    sealed = c.feed("more code here")
    assert len(sealed) == 1
    assert sealed[0].endswith("```")
    assert c.current.startswith("```python\n")
    assert "more code here" in c.current


def test_split_inside_unlabelled_fence_reopens_unlabelled():
    c = Chunker()
    c.feed("```\n")
    c.feed("y" * (LIMIT - 20))
    c.feed("more code here")
    assert c.current.startswith("```\n")


def test_split_outside_fence_does_not_add_fences():
    c = Chunker()
    c.feed("a" * (LIMIT - 5))
    sealed = c.feed("plain text")
    assert not sealed[0].endswith("```")
    assert not c.current.startswith("```")


def test_flush_closes_an_unterminated_fence():
    c = Chunker()
    c.feed("```python\nprint(1)")
    assert c.flush() == ["```python\nprint(1)\n```"]


def test_flush_does_not_double_close_a_balanced_fence():
    c = Chunker()
    c.feed("```python\nprint(1)\n```")
    assert c.flush() == ["```python\nprint(1)\n```"]


def test_fresh_chunker_carries_no_fence_state():
    c1 = Chunker()
    c1.feed("```python\nunterminated")
    c2 = Chunker()
    assert c2.feed("plain") == []
    assert fence_state(c2.current) is None


def test_sealed_body_with_open_fence_respects_the_limit():
    c = Chunker()
    c.feed("```\n")
    while len(c.current) < LIMIT - 10:
        c.feed("abcde")
    bodies = c.feed("xy") + c.flush()
    assert all(len(b) <= LIMIT for b in bodies), \
        [len(b) for b in bodies if len(b) > LIMIT]


def test_no_body_exceeds_the_limit_while_a_fence_stays_open():
    c = Chunker()
    bodies = c.feed("```python\n")
    for _ in range(200):
        bodies += c.feed("x" * 37)
    bodies += c.flush()
    assert all(len(b) <= LIMIT for b in bodies), \
        [len(b) for b in bodies if len(b) > LIMIT]


def test_flush_of_a_nearly_full_open_fence_respects_the_limit():
    c = Chunker()
    c.feed("```\n")
    while len(c.current) < LIMIT - 6:
        c.feed("ab")
    assert all(len(b) <= LIMIT for b in c.flush())


def test_overlong_fence_language_is_dropped_on_reopen():
    c = Chunker()
    c.feed("```" + "z" * 40 + "\n")
    c.feed("y" * (LIMIT - 60))
    sealed = c.feed("more text here")
    assert sealed, "this feed should have forced a seal"
    assert c.current.startswith("```\n")
    assert all(len(b) <= LIMIT for b in sealed)


class FakeChannel:
    """Records every send and edit so tests can assert on final state."""

    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str) -> int:
        self.messages.append(content)
        return len(self.messages) - 1

    async def edit(self, handle: int, content: str) -> None:
        self.messages[handle] = content


@pytest.fixture
def ch():
    return FakeChannel()


def sink_for(ch) -> Sink:
    # A short interval keeps tests fast without spinning the writer hot.
    # interval=0 would busy-loop asyncio.sleep(0) for the whole turn.
    return Sink(ch.send, ch.edit, interval=0.01)


def test_fmt_elapsed_under_a_minute():
    assert fmt_elapsed(9.4) == "9s"


def test_fmt_elapsed_over_a_minute():
    assert fmt_elapsed(82) == "1m22s"


async def test_start_posts_a_placeholder(ch):
    s = sink_for(ch)
    await s.start()
    assert ch.messages == ["🤔 …"]


async def test_text_replaces_the_placeholder(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("hello world"))
    await s.finish(0)
    assert ch.messages[0].startswith("hello world")


async def test_tool_renders_as_subtext(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("read", "auth.py", ok=None))
    await s.finish(0)
    assert "-# 🔧 read · auth.py" in ch.messages[0]


async def test_failed_tool_renders_a_warning(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("bash", "exit 1", ok=False))
    await s.finish(0)
    assert "-# ⚠️ bash · exit 1" in ch.messages[0]


async def test_meta_is_captured_and_not_displayed(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Meta("c-abc"))
    await s.feed(Text("body"))
    await s.finish(0)
    assert s.conversation_id == "c-abc"
    assert "c-abc" in ch.messages[0]      # only via the footer
    assert ch.messages[0].index("body") < ch.messages[0].index("c-abc")


async def test_overflow_creates_a_second_message(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("a" * (LIMIT - 5)))
    await s.feed(Text("bbbbbbbbbb"))
    await s.finish(0)
    assert len(ch.messages) == 2
    assert all(len(m) <= 2000 for m in ch.messages)


async def test_success_footer_on_the_last_message(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("done"))
    await s.finish(0)
    assert "-# ✅" in ch.messages[-1]


async def test_failure_footer_includes_stderr(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("partial"))
    await s.finish(1, stderr_tail="boom: could not open project")
    assert "-# ❌ exit 1" in ch.messages[-1]
    assert "boom: could not open project" in ch.messages[-1]


async def test_cancelled_footer(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("partial"))
    await s.finish(-15, cancelled=True)
    assert "-# 🛑 cancelled" in ch.messages[-1]


async def test_tool_count_appears_in_the_footer(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("read", "a.py", ok=True))
    await s.feed(Tool("read", "b.py", ok=True))
    await s.finish(0)
    assert "2 tools" in ch.messages[-1]


async def test_finish_without_any_output_still_reports(ch):
    s = sink_for(ch)
    await s.start()
    await s.finish(1, stderr_tail="not authenticated")
    assert "not authenticated" in ch.messages[-1]


class SuspendingChannel:
    """A channel whose send and edit genuinely yield to the event loop.

    FakeChannel's coroutines contain no suspension point, so awaiting them
    never returns control to the loop and the pump task is never scheduled.
    Concurrency can only be observed through a channel that really suspends.
    """

    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str) -> int:
        await asyncio.sleep(0)
        self.messages.append(content)
        return len(self.messages) - 1

    async def edit(self, handle: int, content: str) -> None:
        await asyncio.sleep(0)
        self.messages[handle] = content


def racing_sink(ch) -> Sink:
    # interval=0 is deliberate here: maximum pump pressure, to force the
    # interleavings a realistic interval would only hit occasionally.
    return Sink(ch.send, ch.edit, interval=0)


async def test_the_pump_actually_runs():
    ch = SuspendingChannel()
    s = racing_sink(ch)
    await s.start()
    await s.feed(Text("partial"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert ch.messages[0] == "partial", "the pump never wrote anything"
    await s.finish(0)


async def test_pump_cannot_overwrite_a_sealed_message():
    ch = SuspendingChannel()
    s = racing_sink(ch)
    await s.start()
    await s.feed(Text("a" * (LIMIT - 5)))
    for _ in range(5):
        await asyncio.sleep(0)
    await s.feed(Text("bbbbbbbbbb"))
    for _ in range(5):
        await asyncio.sleep(0)
    await s.finish(0)
    assert ch.messages[0] == "a" * (LIMIT - 5)
    assert "bbbbbbbbbb" not in ch.messages[0]
    assert "bbbbbbbbbb" in ch.messages[1]


async def test_finish_is_not_clobbered_by_an_in_flight_pump_edit():
    ch = SuspendingChannel()
    s = racing_sink(ch)
    await s.start()
    await s.feed(Text("body"))
    for _ in range(5):
        await asyncio.sleep(0)
    await s.finish(0)
    assert "-# ✅" in ch.messages[-1]
    assert ch.messages[-1].startswith("body")


async def test_no_text_is_lost_or_duplicated_across_many_seals():
    ch = SuspendingChannel()
    s = racing_sink(ch)
    await s.start()
    for i in range(60):
        await s.feed(Text(f"[{i:03d}]" + "x" * 60))
        await asyncio.sleep(0)
    await s.finish(0)
    joined = "".join(ch.messages)
    for i in range(60):
        assert joined.count(f"[{i:03d}]") == 1, f"marker {i} lost or duplicated"


async def test_every_message_stays_within_discords_hard_cap():
    ch = SuspendingChannel()
    s = racing_sink(ch)
    await s.start()
    for _ in range(40):
        await s.feed(Text("y" * 120))
        await asyncio.sleep(0)
    await s.finish(1, stderr_tail="e" * 500)
    assert all(len(m) <= 2000 for m in ch.messages), \
        [len(m) for m in ch.messages if len(m) > 2000]
