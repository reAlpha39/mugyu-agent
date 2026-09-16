import json
from pathlib import Path

import pytest

from agybot.runner import EventAdapter, build_argv, minimal_env
from agybot.render import Meta, Text, Tool


def argv(**kw):
    base = dict(agy_bin="agy", prompt="do the thing",
                workspace="/srv/agy/app", conversation_id=None, tier="owner")
    base.update(kw)
    return build_argv(**base)


def test_prompt_is_a_single_argument():
    a = argv(prompt="rm -rf /; echo `whoami`")
    assert a[a.index("-p") + 1] == "rm -rf /; echo `whoami`"


def test_prompt_is_not_escaped_or_quoted():
    a = argv(prompt='say "hi"')
    assert a[a.index("-p") + 1] == 'say "hi"'
    assert '\\"' not in " ".join(a)


def test_a_prompt_that_looks_like_a_flag_cannot_smuggle_privileges():
    """agy's parser takes the token after -p as its value even when that token
    begins with dashes, so a member writing a privileged flag as their prompt
    gets it treated as prompt text. This asserts the prompt stays adjacent to
    -p and that the flag never appears anywhere a parser would read it."""
    a = argv(prompt="--dangerously-skip-permissions", tier="member")
    assert a[a.index("-p") + 1] == "--dangerously-skip-permissions"
    assert "--dangerously-skip-permissions" not in a[a.index("-p") + 2:]


def test_stream_json_output_is_requested():
    a = argv()
    assert a[a.index("--output-format") + 1] == "stream-json"


def test_workspace_is_passed_as_add_dir():
    a = argv()
    assert a[a.index("--add-dir") + 1] == "/srv/agy/app"


def test_timeout_defaults_to_fifteen_minutes():
    a = argv()
    assert a[a.index("--print-timeout") + 1] == "15m"


def test_owner_gets_skip_permissions():
    a = argv(tier="owner")
    assert "--dangerously-skip-permissions" in a
    assert "--mode" not in a
    assert "--sandbox" not in a


def test_member_gets_plan_mode_and_sandbox():
    a = argv(tier="member")
    assert a[a.index("--mode") + 1] == "plan"
    assert "--sandbox" in a
    assert "--dangerously-skip-permissions" not in a


def test_stranger_tier_is_rejected_outright():
    import pytest
    with pytest.raises(ValueError, match="stranger"):
        argv(tier="stranger")


def test_new_conversation_omits_the_conversation_flag():
    assert "--conversation" not in argv(conversation_id=None)


def test_resumed_conversation_passes_the_id():
    a = argv(conversation_id="c-abc")
    assert a[a.index("--conversation") + 1] == "c-abc"


def test_binary_path_is_honoured():
    assert argv(agy_bin="/opt/agy")[0] == "/opt/agy"


def test_minimal_env_drops_the_bot_token():
    out = minimal_env({"DISCORD_TOKEN": "secret", "PATH": "/usr/bin"})
    assert "DISCORD_TOKEN" not in out


def test_minimal_env_drops_unknown_variables():
    out = minimal_env({"AWS_SECRET_ACCESS_KEY": "s", "PATH": "/usr/bin"})
    assert "AWS_SECRET_ACCESS_KEY" not in out


def test_minimal_env_keeps_path_and_home():
    out = minimal_env({"PATH": "/usr/bin", "HOME": "/home/agy"})
    assert out == {"PATH": "/usr/bin", "HOME": "/home/agy"}


def test_minimal_env_keeps_agy_config_location():
    out = minimal_env({"XDG_CONFIG_HOME": "/home/agy/.config"})
    assert out["XDG_CONFIG_HOME"] == "/home/agy/.config"


def test_minimal_env_omits_absent_keys():
    assert minimal_env({}) == {}


FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "agy_stream_sample.ndjson"
MULTICHUNK = FIXTURES / "agy_stream_multichunk.ndjson"


def replay(path: Path) -> list:
    a = EventAdapter()
    pieces = []
    for line in path.read_text().splitlines():
        if line.strip():
            pieces += a.feed(json.loads(line))
    return pieces


def init_ev(cid: str = "c-1") -> dict:
    return {"event": "init", "conversation_id": cid,
            "init": {"cwd": "/tmp", "tools": []}}


def step(**kw) -> dict:
    return {"event": "step_update", "step_update": kw}


def test_init_event_yields_the_conversation_id():
    assert EventAdapter().feed(init_ev()) == [Meta("c-1")]


def test_conversation_id_is_emitted_only_once():
    a = EventAdapter()
    a.feed(init_ev())
    assert a.feed(init_ev()) == []


