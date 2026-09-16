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

### Workspaces outside /srv/agy

The unit sets `ProtectHome=read-only` and `ReadWritePaths=/srv/agy`, so the
agent can write only inside that tree. Nothing in the bot enforces this —
`preflight` accepts any absolute, existing, readable directory — so a
workspace at, say, `/home/dev/project` passes startup cleanly and then fails
every write with a permission error that looks like a bug in the agent.

If you configure a workspace outside `/srv/agy`, add it to `ReadWritePaths`
in the unit file and run `systemctl daemon-reload`. Keeping every workspace
under `/srv/agy` avoids the problem, and is the layout the unit assumes.

## Operating

- Three turns run at once; one slot is always held for the owner.
- Cancel a turn with ❌ on the bot's message, or by posting `!stop` in the
  thread. Members may cancel only their own turns; the owner may cancel any.
  Cancellation works even while a turn is still queued for a slot, not only
  while it is running.
- Turns are capped at 15 minutes, with a hard kill at 16.

## Known limitations

**Tool activity is not shown.** Tool calls are counted, not displayed: the
footer reports how many ran, but no individual call produces a line in the
channel. A failing tool is therefore invisible as well — the agent's own
text and the turn's exit code are what report trouble.

Separately, the schema probe that pinned `agy`'s stream-json event shape
never captured a failing tool call, so the adapter deliberately treats any
tool state other than `ACTIVE`/`DONE` as unhandled rather than guessing that
an unknown state means failure. See `docs/agy-stream-schema.md` for the
observed schema and the open question on failure signalling.

## Setup

Prepare the host. The service runs as a dedicated unprivileged account, which
is the real containment boundary for owner-tier turns — see Security notes.

```bash
sudo useradd --system --home-dir /srv/agy --no-create-home agy
sudo mkdir -p /srv/agy
sudo chown -R agy:agy /srv/agy
```

Install as that account, so nothing in the tree ends up root-owned.

```bash
sudo -u agy git clone <repo> /srv/agy/src
sudo -u agy python3 -m venv /srv/agy/venv
sudo -u agy /srv/agy/venv/bin/pip install -e /srv/agy/src
```

Create each workspace you listed in `config.toml`, owned by the same account.

```bash
sudo -u agy mkdir -p /srv/agy/scratch
```

Configure. The token lives in the environment file, never in `config.toml`.

```bash
sudo -u agy cp /srv/agy/src/config.toml.example /srv/agy/config.toml   # then edit
printf 'DISCORD_TOKEN=...\n' | sudo tee /etc/agy-bot.env > /dev/null
sudo chmod 600 /etc/agy-bot.env
```

Install the unit and start it.

```bash
sudo install -m644 /srv/agy/src/deploy/agy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agy-bot
```

`pkill` must be present — the bot shells out to it at startup to clear any
`agy` processes orphaned by an unclean shutdown. On a minimal host without
`procps` the bot logs a warning and carries on, leaving those orphans alive.

The Discord application needs the **Message Content** privileged intent, and
the bot needs Send Messages, Create Public Threads, Send Messages in
Threads, Add Reactions, and Read Message History in its channels.

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
