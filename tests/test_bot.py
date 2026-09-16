from agybot.bot import parse_mention, thread_name


def test_mention_is_stripped():
    m = parse_mention("<@42> fix the bug", "42")
    assert m.prompt == "fix the bug"


def test_nickname_mention_is_stripped():
    m = parse_mention("<@!42> fix the bug", "42")
    assert m.prompt == "fix the bug"


def test_workspace_prefix_is_extracted():
    m = parse_mention("<@42> [bliss-app] fix the bug", "42")
    assert m.workspace == "bliss-app"
    assert m.prompt == "fix the bug"


def test_absent_prefix_yields_no_workspace():
    m = parse_mention("<@42> fix the bug", "42")
    assert m.workspace is None


def test_bracket_later_in_the_text_is_not_a_workspace():
    m = parse_mention("<@42> fix the list[0] bug", "42")
    assert m.workspace is None
    assert m.prompt == "fix the list[0] bug"


def test_extra_whitespace_is_collapsed_at_the_edges():
    m = parse_mention("<@42>    [app]   do it  ", "42")
    assert m.workspace == "app"
    assert m.prompt == "do it"


def test_mention_only_yields_an_empty_prompt():
    assert parse_mention("<@42>", "42").prompt == ""


def test_workspace_only_yields_an_empty_prompt():
    m = parse_mention("<@42> [app]", "42")
    assert m.workspace == "app"
    assert m.prompt == ""


def test_thread_name_is_capped_at_sixty_characters():
    assert len(thread_name("x" * 200)) == 60


def test_thread_name_keeps_short_prompts_whole():
    assert thread_name("fix the bug") == "fix the bug"


def test_thread_name_falls_back_when_the_prompt_is_empty():
    assert thread_name("") == "agy task"
