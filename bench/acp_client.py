"""NDJSON JSON-RPC 2.0 client core for `devin acp` — WP1 (issue #11)
+ WP1b (issue #12: ACP permission policy + refusal-fallback isolation).

Wire contract (bench/PLAN.md §1, verified live in WP0 — ``notes/acp-surface.md``):

* Framing is **NDJSON**: one JSON-RPC 2.0 object per line over stdio.
  **Not** LSP ``Content-Length`` framing (WP0 negative control: the real
  server answers a ``Content-Length`` header line with a ``-32700`` parse
  error — it is a rejected *line*, not framing).
* **Wire method names** (they differ from the ACP SDK names):
  ``initialize`` → ``session/new`` → ``session/prompt`` → streamed
  ``session/update`` notifications → ``session/cancel`` →
  ``session/request_permission`` (server-initiated request — the client
  **must** answer it) → ``session/close`` (sent before terminating the
  process; the real server is not guaranteed to exit on close — WP0 found a
  handler-error dump + a surviving process, so termination is via the group
  kill, close is best-effort hygiene).
* Unsolicited vendor notifications (e.g. ``_cognition.ai/mcp/serversChanged``,
  ``_cognition.ai/thinking_complete``) are **logged and ignored** — never an
  error, never blocking.

Hygiene (PLAN.md §3, risk table):

* The agent process is spawned in its **own process group**; on every exit
  path the client sends ``session/close`` (best-effort) and then
  SIGTERM → SIGKILL's the whole group, so no orphan ``devin`` process leaks
  quota. Use the context-manager form so close runs on all paths.
* ``DEVIN_REFUSAL_FALLBACK`` is **always unset** in the child env (PLAN
  §3.4), even if the caller passes it, and the ``--refusal-fallback`` CLI
  flag is refused. A refusal stop-reason is a terminal
  :class:`AcpRefusalError` — never a model switch, never a retry.
* Every blocking read is **deadline-bounded**; malformed/short reads raise
  typed errors and the client **never hangs**.

Permission policy (PLAN.md §3.7, issue #12 — :class:`SwebenchPolicy`):

* **Allow:** read-only tools (unscoped — a read may leave ``/testbed``,
  e.g. ``/etc``); edit/write **and** shell execution (exec/terminal)
  inside the workdir (``/testbed``) and ``/tmp`` — the agent must be able
  to reproduce bugs and run pytest. An exec that declares a ``cwd`` outside
  the allowed dirs fails closed; an exec with no declared ``cwd`` runs in
  the session cwd (``/testbed``), so it is in scope.
* **Deny:** web-search/fetch tools — ACP ``ToolKind`` ``fetch`` ("retrieving
  external data") plus an explicit name marker list; the code-search tool
  (toolCallKind ``search``) is a *read*, not web; writes outside
  workdir+``/tmp`` (every declared path — ``rawInput`` keys, ``locations``
  and ``content`` blocks — must be in scope); anything else (default deny).
* **Kind-first classification:** ``toolCall.kind`` is the classifier; the
  tool *name* (resolved from ``_meta["cognition.ai/inferenceToolName"]`` →
  ``toolCallId`` prefix → spec ``name`` field, per the WP0 live capture)
  is a fallback only when the kind is absent/``other``. A mislabelled
  write tool (kind ``edit``) is never allowed via a read-like name without
  a path check.
* Path checks are lexical (after ``~``/``$VAR`` expansion); they do not
  resolve symlinks — the allow dirs are real dirs in the sweb.eval images.

The module is pure stdlib: no SDK dependency, no real ``devin`` binary —
the whole behaviour is unit-tested against an in-process mock server
(``tests/mock_server.py``), zero quota.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "ACP_PROTOCOL_VERSION",
    "DEFAULT_PROMPT_TIMEOUT_S",
    "WIRE",
    "AcpSession",
    "PromptResult",
    "SessionEvent",
    "PermissionDecision",
    "PermissionPolicy",
    "SwebenchPolicy",
    "AcpError",
    "AcpTransportError",
    "AcpMalformedFrameError",
    "AcpRpcError",
    "AcpTimeoutError",
    "AcpProtocolError",
    "AcpRefusalError",
]

log = logging.getLogger("devinp_bench.acp_client")

ACP_PROTOCOL_VERSION = 1

#: PLAN.md §2.2 — default per-session wall-clock cap (45 min).
DEFAULT_PROMPT_TIMEOUT_S = 45 * 60

#: Wire method names implemented by ``devin acp`` (WP0 live capture).
WIRE = {
    "INITIALIZE": "initialize",
    "SESSION_NEW": "session/new",
    "SESSION_PROMPT": "session/prompt",
    "SESSION_CANCEL": "session/cancel",
    "SESSION_PERMISSION": "session/request_permission",
    "SESSION_CLOSE": "session/close",
    "SESSION_UPDATE": "session/update",
}

#: Stop-reason substrings (lowercased) treated as a policy refusal —
#: terminal, no model switch (PLAN §3.4).
_REFUSAL_MARKERS = ("refus",)


# ---------------------------------------------------------------------------
# typed errors — every failure the client can produce has a type
# ---------------------------------------------------------------------------

class AcpError(Exception):
    """Base class for every typed failure the client raises."""


class AcpTransportError(AcpError):
    """The agent process's stdio went away (EOF / process death)."""


