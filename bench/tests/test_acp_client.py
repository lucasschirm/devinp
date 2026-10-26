"""WP1 (issue #11) — ``acp_client`` NDJSON JSON-RPC 2.0 client core tests,
and WP1b (issue #12) — ACP permission policy + refusal-fallback isolation.

All tests run against the in-process mock server (``mock_server.py``):
zero quota, no real ``devin`` binary. Acceptance mapping (issue #11):

* NDJSON framing round-trip incl. multi-line string escaping + interleaved
  vendor notifications → :func:`test_happy_path_framing_and_transcript`
* happy-path transcript → :func:`test_happy_path_framing_and_transcript`
* wire order initialize → session/new (cwd=/testbed) → session/prompt →
  stream session/update → session/cancel → session/close before exit →
  happy path + :func:`test_stall_timeout_cancel_kill_no_orphans`
* timeout → cancel + kill group, no orphans (process table) →
  :func:`test_stall_timeout_cancel_kill_no_orphans`
* malformed/short read → typed error, never hangs →
  :func:`test_malformed_frame_typed_error_no_hang`,
  :func:`test_short_read_typed_error`, :func:`test_process_death_typed_error`
* review round 1 (Devin/swe-2) follow-ups: server error responses
  (:func:`test_server_error_response_is_typed`), bounded handshake
  (:func:`test_initialize_timeout_is_bounded`), >64 KB frame round-trip
  + empty-options fail-closed (:func:`test_large_frame_roundtrip_and_empty_options`),
  close-without-start / start-after-close lifecycle guards,
  transport bounded-write + typed write errors

Acceptance mapping (issue #12 — WP1b):

* permission policy — allow read (incl. code search); edit/write + shell
  exec (exec/terminal) inside ``/testbed`` + ``/tmp`` (the agent must run
  pytest) → :func:`test_permission_policy_acceptance_matrix` +
  :func:`test_permission_policy_applied_via_wire`
* deny web-search/fetch tools; deny writes outside ``/testbed``+``/tmp``
  → :func:`test_permission_policy_acceptance_matrix` +
  :func:`test_permission_policy_applied_via_wire`
* env-isolation assertion (``DEVIN_REFUSAL_FALLBACK`` stripped, caller env
  overrides preserved) + refusal → terminal error →
  :func:`test_refusal_is_terminal_and_fallback_env_isolated`
* review round 1 (Devin/swe-2) follow-ups: real-wire toolCall shape
  (no top-level ``name``; name via ``_meta``/``toolCallId``; spec
  ``ToolKind`` ``fetch`` = web, incl. the in-scope-path bypass regression)
  → :func:`test_permission_policy_acceptance_matrix` +
  :func:`test_permission_policy_applied_via_wire` (mock emits the
  WP0-captured shape), ACP ``PermissionOptionKind`` exact matching →
  :func:`test_option_kinds_matched_exactly`, ``--refusal-fallback=x``
  form → :func:`test_refusal_fallback_cli_flag_refused`, lexical-check
  hardening (``~``/``$VAR``, relative paths, ``locations``/``content``
  path blocks) → :func:`test_permission_policy_acceptance_matrix`
"""

from __future__ import annotations

import json
import os
import select
import time

import pytest

import acp_client as ac
from mock_server import living_mock_pids, make_mock, read_state


def _session(root, mode: str, **kw):
    wrapper, state = make_mock(root, mode)
    s = ac.AcpSession(devin_bin=wrapper, model="swe-2-high", **kw)
    s.start()
    return s, state


def _assert_no_orphans(s: ac.AcpSession) -> None:
    """No orphan processes: group check + process-table check."""
    assert not s.group_alive(), "agent process group still alive"
    pids = living_mock_pids()
    if pids is not None:
        assert pids == [], f"orphan mock processes: {pids}"


# ---------------------------------------------------------------------------
# 1. happy path: framing round-trip + transcript + vendor handling + close
# ---------------------------------------------------------------------------

