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
