# Discord ↔ Antigravity CLI Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Discord bot that turns a Discord thread into an Antigravity conversation, driving the local `agy` CLI and streaming its output back into the thread.

**Architecture:** One Python process managed by systemd. Discord messages spawn `agy -p --output-format stream-json` subprocesses, one per turn. A thread's conversation is resumed by storing the `conversation_id` in sqlite and replaying it as `--conversation`. Agent output streams into a live-edited Discord message, chunked at 1800 characters.

**Tech Stack:** Python 3.11+, `discord.py` 2.x, stdlib `asyncio`, `sqlite3`, `tomllib`. `pytest` and `pytest-asyncio` for tests. No other runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-09-16-discord-agy-bridge-design.md`

## Global Constraints

- Python 3.11 or newer. `tomllib` is stdlib from 3.11; do not add a TOML dependency.
- Runtime dependencies are `discord.py` only. Test dependencies are `pytest` and `pytest-asyncio` only.
- Message body limit: **1800 characters**. Flush happens *before* appending a piece that would exceed it. A body never exceeds 1800.
- Edit throttle interval: **1.5 seconds**.
- Concurrency: **3** total turns, **1** slot reserved for the owner. Owner admitted while running < 3; member admitted while running < 2.
- Agent timeout: `--print-timeout 15m`, with a wall-clock kill at **16 minutes**.
- Cancellation: `SIGTERM` to the process group, `SIGKILL` after **5 seconds**.
- Thread names: first **60** characters of the prompt.
- Error reporting: last **300** characters of stderr, in a code block.
- sqlite runs in **WAL** mode.
- Subprocesses are spawned with `create_subprocess_exec` (never a shell), `start_new_session=True`, and an environment that excludes `DISCORD_TOKEN`.
- The `agy` event schema is pinned by observation in `docs/agy-stream-schema.md`. Binding facts: the top-level discriminator is **`event`** with values `init`, `step_update`, `result`; there is no `subtype`; the conversation id is top-level **`conversation_id`** on the `init` event; assistant text is an **incremental** `step_update.text_delta` that is often absent; tool calls are `step_update.step_type == "tool"` with `state` `"ACTIVE"` then `"DONE"`, named by `step_update.tool_name`, with arguments under `step_update.tool_info.parameters`. Tool **failure** signalling was never observed and must not be guessed.
- The prompt is passed positionally: `agy -p "<prompt>"`.
- Workspaces are resolved from the config allowlist only. A filesystem path supplied in chat is never accepted.
- Tier is determined by the author of each individual message, not by the thread creator.

## Deviation from the spec

The spec's Testing section names only `test_render.py` and leaves workspace and tier resolution to manual smoke tests. This plan adds `test_config.py` (Task 2) and `test_state.py` (Task 3), because workspace allowlisting and tier lookup are the security boundary of the whole system and a manual checklist is the wrong guard for them. Everything else follows the spec as approved.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Project metadata, dependencies, pytest config |
| `config.toml.example` | Documented configuration template |
| `src/agybot/config.py` | Load and validate config; tier lookup; workspace resolution |
| `src/agybot/state.py` | sqlite store for thread → conversation mapping |
| `src/agybot/render.py` | Piece types, the 1800-char chunker with fence tracking, the throttled Discord sink |
| `src/agybot/runner.py` | argv construction, environment scrubbing, event adapter, admission control, process lifecycle |
| `src/agybot/bot.py` | Discord gateway wiring, mention parsing, thread creation, startup preflight |
| `tests/test_config.py` | Config loading, tier lookup, workspace allowlisting |
| `tests/test_state.py` | Store round-trips |
| `tests/test_render.py` | Chunker boundary and fence behaviour; sink behaviour |
| `tests/test_runner.py` | argv, env scrubbing, adapter, slots, process lifecycle against a fake `agy` |
| `tests/fake_agy.py` | A stub binary emitting canned NDJSON, used by process-lifecycle tests |
| `deploy/agy-bot.service` | systemd unit |
| `README.md` | Setup, configuration, deployment, operation |

Dependency direction is strictly one way: `bot.py` → `runner.py` → `render.py`, with `config.py` and `state.py` as leaves. `render.py` owns the piece vocabulary because it is the consumer that defines it; `runner.py` imports those types to produce them.

---

### Task 1: Pin the `agy` stream-json schema

This task gates every later task that parses events. No code is written against a guessed schema. It produces a documented artifact, not source code, so it has no test cycle.

**Files:**
- Create: `docs/agy-stream-schema.md`
- Create: `tests/fixtures/agy_stream_sample.ndjson`

**Interfaces:**
- Consumes: nothing.
- Produces: the observed event shape that `adapt()` in Task 6 is written against, and the recorded NDJSON fixture that Task 6's tests replay.

- [ ] **Step 1: Confirm the prompt argument form**

The CLI help lists `--print` as "Run a single prompt non-interactively" and `--prompt` as an alias, which leaves it ambiguous whether the prompt is positional or a flag value. Try both against a trivial prompt in a scratch directory:

```bash
mkdir -p /tmp/agy-probe && cd /tmp/agy-probe
agy -p "say the word banana and nothing else" --output-format text
agy --print="say the word banana and nothing else" --output-format text
```

Record which form works in `docs/agy-stream-schema.md` under a heading "Prompt argument form". If both work, prefer the positional form, because that is what Task 6 builds.

- [ ] **Step 2: Capture a full stream-json turn with tool use**

The prompt must force at least one tool call, so that tool events appear in the capture:

```bash
cd /tmp/agy-probe
echo "hello" > probe.txt
agy -p "read probe.txt and tell me what single word it contains" \
    --output-format stream-json \
    --add-dir /tmp/agy-probe \
    --dangerously-skip-permissions \
    > /tmp/agy-probe/capture.ndjson 2>/tmp/agy-probe/capture.stderr
```

- [ ] **Step 3: Save the capture as a test fixture**

```bash
cp /tmp/agy-probe/capture.ndjson \
   /Users/spica/code/oss/mugyu-agent/tests/fixtures/agy_stream_sample.ndjson
```

If the capture contains absolute paths from your machine, leave them. The fixture is replayed as opaque text; the adapter does not interpret paths.

- [ ] **Step 4: Document the schema**

Inspect the distinct event shapes:

```bash
jq -r '.type + " / " + (.subtype // "-")' /tmp/agy-probe/capture.ndjson | sort | uniq -c
jq -s '.[0]' /tmp/agy-probe/capture.ndjson
```

Write `docs/agy-stream-schema.md` answering exactly these five questions. Each answer must name a concrete JSON path, not a description:

1. Which event carries the conversation identifier, and at what JSON path? (Expected to be an init or system event carrying something like `conversation_id`.)
2. Which event carries assistant text, at what path, and is it a cumulative snapshot or an incremental delta? This determines whether the adapter appends or diffs.
3. Which event signals a tool starting, and where is the tool name?
4. Which event signals a tool finishing, and where is its success or failure indicated?
5. Which event terminates the turn, and does it carry timing or token totals worth showing in the footer?

- [ ] **Step 5: Commit**

```bash
git add docs/agy-stream-schema.md tests/fixtures/agy_stream_sample.ndjson
git commit -m "docs: pin agy stream-json event schema from observed run"
```

---

### Task 2: Project skeleton and configuration

**Files:**
- Create: `pyproject.toml`
- Create: `config.toml.example`
- Create: `src/agybot/__init__.py`
- Create: `src/agybot/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Config` frozen dataclass with fields `owner_id: str`, `members: frozenset[str]`, `channels: frozenset[str]`, `default_workspace: str`, `workspaces: Mapping[str, str]` (a read-only `MappingProxyType`), `agy_bin: str`, `token: str` (hidden from `repr`)
  - `load_config(path: Path, env: Mapping[str, str]) -> Config`
  - `tier_of(cfg: Config, user_id: str) -> str` returning `"owner"`, `"member"`, or `"stranger"`
  - `resolve_workspace(cfg: Config, name: str | None) -> str` returning an absolute path
  - `UnknownWorkspace(Exception)` with attribute `allowed: list[str]`

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "agybot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["discord.py>=2.3,<3"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

```bash
mkdir -p src/agybot tests/fixtures
touch src/agybot/__init__.py
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:

