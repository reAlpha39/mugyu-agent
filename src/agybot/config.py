"""Configuration loading and access control lookups."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class UnknownWorkspace(Exception):
    """Raised when a chat message names a workspace that is not allowlisted."""

    def __init__(self, name: str, allowed: list[str]) -> None:
        super().__init__(f"unknown workspace {name!r}")
        self.name = name
        self.allowed = allowed


@dataclass(frozen=True)
class Config:
    owner_id: str
    members: frozenset[str]
    channels: frozenset[str]
    default_workspace: str
    workspaces: dict[str, str]
    agy_bin: str
    token: str


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    token = env.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set in the environment")

    workspaces = {str(k): str(v) for k, v in raw.get("workspaces", {}).items()}
    if not workspaces:
        raise ValueError("config defines no [workspaces]")

    default_workspace = str(raw["default_workspace"])
    if default_workspace not in workspaces:
        raise ValueError(
            f"default_workspace {default_workspace!r} is not a key in [workspaces]"
        )

    return Config(
        owner_id=str(raw["owner_id"]),
        members=frozenset(str(m) for m in raw.get("members", [])),
        channels=frozenset(str(c) for c in raw.get("channels", [])),
        default_workspace=default_workspace,
        workspaces=workspaces,
        agy_bin=env.get("AGY_BIN", "agy"),
        token=token,
    )


def tier_of(cfg: Config, user_id: str) -> str:
    """Return "owner", "member", or "stranger" for a Discord user id."""
    if user_id == cfg.owner_id:
        return "owner"
    if user_id in cfg.members:
        return "member"
    return "stranger"


def resolve_workspace(cfg: Config, name: str | None) -> str:
    """Map an allowlisted workspace name to its absolute path.

    Only names that are keys of [workspaces] are accepted. A path supplied
    from chat is never treated as a location.
    """
    key = cfg.default_workspace if name is None else name
    try:
        return cfg.workspaces[key]
    except KeyError:
        raise UnknownWorkspace(key, sorted(cfg.workspaces)) from None
