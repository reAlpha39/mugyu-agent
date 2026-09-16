import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agybot.runner import EventAdapter, Slots, Turn, build_argv, minimal_env
from agybot.render import Meta, Sink, Text, Tool


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


async def test_owner_may_take_all_three_slots():
    s = Slots()
    for _ in range(3):
        await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert s.running == 3


async def test_owner_blocks_at_the_fourth():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.acquire("owner"), 0.05)


async def test_member_may_take_only_two():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    assert s.running == 2
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.acquire("member"), 0.05)


async def test_owner_is_admitted_while_members_hold_two():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert s.running == 3


async def test_release_admits_a_waiter():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    waiter = asyncio.create_task(s.acquire("owner"))
    await asyncio.sleep(0)
    s.release()
    await asyncio.wait_for(waiter, 0.1)
    assert s.running == 3


async def test_owner_overtakes_a_waiting_member():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    await s.acquire("owner")                       # full at 3

    member_waiter = asyncio.create_task(s.acquire("member"))
    await asyncio.sleep(0)
    s.release()                                    # running drops to 2

    # A member still may not enter at 2; the owner may.
    await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert not member_waiter.done()
    member_waiter.cancel()


async def test_ahead_counts_queued_requests():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    w1 = asyncio.create_task(s.acquire("owner"))
    w2 = asyncio.create_task(s.acquire("owner"))
    await asyncio.sleep(0)
    assert s.ahead("owner") == 2
    for w in (w1, w2):
        w.cancel()


async def test_ahead_is_zero_when_idle():
    assert Slots().ahead("member") == 0


async def test_would_block_tracks_the_tier_thresholds():
    s = Slots()
    assert not s.would_block("member")
    await s.acquire("member")
    await s.acquire("member")
    assert s.would_block("member")
    assert not s.would_block("owner")
    await s.acquire("owner")
    assert s.would_block("owner")


async def test_release_below_zero_is_refused():
    s = Slots()
    with pytest.raises(RuntimeError):
        s.release()


FAKE = str(Path(__file__).parent / "fake_agy.py")


class RecordingChannel:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str) -> int:
        self.messages.append(content)
        return len(self.messages) - 1

    async def edit(self, handle: int, content: str) -> None:
        self.messages[handle] = content


def turn_for(mode: str, tmp_path, wall_timeout: float = 960, **env_extra):
    ch = RecordingChannel()
    sink = Sink(ch.send, ch.edit, interval=0.01)
    env = {"FAKE_AGY_MODE": mode, "PATH": os.environ["PATH"], **env_extra}
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env,
             sink=sink, wall_timeout=wall_timeout)
    return t, sink, ch


async def test_successful_turn_exits_zero(tmp_path):
    t, _, _ = turn_for("normal", tmp_path)
    assert await t.run() == 0


async def test_successful_turn_captures_the_conversation_id(tmp_path):
    t, sink, _ = turn_for("normal", tmp_path)
    await t.run()
    assert sink.conversation_id == "c-fake"


async def test_successful_turn_renders_text_and_tools(tmp_path):
    t, _, ch = turn_for("normal", tmp_path)
    await t.run()
    whole = "\n".join(ch.messages)
    assert "the word is banana" in whole
    assert "🔧 view_file · probe.txt" in whole
    assert "-# ✅" in whole


async def test_failing_turn_reports_stderr(tmp_path):
    t, _, ch = turn_for("fail", tmp_path)
    assert await t.run() == 1
    whole = "\n".join(ch.messages)
    assert "-# ❌ exit 1" in whole
    assert "not authenticated with Antigravity" in whole


async def test_cancel_stops_the_turn_promptly(tmp_path):
    t, _, ch = turn_for("slow", tmp_path)
    task = asyncio.create_task(t.run())
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await t.cancel()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 8
    assert t.cancelled
    assert "-# 🛑 cancelled" in "\n".join(ch.messages)


async def test_cancel_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "grandchild.txt"
    t, _, _ = turn_for("spawnchild", tmp_path, FAKE_AGY_MARKER=str(marker))
    task = asyncio.create_task(t.run())
    await asyncio.sleep(0.5)
    await t.cancel()
    await asyncio.wait_for(task, 10)
    await asyncio.sleep(3.5)
    assert not marker.exists(), "a grandchild outlived the cancelled turn"


async def test_wall_timeout_kills_a_wedged_turn(tmp_path):
    t, _, ch = turn_for("slow", tmp_path, wall_timeout=0.5)
    assert await asyncio.wait_for(t.run(), 10) != 0
    assert "-# 🛑 cancelled" in "\n".join(ch.messages)


class ExplodingSink:
    """A sink whose feed raises, standing in for a Discord API failure."""

    def __init__(self, fail_on: int = 1) -> None:
        self.conversation_id = None
        self._seen = 0
        self._fail_on = fail_on
        self.finished = None

    async def start(self) -> None:
        pass

    async def feed(self, piece) -> None:
        self._seen += 1
        if self._seen >= self._fail_on:
            raise RuntimeError("discord exploded")

    async def finish(self, returncode: int, stderr_tail: str = "",
                     cancelled: bool = False) -> None:
        self.finished = (returncode, stderr_tail, cancelled)