class AcpMalformedFrameError(AcpError):
    """A read line is not a valid JSON-RPC 2.0 message (or EOF mid-line)."""

    def __init__(self, reason: str, line: Optional[str] = None) -> None:
        super().__init__(f"malformed frame: {reason}")
        self.reason = reason
        self.line = line


class AcpRpcError(AcpError):
    """The server answered a request with a JSON-RPC error object."""

    def __init__(self, code: Any, message: Any, request_id: Any = None,
                 data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data


class AcpTimeoutError(AcpError):
    """A bounded read/operation exceeded its deadline."""


class AcpProtocolError(AcpError):
    """A valid JSON-RPC frame/state that violates the ACP session contract."""


class AcpRefusalError(AcpError):
    """The session ended in a policy refusal — terminal, no model switch."""

    def __init__(self, stop_reason: str, raw: Any = None) -> None:
        super().__init__(f"refusal stop-reason: {stop_reason!r}")
        self.stop_reason = stop_reason
        self.raw = raw


# ---------------------------------------------------------------------------
# permission policy (PLAN.md §3.7)
# ---------------------------------------------------------------------------

#: option-id/name/kind markers used to map a policy decision onto the
#: server-provided option list — fallback for non-spec option shapes (the
#: ACP-spec ``PermissionOptionKind`` values are matched exactly first, see
#: :meth:`SwebenchPolicy._pick`).
_PROCEED_MARKERS = ("proceed", "allow", "approved", "yes", "ok")
_DENY_MARKERS = ("deny", "reject", "denied", "no", "cancel", "refuse")

#: ACP-spec ``PermissionOptionKind`` values (schema v1) — matched exactly,
#: so a substring in an option id (e.g. "ok" in "token") can never
#: mis-select.
_ACP_OPTION_KINDS = {
    True: ("allow_once", "allow_always"),
    False: ("reject_once", "reject_always"),
}

#: Explicit web-tool name markers — the name-level web deny (PLAN §3.7:
#: "Deny: web-search/fetch tools"), in addition to the spec ``ToolKind``
#: ``fetch`` ("retrieving external data"). Deliberately an exact-name set,
#: not a prefix match: ACP's ``search`` toolCallKind (codebase search) is a
#: *read*, not a web tool — a prefix heuristic denied it and would break
#: the benchmark agent's code search.
_WEB_TOOL_NAMES = frozenset({
    "web", "web_fetch", "websearch", "web_search", "search_web",
    "web_browse", "browse", "browser",
    "fetch", "fetch_url", "url_fetch",
    "http", "http_get", "http_post", "http_request",
})

#: read-only tool names — fallback when kind is absent/"other" (a read
#: tool with kind "read"/"search" is allowed regardless of name).
_READ_NAMES = frozenset({
    "read", "read_file", "view", "list", "ls", "glob",
    "search", "grep",
})

#: shell-execution tool names — fallback when kind is absent/"other".
_EXEC_NAMES = frozenset({
    "bash", "shell", "exec", "terminal", "run", "run_command",
})

#: write-class tool kinds (ACP toolCallKind) and names — every declared
#: path must be inside the allowed dirs.
_WRITE_KINDS = ("edit", "delete", "move")
_WRITE_NAMES = frozenset({
    "write", "edit", "multiedit", "create_file", "delete_file",
    "rename", "move", "apply_patch", "patch",
})

#: rawInput keys that declare a file path (any of them may appear; when
#: several do, **all** declared paths are checked). A toolCall also carries
#: affected paths in ``locations[].path`` and ``content[].path`` (WP0 live
#: capture: ``content: [{type: "diff", path: …}]``) — both are collected
#: too, so a write that declares its target only there is still checked.
_PATH_KEYS = ("file_path", "filePath", "path", "notebook_path")


def _tool_name(tool: dict) -> str:
    """The tool name of a ToolCall, resolved for the **real wire shape**.

    The WP0 live capture (``notes/acp-probe-handshake2.frames.jsonl``)
    shows ``devin acp`` sends tool names in
    ``_meta["cognition.ai/inferenceToolName"]`` and as the ``toolCallId``
    prefix (e.g. ``"write:0#…"``) — with **no** top-level ``name`` field
    (it is optional in the ACP schema, absent in the capture). The
    human-readable ``title`` is deliberately *not* a name source: it is a
    display string ("Wrote ./wp0_probe.txt"), not a tool identifier.
    """
    meta = tool.get("_meta")
    if isinstance(meta, dict):
        n = meta.get("cognition.ai/inferenceToolName")
        if isinstance(n, str) and n:
            return n.lower()
    cid = tool.get("toolCallId")
    if isinstance(cid, str) and ":" in cid:
        return cid.split(":", 1)[0].lower()
    n = tool.get("name")
    return str(n).lower() if n else ""


@dataclass(frozen=True)
class PermissionDecision:
    """Outcome of a policy decision on a ``session/request_permission``."""

    allow: bool
    option_id: Optional[str]
    reason: str


class PermissionPolicy:
    """Interface for answering ``session/request_permission`` per policy."""

    def decide(self, params: dict) -> PermissionDecision:
        raise NotImplementedError


class SwebenchPolicy(PermissionPolicy):
    """Default benchmark policy (PLAN.md §3.7, issue #12).

    * **Allow:** read-only tools (unscoped); edit/write inside *workdir*
      (``/testbed``) and ``/tmp``; shell execution (exec/terminal) — an
      undeclared ``cwd`` runs in the session cwd (``/testbed``), an
      out-of-scope declared ``cwd`` is denied. The agent must be able to
      reproduce bugs and run pytest.
    * **Deny:** web-search/fetch tools (ACP ``ToolKind`` ``fetch`` + an
      explicit name marker list — a code-search tool is a *read*, not
      web); writes outside workdir+``/tmp``; anything else (default deny).

    Classification is **kind-first**: ``toolCall.kind`` decides the class;
    the tool name is a fallback only when the kind is absent/``other``,
    so a mislabelled write tool (kind ``edit``) is never allowed via a
    read-like name without a path check. Relative declared paths and
    relative exec cwds resolve against the policy's *workdir* (default
    ``/testbed`` — the adapter creates the session with ``cwd=/testbed``),
    not the client process cwd; ``~``/``$VAR`` are expanded; symlinks are
    not resolved (lexical checks — the allow dirs are real dirs in the
    sweb.eval images). The exec ``cwd`` rawInput key is unobserved live
    (WP0 §9.1): ``cwd``/``workingDirectory`` are checked; Pilot A confirms
    the real key set.
    """

    def __init__(self, workdir: str = "/testbed",
                 extra_allow_dirs: tuple = ("/tmp",)) -> None:
        self._allowed = tuple(
            os.path.abspath(d) for d in (workdir, *extra_allow_dirs)
        )

    # -- public ----------------------------------------------------------

    def decide(self, params: dict) -> PermissionDecision:
        tool = params.get("toolCall") or {}
        if not isinstance(tool, dict):
            tool = {}
        name = _tool_name(tool)
        kind = str(tool.get("kind") or "").lower()
        raw = tool.get("rawInput")
        raw = raw if isinstance(raw, dict) else {}
        options = params.get("options") or []
        options = [o for o in options if isinstance(o, dict)]

        # 1) web-search / fetch — always denied. ``kind == "fetch"`` is
        #    the ACP ToolKind for "retrieving external data" (schema v1;
        #    the real devin wire has no "web" kind — "web" is kept for
        #    non-spec servers); a code-search tool (kind "search") is a
        #    read, not web, so it never lands here.
        if kind in ("web", "fetch") or name in _WEB_TOOL_NAMES:
            return self._pick(options, False, "web tools are denied")

        # 2) shell execution — allowed (the agent must run pytest). A
        #    *declared* cwd outside the allowed dirs fails closed; no
        #    declared cwd → the session cwd (workdir), which is in scope.
        #    (The cwd rawInput key is unobserved live — WP0 §9.1; Pilot A
        #    confirms the real key set.)
        if kind in ("execute", "shell") \
                or (kind in ("", "other") and name in _EXEC_NAMES):
            cwd = raw.get("cwd") or raw.get("workingDirectory")
            if cwd and not self._exec_cwd_allowed(str(cwd)):
                return self._pick(options, False,
                                 "exec cwd outside allowed dirs")
            return self._pick(options, True,
                             "shell execution (in-scope cwd)")

        # 3) read-only — allowed, unscoped (a read may leave the allowed
        #    dirs, e.g. /etc or docs).
        if kind in ("read", "search") \
                or (kind in ("", "other") and name in _READ_NAMES):
            return self._pick(options, True, "read-only access")

        # 4) path-scoped writes — allowed only if **every** declared path
        #    (rawInput keys + locations + content blocks) is inside
        #    workdir + /tmp.
        paths = [str(raw[k]) for k in _PATH_KEYS if raw.get(k)]
        for loc in (tool.get("locations") or []):
            if isinstance(loc, dict) and loc.get("path"):
                paths.append(str(loc["path"]))
        for c in (tool.get("content") or []):
            if isinstance(c, dict) and c.get("path"):
                paths.append(str(c["path"]))
        if kind in _WRITE_KINDS \
                or (kind in ("", "other") and name in _WRITE_NAMES) \
                or paths:
            if not paths:
                # a write-class tool that declares no path cannot be
                # verified → fail closed.
                return self._pick(options, False,
                                 "write tool with no declared path (fail closed)")
            if all(self._path_allowed(p) for p in paths):
                return self._pick(options, True, "paths inside allowed dirs")
            return self._pick(options, False, "paths outside allowed dirs")

        # 5) default deny — anything the policy does not recognize.
        return self._pick(options, False, "unrecognized tool (default deny)")

    # -- internals -------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        """``~``/``$VAR`` expansion + absolutization. Lexical: symlinks
        are **not** resolved (the allow dirs are real dirs in the
        sweb.eval images; resolving a symlinked workdir would false-deny).
        """
        return os.path.abspath(
            os.path.expandvars(os.path.expanduser(path))
        )

    def _path_allowed(self, path: str) -> bool:
        # expand ``~``/``$VAR`` **first**, then a still-relative path
        # resolves against the session cwd — the policy's workdir (the
        # adapter creates the session with cwd=/testbed), not the client
        # process cwd.
        p = os.path.expandvars(os.path.expanduser(str(path)))
        if not os.path.isabs(p):
            p = os.path.join(self._allowed[0], p)
        p = self._normalize(p)
        return any(p == d or p.startswith(d + os.sep) for d in self._allowed)

    def _exec_cwd_allowed(self, cwd: str) -> bool:
        """An *absolute* declared cwd must be inside the allowed dirs;
        a relative cwd resolves against the session cwd (the workdir).
        ``~``/``$VAR`` are expanded **before** the absoluteness check,
        so ``cwd="~"`` cannot slip through as a literal relative path."""
        cwd = os.path.expandvars(os.path.expanduser(str(cwd)))
        if not os.path.isabs(cwd):
            cwd = os.path.join(self._allowed[0], cwd)
        return self._path_allowed(cwd)

    def _pick(self, options: list, allow: bool, reason: str) -> PermissionDecision:
        # 1st pass: the ACP-spec PermissionOptionKind values, matched
        # exactly — a substring in an option id/name ("ok" in "token")
        # can never mis-select a real option.
        wanted = _ACP_OPTION_KINDS[allow]
        for opt in options:
            if str(opt.get("kind") or "").lower() in wanted:
                return PermissionDecision(
                    allow=allow,
                    option_id=opt.get("optionId") or opt.get("id"),
                    reason=reason,
                )
        # 2nd pass: substring markers for non-spec option shapes.
        markers = _PROCEED_MARKERS if allow else _DENY_MARKERS
        for opt in options:
            oid = str(opt.get("optionId") or opt.get("id") or "").lower()
            name = str(opt.get("name") or "").lower()
            kind = str(opt.get("kind") or "").lower()
            if (any(m in oid for m in markers)
                    or any(m in name for m in markers)
                    or any(m in kind for m in markers)):
                return PermissionDecision(
                    allow=allow,
                    option_id=opt.get("optionId") or opt.get("id"),
                    reason=reason,
                )
        # No matching option. A deny without a deny-option is expressed as
        # outcome "cancelled". An allow without a proceed-option **fails
        # closed**: the wire cannot express the allow, so we send
        # "cancelled" rather than gamble on a wrong option (the decision
        # keeps allow=True so the transcript records the intent truthfully).
        return PermissionDecision(
            allow=allow, option_id=None,
            reason=f"{reason} (no matching option; failed closed)",
        )


# ---------------------------------------------------------------------------
# transcript / results
# ---------------------------------------------------------------------------

@dataclass
class SessionEvent:
    """One entry of the session transcript (stream order preserved)."""

    ts: float
    kind: str  # "update" | "permission" | "permission_decision" | "vendor"
    method: Optional[str]
    payload: dict = field(default_factory=dict)


@dataclass
class PromptResult:
    """Outcome of a ``session/prompt`` call (usage captured verbatim)."""

    stop_reason: str
    usage: Optional[dict]
    updates: int
    duration_s: float
    raw: dict


# ---------------------------------------------------------------------------
# NDJSON transport
# ---------------------------------------------------------------------------

class _Ndjrpc:
    """Line-oriented JSON-RPC 2.0 over a subprocess stdio pair.

    * Outgoing frames are ``json.dumps``-ed, which escapes every control
      character (``\\n`` → ``\\\\n``), so a multi-line string always stays
      on **one line** — NDJSON-safe by construction.
    * Every :meth:`read_frame` is bounded by a *deadline*; EOF with a
      partial line is a typed short-read error, a bad line is a typed
      malformed-frame error — the read loop can never hang.
    """

    _CHUNK = 65536
    _SLICE = 0.25  # max select slice — keeps deadline checks tight
    #: hard cap applied when a caller omits the deadline — the write stays
    # bounded even if a future caller forgets ("never hangs").
    _FALLBACK_WRITE_CAP_S = 10.0

    def __init__(self, proc: "subprocess.Popen") -> None:
        self._proc = proc
        self._buf = b""
        self._next_id = 0
        #: set when a write deadline fires mid-frame (a partial NDJSON line is
        # now sitting in the pipe) or the pipe dies — any further frame
        # appended to a torn line would be a wire parse error, so the
        # best-effort sends (cancel/close) skip a broken transport.
        self._broken = False

    @property
    def broken(self) -> bool:
        return self._broken

    # -- send ------------------------------------------------------------

    def send_request(self, method: str, params: Optional[dict] = None,
                    deadline: Optional[float] = None) -> int:
        self._next_id += 1
        msg: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg, deadline)
        return self._next_id

    def send_notification(self, method: str, params: Optional[dict] = None,
                         deadline: Optional[float] = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg, deadline)

    def send_response(self, frame: dict,
                     deadline: Optional[float] = None) -> None:
        self._send(frame, deadline)

    def _send(self, msg: dict, deadline: Optional[float] = None) -> None:
        """Write one NDJSON frame to the agent's stdin.

        ``json.dumps`` escapes every control character (``\n`` → ``\\n``),
        so a multi-line string always stays on **one line** — NDJSON-safe by
        construction (enforced explicitly below, not via ``assert``, so the
        check survives ``python -O``). When a ``deadline`` is given the write
        is bounded (select + raw ``os.write``): a server that stops reading
        stdin cannot hang the client, and a dead process surfaces as a typed
        :class:`AcpTransportError` rather than a raw ``BrokenPipeError``.
        """
        line = json.dumps(msg, ensure_ascii=False)
        if "\n" in line or "\r" in line:
            raise AcpProtocolError(
                "NDJSON frame must not contain raw newline/CR characters"
            )
        view = memoryview(line.encode("utf-8") + b"\n")
        if deadline is None:
            deadline = time.monotonic() + self._FALLBACK_WRITE_CAP_S
        try:
            fd = self._proc.stdin.fileno()
            # non-blocking: a single blocking os.write() larger than the
            # pipe's free space would block inside the syscall and defeat
            # every deadline; EAGAIN is retried through select instead.
            os.set_blocking(fd, False)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._broken = True  # partial frame now sits in the pipe
                    raise AcpTimeoutError("stdin write deadline exceeded")
                _, writable, _ = select.select([], [fd], [],
                                             min(remaining, self._SLICE))
                if not writable:
                    continue
                try:
                    n = os.write(fd, view)
                except BlockingIOError:
                    continue  # pipe momentarily full — re-select
                view = view[n:]
        except (OSError, ValueError) as exc:
            self._broken = True  # pipe dead (EBADF/EPIPE) — don't retry sends
            raise AcpTransportError(f"agent stdin write failed: {exc}") from exc

    # -- receive ---------------------------------------------------------

    def read_frame(self, deadline: float) -> dict:
        """Read the next JSON-RPC frame, bounded by ``deadline``.

        Raises :class:`AcpTimeoutError`, :class:`AcpTransportError` or
        :class:`AcpMalformedFrameError` — never blocks past the deadline.
        """
        stdout = self._proc.stdout
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                obj = self._parse_line(line)
                if obj is not None:
                    return obj
                continue  # blank line: not a message — keep reading (bounded)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpTimeoutError(
                    f"no frame by deadline (buffer={len(self._buf)}B)"
                )
            try:
                ready, _, _ = select.select([stdout], [], [],
                                           min(remaining, self._SLICE))
                if not ready:
                    continue
                chunk = os.read(stdout.fileno(), self._CHUNK)
            except (OSError, ValueError) as exc:
                raise AcpTransportError(
                    f"agent stdout read failed: {exc}"
                ) from exc
            if not chunk:
                # EOF: a pending partial line is a short read, not a frame.
                if self._buf.strip():
                    raise AcpMalformedFrameError(
                        "short read: EOF mid-line",
                        self._buf[:200].decode("utf-8", "replace"),
                    )
                raise AcpTransportError("agent process stdout closed (EOF)")
            self._buf += chunk

    @staticmethod
    def _parse_line(line: bytes) -> Optional[dict]:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return None  # blank line — tolerate, not a message
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AcpMalformedFrameError(
                f"invalid JSON ({exc.msg} @ {exc.pos})", text[:200]
            ) from None
        if not isinstance(obj, dict):
            raise AcpMalformedFrameError("frame is not a JSON object", text[:200])
        if obj.get("jsonrpc") != "2.0":
            raise AcpMalformedFrameError("jsonrpc != '2.0'", text[:200])
        is_response = "id" in obj and ("result" in obj or "error" in obj)
        is_message = "method" in obj
        if not (is_response or is_message):
            raise AcpMalformedFrameError(
                "neither a request nor a response", text[:200]
            )
        return obj


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

class AcpSession:
    """One ``devin acp`` process, driven over NDJSON JSON-RPC 2.0.

    Lifecycle (enforced): ``start`` → ``initialize`` → ``create_session`` →
    ``prompt`` → ``close``. ``close`` sends ``session/close`` before
    terminating the process (best-effort — the real server may ignore it)
    and then kills the whole process group on all paths, so no orphan
    ``devin`` process survives. Use as a context manager::

        with AcpSession(devin_bin, model="swe-2-high") as s:
            s.initialize()
            s.create_session(cwd="/testbed")
            result = s.prompt("… task …", timeout=2700)

    ``prompt`` bounds the wall clock: on timeout it sends
    ``session/cancel``, gives the server a short grace, kills the process
    group and raises :class:`AcpTimeoutError`.
    """

    def __init__(
        self,
        devin_bin: str = "devin",
        model: str = "swe-2-high",
        cwd: Optional[str] = None,
        extra_args: Optional[list] = None,
        env: Optional[dict] = None,
        policy: Optional[PermissionPolicy] = None,
        initialize_timeout_s: float = 30.0,
        session_timeout_s: float = 30.0,
        prompt_timeout_s: Optional[float] = DEFAULT_PROMPT_TIMEOUT_S,
        cancel_grace_s: float = 0.5,
        close_grace_s: float = 2.0,
    ) -> None:
        self._devin_bin = devin_bin
        self._model = model
        self._cwd = cwd
        self._extra_args = list(extra_args or [])
        self._env = dict(env or {})
        self._policy = policy or SwebenchPolicy()
        self._init_timeout = initialize_timeout_s
        self._session_timeout = session_timeout_s
        self._prompt_timeout = prompt_timeout_s
        self._cancel_grace = cancel_grace_s
        self._close_grace = close_grace_s

        self._proc: Optional[subprocess.Popen] = None
        self._tx: Optional[_Ndjrpc] = None
        self._state = "new"  # new -> running -> initialized -> session -> closed
        self._killed = False
        self._closed = False
        self._stderr_chunks: list = []
        self._stderr_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_max_bytes = 1 << 20  # keep at most ~1 MB for diagnostics
        #: join cap for the stderr drain thread in close() — bounded, and a
        # leaked fd + lingering daemon thread beats a hung close().
        self._drain_join_timeout_s = 1.0

        self.session_id: Optional[str] = None
        self.agent_info: dict = {}
        self.last_usage_update: Optional[dict] = None
        self.stderr_tail: str = ""
        #: stream-ordered transcript (updates, permission events, vendor notes)
        self.transcript: list[SessionEvent] = []
        #: every ignored vendor notification (logged, never an error)
        self.vendor_notifications: list[dict] = []

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "AcpSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Spawn the agent process (own process group, env hygiene)."""
        if self._proc is not None:
            raise AcpProtocolError("session already started")
        if self._closed or self._state != "new":
            raise AcpProtocolError(
                "session already closed — AcpSession is one-shot"
            )
        # PLAN §3.4: the refusal fallback must stay off — both the env var
        # and the CLI flag (a refusal must be terminal, never a model switch).
        for arg in self._extra_args:
            if str(arg) == "--refusal-fallback" or str(arg).startswith(
                    "--refusal-fallback="):
                raise AcpProtocolError(
                    "--refusal-fallback is forbidden (PLAN §3.4)"
                )
        # Env hygiene (PLAN §3.4): refusal-fallback is NEVER in the child
        # env, even if the caller supplied it.
        env = dict(os.environ)
        env.update(self._env)
        env.pop("DEVIN_REFUSAL_FALLBACK", None)
        cmd = [self._devin_bin, "acp"]
        if self._model:
            cmd += ["--model", self._model]
        cmd += self._extra_args
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=env,
            start_new_session=True,  # own process group -> killable as a group
        )
        self._tx = _Ndjrpc(self._proc)
        self._state = "running"
        log.debug("spawned agent: pid=%s cmd=%r", self._proc.pid, cmd)
        # Continuously drain stderr in a daemon thread: a chatty agent (WP0
        # saw a huge stderr dump on close) must not fill the 64 KB pipe and
        # stall the agent mid-session, and close() must never block on an
        # unbounded stderr read (a detached grandchild that escapes the group
        # kill but holds the fd would hang it forever). The daemon thread
        # simply lives until the process dies or the fd's last holder closes.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()

    def initialize(self) -> dict:
        """Handshake (``initialize``); returns the agent info verbatim."""
        self._require_state("running")
        # one shared budget for the send and the wait — worst-case wall
        # clock is the configured timeout, not 2x it.
        deadline = time.monotonic() + self._init_timeout
        rid = self._tx.send_request(
            WIRE["INITIALIZE"],
            {"protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}},
            deadline=deadline,
        )
        frame = self._wait_for_response(rid, deadline - time.monotonic())
        result = frame["result"]
        if not isinstance(result, dict):
            raise AcpProtocolError(
                f"initialize result not an object: {result!r}"
            )
        self.agent_info = result
        self._state = "initialized"
        return self.agent_info

    def create_session(self, cwd: Optional[str] = None,
                      mcp_servers: Optional[list] = None) -> str:
        """``session/new`` (benchmark default: ``cwd="/testbed"``)."""
        self._require_state("initialized")
        deadline = time.monotonic() + self._session_timeout
        rid = self._tx.send_request(
            WIRE["SESSION_NEW"],
            {"cwd": cwd or self._cwd or "/testbed",
             "mcpServers": mcp_servers if mcp_servers is not None else []},
            deadline=deadline,
        )
        frame = self._wait_for_response(rid, deadline - time.monotonic())
        result = frame["result"]
        sid = result.get("sessionId") if isinstance(result, dict) else None
        if not sid:
            raise AcpProtocolError(f"session/new result missing sessionId: {result!r}")
        self.session_id = sid
        self._state = "session"
        return sid

    def prompt(self, text: Optional[str] = None,
               blocks: Optional[list] = None,
               timeout: Optional[float] = None) -> PromptResult:
        """``session/prompt`` with streamed ``session/update`` handling.

        Answers ``session/request_permission`` per policy, streams updates
        into :attr:`transcript`, and returns the prompt result with the
        stop-reason + usage captured **verbatim** (WP0 §1). On wall-clock
        timeout: ``session/cancel`` → grace → kill process group →
        :class:`AcpTimeoutError`. On a refusal stop-reason:
        :class:`AcpRefusalError` (terminal — no model switch, no retry).
        """
        self._require_state("session")
        if blocks is None:
            if text is None:
                raise AcpProtocolError("prompt requires text or blocks")
            blocks = [{"type": "text", "text": text}]
        self._state = "prompting"  # after validation — a bad call must not
        timeout_s = (timeout if timeout is not None
                    else (self._prompt_timeout or DEFAULT_PROMPT_TIMEOUT_S))
        t0 = time.monotonic()
        updates_at_start = sum(1 for e in self.transcript if e.kind == "update")
        deadline = t0 + timeout_s  # shared budget for send + wait
        try:
            rid = self._tx.send_request(
                WIRE["SESSION_PROMPT"],
                {"sessionId": self.session_id, "prompt": blocks},
                deadline=deadline,
            )
            frame = self._wait_for_response(rid, deadline - time.monotonic())
        except AcpTimeoutError:
            # Stalled session: cancel, best-effort close, grace, kill the
            # group (PLAN §5 WP1). The close is sent *before* the kill so
            # the wire contract holds even on the stall path.
            self.cancel()
            time.sleep(self._cancel_grace)
            if self.session_id:
                try:
                    self._tx.send_request(
                        WIRE["SESSION_CLOSE"], {"sessionId": self.session_id},
                        deadline=time.monotonic() + 1.0,
                    )
                except (OSError, ValueError, AcpError):
                    pass  # best-effort — the group kill follows
                # brief grace so the server can process the close before
                # the group kill (deterministic for the wire contract)
                time.sleep(self._cancel_grace)
            self._kill_group()
            raise
        except (AcpTransportError, AcpMalformedFrameError):
            # The stream is broken; make sure nothing is left running.
            self._kill_group()
            raise
        updates = sum(1 for e in self.transcript if e.kind == "update") - updates_at_start
        result = frame["result"]
        if not isinstance(result, dict):
            raise AcpProtocolError(f"session/prompt result not an object: {result!r}")
        stop = str(result.get("stopReason") or "")
        if any(m in stop.lower() for m in _REFUSAL_MARKERS):
            # Terminal (PLAN §3.4): surface the error; close() (context
            # manager) handles session/close + group kill on this path too.
            raise AcpRefusalError(stop, raw=result)
        self._state = "session"  # turn complete — session stays usable
        return PromptResult(
            stop_reason=stop,
            usage=result.get("usage"),
            updates=updates,
            duration_s=round(time.monotonic() - t0, 3),
            raw=result,
        )

    def cancel(self) -> None:
        """Best-effort ``session/cancel`` notification for the session.

        Skipped when the transport is broken (a torn NDJSON line in the
        pipe would corrupt this frame); termination is the group kill's job.
        """
        if (self._tx is not None and self._proc is not None
                and self.session_id and self._state not in ("new", "closed")
                and not self._tx.broken):
            try:
                self._tx.send_notification(
                    WIRE["SESSION_CANCEL"], {"sessionId": self.session_id},
                    deadline=time.monotonic()
                    + max(1.0, self._cancel_grace),
                )
            except (OSError, ValueError, AcpError):
                pass  # process already gone — the kill below covers it

    def close(self) -> None:
        """``session/close`` before process exit, then group-kill (all paths).

        WP0 finding: the real server handler-errors on ``session/close`` and
        may survive it — so close is best-effort and termination always
        falls through to the group kill. Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc is None:
                return
            if self.session_id and self._state in ("session", "prompting"):
                if self._state == "prompting":
                    self.cancel()
                if not self._tx.broken:
                    # request (schema: CloseSessionRequest) — its reply is
                    # best-effort: the real server may never answer it.
                    try:
                        self._tx.send_request(
                            WIRE["SESSION_CLOSE"],
                            {"sessionId": self.session_id},
                            deadline=time.monotonic() + self._close_grace,
                        )
                    except (OSError, ValueError, AcpError):
                        pass
            # Grace for a natural exit (the mock honors close; the real
            # server often does not — WP0 §1).
            try:
                self._proc.wait(timeout=self._close_grace)
            except subprocess.TimeoutExpired:
                pass
        finally:
            self._state = "closed"
            # group kill on ALL paths — including a BaseException mid-close
            # (e.g. KeyboardInterrupt in the grace wait) that skips the try
            # body; idempotent via the _killed guard
            self._kill_group()
            # Join the stderr drain thread (bounded) BEFORE touching the
            # stderr pipe: while the thread is inside read1(), close()
            # blocks on the buffer's lock — and a detached grandchild
            # that survived the group kill keeps the write end open, so
            # the join can time out. A leaked fd + a lingering daemon
            # thread beats a hung close(). Joining first also lets
            # _capture_stderr() pick up the final drained bytes.
            drain = self._stderr_thread
            if drain is not None:
                drain.join(timeout=self._drain_join_timeout_s)
            self._capture_stderr()
            self._close_stdin()
            if self._proc is not None:
                self._close_pipe(self._proc.stdout)
                # stderr: only close once the drain thread has exited
                # (no in-flight read1 -> close cannot block on its lock)
                if drain is None or not drain.is_alive():
                    self._close_pipe(self._proc.stderr)

    # -- introspection ---------------------------------------------------

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def returncode(self) -> Optional[int]:
        """The agent process's exit code (None while it is still running)."""
        return self._proc.poll() if self._proc is not None else None

    def group_alive(self) -> bool:
        """True if the agent's process group still exists."""
        if self._proc is None:
            return False
        try:
            os.killpg(self._proc.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # -- internals -------------------------------------------------------

    def _require_state(self, expected: str) -> None:
        if self._state != expected:
            raise AcpProtocolError(
                f"cannot call in state {self._state!r} (expected {expected!r})"
            )

    def _wait_for_response(self, rid: int, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            frame = self._tx.read_frame(deadline)
            self._dispatch(frame)
            if "error" in frame and frame.get("id") != rid:
                # e.g. a server-side parse error for one of our lines —
                # surface it instead of silently waiting out the deadline.
                log.warning(
                    "server error response for unmatched id=%r: %r",
                    frame.get("id"), frame.get("error"),
                )
            if frame.get("id") == rid and ("result" in frame or "error" in frame):
                if "error" in frame:
                    err = frame["error"] if isinstance(frame["error"], dict) else {}
                    raise AcpRpcError(
                        err.get("code"), err.get("message"),
                        request_id=rid, data=err.get("data"),
                    )
                return frame

    def _dispatch(self, frame: dict) -> None:
        method = frame.get("method")
        if method is None:
            return  # a response — handled by the caller
        if "id" in frame:
            # Server-initiated request — the client MUST answer it.
            if method == WIRE["SESSION_PERMISSION"]:
                if self._session_matches(frame):
                    self._answer_permission(frame)
            else:
                log.debug("rejecting unknown server request %s", method)
                try:
                    self._tx.send_response({
                        "jsonrpc": "2.0", "id": frame["id"],
                        "error": {"code": -32601,
                                 "message": f"Method not found: {method}"},
                    }, deadline=time.monotonic() + 2.0)
                except (OSError, ValueError, AcpError):
                    pass
            return
        # Notification.
        if not self._session_matches(frame):
            return
        if method == WIRE["SESSION_UPDATE"]:
            self._on_update(frame)
        elif method.startswith("_"):
            # Vendor notification (e.g. _cognition.ai/mcp/serversChanged):
            # log, record, ignore — never an error, never blocking.
            self.vendor_notifications.append(frame)
            log.info("vendor notification ignored: %s", method)
        else:
            log.debug("ignoring unknown notification: %s", method)

    def _session_matches(self, frame: dict) -> bool:
        """Ignore frames that name a *foreign* session id (stray-session
        guard — a permission request must only be answered for our session)."""
        params = frame.get("params")
        if not isinstance(params, dict):
            return True
        sid = params.get("sessionId")
        if sid and self.session_id and sid != self.session_id:
            log.warning("ignoring %s frame for foreign session %r",
                       frame.get("method"), sid)
            return False
        return True

    def _on_update(self, frame: dict) -> None:
        params = frame.get("params") or {}
        update = params.get("update")
        update = update if isinstance(update, dict) else {}
        kind = update.get("sessionUpdate") or "unknown"
        if kind == "usage_update":
            self.last_usage_update = params
        self.transcript.append(
            SessionEvent(time.time(), "update", kind, params)
        )

    def _answer_permission(self, frame: dict) -> None:
        params = frame.get("params") or {}
        tool = params.get("toolCall")
        tool = tool if isinstance(tool, dict) else {}
        decision = self._policy.decide(params)
        self.transcript.append(SessionEvent(
            time.time(), "permission",
            _tool_name(tool) or "?",
            {"allow": decision.allow, "reason": decision.reason,
             "option_id": decision.option_id},
        ))
        if decision.option_id is not None:
            result = {"outcome": {"outcome": "selected",
                                 "optionId": decision.option_id}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        try:
            self._tx.send_response(
                {"jsonrpc": "2.0", "id": frame["id"], "result": result},
                deadline=time.monotonic() + 2.0,
            )
        except (OSError, ValueError, AcpError):
            pass  # process dying — kill path covers it

    def _kill_group(self) -> None:
        if self._killed or self._proc is None:
            return
        self._killed = True
        try:
            os.killpg(self._proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            log.warning("agent process did not exit after SIGKILL: pid=%s",
                       self._proc.pid)

    def _drain_stderr(self) -> None:
        """Daemon: copy the agent's stderr into a bounded buffer."""
        while True:
            try:
                chunk = self._proc.stderr.read1(65536)
            except (OSError, ValueError):
                return
            except Exception as exc:
                # a silently-dead drain thread re-exposes the 64 KB-pipe
                # mid-session stall, so log before exiting
                log.warning("stderr drain thread exiting: %r", exc)
                return
            if not chunk:
                return  # clean EOF
            with self._stderr_lock:
                self._stderr_chunks.append(chunk)
                total = sum(map(len, self._stderr_chunks))
                while total > self._stderr_max_bytes \
                        and len(self._stderr_chunks) > 1:
                    total -= len(self._stderr_chunks.pop(0))

    def _capture_stderr(self) -> None:
        with self._stderr_lock:
            data = b"".join(self._stderr_chunks)
        self.stderr_tail = data[-4000:].decode("utf-8", "replace")

    def _close_stdin(self) -> None:
        self._close_pipe(self._proc.stdin if self._proc is not None else None)

    @staticmethod
    def _close_pipe(stream) -> None:
        """Close one pipe; unblocks any daemon thread blocked on it."""
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass
