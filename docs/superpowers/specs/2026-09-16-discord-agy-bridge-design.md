# Discord ↔ Antigravity CLI Bridge — Design

**Date:** 2026-09-16
**Status:** Approved design, pending implementation plan
**Project:** mugyu-agent

## Problem

Antigravity's agent is only reachable from the desktop IDE or a local terminal. We want to drive it from Discord: ask it to work on a repo, watch it work, read the answer, and let a small group of trusted people do the same — without giving any of them a shell on the machine.

## Goal

A single-process Discord bot, deployed on Linux, that turns a Discord thread into an Antigravity conversation. Messages in the thread become turns; the agent's output streams back into the thread as it is produced.

## Scope

**In scope**

- Mention-to-thread task creation, with the thread bound to one Antigravity conversation for its lifetime.
- Two permission tiers: an owner with full tool access, and members restricted to planning.
- Workspace selection from a fixed allowlist.
- Live streaming of agent output, including tool activity.
- Bounded concurrency with a slot reserved for the owner.
- Cancellation of an in-flight turn.

**Out of scope**

- Public or open-server access. The bot serves an allowlist only.
- Per-user filesystem isolation, containers, or git worktrees per thread.
- Any agent backend other than the `agy` CLI. No abstraction layer for hypothetical future backends.
- A web dashboard, metrics stack, or message broker.

## Decisions

| Question | Decision |
|---|---|
| How to reach Antigravity | The local `agy` CLI in print mode |
| Who may use it | Owner with full access; members restricted to plan mode; everyone else refused |
| Session model | One Discord thread equals one Antigravity conversation |
| Output style | Content streams live into an edited message |
| Chunk boundary | Flush at 1800 characters, before the piece that would exceed it |
| Workspace | Named workspace from an allowlist, with a configured default |
| Concurrency | Three concurrent turns, one slot reserved for the owner |
| Stack | Python with `discord.py` |
| Deployment target | Linux, systemd, dedicated non-root user |

## Architecture

One Python process. No web server, no database server, no broker.

```
Discord gateway (websocket)
        │
    bot.py ──── config.py   env, workspace allowlist, tier lookup
        │
        ├──── state.py      sqlite: thread_id → cascade_id, workspace
        │
        ├──── runner.py     subprocess, NDJSON parsing, slots, cancellation
        │
        └──── render.py     buffering, fence tracking, chunking, throttled edits
```

| Module | Estimated size | Responsibility |
|---|---|---|
| `bot.py` | ~150 lines | Gateway events, thread creation, wiring |
| `runner.py` | ~150 lines | Spawn `agy`, parse NDJSON, admission control, cancellation |
| `render.py` | ~180 lines | Text buffering, code-fence tracking, chunking, throttled edits |
| `state.py` | ~60 lines | sqlite access, four queries |
| `config.py` | ~50 lines | Environment variables, workspace map, tier lookup |
| `test_render.py` | ~80 lines | Chunker and fence tests |

The boundaries follow one rule: the only intricate logic in this system is chunking, and it is pure. `render.py` transforms a stream of text pieces into a sequence of message bodies with no knowledge of Discord or subprocesses, so it can be tested directly. `runner.py` is the only module that touches processes. `bot.py` is the only module that knows Discord. If the `agy` event schema turns out to differ from our expectation, only the adapter in `runner.py` changes.

## Configuration

`config.toml`, read with the standard library `tomllib`. The bot token is never stored here.

```toml
owner_id          = "1234..."
members           = ["5678...", "9012..."]
channels          = ["1111..."]
default_workspace = "scratch"

[workspaces]
scratch   = "/srv/agy/scratch"
bliss-app = "/srv/agy/bliss-app"
```

`DISCORD_TOKEN` comes from the environment. `AGY_BIN` defaults to `agy` on `PATH` and may be overridden by environment variable.

## State

```sql
CREATE TABLE IF NOT EXISTS threads (
  thread_id   TEXT PRIMARY KEY,
  cascade_id  TEXT,
  workspace   TEXT NOT NULL,
  created_by  TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL
);
```

`cascade_id` is null until the first turn reports it. The database uses WAL mode. A single process owns it, so there is no lock contention to design around.

## Message lifecycle

### New task

