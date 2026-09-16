"""Discord gateway wiring."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import discord

from agybot.config import (
    Config, UnknownWorkspace, load_config, resolve_workspace, tier_of,
)
from agybot.render import Sink
from agybot.runner import EventAdapter, Slots, Turn, build_argv, minimal_env
from agybot.state import Store

log = logging.getLogger("agybot")

MENTION_RE = re.compile(r"<@!?(\d+)>")
WORKSPACE_RE = re.compile(r"^\[([A-Za-z0-9._-]+)\]\s*")
THREAD_NAME_MAX = 60
STOP_COMMAND = "!stop"
CANCEL_EMOJI = "❌"


@dataclass(frozen=True)
class Mention:
    workspace: str | None
    prompt: str


def parse_mention(content: str, bot_id: str) -> Mention:
    body = MENTION_RE.sub(
        lambda m: "" if m.group(1) == bot_id else m.group(0), content
    ).strip()
    match = WORKSPACE_RE.match(body)
    if match:
        return Mention(match.group(1), body[match.end():].strip())
    return Mention(None, body)


def thread_name(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    return cleaned[:THREAD_NAME_MAX] if cleaned else "agy task"


class AgyBot(discord.Client):
    def __init__(self, cfg: Config, store: Store) -> None:
        super().__init__(intents=discord.Intents(
            guilds=True, guild_messages=True, message_content=True,
            guild_reactions=True,
        ))
        self.cfg = cfg
        self.store = store
        self.slots = Slots()
        self.turns: dict[int, Turn] = {}          # thread id -> running turn
        self.owners: dict[int, str] = {}          # thread id -> turn's author
        self.locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, thread_id: int) -> asyncio.Lock:
        return self.locks.setdefault(thread_id, asyncio.Lock())

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return

        if isinstance(message.channel, discord.Thread):
            await self._on_thread_message(message)
            return

        if self.user.mentioned_in(message):
            await self._on_new_task(message)

    async def _on_new_task(self, message: discord.Message) -> None:
        if str(message.channel.id) not in self.cfg.channels:
            return

        tier = tier_of(self.cfg, str(message.author.id))
        if tier == "stranger":
            await message.add_reaction("🚫")
            return

        parsed = parse_mention(message.content, str(self.user.id))
        if not parsed.prompt:
            await message.reply("Give me something to do.")
            return

        try:
            workspace = resolve_workspace(self.cfg, parsed.workspace)
        except UnknownWorkspace as exc:
            await message.reply(
                f"Unknown workspace `{exc.name}`. "
                f"Allowed: {', '.join(f'`{a}`' for a in exc.allowed)}"
            )
            return

        thread = await message.create_thread(name=thread_name(parsed.prompt))
        self.store.create_thread(str(thread.id), workspace,
                                 str(message.author.id))
        await self._run(thread, parsed.prompt, tier, str(message.author.id))

    async def _on_thread_message(self, message: discord.Message) -> None:
        thread = message.channel
        row = self.store.get_thread(str(thread.id))
        if row is None:
            return

        if message.content.strip() == STOP_COMMAND:
            await self._cancel(thread, str(message.author.id))
            return

        tier = tier_of(self.cfg, str(message.author.id))
        if tier == "stranger":
            await message.add_reaction("🚫")
            return

        await self._run(thread, message.content.strip(), tier,
                        str(message.author.id))

    async def _run(self, thread: discord.Thread, prompt: str,
                   tier: str, author_id: str) -> None:
        async with self.lock_for(thread.id):
            row = self.store.get_thread(str(thread.id))
            if row is None:
                return

            async def send(content: str):
                return await thread.send(content)

            async def edit(handle, content: str) -> None:
                await handle.edit(content=content)

            sink = Sink(send, edit)
            turn = Turn(
                argv=build_argv(self.cfg.agy_bin, prompt, row.workspace,
                                row.conversation_id, tier),
                cwd=row.workspace,
                env=minimal_env(os.environ),
                sink=sink,
                adapter=EventAdapter(),
            )
            # Registered before queueing, so a cancel arriving while this
            # request waits for a slot is honoured instead of silently
            # ignored. Turn.cancel() on an unspawned turn just sets the flag.
            self.turns[thread.id] = turn
            self.owners[thread.id] = author_id

            notice = None
            if self.slots.would_block(tier):
                notice = await thread.send(
                    f"⏳ queued · {self.slots.ahead(tier)} ahead")

            try:
                await self.slots.acquire(tier)
            except BaseException:
                self.turns.pop(thread.id, None)
                self.owners.pop(thread.id, None)
                raise

            try:
                if notice is not None:
                    # Losing the notice must not lose the request.
                    with contextlib.suppress(Exception):
                        await notice.delete()

                if turn.cancelled:
                    await thread.send("🛑 Cancelled before it started.")
                    return

                await turn.run()

                if sink.conversation_id and not row.conversation_id:
                    self.store.set_conversation(str(thread.id),
                                                sink.conversation_id)
                self.store.touch(str(thread.id))
            finally:
                self.slots.release()
                self.turns.pop(thread.id, None)
                self.owners.pop(thread.id, None)

    async def _cancel(self, thread: discord.Thread, user_id: str) -> None:
        turn = self.turns.get(thread.id)
        if turn is None:
            return
        is_owner = tier_of(self.cfg, user_id) == "owner"
        if not is_owner and self.owners.get(thread.id) != user_id:
            await thread.send("That is not your turn to cancel.")
            return
        await turn.cancel()

    async def on_raw_reaction_add(
            self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != CANCEL_EMOJI or payload.user_id == (
                self.user.id if self.user else None):
            return
        channel = self.get_channel(payload.channel_id)
        if isinstance(channel, discord.Thread):
            await self._cancel(channel, str(payload.user_id))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(Path(os.environ.get("AGY_CONFIG", "config.toml")),
                      os.environ)
    store = Store(os.environ.get("AGY_DB", "state.db"))
    AgyBot(cfg, store).run(cfg.token)


if __name__ == "__main__":
    main()