def test_happy_path_framing_and_transcript(tmp_path):
    text = (
        "Fix the bug.\n"
        "Line two with\ttab, 'quotes', \"quotes\", ünïcode — and a "
        "second\nnew line."
    )
    s, state = _session(tmp_path, "ok")
    try:
        s.initialize()
        assert s.agent_info["protocolVersion"] == 1

        sid = s.create_session(cwd="/testbed")
        assert sid == "mock-sid"

        result = s.prompt(text, timeout=20)
        s.close()
    finally:
        s.close()  # idempotent — double close must be safe

    evs = read_state(state)
    by_event = {}
    for e in evs:
        by_event.setdefault(e["event"], []).append(e)

    # NDJSON framing round-trip: the mock is a strict line-oriented JSON
    # reader — it received the multi-line prompt text intact, i.e. the
    # client frame was a single, valid JSON line (escaping preserved the
    # embedded newlines).
    assert by_event["prompt"][0]["text"] == text

    # The mock echoed the text back inside a streamed update; the client
    # decoded it to the exact original string (both directions proven).
    echo = s.transcript[0].payload["update"]["content"]["text"]
    assert echo == text

    # Happy-path transcript: expected notification sequence, stream order
    # (WP1b: web_fetch denied, bash-in-/testbed allowed, code search
    # allowed, edit-outside-allowed-dirs denied).
    kinds = [(e.kind, e.method) for e in s.transcript]
    assert kinds == [
        ("update", "agent_message_chunk"),
        ("permission", "web_fetch"),
        ("permission", "bash"),
        ("permission", "search"),
        ("permission", "write"),
        ("update", "usage_update"),
    ]

    # stop reason + usage captured verbatim (WP0 §1 fields).
    assert result.stop_reason == "end_turn"
    assert result.usage == {
        "inputTokens": 11996, "outputTokens": 30,
        "cachedReadTokens": 128, "totalTokens": 12026,
    }
    assert result.updates >= 1

    # session/new (cwd=/testbed) reached the server.
    assert by_event["session_new"][0]["cwd"] == "/testbed"

    # Vendor notifications: interleaved + startup ones received, logged,
    # ignored — never an error (the session completed normally).
    assert len(s.vendor_notifications) >= 3  # 2 startup + 1 mid-stream
    assert all(v["method"].startswith("_cognition.ai/")
               for v in s.vendor_notifications)

    # session/close before process exit: the last event the mock saw was
    # close, and the mock exited cleanly on it.
    assert evs[-1]["event"] == "close"
    assert s.returncode == 0

    # no orphans on the clean exit path.
    _assert_no_orphans(s)


# ---------------------------------------------------------------------------
# 2. permission policy (PLAN.md §3.7) — via the wire + unit
# ---------------------------------------------------------------------------

def _permission_outcomes(state) -> dict:
    out = {}
    for e in read_state(state):
        if e["event"] == "permission_response":
            o = e["result"]["outcome"]
            out[e["id"]] = (o.get("outcome"), o.get("optionId"))
    return out


def test_permission_policy_applied_via_wire(tmp_path):
    s, state = _session(tmp_path, "ok")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        s.prompt("task", timeout=20)
        s.close()
    finally:
        s.close()
    out = _permission_outcomes(state)
    # the mock emits the WP0-captured real wire shape (no top-level
    # ``name``; spec ToolKind — web is kind "fetch")
    # web_fetch tool request (kind "fetch") -> denied
    assert out[100] == ("selected", "deny")
    # shell execution with an in-scope declared cwd (/testbed) -> allowed
    # (the agent must be able to run pytest, WP1b AC1)
    assert out[101] == ("selected", "proceed")
    # code search (ACP kind "search" — a read, NOT a web tool) -> allowed
    assert out[102] == ("selected", "proceed")
    # edit outside /testbed + /tmp -> denied (WP1b AC2)
    assert out[103] == ("selected", "deny")
    _assert_no_orphans(s)


