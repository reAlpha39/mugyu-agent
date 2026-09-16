import pytest
from pathlib import Path
from agybot.config import (
    load_config, tier_of, resolve_workspace, UnknownWorkspace,
)

CONFIG_TEXT = """
owner_id = "1"
members = ["2", "3"]
channels = ["100"]
default_workspace = "scratch"

[workspaces]
scratch = "/srv/agy/scratch"
app = "/srv/agy/app"
"""



def with_tier(tier: str) -> str:
    """CONFIG_TEXT with default_tier set.

    Inserted before [workspaces] rather than appended: a key written after a
    table header belongs to that table, so appending would define
    workspaces.default_tier instead.
    """
    return CONFIG_TEXT.replace("[workspaces]",
                               f'default_tier = "{tier}"\n\n[workspaces]')

@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT)
    return load_config(p, {"DISCORD_TOKEN": "tok"})


def test_loads_token_from_environment_not_file(cfg):
    assert cfg.token == "tok"


def test_missing_token_is_fatal(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT)
    with pytest.raises(ValueError, match="DISCORD_TOKEN"):
        load_config(p, {})


def test_agy_bin_defaults_to_path_lookup(cfg):
    assert cfg.agy_bin == "agy"


def test_agy_bin_overridable_by_environment(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT)
    c = load_config(p, {"DISCORD_TOKEN": "tok", "AGY_BIN": "/opt/agy"})
    assert c.agy_bin == "/opt/agy"


def test_owner_is_owner(cfg):
    assert tier_of(cfg, "1") == "owner"


def test_member_is_member(cfg):
    assert tier_of(cfg, "2") == "member"


def test_unknown_user_is_stranger(cfg):
    assert tier_of(cfg, "999") == "stranger"


def test_owner_listed_in_members_is_still_owner(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT.replace('members = ["2", "3"]',
                                     'members = ["1", "2"]'))
    c = load_config(p, {"DISCORD_TOKEN": "tok"})
    assert tier_of(c, "1") == "owner"


def test_named_workspace_resolves(cfg):
    assert resolve_workspace(cfg, "app") == "/srv/agy/app"


def test_absent_name_uses_default(cfg):
    assert resolve_workspace(cfg, None) == "/srv/agy/scratch"


def test_unlisted_name_is_refused(cfg):
    with pytest.raises(UnknownWorkspace) as e:
        resolve_workspace(cfg, "nope")
    assert sorted(e.value.allowed) == ["app", "scratch"]


def test_raw_path_is_refused(cfg):
    with pytest.raises(UnknownWorkspace):
        resolve_workspace(cfg, "/etc")


def test_traversal_attempt_is_refused(cfg):
    with pytest.raises(UnknownWorkspace):
        resolve_workspace(cfg, "../../etc")


def test_default_workspace_must_exist_in_map(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT.replace('default_workspace = "scratch"',
                                     'default_workspace = "ghost"'))
    with pytest.raises(ValueError, match="default_workspace"):
        load_config(p, {"DISCORD_TOKEN": "tok"})


def test_token_is_absent_from_the_repr(cfg):
    assert "tok" not in repr(cfg)


def test_workspace_allowlist_cannot_be_mutated(cfg):
    with pytest.raises(TypeError):
        cfg.workspaces["evil"] = "/etc"


def test_relative_workspace_path_is_refused(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT.replace('scratch = "/srv/agy/scratch"',
                                     'scratch = "relative/path"'))
    with pytest.raises(ValueError, match="absolute"):
        load_config(p, {"DISCORD_TOKEN": "tok"})


def test_default_tier_is_stranger_when_absent(cfg):
    assert cfg.default_tier == "stranger"
    assert tier_of(cfg, "999") == "stranger"


def test_default_tier_member_opens_the_bot_to_everyone(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(with_tier("member"))
    c = load_config(p, {"DISCORD_TOKEN": "tok"})
    assert tier_of(c, "999") == "member"
    # Named tiers still win over the default.
    assert tier_of(c, "1") == "owner"
    assert tier_of(c, "2") == "member"


def test_default_tier_owner_is_refused(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(with_tier("owner"))
    with pytest.raises(ValueError, match="default_tier"):
        load_config(p, {"DISCORD_TOKEN": "tok"})


def test_default_tier_nonsense_is_refused(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(with_tier("admin"))
    with pytest.raises(ValueError, match="default_tier"):
        load_config(p, {"DISCORD_TOKEN": "tok"})