```python
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


def test_default_workspace_must_exist_in_map(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_TEXT.replace('default_workspace = "scratch"',
                                     'default_workspace = "ghost"'))
    with pytest.raises(ValueError, match="default_workspace"):
        load_config(p, {"DISCORD_TOKEN": "tok"})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'agybot.config'`

- [ ] **Step 4: Write the implementation**

`src/agybot/config.py`:

```python
"""Configuration loading and access control lookups."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
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
    workspaces: Mapping[str, str]
    agy_bin: str
    # field(repr=False) supplies no default, so token stays required and the
    # argument order is unchanged. It keeps the live bot token out of any
    # repr(), log line, or crash dump that happens to hold a Config.
    token: str = field(repr=False)


def load_config(path: Path, env: Mapping[str, str]) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    token = env.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set in the environment")

    workspaces = {str(k): str(v) for k, v in raw.get("workspaces", {}).items()}
    if not workspaces:
        raise ValueError("config defines no [workspaces]")

    relative = sorted(n for n, p in workspaces.items()
                      if not Path(p).is_absolute())
    if relative:
        raise ValueError(
            f"workspace paths must be absolute: {', '.join(relative)}")

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
        # frozen=True stops rebinding, not mutation. The allowlist is the
        # access-control boundary, so it is made genuinely read-only here.
        workspaces=MappingProxyType(workspaces),
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
```

Note that `resolve_workspace` needs no traversal-stripping logic: a dictionary lookup against the allowlist rejects `/etc` and `../../etc` for the same reason it rejects `nope`. The two tests exist to lock that property in place, not because a separate code path handles them.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 17 passed

- [ ] **Step 6: Write the configuration template**

`config.toml.example`:

```toml
# Copy to config.toml and fill in. config.toml is gitignored.
# Discord user and channel ids are strings. Enable Developer Mode in
# Discord, then right-click a user or channel and choose "Copy ID".

# Full tool access, runs with --dangerously-skip-permissions.
owner_id = "000000000000000000"

# Restricted to --mode plan --sandbox.
members = ["111111111111111111"]

# Mentions are honored only in these channels.
channels = ["222222222222222222"]

# Used when a message names no workspace. Must be a key below.
default_workspace = "scratch"

# The only directories the bot will ever pass to agy.
[workspaces]
scratch = "/srv/agy/scratch"
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml config.toml.example src/agybot/__init__.py \
        src/agybot/config.py tests/test_config.py
git commit -m "feat: add project skeleton and configuration loading"
```

---

### Task 3: Thread state store

**Files:**
- Create: `src/agybot/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ThreadRow` frozen dataclass with fields `thread_id: str`, `conversation_id: str | None`, `workspace: str`, `created_by: str`, `created_at: int`, `last_used_at: int`
  - `Store(path: str)` with methods `create_thread(thread_id: str, workspace: str, created_by: str) -> ThreadRow`, `get_thread(thread_id: str) -> ThreadRow | None`, `set_conversation(thread_id: str, conversation_id: str) -> None`, `touch(thread_id: str) -> None`, `close() -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_state.py`:

```python
import pytest
from agybot.state import Store, ThreadRow


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_unknown_thread_returns_none(store):
    assert store.get_thread("nope") is None


def test_created_thread_round_trips(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    row = store.get_thread("t1")
    assert isinstance(row, ThreadRow)
    assert row.thread_id == "t1"
    assert row.workspace == "/srv/agy/app"
    assert row.created_by == "u1"


def test_conversation_id_starts_null(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    assert store.get_thread("t1").conversation_id is None


def test_set_conversation_persists(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    store.set_conversation("t1", "c-abc")
    assert store.get_thread("t1").conversation_id == "c-abc"


def test_touch_advances_last_used(store):
    store.create_thread("t1", "/srv/agy/app", "u1")
    before = store.get_thread("t1").last_used_at
    store.touch("t1")
    assert store.get_thread("t1").last_used_at >= before


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    s1 = Store(path)
    s1.create_thread("t1", "/srv/agy/app", "u1")
    s1.set_conversation("t1", "c-abc")
    s1.close()

    s2 = Store(path)
    assert s2.get_thread("t1").conversation_id == "c-abc"
    s2.close()


def test_wal_mode_is_enabled(store):
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_creating_same_thread_twice_is_idempotent(store):
    a = store.create_thread("t1", "/srv/agy/app", "u1")
    b = store.create_thread("t1", "/srv/agy/other", "u2")
    assert b.workspace == a.workspace
    assert b.created_by == a.created_by
```

