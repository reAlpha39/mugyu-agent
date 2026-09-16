"""Process construction and lifecycle for the agy CLI."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
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

    def feed(self, ev: object) -> list[Piece]:
        # Runs per line of a live subprocess stream, so it must never raise:
        # an exception here aborts the user's turn mid-answer. Every field is
        # type-checked rather than merely presence-checked.
        if not isinstance(ev, dict):
            return []

        kind = ev.get("event")

        if kind == "init":
            cid = ev.get("conversation_id")
            if cid and not self._conversation_sent:
                self._conversation_sent = True
                return [Meta(str(cid))]
            return []

        if kind == "step_update":
            su = ev.get("step_update")
            return self._step(su) if isinstance(su, dict) else []

        return []

    def _step(self, su: dict) -> list[Piece]:
        step_type = su.get("step_type")

        if step_type == "agent_response":
            # text_delta is incremental and frequently absent; emitting each
            # delta in arrival order reconstructs the response exactly.
            delta = su.get("text_delta")
            return [Text(delta)] if isinstance(delta, str) and delta else []

        if step_type == "tool":
            state = su.get("state")
            if state == "ACTIVE":
                name = str(su.get("tool_name") or "tool")
                info = su.get("tool_info")
                params = info.get("parameters") if isinstance(info, dict) else None
                detail = _detail(params if isinstance(params, dict) else {})
                return [Tool(name, detail, None)]
            # DONE renders nothing, because the call was announced when it
            # started. Any other state renders nothing either: tool failure
            # signalling was never observed, and docs/agy-stream-schema.md
            # question 4 says to treat unknown states as unhandled rather than
            # assume they mean failure. When a real failing tool is captured,
            # add the branch then.
            return []

        return []


def _detail(parameters: dict) -> str:
    for key in TOOL_DETAIL_KEYS:
        if parameters.get(key):
            return str(parameters[key])[:80]
    return ""


class Slots:
    """Bounded concurrency with a slot held back for the owner.

    The owner is admitted while fewer than max_total turns run; a member
    while fewer than (max_total - reserved_owner) run. No priority queue is
    needed: the differing thresholds mean a woken member simply fails its
    own check and waits again, whatever the wake order.
    """

    def __init__(self, max_total: int = 3, reserved_owner: int = 1) -> None:
        self._max_total = max_total
        self._member_cap = max_total - reserved_owner
        self._running = 0
        self._waiting = {"owner": 0, "member": 0}
        self._cond = asyncio.Condition()

    @property
    def running(self) -> int:
        return self._running

    def _cap(self, tier: str) -> int:
        return self._max_total if tier == "owner" else self._member_cap

    def ahead(self, tier: str) -> int:
        """Queued requests that will be considered before a new one."""
        if tier == "owner":
            return self._waiting["owner"]
        return self._waiting["owner"] + self._waiting["member"]

    def would_block(self, tier: str) -> bool:
        """True if acquire() would wait. Callers use this to warn the user."""
        return self._running >= self._cap(tier)

    async def acquire(self, tier: str) -> None:
        async with self._cond:
            if self._running < self._cap(tier):
                self._running += 1
                return
            self._waiting[tier] += 1
            try:
                await self._cond.wait_for(
                    lambda: self._running < self._cap(tier))
                self._running += 1
            finally:
                self._waiting[tier] -= 1

    def release(self) -> None:
        if self._running <= 0:
            raise RuntimeError("release() called with no turn running")
        self._running -= 1

        async def wake() -> None:
            async with self._cond:
                self._cond.notify_all()

        asyncio.get_running_loop().create_task(wake())


WALL_TIMEOUT = 960.0          # 16 minutes, backstop for --print-timeout 15m
KILL_GRACE = 5.0
STDERR_KEEP = 4096
# asyncio's default 64 KiB readline cap is too small for a result event
# carrying a long answer or a large tool output.
STREAM_LIMIT = 4 * 1024 * 1024


class Turn:
    """One agy invocation, streamed into a sink."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str],
                 sink, adapter: "EventAdapter | None" = None,
                 wall_timeout: float = WALL_TIMEOUT) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._sink = sink
        self._adapter = adapter or EventAdapter()
        self._wall_timeout = wall_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self.cancelled = False

    async def run(self) -> int:
        await self._sink.start()
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
            limit=STREAM_LIMIT,
        )

        # A cancel() that arrived while the sink was starting had no process
        # to signal. Honour it now rather than running the turn to completion
        # and reporting it to the user as cancelled.
        if self.cancelled:
            await self._kill()

        stderr_task = asyncio.create_task(self._proc.stderr.read())
        watchdog = asyncio.create_task(self._watchdog())
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue            # non-JSON noise on stdout is ignored
                for piece in self._adapter.feed(ev):
                    await self._sink.feed(piece)

            returncode = await self._proc.wait()
            stderr = (await stderr_task).decode("utf-8", "replace")
        except BaseException as exc:
            # Anything escaping this loop — a Discord failure raised by the
            # sink, a stream overrun, cancellation of this coroutine — must
            # not leave the process group running. start_new_session means
            # nothing else will ever reap it.
            await self._kill()
            # Leave the user a footer rather than a message frozen at the
            # placeholder. A sink that is itself the cause may raise again;
            # that must never mask the original failure.
            with contextlib.suppress(Exception):
                await self._sink.finish(-1, f"{type(exc).__name__}: {exc}",
                                        cancelled=self.cancelled)
            raise
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

        await self._sink.finish(returncode, stderr[-STDERR_KEEP:],
                                cancelled=self.cancelled)
        return returncode

    async def cancel(self) -> None:
        self.cancelled = True
        await self._kill()

    async def _watchdog(self) -> None:
        await asyncio.sleep(self._wall_timeout)
        self.cancelled = True
        await self._kill()

    async def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return

        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), KILL_GRACE)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
