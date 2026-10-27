"""WP2 (issue #13) — ``mock_devin`` test double: integration tests.

Every test drives the mock **through the real WP1 ``AcpSession`` client**
(``acp_client.py`` + ``SwebenchPolicy``), over real NDJSON framing with
real subprocess spawn/kill — zero quota, no real ``devin`` binary, CI
safe. The mock is the deterministic signal source for the WP3 runner
(issue #14/#15): the terminal JSONL status, checkpoint/resume semantics
and artifact atomicity (issue #13 AC5/AC6) are the runner's job; what
the mock owns is the per-mode **terminal signal contract** asserted
below (WP2 AC1–AC4).

Acceptance mapping (issue #13):

* AC1 — speaks the WP0 ACP subset over NDJSON; the canned agent reads
  the task, applies a canned patch to the cwd repo, answers its own
  permission flow, emits a stop reason →
  :func:`test_ok_mode_full_pipeline` (+ the raw wire-conformance test
  :func:`test_wire_conformance_rpc_errors`, the WP0-captured server
  behaviors: ``-32601`` for unknown methods, ``-32700`` parse errors for
  non-JSON lines — never a crash)
* AC1 (permission flow) — a denying policy makes the canned agent skip
  the patch; the event file proves the agent *waited* for its own
  permission answer before editing →
  :func:`test_ok_mode_permission_denied_skips_canned_patch`
* AC2 — modes ``ok``/``no_patch``/``hang``/``quota``/``crash`` →
  :func:`test_ok_mode_full_pipeline`,
  :func:`test_no_patch_mode`,
  :func:`test_hang_mode_wallclock_cancel_kill_no_orphans`,
  :func:`test_quota_mode_terminal_signal`,
  :func:`test_crash_mode_terminal_signal`
* AC3 — canned ``devin models list --format json`` fixture with
  ``cost_tier`` values, both Free and paid models (Free is exactly the
  WP0 set; paid ``swe-1-6``/``swe-1-7``/``swe-2-*-lightning``; the
  ``adaptive`` variant with no ``cost_tier`` exercises the WP3
  fail-closed gate) → :func:`test_models_list_fixture`
* AC4 — each failure mode produces the correct **terminal** instance
  status in the JSONL (``no_patch``/``timeout``/``quota_limited``/
  ``agent_error``): asserted here at the signal level — the exact
  observable the WP3 runner maps to that status (the JSONL itself, the
  resume dedup and artifact atomicity are WP3, issue #15) →
  :func:`test_terminal_status_signal_mapping`
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import acp_client as ac
from mock_devin import CANNED_CONTENT, CANNED_FILE, VALID_MODES

MOCK_PATH = str(Path(__file__).resolve().parent.parent / "mock_devin.py")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _wrapper(root, mode: str) -> tuple[str, str]:
    """Materialize a wrapper script (the test's ``devin`` binary) that
    execs a **tmp-dir copy** of ``mock_devin.py`` with the mode +
    state path baked in via env (the extra CLI args stay on the real
    wire). The copy — named ``mock_w2_<mode>.py`` — gives the orphan
    check a unique process-table marker, exactly the WP1 pattern
    (``mock_server.py`` copies its mock into the test's tmp dir): a
    repo-path marker would false-positive on any sibling process that
    merely references the repo file."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    state = root / f"state-{mode}.jsonl"
    mock = root / f"mock_w2_{mode}.py"
    mock.write_text(Path(MOCK_PATH).read_text())
    w = root / f"mock-devin-{mode}"
    w.write_text(
        f"#!/bin/sh\n"
        f"export MOCK_DEVIN_MODE=\"{mode}\"\n"
        f"export MOCK_DEVIN_STATE=\"{state}\"\n"
        f"exec \"{sys.executable}\" \"{mock}\" \"$@\"\n"
    )
    w.chmod(w.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(w), str(state)


def _read_state(state_path: str) -> list:
    p = Path(state_path)
    if not p.exists():
        return []
    out = []
    for line in p.open():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _git_repo(path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _assert_no_orphans(s: ac.AcpSession) -> None:
    """Group check + process-table check for surviving mock processes.

    The marker is the unique tmp-dir copy name (``mock_w2_``) — the
    WP1 pattern — so a sibling process that merely references the repo
    file can never false-positive. Ancestor processes are excluded as
    well: a shell wrapper launched as ``bash -c "… pytest
    …/test_mock_devin.py …"`` carries the test name in its own cmdline.
    """
    assert not s.group_alive(), "agent process group still alive"
    if not os.path.isdir("/proc"):
        return

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
    found = []
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
        if b"mock_w2_" in cmd:
            try:
                with open(f"/proc/{entry}/stat") as f:
                    ppid = f.read().rsplit(")", 1)[1].split()[1]
            except (OSError, ValueError, IndexError):
                ppid = "?"
            found.append(f"pid={pid} ppid={ppid} cmd={cmd[:160]!r}")
    assert found == [], f"orphan mock processes: {found}"


def _session(root, mode: str, cwd: str, **kw):
    w, st = _wrapper(root, mode)
    s = ac.AcpSession(devin_bin=w, model="swe-2-high", cwd=cwd, **kw)
    s.start()
    return s, st


class RejectAllPolicy(ac.PermissionPolicy):
    """Denies everything by selecting the mock's ``reject_once`` option."""

    def decide(self, params):
        for o in (params or {}).get("options") or []:
            if isinstance(o, dict) and str(o.get("kind")).lower() \
                    in ("reject_once", "reject_always"):
                return ac.PermissionDecision(
                    allow=False, option_id=o.get("optionId"),
                    reason="test: reject all")
        return ac.PermissionDecision(allow=False, option_id=None,
                                    reason="test: reject all (cancelled)")


class NoOptionPolicy(ac.PermissionPolicy):
    """Fails closed: no option id -> the client sends ``cancelled`` (the
    mock must treat a cancelled outcome as a denial)."""

    def decide(self, params):
        return ac.PermissionDecision(allow=False, option_id=None,
                                    reason="test: cancelled (fail closed)")


# ---------------------------------------------------------------------------
# AC1 — ok mode: full pipeline, canned patch, own permission flow
# ---------------------------------------------------------------------------

def test_ok_mode_full_pipeline(tmp_path):
    """AC1 — the canned agent speaks the WP0 ACP subset: reads the task,
    applies a canned patch to the cwd repo **only after answering its
    own permission flow**, and emits the stop reason + usage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    s, state = _session(
        tmp_path, "ok", cwd=str(repo),
        policy=ac.SwebenchPolicy(workdir=str(repo)),
    )
    task = (
        "Fix the bug.\n"
        "Line two with\ttab, 'quotes', \"quotes\", ünïcode — and a "
        "second\nnew line."
    )
    try:
        s.initialize()
        # initialize result mirrors the WP0 live capture (notes
        # /acp-surface.md §1): protocolVersion 1 + agentInfo.
        assert s.agent_info["protocolVersion"] == 1
        assert s.agent_info["agentInfo"]["name"] == "mock-devin"

        sid = s.create_session(cwd=str(repo))
        assert sid == "mock-sid"

        t0 = time.monotonic()
        result = s.prompt(task, timeout=30)
        assert time.monotonic() - t0 < 25  # bounded — never hangs
        s.close()
    finally:
        s.close()

    # stop reason + usage captured verbatim (WP0 §1 shape).
    assert result.stop_reason == "end_turn"
    assert result.usage == {
        "inputTokens": 11996, "outputTokens": 30,
        "cachedReadTokens": 128, "totalTokens": 12026,
    }

    # the mock read the task (NDJSON round-trip: the multi-line text
    # arrived intact on one JSON line) and echoed it in a streamed
    # update (decoded intact in the other direction).
    evs = _read_state(state)
    by_event = {}
    for e in evs:
        by_event.setdefault(e["event"], []).append(e)
    assert by_event["prompt"][0]["text"] == task
    echo = [e for e in s.transcript
           if e.kind == "update" and e.method == "agent_message_chunk"]
    assert echo and echo[-1].payload["update"]["content"]["text"] == task

    # the canned patch was applied to the cwd repo — single source of
    # truth (mock_devin.CANNED_CONTENT), and it went through ``git
    # apply`` (the cwd is a git repo, the sweb.eval layout).
    f = repo / CANNED_FILE
    assert f.exists()
    assert f.read_text() == CANNED_CONTENT
    applied = by_event["patch_applied"][0]
    assert applied["via"] == "git-apply"
    assert applied["path"] == str(repo / CANNED_FILE)

    # session/new (cwd) + the model reached the server (the runner's
    # cost-tier gate can assert the requested model).
    assert by_event["session_new"][0]["cwd"] == str(repo)
    started = by_event["started"][0]
    assert started["model"] == "swe-2-high"
    assert started["devin_refusal_fallback"] == "ABSENT"  # client hygiene

    # AC1 "answers its own permission flow": the mock emitted its
    # permission request and the patch was applied **only after** the
    # client's answer arrived (event order in the mock's own log).
    order = [e["event"] for e in evs]
    i_req = order.index("permission_request")
    i_resp = order.index("permission_response")
    i_patch = order.index("patch_applied")
    assert i_req < i_resp < i_patch
    assert by_event["permission_response"][0]["allow"] is True

    # the wire sequence the WP1 client observed: thought + message
    # updates, the permission round-trip, the usage update.
    kinds = [(e.kind, e.method) for e in s.transcript]
    assert kinds == [
        ("update", "agent_thought_chunk"),
        ("update", "agent_message_chunk"),
        ("permission", "write"),
        ("update", "usage_update"),
    ]

    # startup vendor notifications: received, logged, ignored — never an
    # error (the session completed normally).
    assert len(s.vendor_notifications) >= 2
    assert all(v["method"].startswith("_cognition.ai/")
               for v in s.vendor_notifications)

    # session/close before process exit: last event the mock saw, clean
    # exit, no orphans.
    assert evs[-1]["event"] == "close"
    assert s.returncode == 0
    _assert_no_orphans(s)


@pytest.mark.parametrize("policy_name,policy", [
    ("reject", RejectAllPolicy()),
    ("cancelled", NoOptionPolicy()),
])
def test_ok_mode_permission_denied_skips_canned_patch(tmp_path,
                                                    policy_name, policy):
    """AC1 — the canned agent answers its *own* permission flow: a
    denial (``reject_once`` selected) or a failed-closed ``cancelled``
    skips the patch, but the turn still ends legitimately (``end_turn``
    — the no-edit outcome the runner classifies ``no_patch`` from the
    empty diff)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    s, state = _session(tmp_path, "ok", cwd=str(repo), policy=policy)
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        result = s.prompt("task", timeout=30)
        s.close()
    finally:
        s.close()

    assert result.stop_reason == "end_turn"
    assert not (repo / CANNED_FILE).exists()
    evs = _read_state(state)
    skipped = [e for e in evs if e["event"] == "patch_skipped"]
    assert skipped, "a denied permission must record patch_skipped"
    assert not any(e["event"] == "patch_applied" for e in evs)
    # the flow still waited: request -> response (deny) -> skip.
    order = [e["event"] for e in evs]
    assert order.index("permission_request") \
        < order.index("permission_response") < order.index("patch_skipped")
    _assert_no_orphans(s)


# ---------------------------------------------------------------------------
# AC2 — the other modes
# ---------------------------------------------------------------------------

def test_no_patch_mode(tmp_path):
    """AC2 — ``no_patch``: the agent finishes with ``end_turn`` + usage
    but makes **no edits** (a legitimate agent outcome; terminal for
    pass@1, PLAN §2.5)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    s, state = _session(tmp_path, "no_patch", cwd=str(repo),
                       policy=ac.SwebenchPolicy(workdir=str(repo)))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        result = s.prompt("fix it", timeout=30)
        s.close()
    finally:
        s.close()

    assert result.stop_reason == "end_turn"
    assert result.usage is not None  # a completed turn is metered
    assert not (repo / CANNED_FILE).exists()
    evs = _read_state(state)
    # no permission round-trip, no patch — and the task was still read.
    assert not any(e["event"] in ("permission_request", "patch_applied")
                   for e in evs)
    assert any(e["event"] == "prompt" for e in evs)
    assert s.returncode == 0
    _assert_no_orphans(s)


def test_hang_mode_wallclock_cancel_kill_no_orphans(tmp_path):
    """AC2 — ``hang``: the agent stalls mid-prompt; the client's
    wall-clock cap fires (PLAN §2.2) -> ``session/cancel`` + group kill
    (the ``timeout`` terminal signal, PLAN §2.5)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    s, state = _session(tmp_path, "hang", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        t0 = time.monotonic()
        with pytest.raises(ac.AcpTimeoutError):
            s.prompt("stall", timeout=1.0)
        assert time.monotonic() - t0 < 15  # bounded — never hangs
        s.close()
    finally:
        s.close()

    # the client sent cancel + (best-effort) close before the kill; the
    # mock recorded the cancel (it kept reading while stalled).
    evs = _read_state(state)
    assert any(e["event"] == "cancel" for e in evs)
    assert not (repo / CANNED_FILE).exists()  # the agent got no work done
    _assert_no_orphans(s)


def test_quota_mode_terminal_signal(tmp_path):
    """AC2/AC4 — ``quota``: the PLAN §2.3 rate-limit signal in both
    observed forms — the ACP stop reason (``quota_limited``, no usage
    object: the hit lands before any metered work) and the CLI stderr
    note. The WP3 runner maps this to the ``quota_limited`` status and
    stops the batch (PLAN §2.3)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    s, state = _session(tmp_path, "quota", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        result = s.prompt("task", timeout=30)
        s.close()
    finally:
        s.close()

    assert result.stop_reason == "quota_limited"
    assert result.usage is None
    assert "quota" in s.stderr_tail.lower()
    evs = _read_state(state)
    # no patch, no permission round-trip — the hit precedes the work.
    assert not any(e["event"] in ("patch_applied", "permission_request")
                   for e in evs)
    _assert_no_orphans(s)


def test_crash_mode_terminal_signal(tmp_path):
    """AC2/AC4 — ``crash``: mid-prompt process death (stderr dump,
    non-zero exit) -> the client sees EOF (``AcpTransportError``) —
    the ``agent_error`` terminal signal (taxonomy WP5; abort-on-2-
    consecutive is the WP3 runner's policy)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    s, state = _session(tmp_path, "crash", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        with pytest.raises(ac.AcpTransportError):
            s.prompt("task", timeout=30)
        s.close()
    finally:
        s.close()

    # a real crash: non-zero exit + a diagnostic dump on stderr.
    assert s.returncode == 1
    assert "panicked" in s.stderr_tail
    _assert_no_orphans(s)


# ---------------------------------------------------------------------------
# AC3 — the canned `devin models list --format json` fixture
# ---------------------------------------------------------------------------

def test_models_list_fixture(tmp_path):
    """AC3 — the fixture mirrors the WP0-captured shape
    (notes/models-list.json): top-level ``families[]``; ``cost_tier``
    values with **both Free and paid** models (the WP3 cost-tier gate
    is fail-closed on this data); Free is exactly the WP0 set; the
    ``adaptive`` router has no ``cost_tier`` (shape-deviation gate)."""
    w, _ = _wrapper(tmp_path, "ok")
    r = subprocess.run([w, "models", "list", "--format", "json"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)

    assert list(d.keys()) == ["families"]
    vs = [v for f in d["families"] for v in f["variants"]]
    assert all(v.get("model_uid") and v.get("label") for v in vs)
    assert all(k in f for f in d["families"]
               for k in ("family_label", "family_uid", "variants"))

    # Free tier: exactly the WP0 set (notes/acp-surface.md §7).
    free = {v["model_uid"] for v in vs if v.get("cost_tier") == "Free"}
    assert free == {"swe-2-medium", "swe-2-high", "swe-2-max"}
    # Free variants carry no $ rates (WP0 §7: cost_summary empty).
    for v in vs:
        if v.get("cost_tier") == "Free":
            assert not v.get("cost_summary")
    # Paid SWE models — must never be selectable under the free policy
    # (PLAN §2.4): swe-1-6 / swe-1-7 (Low cost) + all *-lightning
    # variants (High cost).
    by_uid = {v["model_uid"]: v for v in vs}
    assert by_uid["swe-1-6"]["cost_tier"] == "Low cost"
    assert by_uid["swe-1-7"]["cost_tier"] == "Low cost"
    for uid in ("swe-2-medium-lightning", "swe-2-high-lightning",
                "swe-2-max-lightning"):
        assert by_uid[uid]["cost_tier"] == "High cost"
    # Round 1 [minor]: the non-SWE family is a verbatim copy of a real
    # captured family (not fabricated), and the Med cost tier (179
    # variants in the real capture) is present.
    assert by_uid["claude-opus-4-6"]["cost_tier"] == "High cost"
    assert by_uid["claude-opus-4-6"]["cost_summary"] == \
        "$5 / 1M Input \u00b7 $0.5 / 1M Cached input \u00b7 $25 / 1M Output"
    assert by_uid["gpt-6-sol-medium"]["cost_tier"] == "Med cost"
    # paid variants carry a cost summary (the $ rates, WP0 §7) — the
    # Med cost branch now executes (gpt-6-sol-medium).
    for v in vs:
        if v.get("cost_tier") in ("Low cost", "High cost", "Med cost"):
            assert v.get("cost_summary")
    # a non-SWE family is present — a gate that keyed on the "swe"
    # prefix instead of cost_tier would miss it.
    assert any(not v["model_uid"].startswith("swe")
               for v in vs if "cost_tier" in v)
    # the adaptive router: no cost_tier — the WP3 fail-closed gate must
    # survive a variant without the field.
    assert "cost_tier" not in by_uid["adaptive"]


# ---------------------------------------------------------------------------
# CLI strictness (fail-fast: adapter/runner mistakes must surface)
# ---------------------------------------------------------------------------

def test_cli_rejects_unknown_mode_and_flags(tmp_path):
    w, _ = _wrapper(tmp_path, "ok")
    # unknown mode
    r = subprocess.run([w, "acp", "--mode", "bogus"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "valid" in r.stderr
    # unknown flag (the real CLI would reject it too)
    r = subprocess.run([w, "acp", "--model", "m", "--bogus"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    # unknown subcommand
    r = subprocess.run([w, "frobnicate"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    # the wrapper's baked-in mode still works after the rejections
    # (a fresh process each time — no state leak between invocations)
    r = subprocess.run([w, "models", "list", "--format", "json"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    # a bad mode via the env path is rejected the same way
    env = dict(os.environ, MOCK_DEVIN_MODE="nope")
    r = subprocess.run([sys.executable, MOCK_PATH, "acp"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 2


def test_models_list_table_default(tmp_path):
    w, _ = _wrapper(tmp_path, "ok")
    r = subprocess.run([w, "models", "list"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "swe-2-high" in r.stdout and "Free" in r.stdout
    # the json flag is required for the machine path (WP3 gate)
    r = subprocess.run([w, "models", "list", "--format", "yaml"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


# ---------------------------------------------------------------------------
# wire conformance — the WP0-captured server behaviors (raw wire)
# ---------------------------------------------------------------------------

def _read_line(p, timeout: float = 10.0) -> str:
    """Read one line, bounded — without the select()-on-buffered-stdout
    trap (bytes already in Python's read buffer pass select() as "not
    ready"). A daemon thread + queue keeps the wait bounded: a mock
    that never responds fails the test in ``timeout`` seconds instead of
    hanging CI."""
    import queue
    q: "queue.Queue" = queue.Queue()

    def r() -> None:
        q.put(p.stdout.readline())

    threading.Thread(target=r, daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError("mock did not produce a line in time")
    assert line, "EOF while reading a mock frame"
    return line.decode().strip()


def test_wire_conformance_rpc_errors(tmp_path):
    """The mock keeps serving through wire errors, exactly like the real
    server in the WP0 negative control: non-JSON line -> ``-32700``
    parse error (not a crash); unknown method with an id -> ``-32601``;
    unknown notifications are ignored (never block)."""
    w, state = _wrapper(tmp_path, "ok")
    p = subprocess.Popen([w, "acp"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # the two startup vendor notifications (WP0: unsolicited at boot)
        n1 = json.loads(_read_line(p))
        n2 = json.loads(_read_line(p))
        for n in (n1, n2):
            assert n["method"] == "_cognition.ai/mcp/serversChanged"
            assert "id" not in n

        # a blank line is tolerated (not a message)
        p.stdin.write(b"\n")
        p.stdin.flush()

        # unknown method with an id -> -32601, the server keeps serving
        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": "bogus/method",
             "params": {}}).encode() + b"\n")
        p.stdin.flush()
        obj = json.loads(_read_line(p))
        assert obj.get("id") == 7
        assert obj["error"]["code"] == -32601

        # non-JSON line -> -32700 parse error, not a crash
        p.stdin.write(b"this is not json\n")
        p.stdin.flush()
        obj = json.loads(_read_line(p))
        assert obj["error"]["code"] == -32700
        assert obj.get("id") is None

        # the server still serves after both errors: initialize answers
        p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 8, "method": "initialize",
             "params": {"protocolVersion": 1,
                       "clientCapabilities": {}}}).encode() + b"\n")
        p.stdin.flush()
        obj = json.loads(_read_line(p))
        assert obj.get("id") == 8
        assert obj["result"]["protocolVersion"] == 1
        assert p.poll() is None  # still alive
    finally:
        p.kill()
        p.wait()
    assert _read_state(state)  # the event file was written


# ---------------------------------------------------------------------------
# AC4 — the terminal signal contract (what the WP3 runner maps to the
# JSONL status; the JSONL itself + resume + artifact atomicity are WP3,
# issue #15)
# ---------------------------------------------------------------------------

def test_terminal_status_signal_mapping(tmp_path):
    """AC4 — each failure mode produces the exact terminal signal the
    WP3 runner maps to the instance status in the JSONL:

    ====================  ==========================  ===============
    mode                  observed terminal signal    JSONL status
    ====================  ==========================  ===============
    ``no_patch``          ``end_turn`` + usage, no    ``no_patch``
                          edits (exit 0)              (terminal)
    ``hang``              ``AcpTimeoutError`` at the  ``timeout``
                          wall-clock cap (cancel +   (terminal)
                          group kill)
    ``quota``             stop reason                 ``quota_limited``
                          ``quota_limited`` (no      (retry class)
                          usage) + stderr note
    ``crash``             ``AcpTransportError`` (EOF) ``agent_error``
                          + exit 1                   (taxonomy WP5)
    ====================  ==========================  ===============

    ``timeout``/``no_patch`` are terminal for pass@1 (never re-run,
    PLAN §2.5); ``quota_limited`` is the only retry class here (plus
    ``eval_error`` — a WP4/harness outcome, not a mock mode).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    # no_patch — terminal: completed turn, no edits.
    s, _ = _session(tmp_path, "no_patch", cwd=str(repo),
                   policy=ac.SwebenchPolicy(workdir=str(repo)))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        r = s.prompt("t", timeout=30)
        s.close()
    finally:
        s.close()
    assert r.stop_reason == "end_turn"
    assert not (repo / CANNED_FILE).exists()
    assert s.returncode == 0
    _assert_no_orphans(s)

    # hang — terminal: wall-clock cap -> cancel + kill.
    s, _ = _session(tmp_path, "hang", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        with pytest.raises(ac.AcpTimeoutError):
            s.prompt("t", timeout=1.0)
        s.close()
    finally:
        s.close()
    _assert_no_orphans(s)

    # quota — retry class: stop reason + stderr, no usage.
    s, _ = _session(tmp_path, "quota", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        r = s.prompt("t", timeout=30)
        s.close()
    finally:
        s.close()
    assert r.stop_reason == "quota_limited"
    assert r.usage is None
    _assert_no_orphans(s)

    # crash — taxonomy agent_error: EOF + non-zero exit.
    s, _ = _session(tmp_path, "crash", cwd=str(repo))
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        with pytest.raises(ac.AcpTransportError):
            s.prompt("t", timeout=30)
        s.close()
    finally:
        s.close()
    assert s.returncode == 1
    _assert_no_orphans(s)


def test_valid_modes_are_the_five_documented_ones():
    assert VALID_MODES == ("ok", "no_patch", "hang", "quota", "crash")


# ---------------------------------------------------------------------------
# review round 1 (Devin/swe-2) follow-ups
# ---------------------------------------------------------------------------

def test_ok_mode_multi_turn_prompt(tmp_path):
    """Round 1 [major]: the WP1 client is multi-turn (its state returns
    to ``session`` after each turn) — a second ``session/prompt`` must
    be answered like the first, not silently swallowed (which would
    burn the client's wall-clock cap into a wrong ``timeout`` signal).
    Turn 2's patch re-apply goes through the direct-write fallback (the
    file already exists, so ``git apply`` of a file-creation patch
    fails) — the turn still completes legitimately."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)

    s, state = _session(
        tmp_path, "ok", cwd=str(repo),
        policy=ac.SwebenchPolicy(workdir=str(repo)),
    )
    try:
        s.initialize()
        s.create_session(cwd=str(repo))
        r1 = s.prompt("turn one", timeout=30)
        r2 = s.prompt("turn two", timeout=30)
        s.close()
    finally:
        s.close()

    assert r1.stop_reason == "end_turn"
    assert r2.stop_reason == "end_turn"  # answered, not swallowed
    evs = _read_state(state)
    applied = [e for e in evs if e["event"] == "patch_applied"]
    assert len(applied) == 2
    assert applied[0]["via"] == "git-apply"
    assert applied[1]["via"] == "direct-write"  # idempotent re-apply
    assert (repo / CANNED_FILE).read_text() == CANNED_CONTENT
    assert s.returncode == 0
    _assert_no_orphans(s)


def test_patch_failure_is_an_event_not_a_crash(tmp_path):
    """Round 1 [minor] + round 2 [minor]: when *both* apply paths
    fail, the mock records ``patch_failed`` and stays alive — the turn
    still completes (``end_turn``), per the documented contract. The
    session cwd is a git repo in which the patch target exists as a
    **committed directory**: ``git apply`` refuses ("Directory not
    empty", rc 128) and ``open(target, "w")`` raises
    ``IsADirectoryError`` (EISDIR) — for *any* uid, including
    root/CAP_DAC_OVERRIDE (a ``0o555`` cwd would not: root bypasses
    permission bits, so that variant failed spuriously in root-run CI
    containers; a bare directory does not either — ``git apply``
    happily clobbers an *empty* dir in a non-repo cwd)."""
    cwd = tmp_path / "blocked"
    cwd.mkdir()
    _git_repo(cwd)
    # repo-local identity — CI runners have no global git identity
    for k, v in (("user.email", "mock@example.com"), ("user.name", "mock")):
        r = subprocess.run(["git", "-C", str(cwd), "config", k, v],
                          capture_output=True, timeout=30)
        assert r.returncode == 0, r.stderr
    (cwd / CANNED_FILE).mkdir()
    (cwd / CANNED_FILE / "inner.txt").write_text("tracked content\n")
    r = subprocess.run(["git", "-C", str(cwd), "add", "-A"],
                      capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["git", "-C", str(cwd), "commit", "-qm", "init"],
                      capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    cwd_str = str(cwd)
    s, state = _session(
        tmp_path, "ok", cwd=cwd_str,
        policy=ac.SwebenchPolicy(workdir=cwd_str),
    )
    try:
        s.initialize()
        s.create_session(cwd=cwd_str)
        result = s.prompt("task", timeout=30)
        s.close()
    finally:
        s.close()

    assert result.stop_reason == "end_turn"  # the turn completed
    assert s.returncode == 0  # the mock survived (no traceback)
    evs = _read_state(state)
    failed = [e for e in evs if e["event"] == "patch_failed"]
    assert failed, "a failed patch must record patch_failed"
    assert not any(e["event"] == "patch_applied" for e in evs)
    _assert_no_orphans(s)


def test_auth_status_canned_stub(tmp_path):
    """Round 1 [minor]: PLAN §2.7 pre-flight probes ``devin auth
    status`` — the mock serves a canned logged-in status (exit 0) so a
    runner driven through it does not record ``preflight_failed``."""
    w, _ = _wrapper(tmp_path, "ok")
    r = subprocess.run([w, "auth", "status"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "Logged in" in r.stdout
    # an unknown 'auth' subargument is rejected like other strict-CLI
    # mistakes
    r = subprocess.run([w, "auth", "bogus"],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_env_mode_empty_rejected_and_devin_model_respected(tmp_path):
    """Round 1 [nit]: ``MOCK_DEVIN_MODE=""`` is rejected like
    ``--mode ""`` (no silent fallback to ``ok`` — a typo'd wrapper must
    surface), and with no ``--model`` the ``DEVIN_MODEL`` env var is
    honored (WP0 §1: the real ``devin acp`` reads it too)."""
    env = dict(os.environ, MOCK_DEVIN_MODE="")
    r = subprocess.run([sys.executable, MOCK_PATH, "acp"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 2

    from pathlib import Path
    state = tmp_path / "state-env.jsonl"
    w = tmp_path / "mock-devin-env"
    w.write_text(
        f"#!/bin/sh\n"
        f"export MOCK_DEVIN_MODE=ok\n"
        f"export MOCK_DEVIN_STATE=\"{state}\"\n"
        f"export DEVIN_MODEL=env-model\n"
        f"exec \"{sys.executable}\" \"{MOCK_PATH}\" acp\n"
    )
    import stat as _stat
    w.chmod(w.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    # round 2 [minor]: stdin=DEVNULL for a deterministic EOF — with
    # inherited stdin, an interactive TTY (or any held-open fd 0) would
    # never EOF and subprocess.run's timeout would fire spuriously.
    r = subprocess.run([str(w)], capture_output=True, text=True,
                      timeout=30, stdin=subprocess.DEVNULL)
    # no client attached: the mock reads stdin until EOF -> clean exit 0
    assert r.returncode == 0, r.stderr
    evs = _read_state(str(state))
    started = [e for e in evs if e["event"] == "started"][0]
    assert started["model"] == "env-model"