That last test matters: Discord can deliver a message twice on reconnect, and the second delivery must not repoint an existing thread at a different workspace.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'agybot.state'`

- [ ] **Step 3: Write the implementation**

`src/agybot/state.py`:

```python
"""Persistent mapping from Discord thread to Antigravity conversation."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_id    TEXT PRIMARY KEY,
  conversation_id   TEXT,
  workspace    TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    conversation_id: str | None
    workspace: str
    created_by: str
    created_at: int
    last_used_at: int


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def create_thread(self, thread_id: str, workspace: str,
                      created_by: str) -> ThreadRow:
        now = int(time.time())
        self._db.execute(
            "INSERT OR IGNORE INTO threads"
            " (thread_id, conversation_id, workspace, created_by,"
            "  created_at, last_used_at)"
            " VALUES (?, NULL, ?, ?, ?, ?)",
            (thread_id, workspace, created_by, now, now),
        )
        self._db.commit()
        row = self.get_thread(thread_id)
        assert row is not None
        return row

    def get_thread(self, thread_id: str) -> ThreadRow | None:
        cur = self._db.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        )
        r = cur.fetchone()
        return None if r is None else ThreadRow(**dict(r))

    def set_conversation(self, thread_id: str, conversation_id: str) -> None:
        self._db.execute(
            "UPDATE threads SET conversation_id = ? WHERE thread_id = ?",
            (conversation_id, thread_id),
        )
        self._db.commit()

    def touch(self, thread_id: str) -> None:
        self._db.execute(
            "UPDATE threads SET last_used_at = ? WHERE thread_id = ?",
            (int(time.time()), thread_id),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/state.py tests/test_state.py
git commit -m "feat: add sqlite thread state store"
```

---

### Task 4: The chunker

This is the only intricate logic in the system. It is pure — no Discord, no async, no subprocess — so it is tested directly.

**Files:**
- Create: `src/agybot/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Text`, `Tool`, `Meta` frozen dataclasses and the `Piece` union
  - `LIMIT = 1800`
  - `fence_state(text: str) -> str | None`
  - `Chunker(limit: int = LIMIT)` with `feed(s: str) -> list[str]`, `flush() -> list[str]`, and the read-only property `current -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_render.py`:

```python
from agybot.render import Chunker, fence_state, LIMIT


def test_limit_is_1800():
    assert LIMIT == 1800


def test_short_feed_seals_nothing():
    c = Chunker()
    assert c.feed("hello") == []
    assert c.current == "hello"


def test_flush_returns_the_body():
    c = Chunker()
    c.feed("hello")
    assert c.flush() == ["hello"]
    assert c.current == ""


def test_flush_on_empty_body_returns_nothing():
    assert Chunker().flush() == []


def test_body_exactly_at_limit_is_not_sealed():
    c = Chunker()
    assert c.feed("a" * LIMIT) == []
    assert len(c.current) == LIMIT


def test_piece_crossing_limit_seals_before_appending():
    c = Chunker()
    c.feed("a" * (LIMIT - 5))
    sealed = c.feed("bbbbbbbbbb")
    assert sealed == ["a" * (LIMIT - 5)]
    assert c.current == "bbbbbbbbbb"


def test_no_body_ever_exceeds_the_limit():
    c = Chunker()
    bodies = []
    for _ in range(50):
        bodies += c.feed("word " * 20)
    bodies += c.flush()
    assert all(len(b) <= LIMIT for b in bodies)


def test_normal_piece_is_never_split():
    c = Chunker()
    c.feed("a" * (LIMIT - 3))
    c.feed("intact")
    assert "intact" in c.current
    assert c.current.count("intact") == 1


def test_oversized_piece_splits_at_a_newline():
    c = Chunker()
    piece = ("x" * 100 + "\n") * 40          # 4040 chars, newlines throughout
    bodies = c.feed(piece) + c.flush()
    assert len(bodies) > 1
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies).replace("\n", "") == piece.replace("\n", "")


def test_oversized_piece_without_newlines_splits_at_a_space():
    c = Chunker()
    piece = "word " * 500                    # 2500 chars, no newlines
    bodies = c.feed(piece) + c.flush()
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies).replace(" ", "") == piece.replace(" ", "")


def test_oversized_piece_without_whitespace_hard_splits():
    c = Chunker()
    piece = "z" * 4000
    bodies = c.feed(piece) + c.flush()
    assert all(len(b) <= LIMIT for b in bodies)
    assert "".join(bodies) == piece


def test_fence_state_detects_open_fence_with_language():
    assert fence_state("text\n```python\ncode") == "python"


def test_fence_state_detects_open_fence_without_language():
    assert fence_state("text\n```\ncode") == ""


def test_fence_state_is_none_when_balanced():
    assert fence_state("```python\ncode\n```\nafter") is None


def test_fence_state_is_none_for_plain_text():
    assert fence_state("no fences here") is None


def test_split_inside_fence_closes_and_reopens_with_language():
    c = Chunker()
    c.feed("```python\n")
    c.feed("y" * (LIMIT - 20))
    sealed = c.feed("more code here")
    assert len(sealed) == 1
    assert sealed[0].endswith("```")
    assert c.current.startswith("```python\n")
    assert "more code here" in c.current


def test_split_inside_unlabelled_fence_reopens_unlabelled():
    c = Chunker()
    c.feed("```\n")
    c.feed("y" * (LIMIT - 20))
    c.feed("more code here")
    assert c.current.startswith("```\n")


def test_split_outside_fence_does_not_add_fences():
    c = Chunker()
    c.feed("a" * (LIMIT - 5))
    sealed = c.feed("plain text")
    assert not sealed[0].endswith("```")
    assert not c.current.startswith("```")


def test_flush_closes_an_unterminated_fence():
    c = Chunker()
    c.feed("```python\nprint(1)")
    assert c.flush() == ["```python\nprint(1)\n```"]


def test_flush_does_not_double_close_a_balanced_fence():
    c = Chunker()
    c.feed("```python\nprint(1)\n```")
    assert c.flush() == ["```python\nprint(1)\n```"]


def test_fresh_chunker_carries_no_fence_state():
    c1 = Chunker()
    c1.feed("```python\nunterminated")
    c2 = Chunker()
    assert c2.feed("plain") == []
    assert fence_state(c2.current) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_render.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'agybot.render'`

- [ ] **Step 3: Write the implementation**

`src/agybot/render.py`:

```python
"""Piece vocabulary and the Discord message chunker."""
from __future__ import annotations

from dataclasses import dataclass

LIMIT = 1800

# Headroom kept free in an oversized split so the reopened fence prefix and
# the closing fence always fit.
RESERVE = 24


@dataclass(frozen=True)
class Text:
    s: str


@dataclass(frozen=True)
class Tool:
    name: str
    detail: str
    ok: bool | None


@dataclass(frozen=True)
class Meta:
    conversation_id: str


Piece = Text | Tool | Meta


def fence_state(text: str) -> str | None:
    """Return the language of the open code fence, or None if balanced.

    An empty string means a fence is open with no language given.
    Computed over the whole text rather than incrementally, so a fence
    marker arriving split across two stream deltas cannot be missed.
    """
    fence: str | None = None
    i = 0
    while (j := text.find("```", i)) != -1:
        if fence is None:
            k = text.find("\n", j + 3)
            raw = text[j + 3:k] if k != -1 else text[j + 3:]
            fence = raw.strip()
        else:
            fence = None
        i = j + 3
    return fence


def _split_oversized(s: str, maxlen: int) -> list[str]:
    """Break a single piece that cannot fit in one message.

    Prefers a newline boundary, then a space, then a hard cut.
    """
    if len(s) <= maxlen:
        return [s]

    parts: list[str] = []
    rest = s
    while len(rest) > maxlen:
        window = rest[:maxlen]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = maxlen
        else:
            cut += 1                 # keep the boundary character with the part
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


class Chunker:
    """Accumulates text into message bodies of at most `limit` characters.

    A piece is appended whole. If appending it would exceed the limit, the
    current body is sealed first and the piece opens the next one, so a body
    never exceeds the limit and a piece is never split across bodies. The one
    exception is a piece longer than the limit, which is split internally.
    """

    def __init__(self, limit: int = LIMIT) -> None:
        self._limit = limit
        self._body = ""

    @property
    def current(self) -> str:
        return self._body

    def feed(self, s: str) -> list[str]:
        sealed: list[str] = []
        for piece in _split_oversized(s, self._limit - RESERVE):
            if len(self._body) + len(piece) > self._limit:
                sealed.append(self._seal())
            self._body += piece
        return sealed

    def flush(self) -> list[str]:
        if not self._body:
            return []
        body = self._close_fence(self._body)
        self._body = ""
        return [body]

    def _seal(self) -> str:
        """Close the current body and start the next, carrying fence state."""
        lang = fence_state(self._body)
        body = self._close_fence(self._body)
        self._body = "" if lang is None else f"```{lang}\n"
        return body

    @staticmethod
    def _close_fence(body: str) -> str:
        if fence_state(body) is None:
            return body
        return body + ("```" if body.endswith("\n") else "\n```")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_render.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/render.py tests/test_render.py
git commit -m "feat: add 1800-character chunker with code-fence continuation"
```

---

### Task 5: The throttled Discord sink

**Files:**
- Modify: `src/agybot/render.py` (append the `Sink` class)
- Modify: `tests/test_render.py` (append sink tests)

**Interfaces:**
- Consumes: `Chunker`, `Text`, `Tool`, `Meta`, `Piece` from Task 4.
- Produces:
  - `Sink(send, edit, limit=LIMIT, interval=1.5)` where `send` is `async (content: str) -> Any` returning a message handle and `edit` is `async (handle: Any, content: str) -> None`
  - methods `start() -> None`, `feed(piece: Piece) -> None`, `finish(returncode: int, stderr_tail: str = "", cancelled: bool = False) -> None`
  - attribute `conversation_id: str | None`
  - `fmt_elapsed(seconds: float) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_render.py`:

```python
import pytest
from agybot.render import Sink, Text, Tool, Meta, fmt_elapsed, LIMIT


class FakeChannel:
    """Records every send and edit so tests can assert on final state."""

    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str) -> int:
        self.messages.append(content)
        return len(self.messages) - 1

    async def edit(self, handle: int, content: str) -> None:
        self.messages[handle] = content


@pytest.fixture
def ch():
    return FakeChannel()


def sink_for(ch) -> Sink:
    # A short interval keeps tests fast without spinning the writer hot.
    # interval=0 would busy-loop asyncio.sleep(0) for the whole turn.
    return Sink(ch.send, ch.edit, interval=0.01)


def test_fmt_elapsed_under_a_minute():
    assert fmt_elapsed(9.4) == "9s"


def test_fmt_elapsed_over_a_minute():
    assert fmt_elapsed(82) == "1m22s"


async def test_start_posts_a_placeholder(ch):
    s = sink_for(ch)
    await s.start()
    assert ch.messages == ["🤔 …"]


async def test_text_replaces_the_placeholder(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("hello world"))
    await s.finish(0)
    assert ch.messages[0].startswith("hello world")


async def test_tool_renders_as_subtext(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("read", "auth.py", ok=None))
    await s.finish(0)
    assert "-# 🔧 read · auth.py" in ch.messages[0]


async def test_failed_tool_renders_a_warning(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("bash", "exit 1", ok=False))
    await s.finish(0)
    assert "-# ⚠️ bash · exit 1" in ch.messages[0]


async def test_meta_is_captured_and_not_displayed(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Meta("c-abc"))
    await s.feed(Text("body"))
    await s.finish(0)
    assert s.conversation_id == "c-abc"
    assert "c-abc" in ch.messages[0]      # only via the footer
    assert ch.messages[0].index("body") < ch.messages[0].index("c-abc")


async def test_overflow_creates_a_second_message(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("a" * (LIMIT - 5)))
    await s.feed(Text("bbbbbbbbbb"))
    await s.finish(0)
    assert len(ch.messages) == 2
    assert all(len(m) <= 2000 for m in ch.messages)


async def test_success_footer_on_the_last_message(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("done"))
    await s.finish(0)
    assert "-# ✅" in ch.messages[-1]


async def test_failure_footer_includes_stderr(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("partial"))
    await s.finish(1, stderr_tail="boom: could not open project")
    assert "-# ❌ exit 1" in ch.messages[-1]
    assert "boom: could not open project" in ch.messages[-1]


async def test_cancelled_footer(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Text("partial"))
    await s.finish(-15, cancelled=True)
    assert "-# 🛑 cancelled" in ch.messages[-1]


async def test_tool_count_appears_in_the_footer(ch):
    s = sink_for(ch)
    await s.start()
    await s.feed(Tool("read", "a.py", ok=True))
    await s.feed(Tool("read", "b.py", ok=True))
    await s.finish(0)
    assert "2 tools" in ch.messages[-1]


async def test_finish_without_any_output_still_reports(ch):
    s = sink_for(ch)
    await s.start()
    await s.finish(1, stderr_tail="not authenticated")
    assert "not authenticated" in ch.messages[-1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_render.py -v -k "sink or footer or tool or meta or elapsed or placeholder or overflow"`
Expected: `ImportError: cannot import name 'Sink' from 'agybot.render'`

- [ ] **Step 3: Write the implementation**

Append to `src/agybot/render.py`:

```python
import asyncio
import time
from typing import Any, Awaitable, Callable

SendFn = Callable[[str], Awaitable[Any]]
EditFn = Callable[[Any, str], Awaitable[None]]

PLACEHOLDER = "🤔 …"
STDERR_TAIL = 300


def fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


class Sink:
    """Streams pieces into a Discord thread as live-edited messages.

    Only the most recent message is ever edited; sealed messages are final.
    Edit cost is therefore flat in the length of the turn.
    """

    def __init__(self, send: SendFn, edit: EditFn,
                 limit: int = LIMIT, interval: float = 1.5) -> None:
        self._send = send
        self._edit = edit
        self._interval = interval
        self._chunker = Chunker(limit)
        self._handle: Any = None
        self._dirty = False
        self._writer: asyncio.Task | None = None
        self._started = 0.0
        self._tools = 0
        self.conversation_id: str | None = None

    async def start(self) -> None:
        self._started = time.monotonic()
        self._handle = await self._send(PLACEHOLDER)
        self._writer = asyncio.create_task(self._pump())

    async def feed(self, piece: Piece) -> None:
        if isinstance(piece, Meta):
            self.conversation_id = piece.conversation_id
            return

        if isinstance(piece, Tool):
            self._tools += 1
            icon = "⚠️" if piece.ok is False else "🔧"
            text = f"\n-# {icon} {piece.name} · {piece.detail}\n"
        else:
            text = piece.s

        for sealed in self._chunker.feed(text):
            await self._edit(self._handle, sealed)
            self._handle = await self._send(self._chunker.current or "…")
        self._dirty = True

    async def finish(self, returncode: int, stderr_tail: str = "",
                     cancelled: bool = False) -> None:
        if self._writer is not None:
            self._writer.cancel()
            self._writer = None

        footer = self._footer(returncode, stderr_tail, cancelled)
        bodies = self._chunker.flush()
        body = bodies[0] if bodies else ""

        # flush() yields at most one body, so the footer joins it whenever
        # the pair fits inside Discord's hard 2000-character message cap.
        if len(body) + len(footer) <= 2000:
            await self._edit(self._handle, (body + footer) or PLACEHOLDER)
        else:
            await self._edit(self._handle, body)
            self._handle = await self._send(footer)

    def _footer(self, returncode: int, stderr_tail: str,
                cancelled: bool) -> str:
        elapsed = fmt_elapsed(time.monotonic() - self._started)
        cid = self.conversation_id or "unknown"
        if cancelled:
            head = "-# 🛑 cancelled"
        elif returncode == 0:
            head = "-# ✅"
        else:
            head = f"-# ❌ exit {returncode}"
        out = f"\n{head} · {elapsed} · {self._tools} tools · conv {cid}"
        if returncode != 0 and stderr_tail:
            out += f"\n```\n{stderr_tail[-STDERR_TAIL:]}\n```"
        return out

    async def _pump(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                if self._dirty:
                    self._dirty = False
                    await self._edit(self._handle,
                                     self._chunker.current or PLACEHOLDER)
        except asyncio.CancelledError:
            pass
```

Note the `finish` path. `Chunker.flush()` returns at most one body, so the footer is appended to it and the pair is written as a single edit. A separate message is used only when the two together would breach Discord's hard 2000-character cap, which can happen when a long stderr tail follows a nearly full body. Either way the footer lands on the last message, as the spec requires.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_render.py -v`
Expected: 34 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/render.py tests/test_render.py
git commit -m "feat: add throttled Discord sink with footers and tool subtext"
```

---

### Task 6: Command line and environment construction

**Files:**
- Create: `src/agybot/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `build_argv(agy_bin: str, prompt: str, workspace: str, conversation_id: str | None, tier: str, timeout: str = "15m") -> list[str]`
  - `minimal_env(base: Mapping[str, str]) -> dict[str, str]`
  - `ENV_ALLOWLIST: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_runner.py`:

```python
from agybot.runner import build_argv, minimal_env


def argv(**kw):
    base = dict(agy_bin="agy", prompt="do the thing",
                workspace="/srv/agy/app", conversation_id=None, tier="owner")
    base.update(kw)
    return build_argv(**base)


def test_prompt_is_a_single_argument():
    a = argv(prompt="rm -rf /; echo `whoami`")
    assert "rm -rf /; echo `whoami`" in a


def test_prompt_is_not_escaped_or_quoted():
    a = argv(prompt='say "hi"')
    assert 'say "hi"' in a
    assert '\\"' not in " ".join(a)


def test_stream_json_output_is_requested():
    a = argv()
    assert a[a.index("--output-format") + 1] == "stream-json"


def test_workspace_is_passed_as_add_dir():
    a = argv()
    assert a[a.index("--add-dir") + 1] == "/srv/agy/app"


def test_timeout_defaults_to_fifteen_minutes():
    a = argv()
    assert a[a.index("--print-timeout") + 1] == "15m"


def test_owner_gets_skip_permissions():
    a = argv(tier="owner")
    assert "--dangerously-skip-permissions" in a
    assert "--mode" not in a
    assert "--sandbox" not in a


def test_member_gets_plan_mode_and_sandbox():
    a = argv(tier="member")
    assert a[a.index("--mode") + 1] == "plan"
    assert "--sandbox" in a
    assert "--dangerously-skip-permissions" not in a


def test_stranger_tier_is_rejected_outright():
    import pytest
    with pytest.raises(ValueError, match="stranger"):
        argv(tier="stranger")


def test_new_conversation_omits_the_conversation_flag():
    assert "--conversation" not in argv(conversation_id=None)


def test_resumed_conversation_passes_the_id():
    a = argv(conversation_id="c-abc")
    assert a[a.index("--conversation") + 1] == "c-abc"


def test_binary_path_is_honoured():
    assert argv(agy_bin="/opt/agy")[0] == "/opt/agy"


def test_minimal_env_drops_the_bot_token():
    out = minimal_env({"DISCORD_TOKEN": "secret", "PATH": "/usr/bin"})
    assert "DISCORD_TOKEN" not in out


def test_minimal_env_drops_unknown_variables():
    out = minimal_env({"AWS_SECRET_ACCESS_KEY": "s", "PATH": "/usr/bin"})
    assert "AWS_SECRET_ACCESS_KEY" not in out


def test_minimal_env_keeps_path_and_home():
    out = minimal_env({"PATH": "/usr/bin", "HOME": "/home/agy"})
    assert out == {"PATH": "/usr/bin", "HOME": "/home/agy"}


def test_minimal_env_keeps_agy_config_location():
    out = minimal_env({"XDG_CONFIG_HOME": "/home/agy/.config"})
    assert out["XDG_CONFIG_HOME"] == "/home/agy/.config"


def test_minimal_env_omits_absent_keys():
    assert minimal_env({}) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'agybot.runner'`

- [ ] **Step 3: Write the implementation**

`src/agybot/runner.py`:

```python
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
```

If Task 1 found that the prompt is a flag value rather than positional, change the first three elements to `[agy_bin, f"--print={prompt}"]` and update `test_prompt_is_a_single_argument` to match. Nothing else in the plan depends on that choice.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/runner.py tests/test_runner.py
git commit -m "feat: add agy argv construction and environment scrubbing"
```

---

### Task 7: The event adapter

Write this task against `docs/agy-stream-schema.md`, produced by Task 1 from a real capture. The schema below is observed fact, not assumption — do not "correct" it back toward anything the CLI help implies.

**Files:**
- Modify: `src/agybot/runner.py` (append `EventAdapter`)
- Modify: `tests/test_runner.py` (append adapter tests)

**Interfaces:**
- Consumes: `Text`, `Tool`, `Meta`, `Piece` from `agybot.render`.
- Produces: `EventAdapter()` with `feed(ev: dict) -> list[Piece]`

The observed event shapes, for reference while writing:

```
{"event":"init","conversation_id":"cab28252-...","init":{"cwd":"...","tools":[...]}}
{"event":"step_update","step_update":{"step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"agent_response","text_delta":"Starting the check"}}
{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"view_file","tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/tmp/agy-probe/probe.txt"}}}}
{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool","tool_name":"view_file","duration_seconds":0.32,"tool_info":{"parameters":{...},"output":"2 lines, 6 bytes"}}}
{"event":"result","result":{"status":"SUCCESS","duration_seconds":10.37,"usage":{"total_tokens":30565}}}
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
import json
from pathlib import Path
import pytest
from agybot.runner import EventAdapter
from agybot.render import Text, Tool, Meta

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "agy_stream_sample.ndjson"
MULTICHUNK = FIXTURES / "agy_stream_multichunk.ndjson"


def replay(path: Path) -> list:
    a = EventAdapter()
    pieces = []
    for line in path.read_text().splitlines():
        if line.strip():
            pieces += a.feed(json.loads(line))
    return pieces


def init_ev(cid: str = "c-1") -> dict:
    return {"event": "init", "conversation_id": cid,
            "init": {"cwd": "/tmp", "tools": []}}


def step(**kw) -> dict:
    return {"event": "step_update", "step_update": kw}


def test_init_event_yields_the_conversation_id():
    assert EventAdapter().feed(init_ev()) == [Meta("c-1")]


def test_conversation_id_is_emitted_only_once():
    a = EventAdapter()
    a.feed(init_ev())
    assert a.feed(init_ev()) == []


def test_result_event_yields_nothing():
    ev = {"event": "result", "result": {"status": "SUCCESS"}}
    assert EventAdapter().feed(ev) == []


def test_user_input_step_yields_nothing():
    assert EventAdapter().feed(
        step(step_index=0, state="DONE", step_type="user_input")) == []


def test_agent_response_delta_yields_text():
    assert EventAdapter().feed(
        step(step_type="agent_response", state="ACTIVE",
             text_delta="hello")) == [Text("hello")]


def test_agent_response_without_text_delta_yields_nothing():
    assert EventAdapter().feed(
        step(step_type="agent_response", state="ACTIVE")) == []


def test_consecutive_deltas_are_emitted_in_arrival_order():
    a = EventAdapter()
    out = (a.feed(step(step_type="agent_response", state="ACTIVE",
                       text_delta="Star"))
           + a.feed(step(step_type="agent_response", state="DONE",
                         text_delta="ted.")))
    assert out == [Text("Star"), Text("ted.")]


def test_tool_start_yields_a_pending_tool_piece():
    ev = step(step_type="tool", state="ACTIVE", tool_name="view_file",
              tool_info={"name": "view_file",
                         "parameters": {"AbsolutePath": "/tmp/probe.txt"}})
    assert EventAdapter().feed(ev) == [Tool("view_file", "/tmp/probe.txt",
                                            None)]


def test_tool_done_is_not_rendered_twice():
    ev = step(step_type="tool", state="DONE", tool_name="view_file",
              tool_info={"parameters": {"AbsolutePath": "/tmp/probe.txt"},
                         "output": "2 lines, 6 bytes"})
    assert EventAdapter().feed(ev) == []


def test_unknown_tool_state_is_surfaced_as_a_failure():
    ev = step(step_type="tool", state="ERROR", tool_name="run_command",
              tool_info={"parameters": {"Command": "false"}})
    assert EventAdapter().feed(ev) == [Tool("run_command", "error", False)]


def test_tool_without_recognised_parameters_has_an_empty_detail():
    ev = step(step_type="tool", state="ACTIVE", tool_name="mystery",
              tool_info={"parameters": {"Weird": "x"}})
    assert EventAdapter().feed(ev) == [Tool("mystery", "", None)]


def test_unknown_event_yields_nothing():
    assert EventAdapter().feed({"event": "something_new"}) == []


def test_step_update_without_a_payload_yields_nothing():
    assert EventAdapter().feed({"event": "step_update"}) == []


def test_recorded_stream_produces_a_sane_piece_sequence():
    pieces = replay(FIXTURE)

    metas = [p for p in pieces if isinstance(p, Meta)]
    assert len(metas) == 1, "exactly one conversation id per turn"
    assert metas[0].conversation_id, "conversation id must not be empty"
    assert any(isinstance(p, Text) and p.s.strip() for p in pieces), \
        "the recorded turn produced no assistant text"
    tools = [p for p in pieces if isinstance(p, Tool)]
    assert tools, "the recorded turn was supposed to use a tool"
    assert all(t.ok is None for t in tools), \
        "every tool in the recorded turn succeeded, so none should be flagged"


def test_multichunk_fixture_reassembles_by_concatenation():
    """The sample fixture cannot tell append from replace: its one
    text-bearing step emits everything in a single event. This fixture
    can — step_index 3 arrives as five disjoint deltas."""
    texts = [p.s for p in replay(MULTICHUNK) if isinstance(p, Text)]
    assert len(texts) >= 2, "this fixture must exercise multi-chunk text"
    joined = "".join(texts)
    assert "Starting the check now." in joined
    assert joined.rstrip().endswith("hello.")
    # A replace-instead-of-append adapter would emit only the last chunk.
    assert len(joined) > len(texts[-1])
```

The fixture test asserts properties rather than exact strings, so it stays valid whatever the recorded run happened to say. If it fails, the adapter disagrees with reality and the adapter is wrong, not the fixture.

Note `test_recorded_stream_produces_a_sane_piece_sequence` expects exactly one `Meta` even though `conversation_id` is echoed on every event in the stream: only the `init` event is a source of `Meta`, and the adapter emits at most one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v -k "adapter or event or tool_ or conversation or recorded or delta or step"`
Expected: `ImportError: cannot import name 'EventAdapter' from 'agybot.runner'`

- [ ] **Step 3: Write the implementation**

Append to `src/agybot/runner.py`, hoisting the import to the module's import block:

```python
from agybot.render import Meta, Piece, Text, Tool

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: 31 passed

If `test_recorded_stream_produces_a_sane_piece_sequence` fails, the adapter disagrees with the captured stream. Fix the adapter, never the fixture.

- [ ] **Step 5: Commit**

```bash
git add src/agybot/runner.py tests/test_runner.py
git commit -m "feat: add agy stream-json event adapter"
```

---

### Task 8: Admission control

**Files:**
- Modify: `src/agybot/runner.py` (append `Slots`)
- Modify: `tests/test_runner.py` (append slot tests)

**Interfaces:**
- Consumes: nothing.
- Produces: `Slots(max_total: int = 3, reserved_owner: int = 1)` with `acquire(tier: str) -> None` (async), `release() -> None`, `ahead(tier: str) -> int`, `would_block(tier: str) -> bool`, and the read-only property `running -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
import asyncio
from agybot.runner import Slots


async def test_owner_may_take_all_three_slots():
    s = Slots()
    for _ in range(3):
        await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert s.running == 3


async def test_owner_blocks_at_the_fourth():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.acquire("owner"), 0.05)


async def test_member_may_take_only_two():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    assert s.running == 2
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s.acquire("member"), 0.05)


async def test_owner_is_admitted_while_members_hold_two():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert s.running == 3


async def test_release_admits_a_waiter():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    waiter = asyncio.create_task(s.acquire("owner"))
    await asyncio.sleep(0)
    s.release()
    await asyncio.wait_for(waiter, 0.1)
    assert s.running == 3


async def test_owner_overtakes_a_waiting_member():
    s = Slots()
    await s.acquire("member")
    await s.acquire("member")
    await s.acquire("owner")                       # full at 3

    member_waiter = asyncio.create_task(s.acquire("member"))
    await asyncio.sleep(0)
    s.release()                                    # running drops to 2

    # A member still may not enter at 2; the owner may.
    await asyncio.wait_for(s.acquire("owner"), 0.1)
    assert not member_waiter.done()
    member_waiter.cancel()


async def test_ahead_counts_queued_requests():
    s = Slots()
    for _ in range(3):
        await s.acquire("owner")
    w1 = asyncio.create_task(s.acquire("owner"))
    w2 = asyncio.create_task(s.acquire("owner"))
    await asyncio.sleep(0)
    assert s.ahead("owner") == 2
    for w in (w1, w2):
        w.cancel()


async def test_ahead_is_zero_when_idle():
    assert Slots().ahead("member") == 0


async def test_would_block_tracks_the_tier_thresholds():
    s = Slots()
    assert not s.would_block("member")
    await s.acquire("member")
    await s.acquire("member")
    assert s.would_block("member")
    assert not s.would_block("owner")
    await s.acquire("owner")
    assert s.would_block("owner")


async def test_release_below_zero_is_refused():
    s = Slots()
    with pytest.raises(RuntimeError):
        s.release()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v -k "slot or owner or member or ahead or release"`
Expected: `ImportError: cannot import name 'Slots' from 'agybot.runner'`

- [ ] **Step 3: Write the implementation**

Append to `src/agybot/runner.py`:

```python
import asyncio


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
```

`release` is synchronous so it can be called from a `finally` block without awaiting, and schedules the wake-up as a task. `notify_all` rather than `notify` is deliberate: the waiters have different admission thresholds, so waking exactly one can wake the one that still cannot proceed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: 41 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/runner.py tests/test_runner.py
git commit -m "feat: add admission control with a reserved owner slot"
```

---

### Task 9: Process lifecycle and cancellation

**Files:**
- Create: `tests/fake_agy.py`
- Modify: `src/agybot/runner.py` (append `Turn`)
- Modify: `tests/test_runner.py` (append lifecycle tests)

**Interfaces:**
- Consumes: `EventAdapter`, `minimal_env` from Tasks 6 and 7; `Sink` from Task 5.
- Produces:
  - `WALL_TIMEOUT = 960` (seconds; the 16-minute backstop)
  - `KILL_GRACE = 5.0`
  - `Turn(argv: list[str], cwd: str, env: dict[str, str], sink, adapter=None, wall_timeout: float = WALL_TIMEOUT)` with `run() -> int` (async) and `cancel() -> None` (async), plus the attribute `cancelled: bool`

- [ ] **Step 1: Write the fake CLI**

`tests/fake_agy.py`:

```python
#!/usr/bin/env python3
"""A stub standing in for the agy CLI. Behaviour is chosen by FAKE_AGY_MODE."""
import json
import os
import subprocess
import sys
import time


CID = "c-fake"


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def step(**kw) -> None:
    emit({"event": "step_update",
          "step_update": {"conversation_id": CID, **kw}})


mode = os.environ.get("FAKE_AGY_MODE", "normal")

if mode == "fail":
    sys.stderr.write("fatal: not authenticated with Antigravity\n")
    sys.exit(1)

emit({"event": "init", "conversation_id": CID,
      "init": {"cwd": os.getcwd(), "tools": ["view_file"]}})
step(step_index=0, state="DONE", step_type="user_input")

if mode in ("slow", "spawnchild"):
    if mode == "spawnchild":
        marker = os.environ["FAKE_AGY_MARKER"]
        subprocess.Popen([
            sys.executable, "-c",
            "import time, pathlib, sys; time.sleep(3); "
            "pathlib.Path(sys.argv[1]).write_text('alive')",
            marker,
        ])
    step(step_index=1, state="ACTIVE", step_type="agent_response",
         text_delta="starting")
    time.sleep(30)

params = {"AbsolutePath": "probe.txt"}
step(step_index=2, state="ACTIVE", step_type="tool", tool_name="view_file",
     tool_info={"name": "view_file", "parameters": params})
step(step_index=2, state="DONE", step_type="tool", tool_name="view_file",
     duration_seconds=0.3,
     tool_info={"name": "view_file", "parameters": params,
                "output": "1 line, 7 bytes"})

# Split across two deltas so the test exercises incremental reassembly.
step(step_index=3, state="ACTIVE", step_type="agent_response",
     text_delta="the word is ")
step(step_index=3, state="DONE", step_type="agent_response",
     text_delta="banana")

emit({"event": "result",
      "result": {"conversation_id": CID, "status": "SUCCESS",
                 "duration_seconds": 0.5, "num_turns": 1,
                 "usage": {"total_tokens": 42}}})
```

```bash
chmod +x tests/fake_agy.py
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_runner.py`:

```python
import os
import sys
import time
from pathlib import Path
from agybot.runner import Turn
from agybot.render import Sink

FAKE = str(Path(__file__).parent / "fake_agy.py")


class RecordingChannel:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str) -> int:
        self.messages.append(content)
        return len(self.messages) - 1

    async def edit(self, handle: int, content: str) -> None:
        self.messages[handle] = content


def turn_for(mode: str, tmp_path, wall_timeout: float = 960, **env_extra):
    ch = RecordingChannel()
    sink = Sink(ch.send, ch.edit, interval=0.01)
    env = {"FAKE_AGY_MODE": mode, "PATH": os.environ["PATH"], **env_extra}
    t = Turn([sys.executable, FAKE], cwd=str(tmp_path), env=env,
             sink=sink, wall_timeout=wall_timeout)
    return t, sink, ch


async def test_successful_turn_exits_zero(tmp_path):
    t, _, _ = turn_for("normal", tmp_path)
    assert await t.run() == 0


async def test_successful_turn_captures_the_conversation_id(tmp_path):
    t, sink, _ = turn_for("normal", tmp_path)
    await t.run()
    assert sink.conversation_id == "c-fake"


async def test_successful_turn_renders_text_and_tools(tmp_path):
    t, _, ch = turn_for("normal", tmp_path)
    await t.run()
    whole = "\n".join(ch.messages)
    assert "the word is banana" in whole
    assert "🔧 view_file · probe.txt" in whole
    assert "-# ✅" in whole


async def test_failing_turn_reports_stderr(tmp_path):
    t, _, ch = turn_for("fail", tmp_path)
    assert await t.run() == 1
    whole = "\n".join(ch.messages)
    assert "-# ❌ exit 1" in whole
    assert "not authenticated with Antigravity" in whole


async def test_cancel_stops_the_turn_promptly(tmp_path):
    t, _, ch = turn_for("slow", tmp_path)
    task = asyncio.create_task(t.run())
    await asyncio.sleep(0.5)
    started = time.monotonic()
    await t.cancel()
    await asyncio.wait_for(task, 10)
    assert time.monotonic() - started < 8
    assert t.cancelled
    assert "-# 🛑 cancelled" in "\n".join(ch.messages)


async def test_cancel_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "grandchild.txt"
    t, _, _ = turn_for("spawnchild", tmp_path, FAKE_AGY_MARKER=str(marker))
    task = asyncio.create_task(t.run())
    await asyncio.sleep(0.5)
    await t.cancel()
    await asyncio.wait_for(task, 10)
    await asyncio.sleep(3.5)
    assert not marker.exists(), "a grandchild outlived the cancelled turn"


async def test_wall_timeout_kills_a_wedged_turn(tmp_path):
    t, _, ch = turn_for("slow", tmp_path, wall_timeout=0.5)
    assert await asyncio.wait_for(t.run(), 10) != 0
    assert "-# 🛑 cancelled" in "\n".join(ch.messages)
```

`test_cancel_kills_the_whole_process_group` is the regression guard for the orphan defect found during spec review: `start_new_session=True` detaches the child, so cancellation must target the group rather than the single process.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v -k "turn or cancel or wall"`
Expected: `ImportError: cannot import name 'Turn' from 'agybot.runner'`

- [ ] **Step 4: Write the implementation**

Append to `src/agybot/runner.py`:

```python
import contextlib
import json
import os
import signal

WALL_TIMEOUT = 960.0          # 16 minutes, backstop for --print-timeout 15m
KILL_GRACE = 5.0
STDERR_KEEP = 4096


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
        )

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
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

        stderr = (await stderr_task).decode("utf-8", "replace")
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
```

The signal goes to the process group, not the process, because `start_new_session=True` puts `agy` and everything it spawns into one group. Killing only the leader would leave tool subprocesses running.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v`
Expected: 48 passed in `test_runner.py`, 104 across the suite

- [ ] **Step 6: Commit**

```bash
git add tests/fake_agy.py src/agybot/runner.py tests/test_runner.py
git commit -m "feat: add turn lifecycle with process-group cancellation"
```

---

### Task 10: Discord wiring

**Files:**
- Create: `src/agybot/bot.py`
- Create: `tests/test_bot.py`

**Interfaces:**
- Consumes: everything from Tasks 2 through 9.
- Produces:
  - `Mention` frozen dataclass with fields `workspace: str | None`, `prompt: str`
  - `parse_mention(content: str, bot_id: str) -> Mention`
  - `thread_name(prompt: str) -> str`
  - `AgyBot(cfg: Config, store: Store)`, a `discord.Client` subclass
  - `main() -> None`

- [ ] **Step 1: Write the failing tests for the pure helpers**

`tests/test_bot.py`:

```python
from agybot.bot import parse_mention, thread_name


def test_mention_is_stripped():
    m = parse_mention("<@42> fix the bug", "42")
    assert m.prompt == "fix the bug"


def test_nickname_mention_is_stripped():
    m = parse_mention("<@!42> fix the bug", "42")
    assert m.prompt == "fix the bug"


def test_workspace_prefix_is_extracted():
    m = parse_mention("<@42> [bliss-app] fix the bug", "42")
    assert m.workspace == "bliss-app"
    assert m.prompt == "fix the bug"


def test_absent_prefix_yields_no_workspace():
    m = parse_mention("<@42> fix the bug", "42")
    assert m.workspace is None


def test_bracket_later_in_the_text_is_not_a_workspace():
    m = parse_mention("<@42> fix the list[0] bug", "42")
    assert m.workspace is None
    assert m.prompt == "fix the list[0] bug"


def test_extra_whitespace_is_collapsed_at_the_edges():
    m = parse_mention("<@42>    [app]   do it  ", "42")
    assert m.workspace == "app"
    assert m.prompt == "do it"


def test_mention_only_yields_an_empty_prompt():
    assert parse_mention("<@42>", "42").prompt == ""


def test_workspace_only_yields_an_empty_prompt():
    m = parse_mention("<@42> [app]", "42")
    assert m.workspace == "app"
    assert m.prompt == ""


def test_thread_name_is_capped_at_sixty_characters():
    assert len(thread_name("x" * 200)) == 60


def test_thread_name_keeps_short_prompts_whole():
    assert thread_name("fix the bug") == "fix the bug"


def test_thread_name_falls_back_when_the_prompt_is_empty():
    assert thread_name("") == "agy task"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_bot.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'agybot.bot'`

- [ ] **Step 3: Write the implementation**

`src/agybot/bot.py`:

```python
"""Discord gateway wiring."""
from __future__ import annotations

import asyncio
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

            notice = None
            if self.slots.would_block(tier):
                notice = await thread.send(
                    f"⏳ queued · {self.slots.ahead(tier)} ahead")

            await self.slots.acquire(tier)
            try:
                if notice is not None:
                    await notice.delete()

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
                self.turns[thread.id] = turn
                self.owners[thread.id] = author_id
                try:
                    await turn.run()
                finally:
                    self.turns.pop(thread.id, None)
                    self.owners.pop(thread.id, None)

                if sink.conversation_id and not row.conversation_id:
                    self.store.set_conversation(str(thread.id), sink.conversation_id)
                self.store.touch(str(thread.id))
            finally:
                self.slots.release()

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_bot.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/agybot/bot.py tests/test_bot.py
git commit -m "feat: wire Discord gateway to agy turns"
```

---

### Task 11: Startup preflight and orphan sweep

The spec requires that the bot refuse to boot with a missing `agy`, and that stray `agy` processes left by an unclean shutdown be killed before the gateway connects.

**Files:**
- Modify: `src/agybot/runner.py` (append `preflight` and `sweep_stray_agy`)
- Modify: `src/agybot/bot.py` (call both from `main`)
- Modify: `tests/test_runner.py` (append preflight tests)

**Interfaces:**
- Consumes: `Config` from Task 2.
- Produces:
  - `PreflightError(Exception)`
  - `preflight(cfg: Config) -> None`
  - `sweep_stray_agy(agy_bin: str) -> int` returning the number of signals sent

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:

```python
from agybot.config import Config
from agybot.runner import preflight, PreflightError, sweep_stray_agy


def cfg_with(tmp_path, agy_bin: str, workspaces: dict) -> Config:
    return Config(
        owner_id="1", members=frozenset(), channels=frozenset(),
        default_workspace=next(iter(workspaces)), workspaces=workspaces,
        agy_bin=agy_bin, token="tok",
    )


def test_preflight_passes_with_a_real_binary_and_directory(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    preflight(cfg_with(tmp_path, sys.executable, {"ws": str(ws)}))


def test_preflight_rejects_a_missing_binary(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PreflightError, match="agy"):
        preflight(cfg_with(tmp_path, "/nonexistent/agy", {"ws": str(ws)}))


def test_preflight_rejects_a_non_executable_binary(tmp_path):
    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o644)
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PreflightError, match="executable"):
        preflight(cfg_with(tmp_path, str(fake), {"ws": str(ws)}))


def test_preflight_rejects_a_missing_workspace(tmp_path):
    with pytest.raises(PreflightError, match="does not exist"):
        preflight(cfg_with(tmp_path, sys.executable,
                           {"ws": str(tmp_path / "absent")}))


def test_preflight_rejects_a_workspace_that_is_a_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(PreflightError, match="not a directory"):
        preflight(cfg_with(tmp_path, sys.executable, {"ws": str(f)}))


def test_sweep_is_harmless_when_nothing_matches():
    assert sweep_stray_agy("/definitely/not/a/real/binary/name") == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_runner.py -v -k "preflight or sweep"`
Expected: `ImportError: cannot import name 'preflight' from 'agybot.runner'`

- [ ] **Step 3: Write the implementation**

Append to `src/agybot/runner.py`:

```python
import shutil
import subprocess
from pathlib import Path


class PreflightError(Exception):
    """A startup condition that makes the bot unable to do its job."""


def preflight(cfg) -> None:
    """Refuse to boot on a misconfigured host."""
    resolved = shutil.which(cfg.agy_bin) or cfg.agy_bin
    path = Path(resolved)
    if not path.exists():
        raise PreflightError(
            f"agy binary not found: {cfg.agy_bin!r}. "
            "Install it or set AGY_BIN."
        )
    if not os.access(path, os.X_OK):
        raise PreflightError(f"agy binary is not executable: {path}")

    for name, location in cfg.workspaces.items():
        p = Path(location)
        if not p.exists():
            raise PreflightError(
                f"workspace {name!r} does not exist: {location}")
        if not p.is_dir():
            raise PreflightError(
                f"workspace {name!r} is not a directory: {location}")


def sweep_stray_agy(agy_bin: str) -> int:
    """Kill agy processes this user left behind after an unclean shutdown.

    The bot is the only thing that should ever run `agy -p` as the service
    user, so matching on that pattern is safe. Do not use the service
    account for interactive agy sessions.
    """
    pattern = f"{Path(agy_bin).name} -p"
    result = subprocess.run(
        ["pkill", "-u", str(os.getuid()), "-f", pattern],
        capture_output=True,
    )
    return 1 if result.returncode == 0 else 0
```

- [ ] **Step 4: Wire them into startup**

In `src/agybot/bot.py`, replace the body of `main` with:

```python
def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(Path(os.environ.get("AGY_CONFIG", "config.toml")),
                      os.environ)
    preflight(cfg)
    swept = sweep_stray_agy(cfg.agy_bin)
    if swept:
        log.warning("killed stray agy processes left by a previous run")
    store = Store(os.environ.get("AGY_DB", "state.db"))
    AgyBot(cfg, store).run(cfg.token)
```

and extend the runner import at the top of the file:

```python
from agybot.runner import (
    EventAdapter, Slots, Turn, build_argv, minimal_env, preflight,
    sweep_stray_agy,
)
```

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: 124 passed

- [ ] **Step 6: Commit**

```bash
git add src/agybot/runner.py src/agybot/bot.py tests/test_runner.py
git commit -m "feat: add startup preflight and stray process sweep"
```

---

### Task 12: Deployment and documentation

**Files:**
- Create: `deploy/agy-bot.service`
- Create: `README.md`

**Interfaces:**
- Consumes: everything.
- Produces: no code interfaces. This task delivers a machine an operator can actually stand up.

- [ ] **Step 1: Write the systemd unit**

`deploy/agy-bot.service`:

```ini
[Unit]
Description=Discord bridge to the Antigravity CLI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agy
Group=agy
WorkingDirectory=/srv/agy
Environment=AGY_CONFIG=/srv/agy/config.toml
Environment=AGY_DB=/srv/agy/state.db
EnvironmentFile=/etc/agy-bot.env
ExecStart=/srv/agy/venv/bin/python -m agybot.bot
Restart=always
RestartSec=5

# The service account is the real containment boundary for the owner tier.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/srv/agy

[Install]
WantedBy=multi-user.target
```

`ProtectHome=read-only` and `ReadWritePaths=/srv/agy` mean an agent running with `--dangerously-skip-permissions` can write only inside the workspace tree, even though nothing constrains it at the application level. If workspaces are configured outside `/srv/agy`, add them to `ReadWritePaths` or the agent's edits will fail with confusing permission errors.

- [ ] **Step 2: Write the README**

`README.md`:

````markdown
# agybot

A Discord bot that turns a thread into an Antigravity conversation. Mention
it with a task, and it runs the `agy` CLI in that thread, streaming the
agent's output back as it works.

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

## Operating

- Three turns run at once; one slot is always held for the owner.
- Cancel a turn with ❌ on the bot's message, or by posting `!stop` in the
  thread. Members may cancel only their own turns; the owner may cancel any.
- Turns are capped at 15 minutes, with a hard kill at 16.

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

## Security notes

- Agent output is relayed verbatim to Discord. An agent that reads a secret
  file publishes that secret to the channel. **Keep secrets out of workspace
  directories.**
- `agy` runs with an allowlisted environment that excludes `DISCORD_TOKEN`.
- Do not use the `agy` service account for interactive `agy` sessions. The
  startup sweep kills `agy -p` processes owned by that user.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
````

- [ ] **Step 3: Run the full suite one last time**

Run: `.venv/bin/pytest tests/ -v`
Expected: 124 passed

- [ ] **Step 4: Commit**

```bash
git add deploy/agy-bot.service README.md
git commit -m "docs: add systemd unit and operator documentation"
```

---

## Manual smoke test

Automated tests cover every pure component and the process lifecycle against
a fake CLI. They do not cover Discord itself. Run this list once against a
real server before declaring the bot working. Each line is the spec's
acceptance criteria restated as an action.

- [ ] Owner mentions the bot with no workspace prefix. A thread opens, the
      default workspace is used, the answer arrives with a ✅ footer.
- [ ] Owner mentions with `[name]` for a configured workspace. The agent's
      answer shows it read files from that directory.
- [ ] Owner mentions with `[nope]`. The reply lists the allowed names and no
      process starts.
- [ ] Member mentions the bot. The turn runs in plan mode and the agent
      declines to edit files.
- [ ] A non-allowlisted user mentions the bot. They get 🚫 and nothing else.
- [ ] Follow-up message in an existing thread continues the conversation:
      the agent remembers the earlier turn.
- [ ] A member posts in a thread the owner created. The turn runs with
      member flags, not owner flags.
- [ ] Ask for an answer longer than 3000 characters. It arrives across
      multiple messages, each under the limit, none cut mid-word.
- [ ] Ask for a long code block that crosses 1800 characters. Both messages
      render as code blocks; neither shows stray backticks.
- [ ] Cancel a running turn with ❌. The turn stops within a few seconds and
      the footer reads 🛑 cancelled.
- [ ] A member tries to cancel the owner's turn. It is refused.
- [ ] Start four turns at once as members and the owner. Members queue at
      two; the owner starts immediately.
- [ ] Restart the bot mid-turn. It comes back up, logs the stray sweep, and
      the thread still resumes its conversation on the next message.

---

## Self-review

**Spec coverage.** Every section of the design maps to a task: architecture
and modules to the File Structure table; configuration to Task 2; state to
Task 3; message lifecycle to Tasks 10 and 12; invocation and the adapter to
Tasks 6, 7, and 9; the output pipeline to Tasks 4 and 5; concurrency to Task
8; cancellation and timeouts to Task 9; failure handling to Tasks 5, 9, and
11; the security model to Tasks 6, 11, and 12; deployment and testing to
Task 12 and the smoke list. The spec's three prerequisites are Task 1 and
the two notes below.

**Placeholders.** None. Every step carries the code or command it describes.
Task 7's reconciliation step lists four concrete edits keyed to five
concrete questions rather than saying "adjust as needed", and Task 1
produces the answers before Task 7 runs.

**Type consistency.** Checked across tasks: `Config`, `ThreadRow`, `Store`,
`Text`, `Tool`, `Meta`, `Piece`, `Chunker`, `fence_state`, `Sink`,
`fmt_elapsed`, `build_argv`, `minimal_env`, `EventAdapter`, `Slots`,
`Slots.would_block`, `Turn`,
`preflight`, `sweep_stray_agy`, `Mention`, `parse_mention`, `thread_name`.
Names and signatures in the Interfaces blocks match their definitions and
their call sites in Task 10.

## Blocking prerequisites

Task 1 resolves the stream schema. Two further conditions are outside this
plan and must be confirmed on the deployment host before Task 12, because
either can invalidate the project:

1. **A Linux `agy` binary must exist.** The development machine has a
   Mach-O arm64 build. If Antigravity ships no Linux build, this bot cannot
   be deployed as designed.
2. **`agy` must authenticate on a headless host.** If there is no device-code
   flow, confirm that copying an authenticated configuration directory from
   an interactive machine works, and document the path in the README.

Tasks 1 through 11 can proceed on macOS regardless, since every test runs
against the fake CLI rather than the real one.
