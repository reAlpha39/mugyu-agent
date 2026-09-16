# agybot

A Discord bot that turns a thread into an Antigravity conversation. Mention
it with a task, and it runs the `agy` CLI in that thread, streaming the
agent's output back as it works.

## Status

This has never been run against a live Discord server. The bot is verified
by 150 automated tests plus a fake `agy` CLI that replays captured
stream-json fixtures; the manual smoke test in
`docs/superpowers/plans/2026-09-16-discord-agy-bridge.md` has not been
executed. Treat this as code-complete and test-verified, not field-verified.

Two deployment prerequisites are also still unresolved:

1. **A Linux `agy` binary must exist.** The development machine has a
   macOS arm64 build. If Antigravity ships no Linux build, this bot cannot
   be deployed as designed.
2. **`agy` must authenticate on a headless host.** Whether a device-code
   flow exists, or an authenticated configuration directory can simply be
   copied over from an interactive machine, has not been confirmed.

If either of these fails, this bot cannot be deployed as designed.

## How it works

Mentioning the bot creates a thread. Every message in that thread is a turn
in one Antigravity conversation, resumed by conversation id. Agent output
streams into a live-edited message, split at 1800 characters with code
fences carried across the split.

```
@agybot [bliss-app] fix the token expiry check
```

The `[bliss-app]` prefix selects a workspace by name. Omit it to use the
default. Only names listed in `config.toml` are accepted; a filesystem path
in a message is not a location, just an unknown name.

## Access tiers

| Tier | Flags | Can |
|---|---|---|
| Owner | `--dangerously-skip-permissions` | Everything, including writes and shell |
| Member | `--mode plan --sandbox` | Read and plan |
| Everyone else | none | Nothing; the bot reacts 🚫 and ignores them |

Tier follows the author of each message, not the thread's creator.

`--mode plan` is a model-level guardrail, not a sandbox. Members are treated
as semi-trusted colleagues. The real containment is the `agy` service
account, which has no sudo rights and can write only to the workspace tree.

## Configuration

Workspace paths in `config.toml` must be absolute. The bot validates this at
load time and refuses to start if any workspace path is relative. At
startup it also checks, for every configured workspace, that the path
exists, is a directory, and is readable by the service user; any failure
there aborts startup too, before the bot ever touches Discord. See the
comments in `config.toml.example` for the full set of fields.

## Operating

- Three turns run at once; one slot is always held for the owner.
- Cancel a turn with ❌ on the bot's message, or by posting `!stop` in the
  thread. Members may cancel only their own turns; the owner may cancel any.
  Cancellation works even while a turn is still queued for a slot, not only
  while it is running.
- Turns are capped at 15 minutes, with a hard kill at 16.

## Known limitations

**Tool failures are not flagged in the output.** The schema probe that
pinned `agy`'s stream-json event shape never captured a failing tool call,
so the adapter deliberately renders nothing for any tool state other than
`ACTIVE`/`DONE` rather than guessing that an unknown state means failure. A
failing tool call therefore shows as an ordinary `-# 🔧` line, the same as a
successful one; the agent's own text and the turn's exit code are what
report trouble. See `docs/agy-stream-schema.md` for the observed schema and
the open question on failure signalling.

## Setup

```bash
git clone <repo> /srv/agy/src && cd /srv/agy/src
python3 -m venv /srv/agy/venv
/srv/agy/venv/bin/pip install -e .
cp config.toml.example /srv/agy/config.toml   # then edit it
echo 'DISCORD_TOKEN=...' > /etc/agy-bot.env
chmod 600 /etc/agy-bot.env
install -m644 deploy/agy-bot.service /etc/systemd/system/
systemctl enable --now agy-bot
```

The Discord application needs the **Message Content** privileged intent, and
the bot needs Send Messages, Create Public Threads, Send Messages in
Threads, Add Reactions, and Read Message History in its channels.

The host needs `pkill` (from `procps`) on `PATH`. The bot shells out to it
at startup to sweep away any `agy` process left running by a previous,
uncleanly stopped instance. On a minimal image without `procps` the bot
logs a warning and keeps starting anyway, which means any orphaned `agy`
process from a previous run is left alive. Install `procps` before relying
on the sweep.

**Do not use the `agy` service account for interactive `agy` sessions.**
The startup sweep kills every `agy -p` process owned by that user, so an
interactive session left running under the same account will be killed the
next time the bot restarts.

## Security notes

- Agent output is relayed verbatim to Discord. An agent that reads a secret
  file publishes that secret to the channel. **Keep secrets out of workspace
  directories.**
- `agy` runs with an allowlisted environment that excludes `DISCORD_TOKEN`.
- Do not use the `agy` service account for interactive `agy` sessions (see
  Setup above): the startup sweep kills `agy -p` processes owned by that
  user.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
