#!/usr/bin/env python3
"""NDJSON JSON-RPC 2.0 probe for `devin acp` — WP0 recon (issue #10).

Purpose: record the real wire surface of `devin acp` (see bench/PLAN.md §5 WP0):
framing (NDJSON vs LSP), wire method names, initialize/session params and
responses, permission request shape, stop-reason + usage metadata fields, and
unsolicited startup vendor notifications.

Modes:
  passive   Spawn the process, log the first --seconds of unsolicited frames
            (captures startup vendor notifications such as
            _cognition.ai/mcp/serversChanged), then exit cleanly.
  handshake Full lifecycle with a trivial task:
            initialize -> session/new -> session/prompt ->
            (auto-answer session/request_permission per policy) ->
            record stop reason + usage -> session/cancel (only if still active)
            -> session/close before terminating the process.
  framing   Negative control: prefix the initialize request with an LSP-style
            "Content-Length: <n>\\r\\n\\r\\n" header and record whether a reply
            arrives within --timeout seconds (expected: none -> NDJSON confirmed).
  selftest  Zero-cost validation of THIS probe against an in-process mock
            server that speaks the expected ACP subset. No devin, no quota.

Hygiene (reproduces the adapter contract from bench/PLAN.md §3 where possible):
  * process is spawned in its own process group (start_new_session) and the
    whole group is killed on every exit path (no orphans);
  * DEVIN_REFUSAL_FALLBACK is always unset in the child env;
  * --isolated-xdg creates a fresh XDG_DATA_HOME/XDG_CONFIG_HOME (seeded with
    the auth token file when --auth-seed is given);
  * --config-file supplies an isolated --config (empty MCP/rules/plugins).

Usage:
  python acp_probe.py selftest
  python acp_probe.py passive   --model swe-2-high --seconds 30
  python acp_probe.py handshake --model swe-2-high --cwd /tmp/task
  python acp_probe.py framing   --model swe-2-high

The JSON report is written to --out (default: acp-probe-<mode>.json) and the
raw frame stream to --frames-out (default: <out>.frames.jsonl).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ACP_METHODS = {
    "initialize",
    "session/new",
    "session/prompt",
    "session/cancel",
    "session/request_permission",
    "session/close",
}
USAGE_FIELD_MARKERS = ("usage", "cost", "token", "quota", "credit")


@dataclass
class Report:
    mode: str
    devin_bin: str
    model: str
    started_at: float = field(default_factory=time.time)
    frames: list = field(default_factory=list)        # raw parsed lines
    method_names: set = field(default_factory=set)    # every method observed (server->client)
    sent_methods: set = field(default_factory=set)    # client->server methods sent
    startup_notifications: list = field(default_factory=list)
    responses: dict = field(default_factory=dict)     # id -> parsed response
    permission_requests: list = field(default_factory=list)
    update_notifications: list = field(default_factory=list)
    usage_fields: list = field(default_factory=list)
    stop_reason: object = None
    framing: dict = field(default_factory=dict)
    timing: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def record(self, obj: dict, when: float) -> None:
        self.frames.append({"t": round(when - self.started_at, 3), "frame": obj})
        if "method" in obj:
            self.method_names.add(obj["method"])
        if "id" in obj and ("result" in obj or "error" in obj):
            self.responses.setdefault(obj["id"], obj)
            # scan for usage-ish fields in result
            res = obj.get("result")
            if isinstance(res, dict):
                self._scan_usage(res)
        if "method" in obj and "id" not in obj:
            self.update_notifications.append(obj.get("method"))
            if obj.get("method") == "session/request_permission":
                self.permission_requests.append(obj)
        # prompt response carries stopReason
        res = obj.get("result") if isinstance(obj.get("result"), dict) else {}
        if isinstance(res, dict) and "stopReason" in res:
            self.stop_reason = res["stopReason"]

    def _scan_usage(self, obj: dict, prefix: str = "") -> None:
        for k, v in obj.items():
            if any(m in k.lower() for m in USAGE_FIELD_MARKERS):
                self.usage_fields.append(f"{prefix}{k}")
            if isinstance(v, dict):
                self._scan_usage(v, f"{prefix}{k}.")

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "devin_bin": self.devin_bin,
            "model": self.model,
            "duration_s": round(time.time() - self.started_at, 3),
            "method_names": sorted(self.method_names),
            "sent_methods": sorted(self.sent_methods),
            "expected_methods_present": sorted(ACP_METHODS & (self.method_names | self.sent_methods)),
            "startup_notifications": self.startup_notifications,
            "responses": self.responses,
            "permission_request_count": len(self.permission_requests),
            "permission_request_sample": self.permission_requests[:3],
            "update_notifications": self.update_notifications,
            "usage_fields": sorted(set(self.usage_fields)),
            "stop_reason": self.stop_reason,
            "framing": self.framing,
            "timing": self.timing,
            "errors": self.errors,
        }


def make_env(isolated_xdg: Path | None, auth_seed: str | None, devin_bin: str) -> dict:
    env = dict(os.environ)
    env.pop("DEVIN_REFUSAL_FALLBACK", None)  # never let a refusal switch models
    if isolated_xdg is not None:
        env["XDG_DATA_HOME"] = str(isolated_xdg / "data")
        env["XDG_CONFIG_HOME"] = str(isolated_xdg / "config")
        (isolated_xdg / "data").mkdir(parents=True, exist_ok=True)
        (isolated_xdg / "config").mkdir(parents=True, exist_ok=True)
        if auth_seed:
            # seed the ephemeral XDG with whatever the host auth file is
            src = Path(auth_seed).expanduser()
            if src.exists():
                rel = src.name
                (isolated_xdg / "data" / "devin" / rel).parent.mkdir(
                    parents=True, exist_ok=True
                )
                shutil.copy2(src, isolated_xdg / "data" / "devin" / rel)
                # also copy alongside, some builds read XDG_DATA_HOME/<name>
                (isolated_xdg / "data" / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, isolated_xdg / "data" / rel)
    return env


class Ndjrpc:
    """Minimal newline-delimited JSON-RPC 2.0 client over a process stdio."""

    def __init__(self, proc: subprocess.Popen, report: Report):
        self.proc = proc
        self.report = report
        self._next_id = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, method: str, params: dict | None = None, id_: int | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if id_ is not None:
            msg["id"] = id_
        self.report.sent_methods.add(method)
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def send_request(self, method: str, params: dict | None = None, prefix: str = "") -> int:
        rid = self.next_id()
        msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        line = json.dumps(msg)
        self.report.sent_methods.add(method)
        self.proc.stdin.write((prefix + line + "\n").encode())
        self.proc.stdin.flush()
        return rid

    def read_line(self, timeout: float) -> str | None:
        # select-free approach: use a threading-free readline with deadline via
        # os.read on the raw fd in small chunks is overkill for a probe;
        # subprocess + select on the file object works on Linux.
        import select

        fd = self.proc.stdout
        deadline = time.monotonic() + timeout
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if r:
                chunk = os.read(fd.fileno(), 65536)
                if not chunk:
                    return None  # EOF
                buf += chunk
                if b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    # push remainder back is not possible; probe frames are
                    # line-bounded and small — drain rest next loop via buffer
                    self._carry = getattr(self, "_carry", b"") + rest
                    return line.decode("utf-8", "replace")
            carry = getattr(self, "_carry", b"")
            if carry:
                self._carry = b""
                if b"\n" in carry:
                    line, _, rest = carry.partition(b"\n")
                    self._carry = rest
                    return line.decode("utf-8", "replace")
        # unreachable

    def wait_for_response(self, id_: int, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.read_line(timeout=max(0.05, deadline - time.monotonic()))
            if line is None:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self.report.errors.append(
                    {"error": "non_json_line", "line": line[:200]}
                )
                continue
            self.report.record(obj, time.time())
            if obj.get("id") == id_ and ("result" in obj or "error" in obj):
                return obj
        return None


def spawn(devin_bin: str, model: str, cwd: str, config_file: str | None,
         isolated_xdg: Path | None, auth_seed: str | None) -> subprocess.Popen:
    cmd = [devin_bin, "acp", "--model", model]
    if config_file:
        cmd += ["--config", config_file]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=make_env(isolated_xdg, auth_seed, devin_bin),
        start_new_session=True,  # own process group -> killable as a group
    )


def kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)


def answer_permission(proc: Ndjrpc, obj: dict, cwd: str) -> None:
    """WP0 policy: allow testbed-local + read; deny web tools. Record the shape."""
    params = obj.get("params", {})
    tool = (
        params.get("toolCall", {}).get("name")
        or params.get("toolName")
        or params.get("kind")
        or "unknown"
    )
    deny = isinstance(tool, str) and tool.lower().startswith(("web", "search", "fetch"))
    options = params.get("options") or []
    chosen = None
    for opt in options:
        oid = opt.get("optionId") or opt.get("id") or opt.get("kind")
        ok = str(oid).lower() in ("proceed", "allow", "allow_once", "approved", "yes")
        no = str(oid).lower() in ("deny", "reject", "denied", "no", "cancel")
        if (not deny and ok) or (deny and no):
            chosen = {"optionId": oid}
            break
    if chosen is None and options:
        chosen = {"optionId": options[0].get("optionId") or options[0].get("id")}
    rid = obj.get("id")
    if rid is not None:
        msg = {"jsonrpc": "2.0", "id": rid,
               "result": chosen or {"outcome": "cancelled"}}
        proc.proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.proc.stdin.flush()


def run_passive(args) -> int:
    xdg = Path(tempfile.mkdtemp(prefix="acp-probe-xdg-")) if args.isolated_xdg else None
    proc = spawn(args.devin_bin, args.model, args.cwd, args.config, xdg, args.auth_seed)
    report = Report(mode="passive", devin_bin=args.devin_bin, model=args.model)
    cli = Ndjrpc(proc, report)
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        line = cli.read_line(timeout=1.0)
        if line is None:
            break
        line = line.strip()
        if line:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                report.errors.append({"error": "non_json_line", "line": line[:200]})
                continue
            report.record(obj, time.time())
            if "method" in obj and "id" not in obj:
                report.startup_notifications.append(obj["method"])
    kill_group(proc)
    report.timing["passive_window_s"] = args.seconds
    try:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    except Exception:
        err = ""
    report.timing["stderr_tail"] = err[-4000:] if err else "(none)"
    finish(report, args)
    return 0


def run_framing(args) -> int:
    """Negative control: LSP-style header before the first request."""
    xdg = Path(tempfile.mkdtemp(prefix="acp-probe-xdg-")) if args.isolated_xdg else None
    proc = spawn(args.devin_bin, args.model, args.cwd, args.config, xdg, args.auth_seed)
    report = Report(mode="framing", devin_bin=args.devin_bin, model=args.model)
    cli = Ndjrpc(proc, report)
    # warm-up: plain NDJSON initialize — expected to reply (positive control)
    rid = cli.send_request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    got = cli.wait_for_response(rid, timeout=args.timeout)
    report.framing["ndjson_reply"] = got is not None
    if got is None:
        # server may not reply to initialize before other setup; still record
        report.errors.append({"error": "no_ndjson_initialize_reply"})
    # now the negative control on a *fresh* process
    kill_group(proc)
    proc = spawn(args.devin_bin, args.model, args.cwd, args.config, xdg, args.auth_seed)
    cli = Ndjrpc(proc, report)
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 999, "method": "initialize",
         "params": {"protocolVersion": 1, "clientCapabilities": {}}}
    )
    header = f"Content-Length: {len(payload.encode())}\r\n\r\n"
    rid = cli.send_request("initialize", {"protocolVersion": 1, "clientCapabilities": {}},
                          prefix=header)
    got = cli.wait_for_response(rid, timeout=args.timeout)
    report.framing["lsp_header_reply"] = got is not None
    report.framing["verdict"] = (
        "NDJSON confirmed" if report.framing.get("ndjson_reply")
        and not report.framing.get("lsp_header_reply")
        else "INCONCLUSIVE — inspect frames"
    )
    kill_group(proc)
    finish(report, args)
    return 0


def run_handshake(args) -> int:
    xdg = Path(tempfile.mkdtemp(prefix="acp-probe-xdg-")) if args.isolated_xdg else None
    proc = spawn(args.devin_bin, args.model, args.cwd, args.config, xdg, args.auth_seed)
    report = Report(mode="handshake", devin_bin=args.devin_bin, model=args.model)
    cli = Ndjrpc(proc, report)
    t0 = time.time()

    # Framing canary (same process, zero extra sessions): an LSP-style
    # "Content-Length: <n>\r\n\r\n" prefix before a JSON line, sent before
    # initialize. If the server is NDJSON-line-oriented it will (a) ignore the
    # header line as garbage and (b) treat the canary JSON line as a message
    # (answering its id) — or simply ignore the unknown method. If the server
    # required Content-Length framing, the bare initialize that follows would
    # never be answered. Verdict is recorded against the initialize reply.
    canary_id = 9000
    canary = json.dumps(
        {"jsonrpc": "2.0", "id": canary_id, "method": "__framing_canary__",
         "params": {"probe": True}}
    )
    canary_bytes = f"Content-Length: {len(canary.encode())}\r\n\r\n{canary}\n"
    proc.stdin.write(canary_bytes.encode())
    proc.stdin.flush()
    report.framing["canary_id"] = canary_id

    # 0) drain startup notifications until initialize can be answered:
    #    we send initialize and keep reading until its reply, logging anything
    #    unsolicited that arrives in between as startup notifications.
    rid_init = cli.send_request(
        "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
    )
    res_init = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        line = cli.read_line(timeout=1.0)
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            report.errors.append({"error": "non_json_line", "line": line[:200]})
            continue
        report.record(obj, time.time())
        if obj.get("id") == rid_init and ("result" in obj or "error" in obj):
            res_init = obj
            break
        if "method" in obj and "id" not in obj:
            report.startup_notifications.append(obj["method"])
    report.timing["initialize_s"] = round(time.time() - t0, 3)
    report.framing["initialize_replied"] = res_init is not None
    report.framing["canary_replied"] = canary_id in report.responses
    report.framing["verdict"] = (
        "NDJSON confirmed (bare-NDJSON initialize answered)"
        if res_init is not None
        else "INCONCLUSIVE — inspect frames"
    )
    if res_init is None:
        report.errors.append({"error": "initialize_timeout_or_error"})
        kill_group(proc)
        finish(report, args)
        return 1
    report.responses["initialize"] = res_init

    # 1) session/new
    rid_new = cli.send_request(
        "session/new", {"cwd": args.cwd, "mcpServers": []}
    )
    t1 = time.time()
    res_new = _wait_handshake(cli, rid_new, deadline_s=args.timeout, report=report)
    report.timing["session_new_s"] = round(time.time() - t1, 3)
    sid = None
    if res_new and "result" in res_new:
        sid = res_new["result"].get("sessionId") or res_new["result"].get("session_id")
    if sid is None:
        report.errors.append({"error": "session_new_failed", "res": res_new})
        kill_group(proc)
        finish(report, args)
        return 1
    report.responses["session_new"] = res_new

    # 2) session/prompt (trivial task)
    prompt = args.prompt or (
        "Create a file named wp0_probe.txt containing the text 'wp0 ok'. "
        "Do not do anything else. When done, stop."
    )
    rid_prompt = cli.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
    )
    t2 = time.time()
    res_prompt = None
    deadline = time.monotonic() + args.max_session_s
    while time.monotonic() < deadline:
        line = cli.read_line(timeout=1.0)
        if line is None:
            report.errors.append({"error": "eof_during_prompt"})
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            report.errors.append({"error": "non_json_line", "line": line[:200]})
            continue
        report.record(obj, time.time())
        if obj.get("method") == "session/request_permission":
            answer_permission(cli, obj, args.cwd)
        if obj.get("id") == rid_prompt and ("result" in obj or "error" in obj):
            res_prompt = obj
            break
    report.timing["prompt_s"] = round(time.time() - t2, 3)
    report.responses["session_prompt"] = res_prompt

    # 3) session/cancel only if the prompt is still active; then session/close
    #    before terminating the process (PLAN.md §1 wire contract).
    if res_prompt is None:
        cli.send("session/cancel", {"sessionId": sid})
        time.sleep(0.5)
        report.errors.append({"error": "prompt_timeout_cancelled"})
    cli.send("session/close", {"sessionId": sid})
    t3 = time.time()
    try:
        proc.wait(timeout=10)
        report.timing["close_to_exit_s"] = round(time.time() - t3, 3)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        report.errors.append({"error": "process_survived_close"})
    # orphan check
    time.sleep(0.3)
    report.timing["orphan_check"] = _check_orphans(proc)
    # persist stderr (diagnostic: a dying agent often explains itself there)
    err = ""
    try:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    except Exception:
        pass
    report.timing["stderr_tail"] = err[-4000:] if err else "(none)"
    finish(report, args)
    return 0 if res_prompt is not None else 1


def _wait_handshake(cli: Ndjrpc, rid: int, deadline_s: float, report: Report) -> dict | None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        line = cli.read_line(timeout=1.0)
        if line is None:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            report.errors.append({"error": "non_json_line", "line": line[:200]})
            continue
        report.record(obj, time.time())
        if obj.get("method") == "session/request_permission":
            answer_permission(cli, obj, "")
        if obj.get("id") == rid and ("result" in obj or "error" in obj):
            return obj
    return None


def _check_orphans(proc: subprocess.Popen) -> str:
    try:
        os.killpg(proc.pid, 0)  # does the group still exist?
        return "GROUP_STILL_ALIVE"
    except (ProcessLookupError, PermissionError):
        return "clean"


def finish(report: Report, args) -> None:
    out = Path(args.out) if args.out else Path(f"acp-probe-{report.mode}.json")
    out.write_text(json.dumps(report.to_json(), indent=2))
    frames_out = Path(args.frames_out) if args.frames_out else out.with_suffix(".frames.jsonl")
    with frames_out.open("w") as f:
        for fr in report.frames:
            f.write(json.dumps(fr) + "\n")
    print(f"[probe] mode={report.mode} methods={report.to_json()['method_names']}")
    print(f"[probe] report -> {out}")
    print(f"[probe] frames -> {frames_out}")


# ---------------------------------------------------------------------------
# selftest: in-process mock of the expected ACP subset (zero quota)
# ---------------------------------------------------------------------------

def run_selftest(args) -> int:
    # drive the probe logic against the mock by monkeypatching spawn
    global spawn
    real_spawn = spawn

    class FakeProc:
        def __init__(self):
            import subprocess as sp
            # run mock in a real subprocess for fidelity
            self.p = sp.Popen(
                [sys.executable, "-c", MOCK_SUBPROCESS],
                stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
                start_new_session=True,
            )
            self.stdin = self.p.stdin
            self.stdout = self.p.stdout
            self.pid = self.p.pid
            self.returncode = None

        def wait(self, timeout=None):
            self.p.wait(timeout=timeout)
            self.returncode = self.p.returncode

        def poll(self):
            return self.p.poll()

    def fake_spawn(*a, **k):
        return FakeProc()

    spawn = fake_spawn
    args.isolated_xdg = False
    args.auth_seed = None
    args.config = None
    args.devin_bin = "mock-devin"
    args.model = "swe-2-high"
    args.cwd = "/tmp"
    args.timeout = 15
    args.max_session_s = 30
    args.seconds = 5
    args.out = None
    args.frames_out = None
    import tempfile as _tf
    out_dir = _tf.mkdtemp(prefix="acp-probe-selftest-")
    args.out = str(Path(out_dir) / "selftest.json")
    rc = run_handshake(args)
    rep = json.loads(Path(args.out).read_text())
    spawn = real_spawn
    expected_sent = {"initialize", "session/new", "session/prompt", "session/close"}
    got_sent = set(rep["sent_methods"])
    got_srv = set(rep["method_names"])
    ok = (rc == 0 and expected_sent <= got_sent
         and "session/request_permission" in got_srv
         and rep["stop_reason"] == "end_turn"
         and "_cognition.ai/mcp/serversChanged" in rep["startup_notifications"]
         and len(rep["usage_fields"]) >= 1
         and rep["timing"].get("orphan_check") == "clean")
    print("[selftest] sent:", sorted(got_sent))
    print("[selftest] server methods seen:", sorted(got_srv))
    print("[selftest] stop_reason:", rep["stop_reason"])
    print("[selftest] startup_notifications:", rep["startup_notifications"])
    print("[selftest] usage_fields:", rep["usage_fields"])
    print("[selftest] RESULT:", "PASS" if ok else f"FAIL (rc={rc})")
    return 0 if ok else 1


MOCK_SUBPROCESS = r'''
import json, select, sys
def send(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()
send({"jsonrpc": "2.0", "method": "_cognition.ai/mcp/serversChanged", "params": {"servers": []}})
send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "mock-sid", "update": {"kind": "note"}}})
await55 = False
prompt_rid = None
buf = b""
def handle(obj):
    global await55, prompt_rid
    m = obj.get("method")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": obj["id"], "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}})
    elif m == "session/new":
        send({"jsonrpc": "2.0", "id": obj["id"], "result": {"sessionId": "mock-sid"}})
    elif m == "session/prompt":
        prompt_rid = obj["id"]
        await55 = True
        send({"jsonrpc": "2.0", "id": 55, "method": "session/request_permission",
             "params": {"sessionId": "mock-sid", "toolCall": {"name": "web_fetch", "kind": "web"},
                       "options": [{"optionId": "deny"}, {"optionId": "proceed"}]}})
    elif m == "session/cancel":
        pass
    elif m == "session/close":
        sys.exit(0)
    elif obj.get("id") == 55 and await55:
        await55 = False
        send({"jsonrpc": "2.0", "id": prompt_rid,
             "result": {"stopReason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}})
while True:
    r, _, _ = select.select([sys.stdin.buffer], [], [], 30)
    if not r:
        break
    buf += sys.stdin.buffer.read1(4096)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if line.strip():
            try:
                handle(json.loads(line))
            except json.JSONDecodeError:
                pass  # framing canary header line / any non-JSON line: ignore
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["passive", "handshake", "framing", "selftest"])
    ap.add_argument("--devin-bin", default="devin")
    ap.add_argument("--model", default="swe-2-high")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--config", default=None, help="isolated --config file")
    ap.add_argument("--auth-seed", default=None,
                    help="path to host devin auth token file to seed into isolated XDG")
    ap.add_argument("--isolated-xdg", action="store_true")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--max-session-s", type=float, default=900)
    ap.add_argument("--prompt", default=None, help="override the trivial task")
    ap.add_argument("--out", default=None)
    ap.add_argument("--frames-out", default=None)
    args = ap.parse_args()
    if args.mode == "selftest":
        return run_selftest(args)
    if args.mode == "passive":
        return run_passive(args)
    if args.mode == "framing":
        return run_framing(args)
    return run_handshake(args)


if __name__ == "__main__":
    raise SystemExit(main())