async def test_a_sink_failure_does_not_leak_the_process(tmp_path):
    env = {"FAKE_AGY_MODE": "slow", "PATH": os.environ["PATH"]}
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env,
             sink=ExplodingSink())
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(t.run(), 15)
    assert t._proc is not None
    assert t._proc.returncode is not None, "the process was left running"


async def test_a_sink_failure_does_not_leak_tasks(tmp_path):
    before = asyncio.all_tasks()
    env = {"FAKE_AGY_MODE": "slow", "PATH": os.environ["PATH"]}
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env,
             sink=ExplodingSink())
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(t.run(), 15)
    await asyncio.sleep(0)
    leaked = asyncio.all_tasks() - before
    leaked.discard(asyncio.current_task())
    assert not leaked, [t.get_coro() for t in leaked]


async def test_cancel_before_the_process_spawns_still_stops_the_turn(tmp_path):
    t, _, ch = turn_for("slow", tmp_path)
    await t.cancel()
    returncode = await asyncio.wait_for(t.run(), 15)
    assert t.cancelled
    assert returncode != 0, "a cancelled turn must not report success"
    assert "-# 🛑 cancelled" in "\n".join(ch.messages)


async def test_a_line_past_the_default_stream_limit_is_read(tmp_path):
    t, _, ch = turn_for("bigline", tmp_path)
    assert await asyncio.wait_for(t.run(), 15) == 0
    assert "Z" * 100 in "".join(ch.messages)


async def test_a_sink_failure_still_reports_a_footer(tmp_path):
    env = {"FAKE_AGY_MODE": "slow", "PATH": os.environ["PATH"]}
    sink = ExplodingSink()
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env, sink=sink)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(t.run(), 15)
    assert sink.finished is not None, "the user was left with no footer"
    assert sink.finished[0] != 0


async def test_a_sink_failing_in_finish_does_not_mask_the_original_error(
        tmp_path):
    class DoublyExploding(ExplodingSink):
        async def finish(self, *a, **k):
            raise ValueError("finish exploded too")

    env = {"FAKE_AGY_MODE": "slow", "PATH": os.environ["PATH"]}
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env,
             sink=DoublyExploding())
    # The original RuntimeError must surface, not the ValueError from finish.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(t.run(), 15)

from agybot.config import Config
from agybot.runner import preflight, PreflightError, sweep_stray_agy


def cfg_with(tmp_path, agy_bin: str, workspaces: dict) -> Config:
    return Config(
        owner_id="1", members=frozenset(), channels=frozenset(),
        default_workspace=next(iter(workspaces)), workspaces=workspaces,
        agy_bin=agy_bin, token="tok",
    )


def test_preflight_passes_with_a_real_binary_and_directory(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    preflight(cfg_with(tmp_path, sys.executable, {"ws": str(ws)}))


def test_preflight_rejects_a_missing_binary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PreflightError, match="agy"):
        preflight(cfg_with(tmp_path, "/nonexistent/agy", {"ws": str(ws)}))


def test_preflight_rejects_a_non_executable_binary(tmp_path):
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o644)
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PreflightError, match="executable"):
        preflight(cfg_with(tmp_path, str(fake), {"ws": str(ws)}))


def test_preflight_rejects_a_missing_workspace(tmp_path):
    with pytest.raises(PreflightError, match="does not exist"):
        preflight(cfg_with(tmp_path, sys.executable,
                           {"ws": str(tmp_path / "absent")}))


def test_preflight_rejects_a_workspace_that_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(PreflightError, match="not a directory"):
        preflight(cfg_with(tmp_path, sys.executable, {"ws": str(f)}))


def test_sweep_is_harmless_when_nothing_matches():
    assert sweep_stray_agy("/definitely/not/a/real/binary/name") == 0


def test_preflight_rejects_a_directory_as_the_binary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    notabinary = tmp_path / "notabinary"
    notabinary.mkdir()
    with pytest.raises(PreflightError, match="not a file"):
        preflight(cfg_with(tmp_path, str(notabinary), {"ws": str(ws)}))


def test_preflight_rejects_an_unreadable_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ws.chmod(0o000)
    try:
        with pytest.raises(PreflightError, match="not readable"):
            preflight(cfg_with(tmp_path, sys.executable, {"ws": str(ws)}))
    finally:
        ws.chmod(0o755)


def test_sweep_survives_a_missing_pkill(monkeypatch):
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError("pkill")

    monkeypatch.setattr(subprocess, "run", missing)
    assert sweep_stray_agy("/opt/agy") == 0


def test_sweep_reports_nothing_when_pkill_errors(monkeypatch):
    import subprocess

    class Failed:
        returncode = 2
        stderr = b"pkill: bad syntax"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    assert sweep_stray_agy("/opt/agy") == 0


def test_sweep_reports_one_when_something_matched(monkeypatch):
    import subprocess

    class Matched:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Matched())
    assert sweep_stray_agy("/opt/agy") == 1
