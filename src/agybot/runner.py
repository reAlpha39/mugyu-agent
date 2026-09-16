"""Process construction and lifecycle for the agy CLI."""
from __future__ import annotations

from typing import Mapping

# An allowlist, not a denylist. A new secret added to the unit file must not
# silently become readable by the agent.
ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TZ",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
})


def build_argv(agy_bin: str, prompt: str, workspace: str,
               conversation_id: str | None, tier: str,
               timeout: str = "15m") -> list[str]:
    if tier not in ("owner", "member"):
        raise ValueError(f"cannot build a command for tier {tier!r}")

    argv = [
        agy_bin, "-p", prompt,
        "--output-format", "stream-json",
        "--add-dir", workspace,
        "--print-timeout", timeout,
    ]
    if conversation_id:
        argv += ["--conversation", conversation_id]
    if tier == "owner":
        argv += ["--dangerously-skip-permissions"]
    else:
        argv += ["--mode", "plan", "--sandbox"]
    return argv


def minimal_env(base: Mapping[str, str]) -> dict[str, str]:
    """Strip the child environment down to the allowlist."""
    return {k: v for k, v in base.items() if k in ENV_ALLOWLIST}