def test_permission_policy_acceptance_matrix():
    """Issue #12 AC — table-driven policy matrix (unit).

    * **Allow:** read (unscoped, incl. code search); edit/write inside
      ``/testbed`` + ``/tmp``; shell execution (exec/terminal) — so the
      agent can reproduce bugs and run pytest.
    * **Deny:** web-search/fetch tools; writes outside ``/testbed``+``/tmp``;
      out-of-scope exec cwd; write tools with no declared path (fail
      closed); mislabelled write tools (kind wins over name); anything
      else (default deny).
    """
    p = ac.SwebenchPolicy()  # workdir=/testbed, extra=/tmp
    options = [{"optionId": "proceed", "name": "Proceed"},
              {"optionId": "deny", "name": "Deny"}]

    def decide(name, kind, raw):
        return p.decide(
            {"toolCall": {"name": name, "kind": kind, "rawInput": raw},
             "options": options}
        ).allow

    def decide_tool(tool):
        """Full toolCall dict — the real wire shape (no top-level name)."""
        return p.decide({"toolCall": tool, "options": options}).allow

    cases = [
        # AC1 — allow read (unscoped)
        ("read", "read", {}, True),
        ("read_file", "read", {"path": "/etc/hostname"}, True),
        ("search", "search", {"pattern": "def foo"}, True),  # code search = read
        ("grep", "", {"pattern": "x"}, True),
        # AC1 — allow edit/write inside /testbed + /tmp
        ("write", "edit", {"file_path": "/testbed/src/app.py"}, True),
        ("write", "edit", {"file_path": "/testbed"}, True),
        ("write", "edit", {"filePath": "/tmp/scratch.txt"}, True),
        ("write", "edit", {"file_path": "/testbed/sub/dir"}, True),
        # AC1 — allow shell execution (exec/terminal): run pytest
        ("bash", "execute", {"command": "pytest -q"}, True),
        ("bash", "execute", {"command": "pytest -q", "cwd": "/testbed"}, True),
        ("bash", "execute", {"command": "ls", "cwd": "/testbed/src"}, True),
        ("bash", "execute", {"command": "make test", "cwd": "/tmp"}, True),
        ("terminal", "", {"command": "pytest"}, True),
        ("run_command", "", {"command": "python -m pytest"}, True),
        # AC1/AC2 — exec cwd scoping: out-of-scope declared cwd denied
        ("bash", "execute", {"command": "rm -rf /", "cwd": "/etc"}, False),
        ("bash", "execute", {"command": "touch x", "cwd": "/root"}, False),
        ("bash", "execute", {"command": "ls", "cwd": "../../etc"}, False),
        ("bash", "execute", {"command": "ls", "cwd": "src"}, True),  # rel -> workdir
        ("bash", "execute", {"command": "ls", "workingDirectory": "/etc"}, False),
        ("bash", "execute", {"command": "ls",
                             "cwd": "/testbed", "workingDirectory": "/etc"},
         False),  # dual declaration: the out-of-scope key is checked too
        ("bash", "execute", {"command": "ls", "cwd": "~"}, False),  # ~ expands
        ("bash", "execute", {"command": "ls", "cwd": "$HOME"}, False),  # $VAR expands
        # AC2 — deny web-search/fetch tools (name markers)
        ("web_fetch", "web", {"url": "https://example.com"}, False),
        ("web_search", "web", {"query": "swe-bench"}, False),
        ("search_web", "", {}, False),
        ("fetch", "web", {}, False),
        ("http_get", "", {"url": "https://example.com"}, False),
        # AC2 — deny writes outside /testbed + /tmp
        ("write", "edit", {"file_path": "/etc/passwd"}, False),
        ("write", "edit", {"file_path": "/evil.txt"}, False),
        ("write", "edit", {"file_path": "/testbed-evil/x"}, False),  # prefix trick
        ("write", "edit", {"file_path": "/testbed/a", "path": "/etc/b"}, False),
        ("write", "edit", {"file_path": "src/app.py"}, True),  # rel -> workdir
        ("write", "edit", {"file_path": "../../etc/x"}, False),  # rel escapes
        # fail-closed + default deny
        ("write", "edit", {}, False),            # write, no declared path
        ("list", "edit", {}, False),            # mislabel: kind wins, no path
        ("readme_tool", "edit", {"file_path": "/testbed/x"}, True),  # mislabel: path checked
        ("list_sessions", "", {}, False),
        ("mystery_tool", "weird", {}, False),
    ]
    for name, kind, raw, want in cases:
        got = decide(name, kind, raw)
        assert got is want, f"{name!r} (kind={kind!r}, raw={raw!r}): want {want}, got {got}"

    # Review round 1 (Devin/swe-2): real wire shape (WP0 capture — no
    # top-level ``name``; name via _meta / toolCallId prefix; kind is a
    # spec ToolKind — "fetch" = "retrieving external data", not "web").
    tool_cases = [
        # kind "fetch" = web — the in-scope declared path must NOT rescue it
        ({"kind": "fetch", "rawInput": {"url": "https://example.com",
                                        "path": "/tmp/out"}}, False),
        ({"kind": "fetch", "rawInput": {"url": "https://example.com"}}, False),
        # name via _meta (real devin wire), kind "other"
        ({"toolCallId": "web_fetch:0#x", "kind": "other", "rawInput": {},
          "_meta": {"cognition.ai/inferenceToolName": "web_fetch"}}, False),
        # contradicting sources: _meta wins over the toolCallId prefix
        ({"toolCallId": "search:0#x", "kind": "other", "rawInput": {},
          "_meta": {"cognition.ai/inferenceToolName": "web_fetch"}}, False),
        # empty toolCallId prefix falls through to the spec name field
        ({"toolCallId": ":x", "name": "bash", "kind": ""}, True),
        # non-list locations is type-guarded (no crash) — fail closed
        ({"kind": "edit", "rawInput": {}, "locations": 5}, False),
        ({"kind": "edit", "rawInput": {}, "content": 5}, False),
        # name via toolCallId prefix (write class, no kind → name fallback)
        ({"toolCallId": "write:0#x", "kind": "",
          "rawInput": {"file_path": "/testbed/a"}}, True),
        ({"toolCallId": "write:0#x", "kind": "",
          "rawInput": {"file_path": "/etc/a"}}, False),
        # spec write kinds: delete / move — path-scoped
        ({"kind": "delete", "rawInput": {"file_path": "/testbed/x"}}, True),
        ({"kind": "move", "rawInput": {"file_path": "/etc/x"}}, False),
        # locations[].path / content[].path — WP0-captured path carriers
        ({"kind": "edit", "rawInput": {},
          "locations": [{"path": "/etc/x"}]}, False),
        ({"kind": "edit", "rawInput": {},
          "locations": [{"path": "/testbed/x"}]}, True),
        ({"kind": "edit", "rawInput": {"file_path": "/testbed/a"},
          "content": [{"type": "diff", "path": "/etc/b"}]}, False),
    ]
    for tool, want in tool_cases:
        got = decide_tool(tool)
        assert got is want, f"tool={tool!r}: want {want}, got {got}"

    # option mapping: allow -> proceed option, deny -> deny option
    d = p.decide({"toolCall": {"name": "bash", "kind": "execute",
                              "rawInput": {}}, "options": options})
    assert d.allow is True and d.option_id == "proceed"
    d = p.decide({"toolCall": {"name": "web_fetch", "kind": "web",
                              "rawInput": {}}, "options": options})
    assert d.allow is False and d.option_id == "deny"


