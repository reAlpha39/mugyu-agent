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
from agybot.runner import (
    EventAdapter, Slots, Turn, build_argv, minimal_env, preflight,
    sweep_stray_agy,
)
from agybot.state import Store

log = logging.getLogger("agybot")

MENTION_RE = re.compile(r"<@!?(\d+)>")
WORKSPACE_RE = re.compile(r"^\[([A-Za-z0-9._-]+)\]\s*")
THREAD_NAME_MAX = 60
STOP_COMMAND = "!stop"
# Progress marks on the message that triggered a turn: 👀 the moment it is
# picked up, swapped for the outcome when the turn ends. ❌ is deliberately
# not used for failure — it is the cancel gesture, and seeing it appear on
# your own message would read as an instruction rather than a result.
# Where a conversation lives. A guild thread opened per task, or the owner's
# DM channel, which has no threads and is itself the conversation. Both carry
# the id, send() and typing() that _run needs.
Conversation = discord.Thread | discord.DMChannel

READ_EMOJI = "👀"
DONE_EMOJI = "✅"
FAIL_EMOJI = "⚠️"
RESET_COMMAND = "!reset"
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
            guild_reactions=True, dm_messages=True, dm_reactions=True,
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
        # Report whether each configured channel is actually reachable. A
        # mistyped or unshared channel id otherwise shows up only as the bot
        # silently ignoring every mention.
        for cid in sorted(self.cfg.channels):
            channel = self.get_channel(int(cid))
            if channel is None:
                log.warning("configured channel %s is NOT VISIBLE to this bot "
                            "— check the id, and that the bot is in that "
                            "server with access to the channel", cid)
            else:
                log.info("configured channel %s = #%s in %s", cid,
                         getattr(channel, "name", "?"),
                         getattr(getattr(channel, "guild", None), "name", "?"))
                me = getattr(channel.guild, "me", None)
                if me is not None:
                    p = channel.permissions_for(me)
                    log.info(
                        "  permissions: view=%s send=%s create_threads=%s "
                        "send_in_threads=%s react=%s history=%s",
                        p.view_channel, p.send_messages,
                        p.create_public_threads, p.send_messages_in_threads,
                        p.add_reactions, p.read_message_history)
                    if not p.view_channel:
                        log.warning("  -> without View Channel this bot never "
                                    "receives messages from this channel")

    async def on_message(self, message: discord.Message) -> None:
        # Debug level: this fires for every message the bot can see, its own
        # included. Raise the level to DEBUG when a mention appears to go
        # unnoticed — it distinguishes "never arrived" from "arrived and was
        # ignored", which is otherwise invisible.
        log.debug("message chan=%s author=%s bot=%s thread=%s content=%r",
                  message.channel.id, message.author.id, message.author.bot,
                  isinstance(message.channel, discord.Thread),
                  message.content[:60])
        if message.author.bot or self.user is None:
            return

        if isinstance(message.channel, discord.DMChannel):
            await self._on_direct_message(message)
            return

        if isinstance(message.channel, discord.Thread):
            await self._on_thread_message(message)
            return

        if self.user.mentioned_in(message):
            log.info("mentioned by %s in channel %s",
                     message.author.id, message.channel.id)
            await self._on_new_task(message)

    async def _on_direct_message(self, message: discord.Message) -> None:
        """Owner-only private channel, with the DM itself as the conversation.

        A DM has no threads, so there is nothing to open per task. The channel
        is the conversation instead: every message continues it, and !reset
        starts a fresh one. No mention is needed — in a private channel with
        one bot, addressing it is unambiguous.
        """
        author_id = str(message.author.id)
        if tier_of(self.cfg, author_id) != "owner":
            # Silently ignored rather than refused. A stranger DMing the bot
            # learns nothing about whether it exists or who may use it.
            log.info("ignoring DM from %s: not the owner", author_id)
            return

        content = message.content.strip()
        if not content:
            return

        row = self.store.get_thread(str(message.channel.id))

        if content == STOP_COMMAND:
            await self._cancel(message.channel, author_id)
            return
        if content == RESET_COMMAND:
            if row is None:
                await message.channel.send("Nothing to reset yet.")
                return
            await self._reset(message.channel, row, author_id)
            return

        if row is None:
            parsed = parse_mention(content, str(self.user.id))
            if not parsed.prompt:
                return
            try:
                workspace = resolve_workspace(self.cfg, parsed.workspace)
            except UnknownWorkspace as exc:
                await message.channel.send(
                    f"Unknown workspace `{exc.name}`. "
                    f"Allowed: {', '.join(f'`{a}`' for a in exc.allowed)}"
                )
                return
            self.store.create_thread(str(message.channel.id), workspace,
                                     author_id)
            content = parsed.prompt

        log.info("accepted owner DM turn: prompt=%r", content[:80])
        await self._run(message.channel, content, "owner", author_id, message)

    async def _on_new_task(self, message: discord.Message) -> None:
        if str(message.channel.id) not in self.cfg.channels:
            log.info("ignoring mention in channel %s: not in the allowlist %s",
                     message.channel.id, sorted(self.cfg.channels))
            return

        tier = tier_of(self.cfg, str(message.author.id))
        if tier == "stranger":
            log.info("refusing %s: not in the allowlist", message.author.id)
            await message.add_reaction("🚫")
            return

        parsed = parse_mention(message.content, str(self.user.id))
        log.info("accepted %s turn from %s: workspace=%s prompt=%r",
                 tier, message.author.id, parsed.workspace, parsed.prompt[:80])
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
        await self._run(thread, parsed.prompt, tier, str(message.author.id),
                        message)

    async def _on_thread_message(self, message: discord.Message) -> None:
        thread = message.channel
        row = self.store.get_thread(str(thread.id))
        if row is None:
            return

        if message.content.strip() == STOP_COMMAND:
            await self._cancel(thread, str(message.author.id))
            return

        if message.content.strip() == RESET_COMMAND:
            await self._reset(thread, row, str(message.author.id))
            return

        tier = tier_of(self.cfg, str(message.author.id))
        if tier == "stranger":
            await message.add_reaction("🚫")
            return

        content = message.content.strip()
        if not content:
            # An attachment-only or sticker-only post has no prompt to run.
            # No reply: a reply on every image post would be noise.
            return

        await self._run(thread, content, tier, str(message.author.id),
                        message)

    async def _mark(self, message: discord.Message, emoji: str) -> None:
        """Replace the read mark with an outcome. Never raises."""
        with contextlib.suppress(Exception):
            await message.remove_reaction(READ_EMOJI, self.user)
        with contextlib.suppress(Exception):
            await message.add_reaction(emoji)

    async def _run(self, thread: Conversation, prompt: str, tier: str,
                   author_id: str, message: discord.Message) -> None:
        # Marked before the per-thread lock, so a follow-up posted while an
        # earlier turn is still running is visibly acknowledged rather than
        # sitting unanswered until that turn finishes.
        with contextlib.suppress(Exception):
            await message.add_reaction(READ_EMOJI)

        async with self.lock_for(thread.id):
            row = self.store.get_thread(str(thread.id))
            if row is None:
                await self._mark(message, FAIL_EMOJI)
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
                await self._mark(message, FAIL_EMOJI)
                raise

            try:
                if notice is not None:
                    # Losing the notice must not lose the request.
                    with contextlib.suppress(Exception):
                        await notice.delete()

                if turn.cancelled:
                    await thread.send("🛑 Cancelled before it started.")
                    await self._mark(message, FAIL_EMOJI)
                    return

                # Discord's own typing indicator, refreshed by discord.py for
                # as long as the turn runs. Editing the live message does not
                # clear it, so it persists across the whole turn.
                outcome = FAIL_EMOJI
                try:
                    async with thread.typing():
                        returncode = await turn.run()
                    if returncode == 0 and not turn.cancelled:
                        outcome = DONE_EMOJI
                finally:
                    # Cancelled and failed turns share ⚠️: both mean "this did
                    # not produce what you asked for", and the footer says
                    # which.
                    await self._mark(message, outcome)
            finally:
                try:
                    # Persist even when the turn failed: agy created the
                    # conversation regardless, and losing the id orphans it.
                    if sink.conversation_id and not row.conversation_id:
                        self.store.set_conversation(str(thread.id),
                                                    sink.conversation_id)
                    self.store.touch(str(thread.id))
                except Exception:
                    # A sqlite failure must never cost the slot — leaking one
                    # permanently reduces capacity with no way to recover.
                    log.exception("failed to persist state for thread %s",
                                  thread.id)
                finally:
                    self.slots.release()
                    self.turns.pop(thread.id, None)
                    self.owners.pop(thread.id, None)

    async def _cancel(self, thread: Conversation, user_id: str) -> None:
        turn = self.turns.get(thread.id)
        if turn is None:
            return
        is_owner = tier_of(self.cfg, user_id) == "owner"
        if not is_owner and self.owners.get(thread.id) != user_id:
            await thread.send("That is not your turn to cancel.")
            return
        await turn.cancel()

    async def _reset(self, thread: Conversation, row,
                     user_id: str) -> None:
        """Forget this thread's agy conversation without closing the thread.

        Recovery path for a conversation that no longer exists on disk, which
        would otherwise make every later message in the thread fail against a
        dead id.
        """
        if tier_of(self.cfg, user_id) != "owner" and row.created_by != user_id:
            await thread.send("That is not your thread to reset.")
            return
        self.store.set_conversation(str(thread.id), "")
        await thread.send("🔄 Next message starts a fresh conversation.")

    async def on_raw_reaction_add(
            self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != CANCEL_EMOJI or payload.user_id == (
                self.user.id if self.user else None):
            return
        channel = self.get_channel(payload.channel_id)
        if isinstance(channel, (discord.Thread, discord.DMChannel)):
            await self._cancel(channel, str(payload.user_id))


def main() -> None:
    # AGY_LOG_LEVEL=DEBUG turns on per-message receipt logging, which is what
    # distinguishes "the mention never arrived" from "it arrived and was
    # ignored" — the single most useful thing to know when the bot appears
    # silent. discord.py's own loggers stay at INFO, since their DEBUG output
    # is gateway noise that buries ours.
    level = os.environ.get("AGY_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO))
    logging.getLogger("discord").setLevel(logging.INFO)
    cfg = load_config(Path(os.environ.get("AGY_CONFIG", "config.toml")),
                      os.environ)
    preflight(cfg)
    swept = sweep_stray_agy(cfg.agy_bin)
    if swept:
        log.warning("killed stray agy processes left by a previous run")
    store = Store(os.environ.get("AGY_DB", "state.db"))
    AgyBot(cfg, store).run(cfg.token)


if __name__ == "__main__":
    main()
