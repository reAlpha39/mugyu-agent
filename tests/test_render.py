from agybot.render import Chunker, fence_state, LIMIT


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