def test_option_kinds_matched_exactly():
    """Review round 1 (Devin/swe-2): the ACP-spec PermissionOptionKind
    values (allow_once/allow_always/reject_once/reject_always) are
    matched **exactly**, so a substring in an option id ("ok" in
    "token") or name can never mis-select."""
    p = ac.SwebenchPolicy()
    acao = [{"optionId": "allow_once", "kind": "allow_once",
             "name": "Allow once"},
            {"optionId": "reject_once", "kind": "reject_once",
             "name": "Reject once"}]
    d = p.decide({"toolCall": {"name": "bash", "kind": "execute",
                              "rawInput": {}}, "options": acao})
    assert d.allow is True and d.option_id == "allow_once"
    d = p.decide({"toolCall": {"name": "web_fetch", "kind": "fetch",
                              "rawInput": {}}, "options": acao})
    assert d.allow is False and d.option_id == "reject_once"
    # adversarial: id/name substrings contradict the kind — the kind
    # (the spec semantic) must win
    trap = [{"optionId": "proceed_ok", "kind": "reject_once",
             "name": "No"},
            {"optionId": "deny_no", "kind": "allow_once",
             "name": "Yes"}]
    d = p.decide({"toolCall": {"name": "bash", "kind": "execute",
                              "rawInput": {}}, "options": trap})
    assert d.option_id == "deny_no"  # kind allow_once, despite the id
    d = p.decide({"toolCall": {"name": "web_fetch", "kind": "fetch",
                              "rawInput": {}}, "options": trap})
    assert d.option_id == "proceed_ok"  # kind reject_once, despite the id