1. A user mentions the bot in an allowed channel, optionally prefixed with a workspace name in square brackets: `@bot [bliss-app] fix the token expiry check`.
2. The bot resolves the author's tier. An author who is neither the owner nor a member gets a 🚫 reaction and nothing else — no reply, no process.
3. The bot parses the optional `[name]` prefix and resolves it against `[workspaces]`. An absent prefix selects `default_workspace`. A prefix that names an unlisted workspace produces a reply listing the allowed names, and the turn stops. Raw filesystem paths from chat are never accepted.
4. The bot creates a thread named from the first 60 characters of the prompt.
5. The bot inserts a `threads` row with a null `cascade_id`.
6. The bot spawns `agy` and streams output into the thread.
7. The first conversation identifier observed in the stream is written to the row.

### Follow-up

A message posted in a thread whose id appears in `threads` continues that conversation. The workspace and conversation id come from the row. The permission tier comes from the author of the new message, not from the thread's creator: a member posting in a thread the owner created runs with member flags.

### Invocation

```python
argv = [
    AGY_BIN, "-p", prompt,
    "--output-format", "stream-json",
    "--add-dir", workspace,
    "--print-timeout", "15m",
]
if cascade_id:
    argv += ["--conversation", cascade_id]
argv += (["--dangerously-skip-permissions"] if tier == "owner"
         else ["--mode", "plan", "--sandbox"])
```

The process is spawned with `asyncio.create_subprocess_exec`, never through a shell. The prompt is a single argument, so backticks, semicolons, and quotes in user text carry no shell meaning.

```python
proc = await asyncio.create_subprocess_exec(
    *argv,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=workspace,
    env=MINIMAL_ENV,
    start_new_session=True,
)

async for raw in proc.stdout:
    for piece in adapt(json.loads(raw)):
        await sink.feed(piece)

await sink.finish(await proc.wait())
```

`start_new_session=True` puts `agy` and every tool subprocess it spawns into one process group, so cancellation can kill the group as a unit. `MINIMAL_ENV` excludes `DISCORD_TOKEN`.

Because the child runs in its own session, it outlives the bot. The bot is the only thing on the machine that launches `agy -p`, so a startup sweep that kills such processes owned by the bot user is sufficient cleanup after an unclean shutdown.

### The adapter

`adapt()` is the single function that understands the `agy` event schema. It emits three piece types, and nothing downstream knows anything else:

| Piece | Source | Rendering |
|---|---|---|
| `Text(str)` | Assistant text delta | Appended to the message buffer |
| `Tool(name, detail, ok)` | Tool start or result | A subtext line, e.g. `-# 🔧 read · auth.py` |
| `Meta(cascade_id)` | Init or system event | Written to sqlite, never displayed |

Conversation continuity is therefore one stored string. Antigravity holds the history; the bot holds the identifier.

### Why one process per turn

A long-lived process per thread, fed through `--input-format stream-json`, would also work. It is rejected because a bot restart would destroy every session, idle threads would hold memory indefinitely, and a wedged process would need its own supervision. Resume-by-identifier provides the same continuity for the cost of process startup per message.

## Output pipeline

### Chunking

The buffer holds the body of the message currently being edited. When appending a piece would push the body past 1800 characters, the buffer flushes first and the new message begins with that piece. The body therefore never exceeds 1800 characters and a piece is never split across messages.

The one exception is a single piece longer than 1800 characters, which can occur when a large code block arrives in one event. Such a piece is split at the last newline before the limit, failing that at the last space, failing that at 1800 exactly.

### Code fences

A tracker counts fence toggles and remembers the language of the open fence. When a flush occurs inside an open fence, the outgoing message is closed with a fence and the next message opens with a fence carrying the same language. A code block split across messages therefore renders as two code blocks rather than as broken markdown. Fence state resets at the start of every turn.

### Tool activity

Tool calls appear inline in the content stream as Discord subtext lines. Raw tool output is not streamed. A failed tool renders as `-# ⚠️ bash · exit 1`.

### Throttling

One writer task per turn sleeps 1.5 seconds and edits the current message if the buffer changed. Only the most recent message is ever edited; flushed messages are sealed. Edit cost is therefore flat in the length of the turn and stays well inside Discord's edit rate limit.

### Turn boundaries

A thread opens with a `🤔` placeholder, replaced in place by the first text delta. On completion, a subtext footer is appended to the final message: `-# ✅ 1m22s · 3 tools · cascade a1b2c3`. On failure the footer reads `-# ❌ exit 1` and the last 300 characters of stderr follow in a code block.

## Concurrency

