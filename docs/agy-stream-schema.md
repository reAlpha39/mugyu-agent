# `agy` stream-json event schema (observed)

This document pins the actual shape of `agy --output-format stream-json` events,
captured against the real binary at `/Users/spica/.local/bin/agy`
(macOS arm64, `agy --help` responded before this probe was run). It supersedes
any assumption based on the CLI help text alone. All later tasks that parse
`agy` events must be written against what is documented here, not against
the shapes implied by the brief's example `jq` commands (see "Divergence from
the brief" below — the top-level field is `event`, not `type`).

The recorded fixture is `tests/fixtures/agy_stream_sample.ndjson`, captured
exactly per Step 2 of the task-1 brief.

## Prompt argument form

Both forms work:

```bash
agy -p "say the word banana and nothing else" --output-format text        # -> banana
agy --print="say the word banana and nothing else" --output-format text   # -> banana
```

**Use the positional form: `agy -p "<prompt>"`.** Both `-p "<prompt>"` and
`--print="<prompt>"` produced identical output (`banana`, exit 0). Per the
brief's ambiguity resolution, the positional form is what Task 6 builds
against, so it is the one to standardize on. (`--prompt` is documented as a
plain alias for `--print` and was not separately tested; it takes the same
form as `--print`.)

## Divergence from the brief's Step 4 commands

The brief's inspection commands assume a top-level `.type` / `.subtype` shape:

```bash
jq -r '.type + " / " + (.subtype // "-")' capture.ndjson | sort | uniq -c
```

Against the real fixture this prints a single line: `  10  / -` — every event
object's top-level discriminator field is actually named **`event`**, not
`type`, and there is no `subtype` field anywhere. The real top-level shape is:

```
{"event": "init" | "step_update" | "result", ...event-specific keys...}
```

- `event == "init"`: one per turn, first line. Payload nested under `.init`.
- `event == "step_update"`: zero or more per turn, payload nested under
  `.step_update`. Each represents one lifecycle update of one "step"
  (`step_index`), where a step is one of `step_type`: `user_input`,
  `agent_response`, or `tool`.
- `event == "result"`: exactly one per turn, last line. Payload nested under
  `.result`.

Corrected inspection command and its actual output against the fixture:

```bash
jq -r '.event + " / " + (.step_update.step_type // "-")' tests/fixtures/agy_stream_sample.ndjson | sort | uniq -c
```

```
   1 init / -
   1 result / -
   3 step_update / agent_response
   1 step_update / user_input
   4 step_update / tool
```

(4 tool `step_update` lines = 2 tool invocations x 2 lifecycle states each,
`ACTIVE` then `DONE` — see Q3/Q4 below.)

## The five questions

### 1. Conversation identifier

The `init` event, at the **top level, sibling of `event`/`init`**:

```
$.conversation_id          # e.g. "cab28252-d3b9-472f-8cb4-76f5c6708a98"
```

Example line 1 of the fixture:

```json
{"event":"init","conversation_id":"cab28252-d3b9-472f-8cb4-76f5c6708a98","init":{"cwd":"/tmp/agy-probe","tools":[...],"permission_mode":"always-proceed"}}
```

The same id is echoed on every subsequent event too, at
`$.step_update.conversation_id` (for `step_update` events) and
`$.result.conversation_id` (for the terminal `result` event) — all three
paths carry the identical value within one turn. The canonical/first
occurrence is the `init` event's top-level `conversation_id`.

### 2. Assistant text: path, and cumulative snapshot vs. incremental delta

Path: `$.step_update.text_delta`, present only on `step_update` events where
`step_update.step_type == "agent_response"` (and, when present, only on
`agent_response` updates that actually produced visible output — see below).

**It is an incremental delta, not a cumulative snapshot.** This was
determined empirically, not assumed, by re-running the probe with a prompt
forced to produce narration both before and after a tool call
(`"Say the sentence 'Starting the check now.' verbatim first. Then read
probe.txt and, in a new sentence, tell me the single word it contains."`)
and diffing consecutive `text_delta` values for the same `step_index`:

```
step_index=3 state=ACTIVE text_delta='Starting the check now. The single w'
step_index=3 state=ACTIVE text_delta='ord contained in [probe.'
step_index=3 state=ACTIVE text_delta='txt](file://'
step_index=3 state=ACTIVE text_delta='/tmp/agy-probe/'
step_index=3 state=DONE  text_delta='probe.txt) is hello.\n'
```

Each chunk is disjoint text that only makes sense concatenated in order
(`'Starting the check now. The single w' + 'ord contained in [probe.' + ... `
reconstructs the full sentence). None of the later chunks repeat the earlier
ones, which rules out "cumulative snapshot." **The adapter must append
`text_delta` to a per-`step_index` buffer, not replace it.**