# ---------------------------------------------------------------------------
# 3. timeout -> cancel + kill group, no orphans (process-table assertion)
# ---------------------------------------------------------------------------

def test_stall_timeout_cancel_kill_no_orphans(tmp_path):
    s, state = _session(tmp_path, "hang")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        t0 = time.monotonic()
        with pytest.raises(ac.AcpTimeoutError):
            s.prompt("stall", timeout=1.0)
        elapsed = time.monotonic() - t0
        s.close()
    finally:
        s.close()

    # the client sent session/cancel AND session/close (best-effort) before
    # killing the group
    evs = read_state(state)
    assert any(e["event"] == "cancel" for e in evs)
    assert any(e["event"] == "close" for e in evs)
    # bounded — never hangs
    assert elapsed < 15
    # no orphan devin process on the timeout exit path
    _assert_no_orphans(s)


# ---------------------------------------------------------------------------
# 4. malformed / short read / crash -> typed errors, never hang
# ---------------------------------------------------------------------------

def test_malformed_frame_typed_error_no_hang(tmp_path):
    s, state = _session(tmp_path, "malformed")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        t0 = time.monotonic()
        with pytest.raises(ac.AcpMalformedFrameError) as ei:
            s.prompt("x", timeout=10)
        assert time.monotonic() - t0 < 15  # bounded
        s.close()
    finally:
        s.close()
    assert "invalid JSON" in ei.value.reason
    _assert_no_orphans(s)


def test_short_read_typed_error(tmp_path):
    s, state = _session(tmp_path, "short")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        with pytest.raises(ac.AcpMalformedFrameError) as ei:
            s.prompt("x", timeout=10)
        s.close()
    finally:
        s.close()
    assert "EOF mid-line" in str(ei.value)
    _assert_no_orphans(s)


def test_process_death_typed_error(tmp_path):
    s, state = _session(tmp_path, "crash")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        with pytest.raises(ac.AcpTransportError):
            s.prompt("x", timeout=10)
        s.close()
    finally:
        s.close()
    _assert_no_orphans(s)


def test_bad_session_new_protocol_error(tmp_path):
    s, state = _session(tmp_path, "bad_new")
    try:
        s.initialize()
        with pytest.raises(ac.AcpProtocolError):
            s.create_session(cwd="/testbed")
        s.close()
    finally:
        s.close()
    _assert_no_orphans(s)


# ---------------------------------------------------------------------------
# 5. refusal-fallback isolation + terminal refusal
# ---------------------------------------------------------------------------