Three turns may run at once, with one slot reserved for the owner. The owner is admitted while fewer than three turns are running; a member is admitted while fewer than two are running. The owner therefore never queues behind members. A user who cannot be admitted sees `⏳ queued · 2 ahead`, and admission is re-checked whenever a slot frees.

Each thread additionally holds a lock, so a second message in a busy thread queues behind that thread's current turn. Two `agy` processes never run against the same conversation.

## Cancellation

A ❌ reaction on the bot's message, or `!stop` posted in the thread, cancels the turn. A member may cancel only their own turn; the owner may cancel any. Cancellation sends `SIGTERM` to the process group and `SIGKILL` five seconds later. The footer becomes `-# 🛑 cancelled`.

`--print-timeout 15m` bounds the agent itself, and a wall-clock kill at sixteen minutes covers a process that ignores it.

## Failure handling

| Failure | Behaviour |
|---|---|
| `agy` missing or not executable | Startup check fails; the bot refuses to boot and logs the reason |
| `agy` not authenticated | Detected on stderr; the bot posts setup instructions and does not retry |
| `agy` exits non-zero | Failure footer plus the last 300 characters of stderr in a code block |
| Discord disconnects mid-turn | `discord.py` reconnects; the turn continues, output buffers and flushes on reconnect |
| Bot killed mid-turn | `agy` survives as an orphan, since it runs in its own session. A startup sweep kills stray `agy -p` processes owned by the bot user before the gateway connects. The half-written message keeps no footer; the thread resumes normally by `cascade_id` on the next message |
| Thread row exists with a null `cascade_id` | Treated as a new conversation |
| Thread archived | Discord unarchives it on post; the row is untouched |
| Message in a thread the bot did not create | Ignored |

## Security model

The bot's real containment is the unix user it runs as. `--dangerously-skip-permissions` gives the owner's turns arbitrary shell execution, so that user has no sudo rights and owns only `/srv/agy/*`. Nothing else on the machine is readable or writable by it.

`--mode plan --sandbox` for members is a guardrail, not a boundary. Plan mode is a model-level instruction, and a sufficiently determined member could talk the agent into acting outside it. This is accepted: members are semi-trusted colleagues, not adversaries. If that assumption ever stops holding, the correct fix is per-thread git worktrees or containers, not tighter prompting.

Three further rules follow from the design:

- Workspaces come from an allowlist. Chat never supplies a path.
- Unknown users get a reaction and no process. The gate is before spawn, not after.
- Agent output is relayed verbatim to Discord, so an agent that reads a secret file publishes that secret to the channel. Secrets must not live inside workspace directories, and `agy` runs with a minimal environment that excludes the bot token.

## Deployment

systemd on Linux, `Restart=always`, running as a dedicated non-root `agy` user with `WorkingDirectory=/srv/agy` and a virtualenv at `/srv/agy/venv`. `DISCORD_TOKEN` is supplied through `EnvironmentFile=/etc/agy-bot.env` with mode 0600.

## Testing

`test_render.py` covers the chunker, which is the only non-trivial pure logic in the system. It asserts that a flush occurs at the 1800-character boundary rather than after it, that no piece is split mid-word, that a fence open at a split is closed and reopened with the same language, that an oversized single piece splits at a newline, and that fence state does not leak between turns.

Everything else is verified by a manual smoke list: an owner turn, a member turn, a cancellation mid-turn, a 3000-character answer, an answer whose code block crosses the 1800-character boundary, an unknown workspace name, and a mention from a non-allowlisted user.

There are no gateway mocks and no integration harness. The parts that would need them are thin enough to read.

## Prerequisites

Three questions must be answered before implementation begins. The first two can invalidate the design entirely.

1. **A Linux `agy` binary must exist.** The binary on the development Mac is Mach-O arm64. If Antigravity ships no Linux build, this design is unbuildable and the project falls back to driving the Gemini API directly, which is a different system.
2. **`agy` must authenticate on a headless machine.** The login is a Google account flow. If no device-code path exists, the fallback is copying an authenticated configuration directory from an interactive machine, which must be confirmed to work.
3. **The `stream-json` event schema must be pinned by observation.** The binary references `cascade_id` and the keys `type`, `subtype`, and `event`, but the field layout is not readable from the binary. One real run of `agy -p --output-format stream-json` resolves it, and also settles whether the prompt is a positional argument or a flag value. No code is written against a guessed schema.
