"""WP1 (issue #11) — ``acp_client`` NDJSON JSON-RPC 2.0 client core tests.

All tests run against the in-process mock server (``mock_server.py``):
zero quota, no real ``devin`` binary. Acceptance mapping (issue #11):

* NDJSON framing round-trip incl. multi-line string escaping + interleaved
  vendor notifications → :func:`test_happy_path_framing_and_transcript`
* happy-path transcript → :func:`test_happy_path_framing_and_transcript`
* wire order initialize → session/new (cwd=/testbed) → session/prompt →
  stream session/update → session/cancel → session/close before exit →
  happy path + :func:`test_stall_timeout_cancel_kill_no_orphans`
* permission policy (web denied / exec-in-workdir allowed /
  edit-outside denied) → :func:`test_permission_policy_applied_via_wire`
  + :func:`test_permission_policy_unit`
* vendor notifications logged & ignored, never an error → happy path
* timeout → cancel + kill group, no orphans (process table) →
  :func:`test_stall_timeout_cancel_kill_no_orphans`
* malformed/short read → typed error, never hangs →
  :func:`test_malformed_frame_typed_error_no_hang`,
  :func:`test_short_read_typed_error`, :func:`test_process_death_typed_error`
* refusal-fallback isolation + terminal refusal →
  :func:`test_refusal_is_terminal_and_fallback_env_isolated`
"""

from __future__ import annotations

import json
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

    # Happy-path transcript: expected notification sequence, stream order.
    kinds = [(e.kind, e.method) for e in s.transcript]
    assert kinds == [
        ("update", "agent_message_chunk"),
        ("permission", "web_fetch"),
        ("permission", "bash"),
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
    # web_fetch tool request -> denied
    assert out[100] == ("selected", "deny")
    # shell execution inside /testbed (the session cwd) -> allowed
    assert out[101] == ("selected", "proceed")
    # edit outside /testbed + /tmp -> denied
    assert out[102] == ("selected", "deny")
    _assert_no_orphans(s)


def test_permission_policy_unit():
    p = ac.SwebenchPolicy()  # workdir=/testbed, extra=/tmp
    options = [{"optionId": "proceed"}, {"optionId": "deny"}]

    def decide(name, kind, raw):
        return p.decide(
            {"toolCall": {"name": name, "kind": kind, "rawInput": raw},
             "options": options}
        )

    assert decide("web_fetch", "web", {}).allow is False
    assert decide("search_web", "", {}).allow is False
    assert decide("fetch", "web", {}).allow is False
    assert decide("read_file", "read", {"path": "/etc/hostname"}).allow is True
    assert decide("bash", "execute", {"command": "pytest -q"}).allow is True
    assert decide("write", "edit", {"file_path": "/testbed/src/app.py"}).allow is True
    assert decide("write", "edit", {"file_path": "/tmp/scratch.txt"}).allow is True
    assert decide("write", "edit", {"file_path": "/etc/passwd"}).allow is False
    assert decide("write", "edit", {"file_path": "/evil.txt"}).allow is False
    # prefix-trick path must not match the allow dir
    assert decide("write", "edit", {"file_path": "/testbed-evil/x"}).allow is False
    # default deny
    assert decide("mystery_tool", "weird", {}).allow is False
    # option mapping: allow -> proceed option, deny -> deny option
    d = decide("bash", "execute", {})
    assert d.option_id == "proceed"
    d = decide("web_fetch", "web", {})
    assert d.option_id == "deny"


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

    # the client sent session/cancel before killing the group
    assert any(e["event"] == "cancel" for e in read_state(state))
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
        env={"DEVIN_REFUSAL_FALLBACK": "swe-1-6"},  # must be stripped
    )
    try:
        s.initialize()
        started = [e for e in read_state(state) if e["event"] == "started"][0]
        # the client env never contains DEVIN_REFUSAL_FALLBACK
        assert started["devin_refusal_fallback"] == "ABSENT"

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


# ---------------------------------------------------------------------------
# 6. transport framing invariant (unit, no process)
# ---------------------------------------------------------------------------

def test_transport_send_is_single_line_for_multiline_strings():
    class _In:
        def __init__(self):
            self.buf = b""

        def write(self, b):
            self.buf += b

        def flush(self):
            pass

    stdin = _In()
    proc = type("_Proc", (), {"stdin": stdin})()
    tx = ac._Ndjrpc(proc)
    text = "line1\nline2\ttabbed\rwindows"
    tx.send_request(
        "session/prompt", {"sessionId": "s", "prompt": [{"type": "text", "text": text}]}
    )
    data = stdin.buf
    assert data.endswith(b"\n")
    body = data[:-1]
    # one physical line: no raw newlines/CRs inside the frame
    assert b"\n" not in body and b"\r" not in body
    msg = json.loads(body)
    assert msg["jsonrpc"] == "2.0"
    assert msg["method"] == "session/prompt"
    assert msg["params"]["prompt"][0]["text"] == text  # escapes round-trip
