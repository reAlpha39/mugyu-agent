from agybot.runner import build_argv, minimal_env


def argv(**kw):
    base = dict(agy_bin="agy", prompt="do the thing",
                workspace="/srv/agy/app", conversation_id=None, tier="owner")
    base.update(kw)
    return build_argv(**base)


def test_prompt_is_a_single_argument():
    a = argv(prompt="rm -rf /; echo `whoami`")
    assert "rm -rf /; echo `whoami`" in a


def test_prompt_is_not_escaped_or_quoted():
    a = argv(prompt='say "hi"')
    assert 'say "hi"' in a
    assert '\\"' not in " ".join(a)


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
