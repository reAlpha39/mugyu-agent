"""Process construction and lifecycle for the agy CLI."""
from __future__ import annotations

from typing import Mapping

from agybot.render import Meta, Piece, Text, Tool

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


# Parameter keys checked in order when summarising a tool call for display.
# agy's built-in tools use PascalCase; the lowercase spellings cover MCP and
# plugin tools that follow other conventions.
TOOL_DETAIL_KEYS = (
    "AbsolutePath", "Path", "path", "file_path",
    "Command", "command", "Query", "query", "url",
)


class EventAdapter:
    """Translates agy stream-json events into render pieces.

    The only component that knows the CLI's event schema, which is pinned by
    observation in docs/agy-stream-schema.md. Everything downstream sees
    Text, Tool, and Meta and nothing else.
    """

    def __init__(self) -> None:
        self._conversation_sent = False

    def feed(self, ev: dict) -> list[Piece]:
        kind = ev.get("event")

        if kind == "init":
            cid = ev.get("conversation_id")
            if cid and not self._conversation_sent:
                self._conversation_sent = True
                return [Meta(str(cid))]
            return []

        if kind == "step_update":
            return self._step(ev.get("step_update") or {})

        return []

    def _step(self, su: dict) -> list[Piece]:
        step_type = su.get("step_type")

        if step_type == "agent_response":
            # text_delta is incremental and frequently absent; emitting each
            # delta in arrival order reconstructs the response exactly.
            delta = su.get("text_delta") or ""
            return [Text(delta)] if delta else []

        if step_type == "tool":
            state = su.get("state")
            name = str(su.get("tool_name") or "tool")
            info = su.get("tool_info") or {}
            detail = _detail(info.get("parameters") or {})
            if state == "ACTIVE":
                return [Tool(name, detail, None)]
            if state == "DONE":
                return []           # already announced when it started
            # Tool failure was never observed during the schema probe, so any
            # state that is neither ACTIVE nor DONE is surfaced rather than
            # silently dropped. See docs/agy-stream-schema.md, question 4.
            return [Tool(name, str(state).lower(), False)]

        return []


def _detail(parameters: dict) -> str:
    for key in TOOL_DETAIL_KEYS:
        if parameters.get(key):
            return str(parameters[key])[:80]
    return ""
