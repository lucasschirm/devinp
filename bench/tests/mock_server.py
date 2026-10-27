"""In-process mock ``devin acp`` server — WP1 test double (zero quota).

``make_mock(root, mode)`` materializes in ``root``:

* ``mock_devin.py`` — a mock ACP server speaking the WP0 wire subset
  (NDJSON JSON-RPC 2.0, wire method names, startup vendor notifications,
  server-initiated ``session/request_permission``), in one of the modes:

  - ``ok``: full happy path — startup vendor notifications, initialize,
    ``session/new``, ``session/prompt`` that echoes the (multi-line) prompt
    text back in a streamed update, four permission requests (web_fetch /
    bash-exec-in-/testbed / code-search / edit-outside-allowed-dirs), usage
    update, ``end_turn`` + verbatim usage, and clean exit on
    ``session/close``.

  The permission ``toolCall`` frames use the **WP0-captured real wire
  shape** (``notes/acp-probe-handshake2.frames.jsonl`` + ACP schema v1):
  no top-level ``name`` field; the tool name arrives in
  ``_meta["cognition.ai/inferenceToolName"]`` and as the ``toolCallId``
  prefix; ``kind`` is a spec ``ToolKind`` — web is ``fetch``
  ("retrieving external data"), not a "web" kind.
  - ``hang``: answers initialize/session-new, then stalls the prompt
    forever (stall → cancel + kill test).
  - ``malformed``: emits a non-JSON line in the middle of the prompt
    stream (typed-error test).
  - ``short``: emits a truncated JSON line with no newline, then exits
    (short-read typed-error test).
  - ``crash``: exits immediately on ``session/prompt`` (EOF typed-error).
  - ``refusal``: prompt responds ``stopReason: "refusal"`` (no usage).
  - ``bad_new``: ``session/new`` returns a result without ``sessionId``.
  - ``rpc_error``: ``initialize`` is answered with a JSON-RPC error object
    (``AcpRpcError`` path).
  - ``init_hang``: never answers ``initialize`` (handshake timeout path).
  - ``bigframe``: prompt stream includes a ~300 KB ``session/update``
    (multi-chunk read path) and a permission request with an **empty**
    options list (fail-closed path).

* ``mock-devin-<mode>`` — a POSIX wrapper that the client spawns as its
  ``devin`` binary (the trailing ``acp``/``--model`` args are ignored);
  it execs the mock with the mode + state path baked in.

The mock appends an event file (JSON lines) that tests assert against:
env facts (e.g. ``DEVIN_REFUSAL_FALLBACK``), the ``session/new`` cwd, the
prompt text as received (framing round-trip proof), every
``session/request_permission`` response, and cancel/close events.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

MOCK_SOURCE = r'''
import json, os, select, sys

mode = sys.argv[1]
state_path = sys.argv[2]

def state(event, **kw):
    with open(state_path, "a") as f:
        f.write(json.dumps({"event": event, **kw}) + "\n")

def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

# env facts: was the refusal-fallback var present in the child env (must be
# stripped by the client), and is a caller-supplied marker var preserved
# (env overrides must survive — the adapter injects PATH/CONDA_DEFAULT_ENV)?
state("started", pid=os.getpid(),
      devin_refusal_fallback=os.environ.get("DEVIN_REFUSAL_FALLBACK", "ABSENT"),
      bench_env_marker=os.environ.get("BENCH_ENV_MARKER", "ABSENT"))

# Unsolicited startup vendor notifications (WP0: real `devin acp` emits
# _cognition.ai/mcp/serversChanged at boot, before any client request).
for _ in range(2):
    send({"jsonrpc": "2.0", "method": "_cognition.ai/mcp/serversChanged",
          "params": {}})

pending = {}          # permission request ids awaiting the client's answer
rid_prompt = None

OPTIONS = [{"optionId": "proceed", "name": "Proceed", "kind": "proceed_once"},
          {"optionId": "deny", "name": "Deny", "kind": "reject"}]

def emit_permission(rid, tool_call, options=OPTIONS):
    pending[rid] = True
    send({"jsonrpc": "2.0", "id": rid,
          "method": "session/request_permission",
          "params": {"sessionId": "mock-sid", "toolCall": tool_call,
                    "options": options}})

def tc(name, kind, raw):
    # WP0-captured real wire shape (no top-level "name" field; name via
    # _meta + toolCallId prefix; kind is a spec ToolKind).
    return {"toolCallId": name + ":0#mock", "title": name, "kind": kind,
            "rawInput": raw,
            "_meta": {"cognition.ai/inferenceToolName": name}}

def finish_prompt():
    # interleaved vendor notification, then the prompt response
    send({"jsonrpc": "2.0", "method": "_cognition.ai/turn_stats",
          "params": {"turns": 1}})
    send({"jsonrpc": "2.0", "id": rid_prompt,
          "result": {"stopReason": "end_turn",
                    "usage": {"inputTokens": 11996, "outputTokens": 30,
                             "cachedReadTokens": 128, "totalTokens": 12026}}})

def handle(obj):
    global rid_prompt
    m = obj.get("method")
    rid = obj.get("id")
    if m == "initialize":
        if mode == "init_hang":
            return  # never answer — handshake timeout path
        if mode == "rpc_error":
            send({"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32600, "message": "Invalid request (mock)"}})
            return
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True,
                                 "sessionCapabilities": {"close": {}}},
            "agentInfo": {"name": "mock-devin", "version": "0.0.0-mock"}}})
        return
    if m == "session/new":
        state("session_new", cwd=(obj.get("params") or {}).get("cwd"))
        if mode == "bad_new":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
            return
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "mock-sid"}})
        return
    if m == "session/prompt":
        rid_prompt = rid
        text = "".join(
            b.get("text", "")
            for b in (obj.get("params") or {}).get("prompt", [])
            if isinstance(b, dict) and b.get("type") == "text")
        state("prompt", text=text)
        if mode == "ok":
            # echo the prompt text back — proves the multi-line string
            # survived the NDJSON round-trip in both directions.
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "mock-sid",
                            "update": {"sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": text}}}})
            # interleaved vendor notification (log + ignore contract)
            send({"jsonrpc": "2.0",
                  "method": "_cognition.ai/mcp/serversChanged", "params": {}})
            # web tool: kind "fetch" (ACP ToolKind "retrieving external
            # data") — the real wire has no "web" kind.
            emit_permission(100, tc("web_fetch", "fetch",
                                   {"url": "https://example.com"}))
            emit_permission(101, tc("bash", "execute",
                                   {"command": "pytest -q",
                                    "cwd": "/testbed"}))
            emit_permission(102, tc("search", "search",
                                   {"pattern": "def foo"}))
            emit_permission(103, tc("write", "edit",
                                   {"file_path": "/etc/evil.txt"}))
        elif mode == "hang":
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "mock-sid",
                            "update": {"sessionUpdate": "agent_thought_chunk"}}})
            # then: never answer the prompt (stall).
        elif mode == "refusal":
            send({"jsonrpc": "2.0", "id": rid,
                 "result": {"stopReason": "refusal"}})
        elif mode == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        elif mode == "short":
            sys.stdout.write('{"jsonrpc": "2.0", "id": 99, "res')  # no \n
            sys.stdout.flush()
            sys.exit(0)
        elif mode == "crash":
            sys.exit(0)
        elif mode == "bigframe":
            # ~300 KB update — larger than the client's 64 KB read chunks.
            big = "B" * 300000
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "mock-sid",
                            "update": {"sessionUpdate": "agent_message_chunk",
                                      "content": {"type": "text", "text": big}}}})
            # permission request with an EMPTY options list — the client
            # must fail closed ("cancelled") rather than guess an option.
            emit_permission(103, tc("write", "edit",
                                   {"file_path": "/tmp/x.txt"}),
                           options=[])
        return
    if m == "session/cancel":
        state("cancel")
        return
    if m == "session/close":
        state("close")
        if mode in ("ok", "refusal", "bigframe"):
            sys.exit(0)
        return
    if m is None and rid in pending:
        # ``m is None``: a response, not a request — a client request
        # whose id collides with a pending permission id (100–103) and
        # isn't matched by an earlier method branch must get its own
        # answer, not be consumed as the permission answer (round 4
        # [nit]: the same guard the WP2 mock gained in round 3).
        pending.pop(rid)
        state("permission_response", id=rid, result=obj.get("result"))
        if not pending:
            # all permission requests answered -> finish the prompt turn
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "mock-sid",
                            "update": {"sessionUpdate": "usage_update",
                                      "used": 12026, "size": 262000}}})
            finish_prompt()
        return
    if m is not None and rid is not None:
        # Unknown method with an id: the real server answers with a
        # JSON-RPC error, it does not crash / drop the request (WP0
        # negative control; parity with the WP2 mock — round 5 [minor]).
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601,
                        "message": "Method not found"}})
        # (WP0 capture verbatim: the real server's message carries no
        # method-name suffix — round 6 [nit])
        return
    # Unknown notification (no id): ignore — vendor/unknown
    # notifications never block (WP0 contract).

buf = b""
while True:
    r, _, _ = select.select([sys.stdin.buffer], [], [], 30)
    if not r:
        continue
    chunk = sys.stdin.buffer.read1(4096)
    if not chunk:
        break
    buf += chunk
    # buffer until a full line — a client frame larger than one read1()
    # chunk (or split across reads) must not be mangled.
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        # A blank line is not a message — but the WP0 capture shows the
        # real server answers a blank separator line with a -32700
        # parse error (data.line == "") rather than skipping it, so
        # blank lines take the same parse-error path (round 7
        # [minor]: capture fidelity).
        try:
            handle(json.loads(line))
        except json.JSONDecodeError:
            # WP0 negative control: the real server answers a non-JSON
            # line with a parse error (id null, data.line echoing the
            # line) and keeps serving (round 6 [minor]: parity with the
            # WP2 mock, which sends the same frame).
            bad = line[:200].decode("utf-8", "replace")
            state("bad_line", line=bad)
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "Parse error",
                           "data": {"line": bad}}})
'''


def make_mock(root, mode: str):
    """Create the mock ``devin`` binary + state file under ``root``.

    Returns ``(wrapper_path, state_path)`` — pass ``wrapper_path`` as the
    session's ``devin_bin``.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    mock = root / "mock_devin.py"
    state = root / f"state-{mode}.jsonl"
    wrapper = root / f"mock-devin-{mode}"
    mock.write_text(MOCK_SOURCE)
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{mock}\" {mode} \"{state}\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                  | stat.S_IXOTH)
    return str(wrapper), str(state)


def read_state(state_path) -> list:
    """Read the mock's event file (JSON lines, stream order)."""
    out = []
    p = Path(state_path)
    if not p.exists():
        return out
    with p.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def living_mock_pids():
    """Process-table check: pids of any surviving mock processes.

    Returns ``None`` when the process table is not inspectable (no
    ``/proc``) so callers can fall back to the group-alive check.

    Ancestor processes are excluded from the match: a shell wrapper
    launched as ``bash -c "… mock_devin.py …"`` (e.g. a bench runner
    whose own command line names the mock) is not a leaked agent.
    """
    if not os.path.isdir("/proc"):
        return None

    def _ancestors() -> set:
        out = {os.getpid()}
        p = os.getpid()
        while p > 1:
            try:
                with open(f"/proc/{p}/stat") as f:
                    p = int(f.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                break
            out.add(p)
        return out

    skip = _ancestors()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if b"mock_devin.py" in cmd:
            pids.append(pid)
    return pids