def test_refusal_is_terminal_and_fallback_env_isolated(tmp_path):
    s, state = _session(
        tmp_path, "refusal",
        env={"DEVIN_REFUSAL_FALLBACK": "swe-1-6",  # must be stripped
             "BENCH_ENV_MARKER": "keep-me"},  # caller override must survive
    )
    try:
        s.initialize()
        started = [e for e in read_state(state) if e["event"] == "started"][0]
        # env-isolation assertion: the client env never contains
        # DEVIN_REFUSAL_FALLBACK
        assert started["devin_refusal_fallback"] == "ABSENT"
        # caller env overrides are preserved (the adapter injects
        # PATH/CONDA_DEFAULT_ENV this way, PLAN §3.5)
        assert started["bench_env_marker"] == "keep-me"

        s.create_session(cwd="/testbed")
        with pytest.raises(ac.AcpRefusalError) as ei:
            s.prompt("x", timeout=20)
        assert ei.value.stop_reason == "refusal"
        # terminal: no retry, no model switch — the session refuses
        # further prompts.
        with pytest.raises(ac.AcpProtocolError):
            s.prompt("again", timeout=20)
        s.close()
    finally:
        s.close()
    _assert_no_orphans(s)


def test_refusal_fallback_cli_flag_refused(tmp_path):
    """PLAN §3.4: the ``--refusal-fallback`` CLI flag is refused too —
    a refusal must be terminal, never a mid-run model switch. Both the
    space-separated and ``=`` forms (review round 1, Devin/swe-2)."""
    for args in (["--refusal-fallback", "swe-1-6"],
                 ["--refusal-fallback=swe-1-6"]):
        wrapper, _ = make_mock(tmp_path, "ok")
        s = ac.AcpSession(devin_bin=wrapper, model="swe-2-high",
                         extra_args=args)
        with pytest.raises(ac.AcpProtocolError):
            s.start()
        s.close()  # no process was spawned — close must be a safe no-op


# ---------------------------------------------------------------------------
# 6. transport framing invariant (unit, no process)
# ---------------------------------------------------------------------------

def test_transport_send_is_single_line_for_multiline_strings():
    r_fd, w_fd = os.pipe()
    stdin = type("_In", (), {"fileno": lambda self: w_fd})()
    proc = type("_Proc", (), {"stdin": stdin})()
    tx = ac._Ndjrpc(proc)
    text = "line1\nline2\ttabbed\rwindows"
    tx.send_request(
        "session/prompt", {"sessionId": "s", "prompt": [{"type": "text", "text": text}]}
    )
    # drain the pipe (the frame is small — one write, fully buffered)
    data = b""
    while True:
        ready, _, _ = select.select([r_fd], [], [], 1.0)
        if not ready:
            break
        data += os.read(r_fd, 1 << 20)
    os.close(r_fd)
    os.close(w_fd)
    assert data.endswith(b"\n")
    body = data[:-1]
    # one physical line: no raw newlines/CRs inside the frame
    assert b"\n" not in body and b"\r" not in body
    msg = json.loads(body)
    assert msg["jsonrpc"] == "2.0"
    assert msg["method"] == "session/prompt"
    assert msg["params"]["prompt"][0]["text"] == text  # escapes round-trip


def test_transport_send_bounded_deadline_and_typed_errors():
    r_fd, w_fd = os.pipe()
    stdin = type("_In", (), {"fileno": lambda self: w_fd})()
    proc = type("_Proc", (), {"stdin": stdin})()
    tx = ac._Ndjrpc(proc)
    # deadline already passed -> typed AcpTimeoutError, never a raw block
    with pytest.raises(ac.AcpTimeoutError):
        tx.send_request("initialize", {}, deadline=time.monotonic() - 1.0)
    # dead pipe -> typed AcpTransportError (no raw BrokenPipeError/OSError)
    os.close(r_fd)
    os.close(w_fd)
    with pytest.raises(ac.AcpTransportError):
        tx.send_request("initialize", {})