def test_result_event_yields_nothing():
    ev = {"event": "result", "result": {"status": "SUCCESS"}}
    assert EventAdapter().feed(ev) == []


def test_user_input_step_yields_nothing():
    assert EventAdapter().feed(
        step(step_index=0, state="DONE", step_type="user_input")) == []


def test_agent_response_delta_yields_text():
    assert EventAdapter().feed(
        step(step_type="agent_response", state="ACTIVE",
             text_delta="hello")) == [Text("hello")]


def test_agent_response_without_text_delta_yields_nothing():
    assert EventAdapter().feed(
        step(step_type="agent_response", state="ACTIVE")) == []


def test_consecutive_deltas_are_emitted_in_arrival_order():
    a = EventAdapter()
    out = (a.feed(step(step_type="agent_response", state="ACTIVE",
                       text_delta="Star"))
           + a.feed(step(step_type="agent_response", state="DONE",
                         text_delta="ted.")))
    assert out == [Text("Star"), Text("ted.")]


def test_tool_start_yields_a_pending_tool_piece():
    ev = step(step_type="tool", state="ACTIVE", tool_name="view_file",
              tool_info={"name": "view_file",
                         "parameters": {"AbsolutePath": "/tmp/probe.txt"}})
    assert EventAdapter().feed(ev) == [Tool("view_file", "/tmp/probe.txt",
                                            None)]


def test_tool_done_is_not_rendered_twice():
    ev = step(step_type="tool", state="DONE", tool_name="view_file",
              tool_info={"parameters": {"AbsolutePath": "/tmp/probe.txt"},
                         "output": "2 lines, 6 bytes"})
    assert EventAdapter().feed(ev) == []


def test_tool_without_recognised_parameters_has_an_empty_detail():
    ev = step(step_type="tool", state="ACTIVE", tool_name="mystery",
              tool_info={"parameters": {"Weird": "x"}})
    assert EventAdapter().feed(ev) == [Tool("mystery", "", None)]


def test_unknown_event_yields_nothing():
    assert EventAdapter().feed({"event": "something_new"}) == []


def test_step_update_without_a_payload_yields_nothing():
    assert EventAdapter().feed({"event": "step_update"}) == []


def test_recorded_stream_produces_a_sane_piece_sequence():
    pieces = replay(FIXTURE)

    metas = [p for p in pieces if isinstance(p, Meta)]
    assert len(metas) == 1, "exactly one conversation id per turn"
    assert metas[0].conversation_id, "conversation id must not be empty"
    assert any(isinstance(p, Text) and p.s.strip() for p in pieces), \
        "the recorded turn produced no assistant text"
    tools = [p for p in pieces if isinstance(p, Tool)]
    assert tools, "the recorded turn was supposed to use a tool"
    assert all(t.ok is None for t in tools), \
        "every tool in the recorded turn succeeded, so none should be flagged"


def test_multichunk_fixture_reassembles_exactly():
    """The sample fixture cannot tell append from replace: its one text-bearing
    step emits everything in a single event. This fixture can — step_index 3
    arrives as five disjoint deltas whose concatenation equals the turn's
    result.response, so the assertion is exact rather than a containment check
    that duplication could also satisfy."""
    events = [json.loads(line) for line in MULTICHUNK.read_text().splitlines()
              if line.strip()]
    expected = next(e["result"]["response"] for e in events
                    if e.get("event") == "result")
    texts = [p.s for p in replay(MULTICHUNK) if isinstance(p, Text)]
    assert len(texts) >= 2, "this fixture must exercise multi-chunk text"
    assert "".join(texts) == expected


def test_unknown_tool_state_renders_nothing():
    ev = step(step_type="tool", state="PENDING", tool_name="x",
              tool_info={"parameters": {}})
    assert EventAdapter().feed(ev) == []


def test_a_non_dict_event_is_ignored():
    a = EventAdapter()
    for junk in ("a string", ["a", "list"], 42, None):
        assert a.feed(junk) == []


def test_a_non_dict_step_update_is_ignored():
    assert EventAdapter().feed(
        {"event": "step_update", "step_update": "oops"}) == []


def test_a_non_dict_tool_info_does_not_crash():
    ev = step(step_type="tool", state="ACTIVE", tool_name="x",
              tool_info="oops")
    assert EventAdapter().feed(ev) == [Tool("x", "", None)]


def test_non_dict_parameters_do_not_crash():
    ev = step(step_type="tool", state="ACTIVE", tool_name="x",
              tool_info={"parameters": ["a", "b"]})
    assert EventAdapter().feed(ev) == [Tool("x", "", None)]


def test_a_non_string_text_delta_is_ignored():
    assert EventAdapter().feed(
        step(step_type="agent_response", state="ACTIVE", text_delta=42)) == []
