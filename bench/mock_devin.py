#!/usr/bin/env python3
"""``mock_devin`` — WP2 test double: a fake ``devin`` binary (issue #13).

A single-file, pure-stdlib script that masquerades as the ``devin`` binary
so the whole pipeline can be exercised in CI with **zero quota** (PLAN.md
§5 WP2, "the key testability lever"). It speaks the **WP0-verified ACP
subset** (``notes/acp-surface.md``): NDJSON JSON-RPC 2.0 over stdio, the
``devin acp`` **wire method names** (``initialize`` → ``session/new`` →
``session/prompt`` → streamed ``session/update`` → ``session/cancel`` →
``session/request_permission`` (server-initiated, with an id — the client
must answer) → ``session/close``), unsolicited startup vendor
notifications (``_cognition.ai/mcp/serversChanged`` ×2, observed live in
WP0), and the real-wire ``toolCall`` shape (no top-level ``name``; name
via ``_meta["cognition.ai/inferenceToolName"]`` + the ``toolCallId``
prefix; ``kind`` is a spec ``ToolKind``).

Subcommands (the real CLI surface the runner/adapter touches):

* ``mock_devin acp [--model M] [--mode MODE] [--state-file P]``
  — one ACP session. ``--mode`` / ``--state-file`` are **mock-only**
  flags (a test double's extension point); ``MOCK_DEVIN_MODE`` /
  ``MOCK_DEVIN_STATE`` env vars are equivalent (the wrapper scripts the
  tests use set the env vars so the extra CLI args stay on the real
  wire). With no ``--model`` the ``DEVIN_MODEL`` env var is honored
  (WP0 §1: the real ``devin acp`` reads it too). Unknown flags are
  **rejected** (exit 2) so an adapter/runner mistake surfaces instead
  of being silently absorbed. Multi-turn: a second ``session/prompt``
  is answered like the first (per-turn state is reset each turn — the
  WP1 client is multi-turn).
* ``mock_devin models list [--format json|table]`` — the canned
  ``devin models list --format json`` fixture (WP2 AC3): the WP0-captured
  shape (top-level ``{"families": [...]}``), with ``cost_tier`` values and
  **both Free and paid models** — Free is exactly
  ``{swe-2-medium, swe-2-high, swe-2-max}`` (WP0 §7); the paid SWE
  models (``swe-1-6``/``swe-1-7``: ``Low cost``; ``swe-2-*-lightning``:
  ``High cost``) and the non-SWE families (``claude-opus-4.6``,
  ``gpt-6-sol``) carry **verbatim variants** of the WP0 capture's
  families (a representative subset of each captured family;
  ``notes/models-list.json``); cost tiers cover Free, Low cost,
  Med cost and High cost; the ``adaptive`` router variant has **no**
  ``cost_tier`` (the WP3 cost-tier gate is fail-closed on shape
  deviation — this exercises it).
* ``mock_devin auth status`` — canned logged-in status (exit 0), the
  PLAN §2.7 pre-flight probe: a runner driven through this mock must
  not record ``preflight_failed`` on auth.

The canned "agent" (WP2 AC1): reads the task text out of the
``session/prompt`` blocks, streams ``session/update`` notifications,
requests permission for its one edit (a **canned patch** applied to the
session-cwd repo — ``git apply`` when the cwd is a git repo, a direct
write otherwise), **answers its own permission flow** (the patch is
applied only *after* the client's ``session/request_permission`` response
arrives; a denial/``cancelled`` skips it), emits the stop reason +
WP0-shape ``usage`` object, and exits cleanly on ``session/close``.

Modes (WP2 AC2) and the **terminal signal each one presents to the runner**
(PLAN §5 WP3 consumes these — the JSONL status, resume semantics and
artifact atomicity are the WP3 runner's job, issue #14/#15; this mock is
the deterministic signal source those tests drive):

========  ==============  =================================================
mode      terminal signal (what the runner observes)    JSONL status
========  ==============  =================================================
``ok``    ``end_turn`` + usage, canned patch applied    resolved /
          (evaluator decides)                           failed_test
``no_patch``  ``end_turn`` + usage, no edits in the     ``no_patch``
          cwd (a legitimate agent outcome — terminal   (terminal: never
          for pass@1, PLAN §2.5)                        re-run)
``hang``    no prompt response → client wall-clock cap  ``timeout``
          → ``session/cancel`` + group kill            (terminal: never
          (PLAN §2.2/§2.5)                            re-run)
``quota``   stop reason ``quota_limited`` (no usage) +  ``quota_limited``
          a quota note on the CLI stderr (PLAN §2.3   (retry class,
          signals: "ACP stop reason, CLI stderr, or   PLAN §2.5)
          non-zero exit with quota text")
``crash``   process death mid-prompt (stderr dump,     ``agent_error``
          non-zero exit) → client EOF                  (taxonomy WP5)
========  ==============  =================================================

The mock appends an **event file** (JSON lines, stream order) when
``MOCK_DEVIN_STATE`` / ``--state-file`` is set — tests assert the
round-trip (task text as received, ``session/new`` cwd, every permission
response, cancel/close) and the **ordering** of the permission flow
(``permission_response`` strictly before ``patch_applied``: the canned
agent waited for its own permission answer before editing).

Usage::

    mock_devin acp --model swe-2-high --mode ok
    mock_devin models list --format json
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys

MOCK_VERSION = "0.0.0-mock"
VALID_MODES = ("ok", "no_patch", "hang", "quota", "crash")

# ---------------------------------------------------------------------------
# canned data
# ---------------------------------------------------------------------------

#: The one edit the canned agent makes (WP2 AC1: "applies a canned patch
#: to the cwd repo"). A file-creation-only patch: ``git apply``-able in
#: any repo state (no pre-existing file required).
CANNED_FILE = "mock_devin_canned.py"
CANNED_CONTENT = (
    '"""Canned patch applied by mock_devin (WP2 test double, issue #13)."""\n'
    "CANNED = True\n"
    "\n"
    "\n"
    "def ok() -> bool:\n"
    "    return CANNED\n"
)
CANNED_PATCH = (
    "--- /dev/null\n"
    f"+++ b/{CANNED_FILE}\n"
    f"@@ -0,0 +1,{CANNED_CONTENT.count(chr(10))} @@\n"
    + "".join("+" + line for line in CANNED_CONTENT.splitlines(keepends=True))
)