def test_large_write_to_non_draining_server_is_bounded():
    """Round-4 B-1: a single blocking os.write() larger than the pipe's
    free space used to block inside the syscall and defeat the deadline.
    With the non-blocking write + EAGAIN-via-select fix, a 100 KB frame
    to a server that never reads stdin must hit the deadline, not hang."""
    r_fd, w_fd = os.pipe()  # nobody reads r_fd
    stdin = type("_In", (), {"fileno": lambda self: w_fd})()
    proc = type("_Proc", (), {"stdin": stdin})()
    tx = ac._Ndjrpc(proc)
    t0 = time.monotonic()
    with pytest.raises(ac.AcpTimeoutError):
        tx.send_request(
            "session/prompt",
            {"prompt": [{"type": "text", "text": "X" * 100000}]},
            deadline=time.monotonic() + 1.0,
        )
    assert time.monotonic() - t0 < 10  # bounded — never hangs
    os.close(r_fd)
    os.close(w_fd)


# ---------------------------------------------------------------------------
# 7. review round 1 (Devin/swe-2) follow-ups
# ---------------------------------------------------------------------------

def test_server_error_response_is_typed(tmp_path):
    s, state = _session(tmp_path, "rpc_error")
    try:
        with pytest.raises(ac.AcpRpcError) as ei:
            s.initialize()
        assert ei.value.code == -32600
        s.close()
    finally:
        s.close()
    _assert_no_orphans(s)


def test_initialize_timeout_is_bounded(tmp_path):
    wrapper, state = make_mock(tmp_path, "init_hang")
    s = ac.AcpSession(devin_bin=wrapper, model="swe-2-high",
                     initialize_timeout_s=1.5)
    s.start()
    t0 = time.monotonic()
    with pytest.raises(ac.AcpTimeoutError):
        s.initialize()
    assert time.monotonic() - t0 < 15  # bounded — never hangs
    s.close()
    _assert_no_orphans(s)


def test_large_frame_roundtrip_and_empty_options(tmp_path):
    s, state = _session(tmp_path, "bigframe")
    try:
        s.initialize()
        s.create_session(cwd="/testbed")
        result = s.prompt("x", timeout=20)
        s.close()
    finally:
        s.close()
    # >64 KB frame read across multiple chunks, intact
    chunks = [e for e in s.transcript
             if e.kind == "update" and e.method == "agent_message_chunk"]
    assert len(chunks) == 1
    assert len(chunks[0].payload["update"]["content"]["text"]) == 300000
    assert result.stop_reason == "end_turn"
    # allow decision with an empty options list -> failed closed (cancelled)
    perms = _permission_outcomes(state)
    assert perms[103] == ("cancelled", None)
    _assert_no_orphans(s)


def test_lifecycle_close_before_start_and_start_after_close(tmp_path):
    wrapper, state = make_mock(tmp_path, "ok")
    s = ac.AcpSession(devin_bin=wrapper, model="swe-2-high")
    s.close()  # close without start: safe, must not raise
    # one-shot: a closed session must not respawn (group-leak guard)
    with pytest.raises(ac.AcpProtocolError):
        s.start()
    # a normal session still cleans up
    s2 = ac.AcpSession(devin_bin=wrapper, model="swe-2-high")
    s2.start()
    s2.initialize()
    s2.close()
    _assert_no_orphans(s2)


def test_permission_policy_no_proceed_option_fails_closed():
    p = ac.SwebenchPolicy()
    # allowed tool, but the only option is a deny one -> allow intent,
    # wire fails closed (no option id -> "cancelled")
    d = p.decide({"toolCall": {"name": "bash", "kind": "execute",
                              "rawInput": {}},
                 "options": [{"optionId": "deny"}]})
    assert d.allow is True and d.option_id is None
    # no options at all -> same
    d = p.decide({"toolCall": {"name": "bash", "kind": "execute",
                              "rawInput": {}},
                 "options": []})
    assert d.allow is True and d.option_id is None
    # deny with no deny option also maps to cancelled
    d = p.decide({"toolCall": {"name": "web_fetch", "kind": "web",
                              "rawInput": {}},
                 "options": []})
    assert d.allow is False and d.option_id is None