Caveat worth carrying into Task 7: a short response can arrive as a single
`state:"DONE"` event with the entire text in one `text_delta` (this is what
happened in the committed fixture's step_index=5 — one line, no preceding
`ACTIVE` chunks). Longer responses split across multiple `ACTIVE` lines
before the final `DONE` line, as shown above. Append-per-step-index handles
both cases correctly (concatenating a single chunk is a no-op).

Also note: not every `agent_response` step_update carries a `text_delta` at
all — in the committed fixture, step_index 1 and 3 are `agent_response`
updates with no `text_delta` key (the model went straight to a tool call with
no visible narration that turn); only step_index 5 (the final step) has one.
Adapter code must treat a missing `text_delta` as "no text this step", not as
an error.

### 3. Tool starting: event and tool name path

A `step_update` event with `step_update.step_type == "tool"` and
`step_update.state == "ACTIVE"` signals a tool call starting. Example (fixture
line 3):

```json
{"event":"step_update","step_update":{"conversation_id":"...","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"view_file","tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/tmp/agy-probe/probe.txt"}}}}
```

Tool name path: `$.step_update.tool_name` (duplicated at
`$.step_update.tool_info.name`). Tool arguments: `$.step_update.tool_info.parameters`
(an object whose keys are tool-specific, e.g. `AbsolutePath` for `view_file`).

### 4. Tool finishing: event, and success/failure path

The same `step_update.step_type == "tool"`, same `step_index`, re-emitted with
`step_update.state == "DONE"` and an added `tool_info.output`:

```json
{"event":"step_update","step_update":{"conversation_id":"...","step_index":2,"state":"DONE","step_type":"tool","tool_name":"view_file","duration_seconds":0.321271,"tool_info":{"name":"view_file","parameters":{"AbsolutePath":"/tmp/agy-probe/probe.txt"},"output":"2 lines, 6 bytes"}}}
```

Success/failure path: `$.step_update.state` (`"DONE"` observed for success).
**Caveat — not verified:** this probe never produced a failing tool call, so
the exact `state` value (or an `error`-shaped field in `tool_info`) used on
tool failure was not observed and is not documented here. Do not assume
`"ERROR"` or `"FAILED"` without a real failing capture; treat any `state`
value other than `"ACTIVE"`/`"DONE"` as unhandled until confirmed.

Duration is available at `$.step_update.duration_seconds` on the `DONE` event.

### 5. Turn termination: event, timing, and token totals

`event == "result"` is the last line of every turn, payload under `$.result`:

```json
{"event":"result","result":{"conversation_id":"...","status":"SUCCESS","response":"...","duration_seconds":10.374442,"num_turns":1,"usage":{"input_tokens":29497,"output_tokens":1068,"thinking_tokens":933,"cache_read_tokens":32585,"total_tokens":30565}}}
```

Relevant paths:

- `$.result.status` — `"SUCCESS"` observed (failure value not observed/confirmed).
- `$.result.duration_seconds` — wall-clock seconds for the whole turn.
- `$.result.usage.total_tokens`, `$.result.usage.input_tokens`,
  `$.result.usage.output_tokens`, `$.result.usage.thinking_tokens`,
  `$.result.usage.cache_read_tokens`.

Yes, worth showing in a footer: `duration_seconds` and `usage.total_tokens`
(optionally the input/output/thinking breakdown) are both present and
meaningful.

## Surprising/notable observations

- The probe prompt ("read probe.txt and tell me what single word it
  contains") caused **two** tool calls, not one: the first `view_file` call
  (step_index 2) read an unrelated skill file,
  `/Users/spica/.gemini/config/plugins/superpowers/skills/using-superpowers/SKILL.md`,
  before the second `view_file` call (step_index 4) read the actual
  `/tmp/agy-probe/probe.txt`. This looks like `agy` auto-loading a
  "superpowers" skill/plugin as part of its own agent loop, independent of
  the user's prompt. It does not change the event schema, but it means the
  committed fixture's first tool event is *not* the one answering the user's
  question — adapters and later tests should not assume the first tool event
  in a capture is the "interesting" one.
- The `user_input` step (`step_index:0`) does not echo the prompt text
  anywhere in its payload — it is `{"conversation_id":...,"step_index":0,"state":"DONE","step_type":"user_input"}` and nothing else.
- `init.tools` lists ~50 available tool names (browser automation, MCP,
  subagents, file/command tools, etc.) — `view_file` (used here) is one of
  many; tool-name handling in the adapter should not assume a small fixed set.
- stderr was empty and exit code was 0 for every invocation in this probe;
  `agy` was already authenticated in this environment, so no BLOCKED
  condition was hit.