#: Deterministic usage — the WP0 live capture values (notes/acp-surface.md
#: §1), so transcript/usage assertions are stable across runs.
CANNED_USAGE = {
    "inputTokens": 11996,
    "outputTokens": 30,
    "cachedReadTokens": 128,
    "totalTokens": 12026,
}

#: ACP-spec PermissionOptionKind values — the WP1 client matches them
#: exactly, so the canned options cannot be mis-picked by a substring.
CANNED_OPTIONS = [
    {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
    {"optionId": "reject_once", "kind": "reject_once", "name": "Reject once"},
]

#: The canned ``devin models list --format json`` fixture (WP2 AC3). Shape
#: is the WP0 capture (notes/models-list.json, 444 KB): top-level
#: ``families[]`` with ``family_label``/``family_uid``/``slug``/
#: ``aliases[]``/``variants[]``; variants carry ``model_uid``/``label`` and
#: **optional** ``cost_tier``/``cost_summary``/``max_context_tokens``/
#: ``max_output_tokens``. Cost tiers cover Free, Low cost, Med cost and
#: the absent-tier ``adaptive`` router (WP0 §7) — both Free and paid
#: models are present, as the WP3 cost-tier gate requires.
MODELS_LIST_FIXTURE = {
    "families": [
        {
            "family_label": "SWE-2",
            "family_uid": "swe-2",
            "slug": "swe-2",
            "aliases": ["swe"],
            "variants": [
                {
                    "model_uid": "swe-2-high",
                    "label": "SWE-2 High",
                    "max_context_tokens": 262000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Free",
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-2-medium",
                    "label": "SWE-2 Medium",
                    "max_context_tokens": 262000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Free",
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-2-max",
                    "label": "SWE-2 Max",
                    "max_context_tokens": 262000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Free",
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-2-medium-lightning",
                    "label": "SWE-2 Medium Lightning",
                    "max_context_tokens": 262144,
                    "max_output_tokens": 128000,
                    "cost_tier": "High cost",
                    "cost_summary": (
                        "$1 / 1M Input \u00b7 $0.1 / 1M Cached input"
                        " \u00b7 $5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-2-high-lightning",
                    "label": "SWE-2 High Lightning",
                    "max_context_tokens": 262144,
                    "max_output_tokens": 128000,
                    "cost_tier": "High cost",
                    "cost_summary": (
                        "$1 / 1M Input \u00b7 $0.1 / 1M Cached input"
                        " \u00b7 $5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-2-max-lightning",
                    "label": "SWE-2 Max Lightning",
                    "max_context_tokens": 262144,
                    "max_output_tokens": 128000,
                    "cost_tier": "High cost",
                    "cost_summary": (
                        "$1 / 1M Input \u00b7 $0.1 / 1M Cached input"
                        " \u00b7 $5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
        {
            "family_label": "SWE-1.7",
            "family_uid": "swe-1.7",
            "slug": "swe-1.7",
            "aliases": [],
            "variants": [
                {
                    "model_uid": "swe-1-7",
                    "label": "SWE-1.7 Max",
                    "max_context_tokens": 262000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Low cost",
                    "cost_summary": (
                        "$0.5 / 1M Input \u00b7 $0.2 / 1M Cached input"
                        " \u00b7 $2.5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "swe-1-7-medium",
                    "label": "SWE-1.7 Medium",
                    "max_context_tokens": 262000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Low cost",
                    "cost_summary": (
                        "$0.5 / 1M Input \u00b7 $0.2 / 1M Cached input"
                        " \u00b7 $2.5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
        {
            "family_label": "SWE-1.6",
            "family_uid": "swe-1.6",
            "slug": "swe-1.6",
            "aliases": [],
            "variants": [
                {
                    "model_uid": "swe-1-6",
                    "label": "SWE-1.6",
                    "max_context_tokens": 200000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Low cost",
                    "cost_summary": (
                        "$0.5 / 1M Input \u00b7 $0.2 / 1M Cached input"
                        " \u00b7 $2.5 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
        # A non-SWE family — a gate that keyed on the "swe" prefix
        # instead of cost_tier would miss it. Verbatim from the WP0
        # capture (notes/models-list.json, family "claude-opus-4.6").
        {
            "family_label": "Claude Opus 4.6",
            "family_uid": "claude-opus-4.6",
            "slug": "claude-opus-4.6",
            "aliases": [],
            "variants": [
                {
                    "model_uid": "claude-opus-4-6",
                    "label": "Claude Opus 4.6",
                    "max_context_tokens": 200000,
                    "max_output_tokens": 128000,
                    "cost_tier": "High cost",
                    "cost_summary": (
                        "$5 / 1M Input \u00b7 $0.5 / 1M Cached input"
                        " \u00b7 $25 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
                {
                    "model_uid": "claude-opus-4-6-1m",
                    "label": "Claude Opus 4.6 1M",
                    "max_context_tokens": 1000000,
                    "max_output_tokens": 128000,
                    "cost_tier": "High cost",
                    "cost_summary": (
                        "$5 / 1M Input \u00b7 $0.5 / 1M Cached input"
                        " \u00b7 $25 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
        # Verbatim from the WP0 capture (family "gpt-6-sol") — the
        # ``Med cost`` tier (179 variants in the real capture; the most
        # common *paid* tier is High cost) must be present, not just
        # Low/High.
        {
            "family_label": "GPT-6 Sol",
            "family_uid": "gpt-6-sol",
            "slug": "gpt-6-sol",
            "aliases": [],
            "variants": [
                {
                    "model_uid": "gpt-6-sol-medium",
                    "label": "GPT-6 Sol Medium Thinking",
                    "max_context_tokens": 1000000,
                    "max_output_tokens": 128000,
                    "cost_tier": "Med cost",
                    "cost_summary": (
                        "$2 / 1M Input \u00b7 $0.2 / 1M Cached input"
                        " \u00b7 $10 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
        # WP0 §7: the adaptive router has **no** cost_tier — the WP3
        # fail-closed gate (shape deviation) must handle it.
        {
            "family_label": "Adaptive",
            "family_uid": "Adaptive",
            "slug": "adaptive",
            "aliases": [],
            "variants": [
                {
                    "model_uid": "adaptive",
                    "label": "Adaptive",
                    "description": "Automatically balances quality and cost",
                    "cost_summary": (
                        "$0.5 / 1M Input \u00b7 $0.1 / 1M Cached input"
                        " \u00b7 $2 / 1M Output"
                    ),
                    "is_new": False,
                    "is_beta": False,
                },
            ],
        },
    ]
}

#: Canned ``devin auth status`` output (PLAN §2.7 pre-flight probe):
#: the runner's check is "auth status ok" — a logged-in status, exit 0.
#: Shaped like the real CLI output (notes/auth-status.txt) with a mock
#: identity; no real account facts are embedded.
AUTH_STATUS_OUTPUT = """Logged in (via Devin).

Credentials:
  File:              $XDG_DATA_HOME/devin/credentials.toml
  API server:        https://server.codeium.com
  Devin webapp:      https://app.devin.ai
  Devin API:         https://api.devin.ai

User:
  Name:              mock-devin
  User ID:           user-mock0000000000000000000000

Account:
  Tier:              mock
  Plan:              mock
  Enterprise:        no
  Telemetry:         disabled
"""

# ---------------------------------------------------------------------------
# the ACP server (canned agent)
# ---------------------------------------------------------------------------


def _acp(argv: list) -> int:
    # An *empty* env value is validated like an empty ``--mode`` (exit
    # 2) — ``"" or "ok"`` would silently absorb a typo'd wrapper, which
    # is exactly what the strict CLI exists to prevent.
    env_mode = os.environ.get("MOCK_DEVIN_MODE")
    mode = "ok" if env_mode is None else env_mode
    state_path = os.environ.get("MOCK_DEVIN_STATE") or None
    model = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--model":
            i += 1
            if i >= len(argv):
                return _cli_error("--model requires a value")
            model = argv[i]
        elif a.startswith("--model="):
            model = a.split("=", 1)[1]
        elif a == "--mode":  # mock-only extension flag
            i += 1
            if i >= len(argv):
                return _cli_error("--mode requires a value")
            mode = argv[i]
        elif a.startswith("--mode="):
            mode = a.split("=", 1)[1]
        elif a == "--state-file":  # mock-only extension flag
            i += 1
            if i >= len(argv):
                return _cli_error("--state-file requires a value")
            state_path = argv[i]
        elif a.startswith("--state-file="):
            state_path = a.split("=", 1)[1]
        else:
            return _cli_error(
                f"unknown argument for 'acp': {a!r} "
                "(mock_devin is strict: the real CLI would reject it too)"
            )
        i += 1
    # WP0 §1: the real `devin acp` reads DEVIN_MODEL when --model is
    # absent — the mock honors it the same way (the runner's model gate
    # can then assert the requested model in the state file).
    if model is None:
        model = os.environ.get("DEVIN_MODEL")
    if mode not in VALID_MODES:
        return _cli_error(
            f"unknown --mode {mode!r} (valid: {', '.join(VALID_MODES)})"
        )

    def state(event: str, **kw) -> None:
        rec = {"event": event, **kw}
        if state_path:
            d = os.path.dirname(state_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(state_path, "a") as f:
                f.write(json.dumps(rec) + "\n")

    def send(obj) -> None:
        # NDJSON: json.dumps escapes every control character, so one
        # message always stays on one line (WP0-verified framing).
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def note(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    state(
        "started",
        pid=os.getpid(),
        mode=mode,
        model=model,
        # env fact: the WP1 client must strip DEVIN_REFUSAL_FALLBACK
        # (PLAN §3.4) — ABSENT here means the client did its job.
        devin_refusal_fallback=os.environ.get(
            "DEVIN_REFUSAL_FALLBACK", "ABSENT"
        ),
    )

    # Unsolicited startup vendor notifications (WP0: the real `devin acp`
    # emits _cognition.ai/mcp/serversChanged at boot, before any client
    # request). The client must log + ignore, never error.
    for _ in range(2):
        send({"jsonrpc": "2.0", "method": "_cognition.ai/mcp/serversChanged",
              "params": {}})

    session_cwd = os.getcwd()
    prompt_rid = None
    perm_rid = 99  # pre-incremented per turn: turn 1's request is 100,
    # turn 2's is 101, … — per-turn-unique so a stale response can't
    # be misattributed across turns
    pending_permission = False
    prompt_done = False

    def tool_call(name: str, kind: str, raw: dict) -> dict:
        # The WP0-captured real wire shape (no top-level "name" field;
        # name via _meta + toolCallId prefix; kind is a spec ToolKind).
        return {
            "toolCallId": name + ":0#mock",
            "title": name,
            "kind": kind,
            "rawInput": raw,
            "_meta": {"cognition.ai/inferenceToolName": name},
        }

    def finish_prompt() -> None:
        """End the prompt turn: vendor note + (mode-specific) response.

        * ok / no_patch: usage_update + ``end_turn`` + the WP0-shape
          usage object (PLAN §2.6: captured verbatim by the client).
        * quota: the PLAN §2.3 stop-reason signal + a CLI-stderr note —
          **no** usage object (the quota hit ends the turn before any
          metered work).
        """
        nonlocal prompt_done
        if prompt_done:
            return
        prompt_done = True
        # Interleaved vendor notification (WP0 run 2 observed
        # _cognition.ai/thinking_complete / turn_stats mid-session).
        send({"jsonrpc": "2.0", "method": "_cognition.ai/turn_stats",
              "params": {"turns": 1}})
        if mode in ("ok", "no_patch"):
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": SESSION_ID,
                            "update": {"sessionUpdate": "usage_update",
                                      "used": CANNED_USAGE["totalTokens"],
                                      "size": 262000}}})
            send({"jsonrpc": "2.0", "id": prompt_rid,
                 "result": {"stopReason": "end_turn",
                           "usage": dict(CANNED_USAGE)}})
        elif mode == "quota":
            note("mock-devin: daily usage quota exhausted "
                 "(rate limit reached; resume in the next quota window)")
            send({"jsonrpc": "2.0", "id": prompt_rid,
                 "result": {"stopReason": "quota_limited"}})

    def apply_canned_patch() -> None:
        """WP2 AC1: apply the canned patch to the session-cwd repo.

        ``git apply`` when the cwd is a git repo (the sweb.eval layout);
        a direct write otherwise (a non-repo dry-run cwd still gets the
        patch on disk). Best-effort + recorded: a failure — including
        *both* paths failing (e.g. the session cwd does not exist) — is
        a ``patch_failed`` event, not a crash: the mock must stay alive
        for the wire contract and the turn still completes.
        """
        target = os.path.join(session_cwd, CANNED_FILE)
        via = None
        try:
            r = subprocess.run(
                ["git", "-C", session_cwd, "apply", "-"],
                input=CANNED_PATCH.encode(),
                capture_output=True, timeout=30,
            )
            if r.returncode == 0:
                via = "git-apply"
        except (OSError, subprocess.SubprocessError):
            pass  # git missing / cwd gone / not a repo -> direct write
        if via is None:
            try:
                with open(target, "w") as f:
                    f.write(CANNED_CONTENT)
                via = "direct-write"
            except OSError as exc:
                state("patch_failed", reason=str(exc))
                return
        state("patch_applied", via=via, path=target)

    SESSION_ID = "mock-sid"

    def handle(obj) -> None:
        nonlocal prompt_rid, pending_permission, prompt_done, session_cwd, perm_rid
        m = obj.get("method")
        rid = obj.get("id")

        if m == "initialize":
            # Mirrors the WP0 live capture verbatim (notes/
            # acp-probe-handshake2.frames.jsonl): promptCapabilities /
            # mcpCapabilities / sessionCapabilities (incl.
            # close._meta["cognition.ai/force"]) / auth / authMethods
            # and the cognition.ai/* _meta flag block. agentInfo keeps
            # the mock identity (the captured name is a devin codename);
            # the top-level _meta (mcpConfigPath) is a probe-host
            # artifact, not a capability, and is omitted.
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True, "audio": False,
                                         "embeddedContext": True},
                    "mcpCapabilities": {"http": True, "sse": True},
                    "sessionCapabilities": {
                        "list": {},
                        "delete": {},
                        "additionalDirectories": {},
                        "close": {"_meta": {"cognition.ai/force": True}}},
                    "auth": {},
                    "_meta": {
                        "cognition.ai/multiRootWorkspace": True,
                        "cognition.ai/sessionListContentSearch": True,
                        "cognition.ai/sessionListOrderBy":
                            ["updated_at", "created_at"],
                        "cognition.ai/sessionRename": True,
                        "cognition.ai/persistOnCreate": True,
                        "cognition.ai/sessionShare": True,
                        "cognition.ai/documentLifecycle": True,
                        "cognition.ai/userEdits": True,
                        "cognition.ai/terminalLifecycle": True,
                        "cognition.ai/userConfig": True,
                        "cognition.ai/userShellCommand": True,
                        "cognition.ai/editableCommands": True,
                        "cognition.ai/commandRevision": True,
                        "cognition.ai/chains": True,
                        "cognition.ai/megaplan": True,
                        "cognition.ai/ruleMentions": True,
                        "cognition.ai/midTurnPrompts": True}},
                "authMethods": [{"id": "devin-browser",
                                "name": "Log in with browser",
                                "description": "Sign in via your browser"}],
                "agentInfo": {"name": "mock-devin",
                             "title": "Devin Agent",
                             "version": MOCK_VERSION}}})
            return
        if m == "session/new":
            params = obj.get("params") or {}
            cwd = params.get("cwd")
            if cwd:
                session_cwd = str(cwd)
            state("session_new", cwd=session_cwd)
            # Mirrors the WP0 live capture: the real session/new result
            # carries 5 modes (incl. plan/bypass) + configOptions
            # (mode/model/thought_level/speed) + the lock _meta. The
            # model option list is a representative subset of the real
            # one (the captured list is account-specific, 150+ entries);
            # the shape is verbatim.
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": SESSION_ID,
                "modes": {"currentModeId": "accept-edits",
                         "availableModes": [
                             {"id": "accept-edits", "name": "Code"},
                             {"id": "smart", "name": "Smart"},
                             {"id": "ask", "name": "Ask"},
                             {"id": "plan", "name": "Plan"},
                             {"id": "bypass",
                              "name": "Bypass Permissions"}]},
                "configOptions": [
                    {"id": "mode", "name": "Session Mode",
                     "category": "mode", "type": "select",
                     "currentValue": "accept-edits",
                     "options": [
                         {"value": "accept-edits", "name": "Code",
                          "description": "Write and edit code",
                          "_meta": {"cognition.ai/icon": "code"}},
                         {"value": "smart", "name": "Smart",
                          "description":
                              "Auto-approve actions the model judges safe",
                          "_meta": {"cognition.ai/icon": "sparkles"}},
                         {"value": "ask", "name": "Ask",
                          "description":
                              "Answer questions without code changes",
                          "_meta": {"cognition.ai/icon": "message-circle"}},
                         {"value": "plan", "name": "Plan",
                          "description":
                              "Plan changes before implementing",
                          "_meta": {"cognition.ai/icon": "file-text"}},
                         {"value": "bypass",
                          "name": "Bypass Permissions",
                          "description":
                              "Auto-approve all tool calls unless your "
                              "organization's policy requires approval",
                          "_meta": {"cognition.ai/icon": "shield-off"}}]},
                    {"id": "model", "name": "Model",
                     "description": "AI model to use",
                     "category": "model", "type": "select",
                     "currentValue": model or "swe-2-high",
                     "options": [
                         {"value": "adaptive", "name": "Adaptive",
                          "_meta": {"cognition.ai/supportsImages": True}},
                         {"value": "swe-2-high", "name": "SWE-2",
                          "_meta": {"cognition.ai/supportsImages": True}},
                         {"value": "claude-opus-4-6",
                          "name": "Claude Opus 4.6",
                          "_meta": {"cognition.ai/supportsImages": True}},
                         {"value": "gpt-6-sol-medium",
                          "name": "GPT-6 Sol",
                          "_meta": {"cognition.ai/supportsImages": True}}]},
                    {"id": "thought_level", "name": "Thinking",
                     "category": "thought_level", "type": "select",
                     "currentValue": "high",
                     "options": [
                         {"value": "medium", "name": "Medium"},
                         {"value": "high", "name": "High"},
                         {"value": "max", "name": "Max"}]},
                    {"id": "speed", "name": "Speed",
                     "category": "model_config", "type": "select",
                     "currentValue": "standard",
                     "options": [
                         {"value": "standard", "name": "Standard"},
                         {"value": "fast", "name": "Fast"}]},
                ],
                "_meta": {"cognition.ai/isLocked": False,
                         "cognition.ai/lockHolderPid": None}}})
            return
        if m == "session/prompt":
            # A fresh turn: reset the per-turn state so a *second*
            # session/prompt is answered like the first (the WP1 client
            # is multi-turn — _state returns to "session" after each
            # turn; a stale prompt_done would silently swallow the
            # response and the client would burn its wall-clock cap,
            # an indistinguishable wrong `timeout` signal).
            prompt_done = False
            pending_permission = False
            perm_rid += 1  # per-turn-unique permission request id
            prompt_rid = rid
            text = "".join(
                b.get("text", "")
                for b in (obj.get("params") or {}).get("prompt", [])
                if isinstance(b, dict) and b.get("type") == "text")
            state("prompt", text=text)

            if mode == "hang":
                # Stall after one update: never answer the prompt. The
                # client's wall-clock cap fires (PLAN §2.2) -> cancel +
                # group kill -> runner status `timeout`.
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"sessionId": SESSION_ID,
                                "update": {"sessionUpdate":
                                          "agent_thought_chunk"}}})
                return
            if mode == "crash":
                # Mid-prompt process death: stderr dump + non-zero exit.
                # The client sees EOF -> AcpTransportError -> runner
                # status `agent_error`.
                note("mock-devin: thread 'main' panicked at mock: "
                     "simulated agent crash (WP2 'crash' mode)")
                note("note: run with RUST_BACKTRACE=1 for a backtrace")
                sys.exit(1)

            if mode == "quota":
                # PLAN §2.3: the quota hit lands before any agent work —
                # no updates, no permission round-trip, just the stop
                # reason (and the stderr note in finish_prompt).
                finish_prompt()
                return

            # ok / no_patch: the canned agent reads the task (recorded
            # above) and streams its thought + a summary of the task
            # text (the round-trip echo proves NDJSON framing in both
            # directions).
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": SESSION_ID,
                            "update": {"sessionUpdate":
                                      "agent_thought_chunk",
                                      "content": {"type": "text",
                                                 "text":
                                                 "analyzing task"}}}})
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": SESSION_ID,
                            "update": {"sessionUpdate":
                                      "agent_message_chunk",
                                      "content": {"type": "text",
                                                 "text":
                                                 text[:200]}}}})
            if mode == "ok":
                # WP2 AC1: the canned agent requests permission for its
                # edit before making it — a spec ToolKind "edit" with
                # the declared path inside the session cwd (the WP1
                # SwebenchPolicy allows in-scope edits; a denying policy
                # must make the agent skip the patch).
                target = os.path.join(session_cwd, CANNED_FILE)
                state("permission_request", tool="write", path=target)
                send({"jsonrpc": "2.0", "id": perm_rid,
                      "method": "session/request_permission",
                      "params": {"sessionId": SESSION_ID,
                                "toolCall": tool_call(
                                    "write", "edit",
                                    {"file_path": target}),
                                "options": list(CANNED_OPTIONS)}})
                pending_permission = True
                # The agent now **waits** for the client's answer: the
                # patch is applied only in the permission-response
                # branch below (the event file records the ordering).
                return
            # no_patch: the agent finished its turn without any edits —
            # a legitimate agent outcome (PLAN §2.5: terminal `no_patch`
            # for pass@1). End the turn now; no permission round-trip.
            finish_prompt()
            return

        if m == "session/cancel":
            state("cancel")
            # A cancelled turn is abandoned: reset the per-turn state so
            # a subsequent prompt starts fresh (a stale pending
            # permission/prompt_done would swallow or misattribute its
            # frames). A hang stays hung: cancel is a request the
            # stalled agent never processes; the client's group kill is
            # what ends it.
            pending_permission = False
            prompt_done = False
            return
        if m == "session/close":
            state("close")
            # The mock honors close (the real server's handler-errors and
            # survives it — WP0 §1; the client group-kills either way,
            # so a clean exit here cannot mask a kill-path bug).
            if mode in ("ok", "no_patch", "quota"):
                sys.exit(0)
            return

        if m is None and rid == perm_rid and pending_permission:
            # The client's answer to our permission request (WP2 AC1:
            # the canned agent answers its own permission flow — the
            # outcome decides whether the canned patch is applied).
            # ``m is None``: a response, not a request — a client
            # request whose id collides with the outstanding perm_rid
            # is legal (independent id namespaces per direction) and
            # must get its own answer, not be consumed as the
            # permission answer.
            pending_permission = False
            res = obj.get("result") or {}
            outcome = res.get("outcome") or {}
            kind = outcome.get("outcome")
            oid = outcome.get("optionId")
            # The options list is ours: allow_once == allow,
            # reject_once == deny, cancelled == deny (the client fails
            # closed into "cancelled" when it cannot express a deny).
            allowed = (kind == "selected" and oid == "allow_once")
            state("permission_response",
                  outcome=kind, option_id=oid, allow=allowed)
            if allowed:
                apply_canned_patch()
            else:
                state("patch_skipped",
                      reason="permission denied or cancelled")
            # The answer completes the turn (ok mode only — no_patch
            # never asks permission, quota never reaches a permission
            # round-trip).
            if not prompt_done:
                finish_prompt()
            return
        if m is not None and rid is not None:
            # Unknown method with an id: the real server answers with a
            # JSON-RPC error, it does not crash (WP0 negative control).
            send({"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601,
                          "message": "Method not found"}})
            # (WP0 capture verbatim: the real server's message carries
            # no method-name suffix — round 6 [nit])
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
            break  # client closed stdin (or died) -> clean exit
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # The real server answers non-JSON lines with a JSON-RPC
                # parse error and keeps serving (WP0 negative control) —
                # it does not crash.
                bad = line[:200].decode("utf-8", "replace")
                state("bad_line", line=bad)
                send({"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error",
                              "data": {"line": bad}}})
                continue
            handle(obj)
    # stdin EOF: exit cleanly (the group kill covers the hang mode).
    sys.exit(0)


def _cli_error(msg: str) -> int:
    print(f"mock-devin: {msg}", file=sys.stderr, flush=True)
    return 2


def _auth_status(argv: list) -> int:
    if argv:
        return _cli_error(f"unknown argument for 'auth status': {argv[0]!r}")
    print(AUTH_STATUS_OUTPUT, end="")
    return 0


def _models_list(argv: list) -> int:
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--format":
            i += 1
            if i >= len(argv):
                return _cli_error("--format requires a value")
            if argv[i] == "json":
                as_json = True
            elif argv[i] == "table":
                as_json = False
            else:
                return _cli_error(
                    f"unknown --format {argv[i]!r} (json|table)")
        elif a.startswith("--format="):
            v = a.split("=", 1)[1]
            if v == "json":
                as_json = True
            elif v == "table":
                as_json = False
            else:
                return _cli_error(f"unknown --format {v!r} (json|table)")
        else:
            return _cli_error(f"unknown argument for 'models list': {a!r}")
        i += 1
    if as_json:
        print(json.dumps(MODELS_LIST_FIXTURE, indent=2))
        return 0
    # table (the real CLI's default): uid / tier / cost summary.
    print(f"{'MODEL UID':<28} {'COST TIER':<12} COST SUMMARY")
    for fam in MODELS_LIST_FIXTURE["families"]:
        for v in fam["variants"]:
            print(f"{v['model_uid']:<28} "
                  f"{v.get('cost_tier', '-'):<12} "
                  f"{v.get('cost_summary', '')}")
    return 0


def main(argv: list) -> int:
    if not argv or argv[0] in ("--help", "help"):
        print(
            "mock-devin " + MOCK_VERSION + " (WP2 test double)\n"
            "usage:\n"
            "  mock_devin acp [--model M] [--mode "
            + "|".join(VALID_MODES) + "] [--state-file P]\n"
            "  mock_devin models list [--format json|table]\n"
            "  mock_devin auth status\n"
        )
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "acp":
        return _acp(rest)
    if cmd == "models":
        if len(rest) >= 1 and rest[0] == "list":
            return _models_list(rest[1:])
        if rest:
            return _cli_error(f"unknown subcommand {rest[0]!r} for 'models'")
        return _cli_error("models requires a subcommand (list)")
    if cmd == "auth":
        if len(rest) >= 1 and rest[0] == "status":
            return _auth_status(rest[1:])
        if rest:
            return _cli_error(f"unknown subcommand {rest[0]!r} for 'auth'")
        return _cli_error("auth requires a subcommand (status)")
    if cmd == "--version":
        print(f"mock-devin {MOCK_VERSION}")
        return 0
    return _cli_error(f"unknown subcommand {cmd!r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
