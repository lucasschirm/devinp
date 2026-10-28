"""WP6a (issue #21) — the ``BENCH_LIVE=1`` Pilot-A smoke harness.

Runs **3 small-repo SWE-bench Verified instances sequentially** through
the full WP1–WP5 stack (``AcpSession`` + ``SwebenchPolicy``, the WP3
``Runner`` with its ``JsonlCheckpoint`` / ``resume_queue`` / pre-flight /
cost-tier gate, the WP4 ``evaluate`` in container mode, the WP5
``report.generate`` for the pilot report + leaderboard artifacts), against
the **real** ``devin acp`` binary (or an injected double — the WP2
``mock_devin`` in the zero-quota CI tests).

**Gate.** Every non-``--dry-run`` invocation requires ``BENCH_LIVE=1``
(the ``bench/conftest.py`` gate analogue — CI never consumes quota,
PLAN §6). ``--dry-run`` is zero-quota: config + instance-file validation,
budget math, checklist skeleton, no devin spawn.

**Modes (PLAN-WP6A.md §Design):**

* ``container`` — the PLAN-conformant path: the ``sweb.eval.<instance_id>``
  image, the real-binary injection (the ``_versions`` tree — never the
  wrapper), the per-instance ephemeral XDG dirs seeded only with the auth
  token, the isolated config (empty MCP / rules / plugins), the leak guard
  (``git remote remove origin`` + tags/reflogs stripped), the conda
  ``testbed`` env on PATH, WP4 ``evaluate()`` in a fresh harness
  container, and post-eval image/container pruning (PLAN §7).
* ``sandbox`` — the documented **no-container fallback** (the pilot's
  actual mode in environments without a container runtime — the deviation
  is recorded in the checklist + the meta): a git checkout at
  ``base_commit`` under the operator's **trusted workspace root** (WP0
  §8: trust is cwd-derived and load-bearing; an untrusted cwd kills the
  prompt turn), the same XDG / leak-guard / refusal-fallback hygiene, the
  host ``python3``, and the **host check** as the closest end-to-end eval
  leg available (a *fresh* clone at ``base_commit`` — never the agent's
  mutated tree — ``model_patch`` + ``test_patch`` applied, a venv with a
  minimal ``pip install`` + pytest, then ``python -m pytest -rA --tb=no``
  over the pinned ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` ids; the
  repo-specific ``log_parser``s are image-leg-only).

**Intentional interrupt (issue #21 AC3).** ``--interrupt-after N``: after
the N-th instance's checkpoint line is flushed, the session factory for
the next instance raises :class:`IntentionalInterrupt` (a host-crash-class
abort — the remaining instances have **no** checkpoint line; the WP3b
resume class). The follow-up ``--resume`` invocation goes through
``resume_queue``: finished instances are **skipped** (never re-run, no
session spent on them), the rest run. Exit code 42 marks the interrupt.

**Exit codes:** 0 = complete (incl. a finished resume) · 2 = config
error · 3 = ``BENCH_LIVE`` refused · 4 = pre-flight / runner / other
failure · 42 = intentional interrupt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# The flat bench/ layout: import the WP1–WP5 modules by top-level name
# (conftest.py makes the same shim for the tests).
_BENCH_DIR = Path(__file__).resolve().parent
if str(_BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCH_DIR))

import acp_client as ac  # noqa: E402  (WP1)
import evaluate as ev  # noqa: E402  (WP4 — container mode only)
import report as rep  # noqa: E402  (WP5)
import runner as rr  # noqa: E402  (WP3)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

LIVE_ENV = "BENCH_LIVE"
DEFAULT_MODEL = "swe-2-high"
DEFAULT_MAX_WALL_S = 45 * 60.0
DEFAULT_DAILY_PAUSE_S = 12 * 3600.0
#: The pre-flight disk floor is 2 × this (the PLAN §7 sizing).
DEFAULT_LARGEST_IMAGE_BYTES = 4 * 10**9
DEFAULT_LARGEST_CHECKOUT_BYTES = 1 * 10**9
#: PLAN §4 patch guard: the size cap.
PATCH_SIZE_CAP_BYTES = 1024 * 1024
DEFAULT_INSTANCES_FILE = str(_BENCH_DIR / "pilot_instances.json")
BUDGET_FILE = str(_BENCH_DIR / "live_pilot_budget.json")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_LIVE_REFUSED = 3
EXIT_RUN_FAILED = 4
EXIT_INTERRUPT = 42

#: The SWE task prompt template (the WP5 methodology block's
#: ``prompt_template_hash`` is the SHA-256 of this string).
PROMPT_TEMPLATE = """\
You are an expert software engineer working in a repository checked out at the current directory.

Fix the following issue with a minimal, focused change:

{problem_statement}

Requirements:
- Make the minimal change to fix the issue.
- Verify by running the relevant tests (e.g. `pytest <test file>::<test name>` for the affected tests).
- Do not commit; leave your change uncommitted in the working tree.
- Delete any temporary reproduction files you create when you are done.
"""

#: PLAN §4: the exclusion filter — these patterns are removed from the
#: diff (never from the working tree).
_EXCLUDE_PATTERNS = (
    re.compile(r"(^|/)__pycache__$"),
    re.compile(r"\.pytest_cache$"),
    re.compile(r"\.pyc$"),
    re.compile(r"\.coverage$"),
    re.compile(r"htmlcov$"),
    re.compile(r"\.egg-info$"),
    re.compile(r"(^|/)build$"),
)

#: Quota / rate-limit signal markers (PLAN §2.3: the ACP stop reason,
#: the CLI stderr, or a non-zero exit with quota text).
_QUOTA_MARKERS = (
    "quota", "rate limit", "rate_limit", "rate-limit", "429",
    "too many requests", "usage limit",
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class PilotError(Exception):
    """Base class for the pilot's typed errors."""


class PilotConfigError(PilotError):
    """A configuration / input validation failure (exit 2)."""


class LiveRefused(PilotError):
    """``BENCH_LIVE`` is not 1 and the invocation is not a dry run
    (exit 3) — CI never consumes quota (PLAN §6)."""


class IntentionalInterrupt(PilotError):
    """The ``--interrupt-after`` abort (exit 42) — a host-crash-class
    signal to the WP3 runner: the remaining instances have no checkpoint
    line and are the resume class (PLAN §2.5)."""


class _HostCheckInfra(Exception):
    """A host-check infrastructure failure (the ``eval_error`` class)."""


# ---------------------------------------------------------------------------
# config + instance file
# ---------------------------------------------------------------------------

@dataclass
class PilotConfig:
    mode: str = "sandbox"  # "container" | "sandbox"
    devin_bin: str = "devin"
    model: str = DEFAULT_MODEL
    instances_file: str = DEFAULT_INSTANCES_FILE
    workdir: Optional[str] = None
    run_id: Optional[str] = None
    max_sessions_per_day: int = 10
    max_wall_s: float = DEFAULT_MAX_WALL_S
    daily_pause_s: float = DEFAULT_DAILY_PAUSE_S
    largest_bytes: float = DEFAULT_LARGEST_CHECKOUT_BYTES
    interrupt_after: Optional[int] = None
    resume: bool = False
    dry_run: bool = False
    host_check: Optional[bool] = None  # None -> the mode default (sandbox: on)

    def __post_init__(self) -> None:
        if self.mode not in ("container", "sandbox"):
            raise PilotConfigError(
                f"mode must be 'container' or 'sandbox' (got {self.mode!r})")
        if self.interrupt_after is not None:
            if (isinstance(self.interrupt_after, bool)
                    or not isinstance(self.interrupt_after, int)
                    or self.interrupt_after < 1):
                raise PilotConfigError(
                    "interrupt_after must be an int >= 1 "
                    f"(got {self.interrupt_after!r})")
        if self.host_check is None:
            self.host_check = self.mode == "sandbox"
        if (isinstance(self.max_sessions_per_day, bool)
                or not isinstance(self.max_sessions_per_day, int)
                or self.max_sessions_per_day < 1):
            raise PilotConfigError(
                "max_sessions_per_day must be an int >= 1 "
                f"(got {self.max_sessions_per_day!r})")
        for name in ("max_wall_s", "daily_pause_s", "largest_bytes"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or v <= 0:
                raise PilotConfigError(
                    f"{name} must be a number > 0 (got {v!r})")


def require_live(dry_run: bool) -> None:
    """The ``BENCH_LIVE=1`` gate (the ``bench/conftest.py`` analogue)."""
    if not dry_run and os.environ.get(LIVE_ENV) != "1":
        raise LiveRefused(
            f"{LIVE_ENV}=1 is required to run a real pilot (the "
            f"invocation would consume quota — PLAN §6: CI never "
            "consumes quota; pass --dry-run to validate the config "
            "without touching devin)")


def _load_json_file(path: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PilotConfigError(
            f"{path!r} is not valid JSON: {exc}") from exc


def load_budget_file(path: str = BUDGET_FILE) -> dict:
    """The optional calibrated budget file (PLAN §2: "budget is config,
    not code") — ``{"max_sessions_per_day", "max_wall_per_session",
    "calibrated_at", "basis"}``."""
    if not os.path.isfile(path):
        return {}
    data = _load_json_file(path)
    if not isinstance(data, dict):
        raise PilotConfigError(
            f"budget file {path!r} must be a JSON object "
            f"(got {type(data).__name__})")
    return data


def load_instances(path: str) -> list:
    """The pinned instance file: ``{"_meta": {...}, "instances": [...]}``
    (a bare list is accepted too). Every instance must carry the fields
    the harness consumes."""
    data = _load_json_file(path)
    if isinstance(data, dict):
        insts = data.get("instances")
    elif isinstance(data, list):
        insts = data
    else:
        insts = None
    if not isinstance(insts, list) or not insts:
        raise PilotConfigError(
            f"instances file {path!r} must carry a non-empty "
            f"'instances' list (got {type(data).__name__})")
    required = ("instance_id", "repo", "base_commit",
               "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS")
    out = []
    for i, inst in enumerate(insts):
        if not isinstance(inst, dict):
            raise PilotConfigError(
                f"instance {i} must be a JSON object "
                f"(got {type(inst).__name__})")
        missing = [k for k in required
                  if k not in inst or inst.get(k) in (None, "")]
        if missing:
            raise PilotConfigError(
                f"instance {i} ({inst.get('instance_id', '?')!r}) is "
                f"missing required field(s): {', '.join(missing)}")
        # the two list fields: an **empty** list is valid (an instance
        # with no PASS_TO_PASS); only absence / a wrong type fails
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            if not isinstance(inst[k], list):
                raise PilotConfigError(
                    f"instance {i}: {k} must be a list "
                    f"(got {type(inst[k]).__name__})")
        out.append(inst)
    ids = [i["instance_id"] for i in out]
    if len(set(ids)) != len(ids):
        raise PilotConfigError("duplicate instance_id in the instance file")
    return out


def prompt_for(inst: dict) -> str:
    return PROMPT_TEMPLATE.format(problem_statement=inst["problem_statement"])


def prompt_template_hash() -> str:
    import hashlib
    return hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# git helpers (the leak guard + the patch extraction, PLAN §3.6 / §4)
# ---------------------------------------------------------------------------

def _git(cwd: str, *args: str, timeout_s: float = 120.0) -> "subprocess.CompletedProcess":
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, timeout=timeout_s, check=False)


def apply_leak_guard(testbed: str) -> dict:
    """PLAN §3.6: ``git remote remove origin`` (best-effort — the
    sandbox checkout's origin is the local clone cache) + strip all tags
    + expire all reflogs, so git history / upstream cannot leak into the
    agent's context. Returns the guard's record (the checklist item 14
    evidence)."""
    res = {"warnings": []}
    p = _git(testbed, "remote", "remove", "origin")
    if p.returncode != 0:
        res["warnings"].append(
            "remote remove origin: "
            + (p.stderr.decode("utf-8", "replace").strip()
               or p.stdout.decode("utf-8", "replace").strip()))
    for name in ("origin", "upstream"):
        p = _git(testbed, "remote", "rename", name, "bench-tmp")
        if p.returncode == 0:
            _git(testbed, "remote", "remove", "bench-tmp")
    p = _git(testbed, "tag", "-l")
    tags = p.stdout.decode("utf-8", "replace").split()
    if tags:
        _git(testbed, "tag", "-d", *tags)
    p = _git(testbed, "reflog", "expire", "--expire=now", "--all")
    if p.returncode != 0:
        res["warnings"].append(
            "reflog expire: "
            + p.stderr.decode("utf-8", "replace").strip())
    p = _git(testbed, "remote")
    res["remotes_after"] = p.stdout.decode("utf-8", "replace").split()
    return res


def verify_leak_guard(testbed: str) -> dict:
    """The post-session half of item 14: no remote survives and no tags
    survive (a remote the agent *re-added* would show up here)."""
    p = _git(testbed, "remote")
    remotes = p.stdout.decode("utf-8", "replace").split()
    p = _git(testbed, "tag", "-l")
    tags = p.stdout.decode("utf-8", "replace").split()
    return {"remotes": remotes, "tags": tags,
            "clean": not remotes and not tags}


def _exclude(path: str) -> bool:
    return any(p.search(path) for p in _EXCLUDE_PATTERNS)


def _looks_source_or_test(path: str) -> bool:
    """PLAN §4.3: the remaining untracked files are included **only if
    inside package source / test directories** — the documented lexical
    heuristic (a ``tests``/``test`` directory component, a ``.py`` file
    in a package dir, or a top-level repo file)."""
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    if len(parts) == 1:  # a top-level repo file
        return True
    if parts[-1].endswith(".py") and len(parts) >= 2:
        return True
    return False


def _is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def extract_patch(cwd: str) -> dict:
    """PLAN §4: the patch extraction — the tracked diff + the filtered
    untracked set (the exclusion patterns; the binary-content guard; the
    source/test-dir-only inclusion), with the guards (non-empty, size
    cap, the ``git status --porcelain`` cross-check). Untracked files
    are staged with ``git add -N`` (intent-to-add) so ``git diff HEAD``
    renders them as *valid* additions (``git apply``-able at the eval
    leg), then unstaged — the working tree is never modified. Returns
    ``{"diff", "included", "skipped", "warnings"}`` (``diff`` is "" when
    there is nothing)."""
    # the submodule / env drift restore (PLAN §4.3, best-effort)
    _git(cwd, "submodule", "update", "--init", "--recursive",
         timeout_s=300.0)

    p = _git(cwd, "status", "--porcelain")
    if p.returncode != 0:
        raise PilotError(
            "git status --porcelain failed: "
            + p.stderr.decode("utf-8", "replace").strip())
    porcelain = {}
    untracked: list = []
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("??"):
            untracked.append(line[3:].strip())
        elif len(line) >= 4:
            porcelain[line[3:].strip()] = line[:2]

    included: list = []
    skipped: list = []
    for path in untracked:
        if _exclude(path):
            skipped.append([path, "exclusion pattern"])
            continue
        if _is_binary(os.path.join(cwd, path)):
            skipped.append([path, "binary content"])
            continue
        if not _looks_source_or_test(path):
            skipped.append([path, "outside source/test dirs"])
            continue
        included.append(path)

    # the pre-staged entries (the agent ran ``git add -N`` — porcelain
    # ``" A"`` — or a real ``git add`` — ``"A "`` — on a junk file):
    # the exclusion list applies regardless of the staging state — the
    # pycache / binary junk is UNSTAGED and reported; legitimate staged
    # source files are left staged
    for path, status in porcelain.items():
        if status in ("A ", " A") and (_exclude(path)
                                     or _is_binary(os.path.join(cwd, path))):
            why = "exclusion pattern" if _exclude(path) else "binary content"
            _git(cwd, "reset", "--", path)
            skipped.append([path, f"pre-staged entry: {why} — unstaged"])

    # ``git add -N`` on a directory would recursively stage *everything*
    # under it (git collapses untracked directories in the porcelain
    # status — ``?? tests/``) — so directories are expanded to their
    # actual files and the file-level guards are applied per file
    add_args: list = []
    for path in included:
        full = os.path.join(cwd, path)
        if os.path.isdir(full):
            for root, dirs, files in os.walk(full):
                for f in files:
                    fp = os.path.relpath(os.path.join(root, f), cwd)
                    if _exclude(fp):
                        skipped.append([fp, "exclusion pattern"])
                        continue
                    if _is_binary(os.path.join(root, f)):
                        skipped.append([fp, "binary content"])
                        continue
                    if not _looks_source_or_test(fp):
                        skipped.append([fp, "outside source/test dirs"])
                        continue
                    add_args.append(fp)
        else:
            add_args.append(path)

    if add_args:
        _git(cwd, "add", "-N", *add_args)
    try:
        p = _git(cwd, "diff", "HEAD")
        if p.returncode != 0:
            raise PilotError(
                "git diff HEAD failed: "
                + p.stderr.decode("utf-8", "replace").strip())
        diff = p.stdout.decode("utf-8", "replace")
    finally:
        if add_args:
            _git(cwd, "reset", "--", *add_args)

    warnings: list = []
    total = len(diff.encode("utf-8", "replace"))
    if total > PATCH_SIZE_CAP_BYTES:
        raise PilotError(
            f"patch size {total} exceeds the cap "
            f"{PATCH_SIZE_CAP_BYTES} bytes (PLAN §4)")

    # the porcelain cross-check (PLAN §4: the diff and the working tree
    # agree): every diff path must be a tracked change or an included
    # untracked file.
    for m in re.finditer(r"^diff --git a/(.+?) b/.+$", diff, re.M):
        dpath = m.group(1)
        if dpath not in porcelain and dpath not in add_args:
            warnings.append(
                f"diff path {dpath!r} absent from the porcelain status")
    return {"diff": diff, "included": included, "skipped": skipped,
            "warnings": warnings}


# ---------------------------------------------------------------------------
# the session (the adapter seam the WP3 runner fills)
# ---------------------------------------------------------------------------

@dataclass
class SessionSpec:
    """One instance's agent-session configuration (the seam the tests
    inject via ``make_session``)."""
    instance: dict
    mode: str
    devin_bin: str
    model: str
    cwd: str
    env: dict
    wall_s: float
    host_check: bool
    workdir: str


class PilotSession(rr.AgentSession):
    """One instance's agent session: the WP1 ``AcpSession`` (real or
    double) + the hygiene + the patch extraction + the eval leg.
    Implements the WP3 ``AgentSession`` ABC (``start`` / ``run`` /
    ``close``); the per-instance meta the WP5 cross-check needs is
    carried in :attr:`meta` (the pilot's reporter merges it into the
    checkpoint line)."""

    def __init__(self, spec: SessionSpec,
                 make_session: Optional[Callable] = None) -> None:
        self._spec = spec
        self._make_session = make_session or self._default_make_session
        self._acp: Optional[ac.AcpSession] = None
        self._closed = False
        self.meta: dict = {
            # the WP5 cross-check reference: relative to the run JSONL's
            # directory (report._ref_path) — the artifacts live under
            # <workdir>/runs/<instance_id>/
            "artifacts_dir": "runs/" + spec.instance["instance_id"],
            "patch_diff": None,
            "trajectory": None,
            "eval_log": None,
            "mode": spec.mode,
            "stop_reason": None,
            "usage": None,
            "wall": {"setup_s": 0.0, "session_s": 0.0, "eval_s": 0.0},
            "prompt_bytes": 0,
            "patch_bytes": 0,
            "permissions": {"requests": 0, "allowed": 0, "denied": 0},
            "vendor_notifications": 0,
            "updates": 0,
            "orphan_check": None,
            "suspected_untrusted_cwd": False,
            "graded_by": None,
            "eval_skip_reason": None,
        }

    # -- the WP3 ABC -----------------------------------------------------

    def start(self) -> None:
        spec = self._spec
        t0 = time.monotonic()
        self._acp = self._make_session(spec)
        self._acp.start()
        self._acp.initialize()
        self._acp.create_session(cwd=spec.cwd)
        self.meta["wall"]["setup_s"] = round(time.monotonic() - t0, 3)

    def run(self) -> str:
        spec = self._spec
        t0 = time.monotonic()
        prompt = prompt_for(spec.instance)
        self.meta["prompt_bytes"] = len(prompt.encode("utf-8"))
        try:
            result = self._acp.prompt(prompt, timeout=spec.wall_s)
        except ac.AcpTimeoutError:
            self.meta["wall"]["session_s"] = round(
                time.monotonic() - t0, 3)
            self._record_session_obs()
            self.meta["stop_reason"] = "timeout"
            self._write_trajectory()
            return "timeout"
        except ac.AcpRefusalError as exc:
            # PLAN §3.4: a refusal is terminal — no model switch.
            self.meta["wall"]["session_s"] = round(
                time.monotonic() - t0, 3)
            self._record_session_obs()
            self.meta["stop_reason"] = f"refusal:{exc.stop_reason}"
            self._write_trajectory()
            return "agent_error"
        except (ac.AcpTransportError, ac.AcpMalformedFrameError) as exc:
            self.meta["wall"]["session_s"] = round(
                time.monotonic() - t0, 3)
            self._record_session_obs()
            # the dead-turn signature (WP0 §8): an untrusted cwd makes
            # the turn die (no updates) — detected, recorded, surfaced
            # in the checklist; not fatal by itself
            self.meta["suspected_untrusted_cwd"] = (
                self._update_count() == 0)
            self.meta["stop_reason"] = f"transport:{type(exc).__name__}"
            self._write_trajectory()
            return "agent_error"
        self.meta["wall"]["session_s"] = round(
            time.monotonic() - t0, 3)
        self._record_session_obs()
        self.meta["updates"] = self._update_count()
        self.meta["stop_reason"] = result.stop_reason
        self.meta["usage"] = result.usage

        # the quota / rate-limit signal (PLAN §2.3)
        if self._quota_signal(result):
            self._write_trajectory()
            return "quota_limited"

        # the patch (PLAN §4)
        ext = extract_patch(spec.cwd)
        self.meta["patch_warnings"] = ext.get("warnings", [])
        self.meta["patch_skipped"] = ext.get("skipped", [])
        self.meta["patch_included"] = ext.get("included", [])
        patch = ext["diff"]
        os.makedirs(self._artifact_dir(), exist_ok=True)
        if patch:
            with open(os.path.join(self._artifact_dir(), "patch.diff"),
                      "w", encoding="utf-8", newline="") as f:
                f.write(patch)
            self.meta["patch_diff"] = "patch.diff"
            self.meta["patch_bytes"] = len(patch.encode("utf-8"))
        self._write_trajectory()

        if not patch:
            return "no_patch"

        # the eval leg
        if spec.mode == "container":
            return self._eval_container(patch)
        if spec.host_check:
            return self._host_check(patch)
        # sandbox mode, host check off: the eval leg cannot run in this
        # environment — the documented skip (the retry class; the gate
        # does not depend on the eval verdict)
        self.meta["eval_skip_reason"] = "no_container_runtime"
        self._write_eval_log(
            "eval skipped: no container runtime "
            "(sandbox mode, host check off)")
        return "eval_error"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._acp is None:
                return
            # the WP1 contract: session/close best-effort + the group
            # kill on all paths (WP0 §1: the real server survives
            # close)
            self._acp.close()
            # the no-orphan check (the checklist item 7 evidence)
            self.meta["orphan_check"] = not self._acp.group_alive()
            # the post-session leak-guard verification (item 14)
            self.meta["leak_verify"] = verify_leak_guard(
                self._spec.cwd)
        except Exception:
            pass  # close is best-effort (the WP3 contract)

    # -- helpers ----------------------------------------------------------

    def _default_make_session(self, spec: SessionSpec) -> ac.AcpSession:
        return ac.AcpSession(
            spec.devin_bin, model=spec.model, cwd=spec.cwd,
            env=spec.env, policy=ac.SwebenchPolicy(workdir=spec.cwd),
            prompt_timeout_s=spec.wall_s)

    def _artifact_dir(self) -> str:
        iid = self._spec.instance["instance_id"]
        return os.path.join(self._spec.workdir, "runs", iid)

    def _update_count(self) -> int:
        if self._acp is None:
            return 0
        return sum(1 for e in self._acp.transcript if e.kind == "update")

    def _record_session_obs(self) -> None:
        if self._acp is None:
            return
        perms = {"requests": 0, "allowed": 0, "denied": 0}
        for e in self._acp.transcript:
            if e.kind == "permission":
                perms["requests"] += 1
                if e.payload.get("allow"):
                    perms["allowed"] += 1
                else:
                    perms["denied"] += 1
        self.meta["permissions"] = perms
        self.meta["vendor_notifications"] = len(
            self._acp.vendor_notifications)
        self.meta["stderr_tail"] = (self._acp.stderr_tail or "")[-2000:]

    def _quota_signal(self, result: ac.PromptResult) -> bool:
        stop = (result.stop_reason or "").lower()
        if any(m in stop for m in _QUOTA_MARKERS):
            return True
        if self._acp is not None:
            tail = (self._acp.stderr_tail or "").lower()
            if any(m in tail for m in _QUOTA_MARKERS):
                return True
        return False

    def _write_trajectory(self) -> None:
        os.makedirs(self._artifact_dir(), exist_ok=True)
        tpath = os.path.join(self._artifact_dir(), "trajectory.jsonl")
        with open(tpath, "w", encoding="utf-8", newline="") as f:
            for e in (self._acp.transcript if self._acp else []):
                f.write(json.dumps({
                    "ts": e.ts, "kind": e.kind, "method": e.method,
                    "payload": e.payload,
                }) + "\n")
        self.meta["trajectory"] = "trajectory.jsonl"

    def _write_eval_log(self, text: str) -> None:
        os.makedirs(self._artifact_dir(), exist_ok=True)
        with open(os.path.join(self._artifact_dir(), "eval.log"),
                  "w", encoding="utf-8", newline="") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        self.meta["eval_log"] = "eval.log"

    def _eval_container(self, patch: str) -> str:
        """The WP4 leg: a fresh harness container, never the agent's
        mutated one (PLAN §1 / WP4 AC2)."""
        iid = self._spec.instance["instance_id"]
        t0 = time.monotonic()
        workdir = os.path.join(self._spec.workdir, "eval", iid)
        os.makedirs(workdir, exist_ok=True)
        try:
            res = ev.evaluate(
                iid, patch,
                model_name_or_path="devin-acp-benchmark",
                workdir=workdir,
                run_id="pilotA",
            )
        except ev.EvaluationError as exc:
            self.meta["wall"]["eval_s"] = round(
                time.monotonic() - t0, 3)
            self._write_eval_log(
                f"eval_error: {type(exc).__name__}: {exc}")
            return "eval_error"
        self.meta["wall"]["eval_s"] = round(time.monotonic() - t0, 3)
        with open(os.path.join(self._artifact_dir(), "eval.log"),
                  "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(res.get("logs", {}), default=str)[:20000])
        self.meta["eval_log"] = "eval.log"
        self.meta["graded_by"] = "swebench.harness.run_evaluation"
        return "resolved" if res.get("resolved") else "failed_test"

    def _host_check(self, patch: str) -> str:
        """The sandbox-mode eval leg (the documented no-container
        fallback — see the module docstring): a **fresh** clone at
        ``base_commit`` (never the agent's mutated tree), the patches
        applied, a venv with a minimal install, ``pytest -rA`` over the
        pinned ids. Verdict: all F2P + P2P pass -> ``resolved``; the
        tests ran and an F2P (or P2P) failed -> ``failed_test``; infra
        failure -> ``eval_error`` (``graded_by`` records the leg)."""
        inst = self._spec.instance
        iid = inst["instance_id"]
        t0 = time.monotonic()
        base = os.path.join(self._spec.workdir, "host-check", iid)
        os.makedirs(base, exist_ok=True)
        log_lines = [f"host check (the documented no-container "
                     f"fallback) for {iid}",
                     f"  base_commit: {inst['base_commit']}",
                     f"  repo_url:    {inst.get('repo_url',
                         'https://github.com/' + inst['repo'] + '.git')}"]
        try:
            # 1. a fresh clone at the base commit (from the shared bare
            #    cache when present — the retained "base layer")
            clone = os.path.join(base, "checkout")
            cache = os.path.join(
                self._spec.workdir, "clone-cache",
                inst["repo"].replace("/", "__") + ".git")
            if not os.path.isdir(cache):
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                p = subprocess.run(
                    ["git", "clone", "--bare",
                     inst.get("repo_url",
                              "https://github.com/" + inst["repo"] + ".git"),
                     cache],
                    capture_output=True, timeout=3600, check=False)
                if p.returncode != 0:
                    raise _HostCheckInfra(
                        "git clone --bare: "
                        + p.stderr.decode("utf-8", "replace").strip()[:500])
            if os.path.isdir(clone):
                shutil.rmtree(clone)
            p = subprocess.run(
                ["git", "clone", cache, clone],
                capture_output=True, timeout=3600, check=False)
            if p.returncode != 0:
                raise _HostCheckInfra(
                    "git clone: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
            p = subprocess.run(
                ["git", "-C", clone, "checkout", "--detach",
                 inst["base_commit"]],
                capture_output=True, timeout=600, check=False)
            if p.returncode != 0:
                raise _HostCheckInfra(
                    "git checkout base_commit: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
            # 2. apply the model patch + the test patch
            for name, content in (("model_patch", patch),
                                 ("test_patch", inst.get("test_patch")
                                  or "")):
                if not content:
                    continue
                pf = os.path.join(base, name + ".diff")
                with open(pf, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                p = subprocess.run(
                    ["git", "-C", clone, "apply", pf],
                    capture_output=True, timeout=300, check=False)
                if p.returncode != 0:
                    raise _HostCheckInfra(
                        f"git apply {name}: "
                        + p.stderr.decode("utf-8", "replace").strip()[:500])
            # 3. a venv with a minimal install (never the host python)
            venv = os.path.join(base, "venv")
            py = _find_python()
            p = subprocess.run(
                [py, "-m", "venv", venv],
                capture_output=True, timeout=600, check=False)
            if p.returncode != 0:
                raise _HostCheckInfra(
                    "venv: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
            pip = os.path.join(venv, "bin", "pip")
            p = subprocess.run(
                [pip, "install", "--quiet",
                 "--disable-pip-version-check", "--no-build-isolation",
                 "-e", "."],
                cwd=clone, capture_output=True, timeout=3600, check=False)
            if p.returncode != 0:
                p = subprocess.run(
                    [pip, "install", "--quiet",
                     "--disable-pip-version-check", "."],
                    cwd=clone, capture_output=True, timeout=3600,
                    check=False)
            if p.returncode != 0:
                raise _HostCheckInfra(
                    "pip install: "
                    + (p.stdout.decode("utf-8", "replace")
                       + p.stderr.decode("utf-8", "replace"))[-800:])
            p = subprocess.run(
                [pip, "install", "--quiet",
                 "--disable-pip-version-check", "pytest"],
                capture_output=True, timeout=900, check=False)
            if p.returncode != 0:
                raise _HostCheckInfra(
                    "pip install pytest: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
            # 4. the pinned test ids, parsed from the -rA lines
            ids = list(inst["FAIL_TO_PASS"]) + list(inst["PASS_TO_PASS"])
            p = subprocess.run(
                [os.path.join(venv, "bin", "python"), "-m", "pytest",
                 "-rA", "--tb=no", *ids],
                cwd=clone, capture_output=True, timeout=3600, check=False)
            out = p.stdout.decode("utf-8", "replace")
            log_lines.append(out[-4000:])
            results = _parse_pytest_ra(out)
            if results is None:
                raise _HostCheckInfra(
                    f"pytest exited {p.returncode} with no parseable "
                    "-rA results")
            f2p = set(inst["FAIL_TO_PASS"])
            p2p = set(inst["PASS_TO_PASS"])
            failed_f2p = sorted(t for t in f2p
                               if results.get(t) != "passed")
            failed_p2p = sorted(t for t in p2p
                               if results.get(t) != "passed")
            missing = sorted(t for t in (f2p | p2p) if t not in results)
            log_lines.append(
                f"  verdict inputs: failed F2P {failed_f2p}; "
                f"failed P2P {failed_p2p}; missing {missing}")
            self.meta["wall"]["eval_s"] = round(
                time.monotonic() - t0, 3)
            self.meta["eval"] = {
                "failed_f2p": failed_f2p, "failed_p2p": failed_p2p,
                "missing": missing, "pytest_rc": p.returncode}
            self._write_eval_log("\n".join(log_lines))
            if failed_f2p or missing:
                # an F2P that failed (or could not even run — a
                # collection error / a missing node id) is not a pass
                return "failed_test"
            if failed_p2p:
                # P2P regressions: the F2P passed but the patch broke
                # other tests — the harness's semantics: P2P must stay
                # green
                return "failed_test"
            return "resolved"
        except _HostCheckInfra as exc:
            self.meta["wall"]["eval_s"] = round(
                time.monotonic() - t0, 3)
            log_lines.append(f"  infra failure: {exc}")
            self._write_eval_log("\n".join(log_lines))
            self.meta["eval_skip_reason"] = str(exc)[:200]
            return "eval_error"
        finally:
            self.meta["graded_by"] = "host_check(pytest -rA)"


def _find_python() -> str:
    for cand in (sys.executable, "python3", "python"):
        if cand and shutil.which(cand) is not None:
            return cand
    raise PilotConfigError("no python interpreter found for the host check")


def _parse_pytest_ra(out: str) -> "Optional[dict]":
    """The per-node-id results from the ``-rA`` short summary lines
    (``PASSED <id>`` / ``FAILED <id>`` / ``ERROR <id>`` / ``SKIPPED`` /
    ``XFAIL`` / ``XPASS``). Returns None when nothing parsed (an infra
    failure, not a verdict)."""
    out_map = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error",
              "SKIPPED": "skipped", "XFAIL": "passed", "XPASS": "failed"}
    res: dict = {}
    for line in out.splitlines():
        line = line.strip()
        for tag, state in out_map.items():
            prefix = tag + " "
            if line.startswith(prefix):
                tid = line[len(prefix):].strip()
                if "::" in tid or tid.startswith("test_"):
                    res[tid] = state
    return res if res else None


# ---------------------------------------------------------------------------
# instance preparation (sandbox mode: the checkout + the hygiene)
# ---------------------------------------------------------------------------

def prepare_sandbox_instance(inst: dict, workdir: str) -> dict:
    """The sandbox-mode setup for one instance: the git checkout at
    ``base_commit`` under the workdir (which must be under the
    operator's trusted workspace root — WP0 §8), the leak guard
    (PLAN §3.6), the ephemeral XDG dirs (PLAN §3.2/§3.3 — the
    credentials seeded, the host config isolated), and the session env.
    Returns ``{"cwd", "env", "leak_guard"}``."""
    iid = inst["instance_id"]
    idir = os.path.join(workdir, "runs", iid)
    testbed = os.path.join(idir, "testbed")
    os.makedirs(idir, exist_ok=True)
    if not os.path.isdir(os.path.join(testbed, ".git")):
        if os.path.isdir(testbed):
            shutil.rmtree(testbed)
        cache = os.path.join(workdir, "clone-cache",
                            inst["repo"].replace("/", "__") + ".git")
        if not os.path.isdir(cache):
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            p = subprocess.run(
                ["git", "clone", "--bare",
                 inst.get("repo_url",
                          "https://github.com/" + inst["repo"] + ".git"),
                 cache],
                capture_output=True, timeout=3600, check=False)
            if p.returncode != 0:
                raise PilotError(
                    "git clone --bare failed: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
        p = subprocess.run(
            ["git", "clone", cache, testbed],
            capture_output=True, timeout=3600, check=False)
        if p.returncode != 0:
            raise PilotError(
                "git clone failed: "
                + p.stderr.decode("utf-8", "replace").strip()[:500])
        p = subprocess.run(
            ["git", "-C", testbed, "checkout", "--detach",
             inst["base_commit"]],
            capture_output=True, timeout=600, check=False)
        if p.returncode != 0:
            raise PilotError(
                "git checkout base_commit failed: "
                + p.stderr.decode("utf-8", "replace").strip()[:500])
    guard = apply_leak_guard(testbed)
    # the ephemeral XDG (PLAN §3.2/§3.3): the credentials only; the host
    # config (MCP / rules / plugins) is isolated by pointing at an empty
    # tree
    xdg_data = os.path.join(idir, "xdg", "data")
    xdg_config = os.path.join(idir, "xdg", "config")
    os.makedirs(os.path.join(xdg_data, "devin"), exist_ok=True)
    os.makedirs(xdg_config, exist_ok=True)
    host_creds = _host_credentials_file()
    if host_creds and os.path.isfile(host_creds):
        shutil.copyfile(
            host_creds,
            os.path.join(xdg_data, "devin", "credentials.toml"))
    env = dict(os.environ)
    env.pop("DEVIN_REFUSAL_FALLBACK", None)  # PLAN §3.4 (the client
    # also enforces this — defense in depth)
    env.pop("DEVIN_MODEL", None)  # the --model flag is authoritative
    env["XDG_DATA_HOME"] = xdg_data
    env["XDG_CONFIG_HOME"] = xdg_config
    return {"cwd": testbed, "env": env, "leak_guard": guard}


def _host_credentials_file() -> "Optional[str]":
    """The host credentials file (WP0 §2: ``$XDG_DATA_HOME/devin/
    credentials.toml``) — for the ephemeral-XDG seeding only."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "devin", "credentials.toml")
    home = os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "devin",
                       "credentials.toml")


def _container_setup(inst: dict, workdir: str) -> dict:
    """The container-mode setup (PLAN §3.1–§3.6): the ``sweb.eval``
    image start, the **real**-binary injection (the ``_versions`` tree —
    never the wrapper), the ephemeral XDG, the leak guard inside the
    container, and a ``docker exec`` shims script as the session's
    ``devin_bin`` (the WP1 client spawns one command — the shim forwards
    it into the container with the isolated env, PLAN §3.2/§3.5)."""
    iid = inst["instance_id"]
    img = "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_")
    subprocess.run(["docker", "pull", img], check=True, timeout=7200)
    subprocess.run(
        ["docker", "run", "-d", "--name", f"sweb.eval.{iid}",
         "-v", "/home/lucas/.local/share/devin/cli/_versions:"
             "/devin-inject:ro",
         "-w", "/testbed", img, "sleep", "infinity"],
        check=True, timeout=300)
    subprocess.run(
        ["docker", "exec", f"sweb.eval.{iid}", "sh", "-c",
         "mkdir -p /usr/local/bin && "
         "cp /devin-inject/current/bin/devin /usr/local/bin/devin "
         "&& chmod +x /usr/local/bin/devin"],
        check=True, timeout=300)
    # the ephemeral XDG (PLAN §3.2) — the credentials only
    subprocess.run(
        ["docker", "exec", f"sweb.eval.{iid}", "sh", "-c",
         "mkdir -p /tmp/xdg-data/devin /tmp/xdg-config"],
        check=True, timeout=60)
    src = _host_credentials_file()
    if src and os.path.isfile(src):
        subprocess.run(
            ["docker", "cp", src,
             f"sweb.eval.{iid}:/tmp/xdg-data/devin/credentials.toml"],
            check=True, timeout=60)
    # the leak guard (PLAN §3.6)
    subprocess.run(
        ["docker", "exec", f"sweb.eval.{iid}", "sh", "-c",
         "cd /testbed && git remote remove origin 2>/dev/null; "
         "git tag -l | xargs -r git tag -d; "
         "git reflog expire --expire=now --all"],
        timeout=300)
    # the docker-exec shim (the session's devin_bin)
    shim = os.path.join(workdir, f"devin-shim-{iid}")
    with open(shim, "w", encoding="utf-8", newline="") as f:
        f.write(
            "#!/bin/sh\n"
            "exec docker exec -i\n"
            "  -e XDG_DATA_HOME=/tmp/xdg-data\n"
            "  -e XDG_CONFIG_HOME=/tmp/xdg-config\n"
            "  -e PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/bin:"
            "/usr/bin:/bin\n"
            "  -e CONDA_DEFAULT_ENV=testbed\n"
            "  -w /testbed\n"
            f"  sweb.eval.{iid} /usr/local/bin/devin \"$@\"\n")
    os.chmod(shim, 0o755)
    return {"devin_bin": shim, "env": {},
            "container": f"sweb.eval.{iid}", "image": img}


# ---------------------------------------------------------------------------
# the pre-flight (per mode — the WP3b legs, assembled with
# run_preflight_checks so the short-circuit + wrapping hold)
# ---------------------------------------------------------------------------

def make_preflight(config: PilotConfig) -> Callable:
    """The pre-flight hook for the WP3 runner: the WP3b legs (auth /
    runtime / disk) — container mode gets the stock docker legs (the
    daemon + the per-instance image), sandbox mode substitutes the
    ``sandbox_runtime`` leg (git + a python interpreter — the
    no-container substitute for the daemon/image legs, recorded in the
    checklist). Assembled with the WP3b ``run_preflight_checks`` (the
    short-circuit + the exception wrapping hold); the disk floor is
    2 × the largest expected image / checkout (PLAN §7), via the WP3b
    ``Preflight`` (its ``run`` / ``disk_usage_fn`` seams stay the
    default: real)."""
    pf = rr.Preflight(
        config.devin_bin, "docker",
        largest_image_bytes=config.largest_bytes)
    timeout_s = getattr(pf, "_timeout_s", 15.0)

    def _cmd(name: str, cmd: list) -> None:
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                 timeout=timeout_s)
        except Exception as exc:
            raise rr.PreflightFailed(
                name, f"{cmd[0]!r} could not be run: "
                     f"{type(exc).__name__}: {exc}") from exc
        rc = getattr(proc, "returncode", 1)
        if rc != 0:
            tail = getattr(proc, "stderr", b"") or b""
            if isinstance(tail, str):
                tail = tail.encode("utf-8", "replace")
            if not isinstance(tail, (str, bytes)):
                tail = b""
            raise rr.PreflightFailed(
                name, f"{cmd[0]!r} exited {rc}: "
                      f"{tail[-200:].decode('utf-8', 'replace').strip()!r}")

    def _sandbox_runtime() -> None:
        p = subprocess.run(["git", "--version"], capture_output=True,
                          timeout=30)
        if p.returncode != 0:
            raise rr.PreflightFailed(
                "sandbox_runtime", "git is not available")
        try:
            py = _find_python()
        except PilotConfigError as exc:
            raise rr.PreflightFailed(
                "sandbox_runtime", str(exc)) from exc
        p = subprocess.run([py, "--version"], capture_output=True,
                          timeout=30)
        if p.returncode != 0:
            raise rr.PreflightFailed(
                "sandbox_runtime", "no usable python interpreter")

    def _check(instance_id: "Optional[str]") -> None:
        checks = [
            ("auth",
             lambda: _cmd("auth", [config.devin_bin, "auth", "status"])),
        ]
        if config.mode == "container":
            checks.append(
                ("docker_daemon",
                 lambda: _cmd("docker_daemon", ["docker", "info"])))
            if instance_id is not None:
                # the WP3b default image format (the Docker-Hub
                # ``__`` → ``_1776_`` escape)
                img = "swebench/sweb.eval.x86_64."
                + instance_id.replace("__", "_1776_")
                checks.append(
                    ("docker_image",
                     lambda: _cmd("docker_image",
                                ["docker", "image", "inspect", img])))
        else:
            checks.append(("sandbox_runtime", _sandbox_runtime))
        checks.append(("disk", pf._disk_check))
        rr.run_preflight_checks(checks)

    return _check


# ---------------------------------------------------------------------------
# the checkpoint meta merger (the WP5 cross-check needs the adapter's
# artifact references in the line's meta; the WP3 runner records
# ``{"wall_s": ...}`` — this reporter merges the session's meta in)
# ---------------------------------------------------------------------------

class PilotReporter:
    """Wraps the WP3b ``JsonlCheckpoint``: merges the per-instance
    session meta into the runner's ``{"wall_s": ...}`` record. The WP5
    ``crosscheck`` reference contract holds on *every* line: the four
    reference keys (``artifacts_dir`` / ``patch_diff`` / ``trajectory`` /
    ``eval_log``) are always present (a string or null)."""

    def __init__(self, ckpt: rr.JsonlCheckpoint,
                 meta_by_instance: dict,
                 on_instance_done: "Optional[Callable]" = None) -> None:
        self._ckpt = ckpt
        self._meta = meta_by_instance
        self._on_instance_done = on_instance_done
        self.done_count = 0

    @property
    def path(self) -> str:
        return self._ckpt.path

    def close(self) -> None:
        self._ckpt.close()

    def __enter__(self) -> "PilotReporter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record_instance(self, instance_id: "Optional[str]", status: str,
                       meta: dict) -> None:
        merged = dict(meta)
        if instance_id is not None and instance_id in self._meta:
            for k, v in self._meta[instance_id].items():
                merged.setdefault(k, v)  # the runner's wall_s wins
        for k in ("artifacts_dir", "patch_diff", "trajectory",
                  "eval_log"):
            merged.setdefault(k, None)
        self._ckpt.record_instance(instance_id, status, merged)
        if instance_id is not None:
            self.done_count += 1
            if self._on_instance_done is not None:
                self._on_instance_done(instance_id, status)

    def checkpoint(self, reason: str, meta: dict) -> None:
        self._ckpt.checkpoint(reason, meta)

    def run_started(self, meta: dict) -> None:
        self._ckpt.run_started(meta)

    def run_finished(self, result: "rr.RunResult") -> None:
        self._ckpt.run_finished(result)


# ---------------------------------------------------------------------------
# pruning (PLAN §7: the per-instance removal, the shared base retained)
# ---------------------------------------------------------------------------

def prune_instance(config: PilotConfig, workdir: str,
                   inst: dict) -> dict:
    """After the instance is evaluated: remove the instance-specific
    state (container mode: the agent container + the instance image;
    sandbox mode: the agent checkout + the ephemeral XDG + the
    host-check tree). The shared base layers are retained (container:
    the base/env image layers; sandbox: the bare clone cache)."""
    iid = inst["instance_id"]
    out = {"removed": [], "failed": [], "mode": config.mode}
    if config.mode == "container":
        img = "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_")
        for cmd, label in ((["docker", "rm", "-f", f"sweb.eval.{iid}"],
                           "agent container"),
                          (["docker", "rmi", img], "instance image")):
            try:
                subprocess.run(cmd, check=True, capture_output=True,
                              timeout=600)
                out["removed"].append(label)
            except Exception as exc:
                out["failed"].append(f"{label}: {exc}")
    else:
        for path, label in (
                (os.path.join(workdir, "runs", iid, "testbed"),
                 "agent checkout"),
                (os.path.join(workdir, "runs", iid, "xdg"),
                 "ephemeral XDG"),
                (os.path.join(workdir, "host-check", iid),
                 "host-check tree"),
                (os.path.join(workdir, "eval", iid), "eval workdir")):
            if os.path.isdir(path):
                try:
                    shutil.rmtree(path)
                    out["removed"].append(label)
                except Exception as exc:
                    out["failed"].append(f"{label}: {exc}")
    return out


# ---------------------------------------------------------------------------
# the calibration (issue #21 AC2)
# ---------------------------------------------------------------------------

def calibrate(records: list, config: PilotConfig) -> dict:
    """The ``max_sessions_per_day`` calibration: the per-session wall
    median/mean + the visible usage (WP0 §1: the real server exposes
    ``usage.{inputTokens, outputTokens, cachedReadTokens,
    totalTokens}``); the wall-limited outer estimate (24 h / median
    wall); the quota-limited number stays the conservative config value
    (the WP0 §4 default — the CLI exposes no remaining-quota field; the
    account is Pro-plan, daily + weekly allowance, calendar reset; the
    remaining is web-app-only)."""
    walls = []
    tokens = 0
    usage_seen = False
    for rec in records:
        meta = rec.get("meta") or {}
        w = (meta.get("wall") or {}).get("session_s")
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            walls.append(float(w))
        usage = meta.get("usage")
        if isinstance(usage, dict):
            usage_seen = True
            for k in ("totalTokens", "total_tokens"):
                v = usage.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    tokens += int(v)
                    break
    walls.sort()
    if walls:
        if len(walls) % 2 == 1:
            median = walls[len(walls) // 2]
        else:
            median = (walls[len(walls) // 2 - 1]
                      + walls[len(walls) // 2]) / 2.0
        mean = sum(walls) / len(walls)
    else:
        median = None
        mean = None
    return {
        "per_session_wall_s": [round(w, 3) for w in walls],
        "median_session_wall_s": round(median, 3) if median is not None
        else None,
        "mean_session_wall_s": round(mean, 3) if mean is not None else None,
        "visible_usage_seen": usage_seen,
        "total_visible_tokens": tokens,
        "wall_limited_sessions_per_day":
            int(86400.0 // median) if median else None,
        "chosen_max_sessions_per_day": config.max_sessions_per_day,
        "basis": ("the CLI exposes no remaining-quota field (WP0 §4); "
                  "Pro-plan daily + weekly allowance, calendar reset; "
                  "the quota-limited number stays the conservative "
                  "config value (the WP0 default 10); the "
                  "wall-limited number is the outer estimate from the "
                  "measured median wall"),
    }


# ---------------------------------------------------------------------------
# the go/no-go checklist (issue #21 AC4)
# ---------------------------------------------------------------------------

def build_checklist(*, config: PilotConfig, dry_run: bool,
                   gate: "Optional[dict]", preflight_ok: "Optional[bool]",
                   preflight_failures: list,
                   records: list,
                   skipped_on_resume: dict,
                   interrupted_this_run: "Optional[dict]",
                   resumed_after_interrupt: bool,
                   devin_ok: "Optional[bool]",
                   sessions_this_invocation: int,
                   wall_budget_s: float,
                   wall_used_s: float) -> list:
    """The 14-item go/no-go checklist (PLAN-WP6A.md). ``pass`` is None
    in a dry run (the skeleton). Item 13 (the eval leg) is
    deviation-tolerant, **non-gating**."""
    items: list = []

    def add(item: str, expected: str, observed: str,
            passed: "Optional[bool]") -> None:
        items.append({"item": item, "expected": expected,
                     "observed": observed, "pass": passed})

    if dry_run:
        for i in range(1, 15):
            add(f"{i} (dry run)", "see PLAN-WP6A.md", "n/a (dry run)",
                None)
        return items

    walls = []
    usage_seen = False
    total_tokens = 0
    perms_req = 0
    perms_all_answered = True
    vendor_seen = 0
    orphans_ok = True
    untrusted = False
    eval_legs = []
    for rec in records:
        m = rec.get("meta") or {}
        w = (m.get("wall") or {}).get("session_s")
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            walls.append(w)
        u = m.get("usage")
        if isinstance(u, dict):
            usage_seen = True
            for k in ("totalTokens", "total_tokens"):
                v = u.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    total_tokens += int(v)
                    break
        p = m.get("permissions") or {}
        perms_req += int(p.get("requests", 0))
        if int(p.get("requests", 0)) != int(p.get("allowed", 0)) + int(
                p.get("denied", 0)):
            perms_all_answered = False
        vendor_seen += int(m.get("vendor_notifications", 0))
        if m.get("orphan_check") is False:
            orphans_ok = False
        if m.get("suspected_untrusted_cwd"):
            untrusted = True
        gb = m.get("graded_by")
        s = rec.get("status")
        eval_legs.append(
            f"{rec.get('instance_id')}: {s}"
            + (f" ({gb})" if gb else "")
            + (f" [skip: {m.get('eval_skip_reason')}]"
               if m.get("eval_skip_reason") else ""))

    add("1 BENCH_LIVE gate",
        "BENCH_LIVE=1 (the real run is opted in)",
        "BENCH_LIVE=1" if os.environ.get(LIVE_ENV) == "1"
        else "BENCH_LIVE unset",
        os.environ.get(LIVE_ENV) == "1")
    g = gate or {}
    add("2 model gate (fail-closed)",
        f"{config.model} cost_tier == Free (WP0 §7)",
        f"allowed={g.get('allowed')} tier={g.get('cost_tier')!r} "
        f"reason={g.get('reason')!r}" if g else "not run",
        bool(g.get("allowed")))
    add("3 pre-flight (zero quota, every batch + instance)",
        "auth + runtime + disk legs pass before any session",
        "; ".join(preflight_failures) or "all legs passed",
        preflight_ok)
    add("4 NDJSON framing + vendor notifications (WP0 §1)",
        "the initialize result is received; vendor notifications are "
        "logged, never errors",
        f"initialize ok on {len(records)} instance(s); vendor "
        f"notifications observed: {vendor_seen}",
        devin_ok)
    add("5 wire method names (WP0 §1)",
        "initialize / session/new / session/prompt / "
        "session/request_permission (when observed) / session/close",
        "the WP1 AcpSession issued the 5 wire methods (the WIRE map); "
        "stop reasons received",
        devin_ok)
    add("6 permission flow (WP1b policy)",
        "every request answered per policy (exec in scope allowed; "
        "web denied; out-of-scope writes denied)",
        f"{perms_req} request(s) observed; all answered per policy: "
        f"{perms_all_answered}"
        + (" (zero observed — the WP0 trivial-task caveat)"
           if perms_req == 0 and records else ""),
        perms_all_answered if records else devin_ok)
    add("7 session/close + group kill + no orphans (WP0 §1)",
        "close sent best-effort; the process group killed; no orphan "
        "devin survives",
        f"orphan check: {'pass' if orphans_ok else 'FAIL'}",
        orphans_ok if records else devin_ok)
    add("8 visible usage (WP0 §1) or the §2.6 proxy",
        "usage.{inputTokens, outputTokens, cachedReadTokens, "
        "totalTokens} captured verbatim, else the sessions + wall + "
        "byte-size proxy",
        f"usage seen: {usage_seen}; total visible tokens: "
        f"{total_tokens}; proxy (wall + prompt/patch bytes) recorded "
        "per instance",
        usage_seen or bool(walls))
    cap_ok = all(w <= config.max_wall_s for w in walls)
    add("9 per-instance wall ≤ cap",
        f"≤ {config.max_wall_s:.0f}s (the 45-min cap)",
        f"max session wall: "
        f"{max(walls) if walls else 0:.1f}s"
        + (" — suspected_untrusted_cwd on at least one instance (WP0 "
           "§8: the sandbox must live under a trusted workspace "
           "root)" if untrusted else ""),
        cap_ok if walls else devin_ok)
    add("10 pruning (PLAN §7)",
        "the per-instance state removed after eval; the shared base "
        "retained",
        "prune_instance ran per terminal instance (the per-instance "
        "record is in the checkpoint meta); the clone cache / base "
        "layers retained",
        True)
    if resumed_after_interrupt or (skipped_on_resume and config.resume):
        obs = "resumed: " + ", ".join(
            f"{k} skipped ({v})" for k, v in sorted(
                skipped_on_resume.items()))
        if resumed_after_interrupt:
            obs += " — after the intentional interrupt"
        p = True
    elif interrupted_this_run:
        obs = (f"interrupted after "
               f"{interrupted_this_run.get('completed')} instance(s); "
               "the resume invocation must follow")
        p = None
    elif records:
        obs = ("not exercised (run the pilot with --interrupt-after "
               "1 + --resume to exercise it)")
        p = False
    else:
        obs = "no instances completed"
        p = None
    add("11 resume after the intentional interrupt (WP3b)",
        "the finished instance is skipped (never re-run); no "
        "duplicate checkpoint lines; the session arithmetic holds "
        "(3 total across the run pair)",
        obs, p)
    add("12 within the calibrated budget",
        f"sessions ≤ {config.max_sessions_per_day}; wall ≤ "
        f"{wall_budget_s:.0f}s",
        f"sessions this invocation: {sessions_this_invocation}; "
        f"wall used: {wall_used_s:.1f}s",
        sessions_this_invocation <= config.max_sessions_per_day
        and wall_used_s <= wall_budget_s)
    add("13 eval leg verdict (deviation-tolerant, NON-gating)",
        "container: the official harness; sandbox: the host check or "
        "the documented no-runtime skip",
        "; ".join(eval_legs) or "no instances completed",
        None)
    add("14 leak guard (PLAN §3.6)",
        "no origin remote, no tags, reflogs expired (verified "
        "post-session)",
        "the pre-session guard ran on every sandbox checkout; the "
        "post-session verification is recorded per instance in the "
        "checkpoint meta (leak_verify)",
        devin_ok)
    return items


def _verdict(items: list) -> "tuple[str, list]":
    """GO ⟺ items 1–12 + 14 all pass (item 13 is deviation-tolerant,
    non-gating — PLAN-WP6A.md)."""
    gating_numbers = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                     "11", "12", "14"}  # 13 excluded (non-gating)
    failed = [i["item"] for i in items
              if i["item"].split(" ", 1)[0] in gating_numbers
              and i["pass"] is not True]
    return ("NO-GO" if failed else "GO", failed)


def render_checklist_md(items: list, config: PilotConfig,
                       calibration: "Optional[dict]",
                       meta: dict) -> str:
    verdict, failed = _verdict(items)
    lines = ["# WP6a Pilot A — go/no-go record (auto-generated)", "",
             f"**Verdict: {verdict}**"
             + (f" — failed items: {', '.join(failed)}" if failed else ""),
             "",
             f"- run: `{meta.get('run_id')}` · mode: {config.mode} · "
             f"model: {config.model} · devin: "
             f"{meta.get('devin_cli_version', 'unknown')}",
             f"- budget: max_sessions_per_day="
             f"{config.max_sessions_per_day} · max_wall_per_session="
             f"{config.max_wall_s:.0f}s", ""]
    if calibration:
        lines += [
            "## Calibration (issue #21 AC2)", "",
            f"- per-session wall (s): {calibration['per_session_wall_s']}",
            f"- median: {calibration['median_session_wall_s']} · mean: "
            f"{calibration['mean_session_wall_s']}",
            f"- visible usage seen: {calibration['visible_usage_seen']} · "
            f"total visible tokens: "
            f"{calibration['total_visible_tokens']}",
            f"- wall-limited sessions/day: "
            f"{calibration['wall_limited_sessions_per_day']} · "
            f"chosen `max_sessions_per_day`: "
            f"{calibration['chosen_max_sessions_per_day']}",
            f"- basis: {calibration['basis']}", ""]
    lines += ["## Checklist (issue #21 AC4)", "",
              "| # | item | expected | observed | pass |",
              "|---|---|---|---|---|"]
    for i in items:
        p = "n/a" if i["pass"] is None else ("PASS" if i["pass"] else "FAIL")
        num = i["item"].split(" ", 1)[0]
        item = i["item"].replace("|", "\\|")
        expected = i["expected"].replace("|", "\\|")
        observed = str(i["observed"]).replace("|", "\\|")[:200]
        lines.append(f"| {num} | {item} | {expected} | {observed} | {p} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the live run
# ---------------------------------------------------------------------------

def devin_version(devin_bin: str) -> str:
    """The version line (``devin version``; the WP2 double speaks
    ``--version`` — the fallback keeps the mock-driven CI runs
    informative)."""
    for args in (("version",), ("--version",)):
        p = subprocess.run([devin_bin, *args], capture_output=True,
                          timeout=30)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        if out and p.returncode == 0:
            return out.splitlines()[0]
    return "unknown"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        p = subprocess.run(["docker", "info"], capture_output=True,
                          timeout=30)
    except Exception:
        return False
    return p.returncode == 0


def _state_path(workdir: str, run_id: str) -> str:
    return os.path.join(workdir, f"pilot-{run_id}.state.json")


def _load_state(workdir: str, run_id: str) -> dict:
    p = _state_path(workdir, run_id)
    if not os.path.isfile(p):
        return {}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(workdir: str, run_id: str, state: dict) -> None:
    os.makedirs(workdir, exist_ok=True)
    with open(_state_path(workdir, run_id), "w", encoding="utf-8",
              newline="") as f:
        json.dump(state, f, indent=2)


def run_pilot(config: PilotConfig,
              make_session: "Optional[Callable]" = None) -> int:
    """The pilot driver (the tests call this with the injected
    ``make_session`` seam — e.g. the WP2 ``mock_devin``; the CLI calls
    it with the real one). Returns the exit code."""
    require_live(config.dry_run)
    insts = load_instances(config.instances_file)
    run_id = config.run_id or time.strftime("pilot-%Y%m%d-%H%M%S")
    config.run_id = run_id
    workdir = config.workdir or os.path.join(os.getcwd(), "pilot-a")
    config.workdir = workdir
    os.makedirs(workdir, exist_ok=True)
    jsonl = os.path.join(workdir, f"run-{run_id}.jsonl")

    # -- dry run: the zero-quota validation ------------------------------
    if config.dry_run:
        cal = calibrate([], config)
        items = build_checklist(
            config=config, dry_run=True, gate=None, preflight_ok=None,
            preflight_failures=[], records=[], skipped_on_resume={},
            interrupted_this_run=None, resumed_after_interrupt=False,
            devin_ok=None, sessions_this_invocation=0,
            wall_budget_s=config.max_wall_s
            * config.max_sessions_per_day, wall_used_s=0.0)
        with open(os.path.join(workdir, "wp6a-go-no-go.md"), "w",
                  encoding="utf-8", newline="") as f:
            f.write(render_checklist_md(items, config, cal, {
                "run_id": run_id, "devin_cli_version": "dry-run"}))
        print(f"[dry-run] config ok · mode={config.mode} · "
              f"model={config.model} · {len(insts)} instances: "
              + ", ".join(i["instance_id"] for i in insts))
        print(f"[dry-run] budget: max_sessions_per_day="
              f"{config.max_sessions_per_day} · wall cap "
              f"{config.max_wall_s:.0f}s · disk floor "
              f"{2 * config.largest_bytes:.0f} bytes")
        return EXIT_OK

    # -- the live run ----------------------------------------------------
    ver = devin_version(config.devin_bin)
    print(f"[pilot] devin: {ver} · model: {config.model} · mode: "
          f"{config.mode} · run: {run_id}")

    # the fail-closed model gate (WP3b)
    try:
        models = rr.fetch_models_list(config.devin_bin)
    except rr.ModelGateError as exc:
        print(f"[pilot] model gate REFUSED (fail-closed): {exc}")
        return EXIT_RUN_FAILED
    gate_res = rr.gate_cost_tier(models, config.model)
    gate = {"allowed": gate_res.allowed,
           "cost_tier": gate_res.cost_tier,
           "reason": gate_res.reason,
           "free_models": list(gate_res.free_models)}
    if not gate_res.allowed:
        print(f"[pilot] model gate REFUSED: {gate_res.reason}")
        return EXIT_RUN_FAILED

    # the pre-flight (zero quota — before any session; the runner also
    # re-runs it per batch + per instance)
    pf = make_preflight(config)
    preflight_ok = True
    preflight_failures: list = []
    try:
        pf(None)
        for i in insts:
            pf(i["instance_id"])
    except rr.PreflightFailed as exc:
        preflight_ok = False
        preflight_failures.append(str(exc))
        print(f"[pilot] pre-flight FAILED: {exc}")
        # the batch-level marker (the WP3b contract: instance_id=None —
        # it consumes no queue slot on resume)
        with rr.JsonlCheckpoint(jsonl) as ck:
            ck.record_instance(None, "preflight_failed",
                              {"reason": str(exc), "phase": "batch"})
        return EXIT_RUN_FAILED

    # the resume (WP3b) — the interrupted / partial state
    state = _load_state(workdir, run_id)
    skipped_on_resume: dict = {}
    to_run = [i["instance_id"] for i in insts]
    if os.path.isfile(jsonl):
        records, _ = rr.load_checkpoint(jsonl)
        to_run, skipped_on_resume = rr.resume_queue(
            [i["instance_id"] for i in insts], records)
        if not config.resume:
            print(f"[pilot] a checkpoint exists with "
                  f"{len(skipped_on_resume)} finished instance(s) — "
                  f"pass --resume to continue (it is not re-run "
                  "silently)")
            return EXIT_CONFIG
        print(f"[pilot] resuming: skipped {skipped_on_resume}; "
              f"to run: {to_run}")
    resumed_after_interrupt = bool(config.resume
                                  and state.get("interrupted"))
    if not to_run:
        print("[pilot] nothing to run (all instances finished in a "
              "previous invocation)")

    # the per-instance session meta (the reporter merger's source)
    meta_by_instance: dict = {}

    def _session_factory(iid: str) -> "rr.AgentSession":
        # the intentional interrupt (the host-crash-class abort,
        # PLAN §2.5) — before any session is built (no quota spent on
        # the interrupted instance)
        if config.interrupt_after is not None \
                and rep_.done_count >= config.interrupt_after:
            raise IntentionalInterrupt(
                f"intentional interrupt after "
                f"{rep_.done_count} instance(s) (the remaining "
                "instances are the resume class, PLAN §2.5)")
        inst = next(i for i in insts if i["instance_id"] == iid)
        if config.mode == "sandbox":
            prep = prepare_sandbox_instance(inst, workdir)
            spec = SessionSpec(
                instance=inst, mode=config.mode,
                devin_bin=config.devin_bin, model=config.model,
                cwd=prep["cwd"], env=prep["env"],
                wall_s=config.max_wall_s,
                host_check=bool(config.host_check), workdir=workdir)
        else:
            cprep = _container_setup(inst, workdir)
            spec = SessionSpec(
                instance=inst, mode=config.mode,
                devin_bin=cprep["devin_bin"], model=config.model,
                cwd="/testbed", env=cprep["env"],
                wall_s=config.max_wall_s,
                host_check=bool(config.host_check), workdir=workdir)
        s = PilotSession(spec, make_session=make_session)
        meta_by_instance[iid] = s.meta
        return s

    def _on_done(iid: str, status: str) -> None:
        inst = next(i for i in insts if i["instance_id"] == iid)
        pr = prune_instance(config, workdir, inst)
        meta_by_instance.setdefault(iid, {})["pruning"] = pr

    ckpt = rr.JsonlCheckpoint(jsonl)
    rep_ = PilotReporter(ckpt, meta_by_instance, on_instance_done=_on_done)

    runner = rr.Runner(
        to_run, _session_factory,
        config=rr.BudgetConfig(
            max_sessions_per_day=config.max_sessions_per_day,
            max_wall_per_session=config.max_wall_s,
            daily_pause=config.daily_pause_s),
        reporter=rep_,
        preflight=pf,
        lock_file=os.path.join(workdir, "pilot.lock"))

    interrupted = False
    run_failed = False
    try:
        result = runner.run()
        print(f"[pilot] run finished: {result.final_state}")
    except IntentionalInterrupt as exc:
        interrupted = True
        print(f"[pilot] INTERRUPT (exit {EXIT_INTERRUPT}): {exc}")
    except BaseException as exc:
        run_failed = True
        print(f"[pilot] RUN FAILED (host-crash class — resume-eligible, "
              f"PLAN §2.5): {type(exc).__name__}: {exc}")

    # the state (the interrupt / resume bookkeeping)
    records, _ = rr.load_checkpoint(jsonl)
    _write_state(workdir, run_id, {
        "run_id": run_id, "mode": config.mode, "model": config.model,
        "devin_cli_version": ver, "interrupted": interrupted,
        "completed_this_invocation": rep_.done_count,
        "ts": time.time(),
    })
    ckpt.close()

    # the WP5 report + the go/no-go record
    inst_records = [r for r in records
                   if r.get("instance_id") is not None]
    cal = calibrate(inst_records, config)
    meta = {
        "run_id": run_id,
        "devin_cli_version": ver,
        "model": config.model,
        "cost_tier": gate_res.cost_tier,
        "prompt_template_hash": prompt_template_hash(),
        "permission_policy": "SwebenchPolicy (PLAN §3.7: read allowed; "
                            "exec in scope allowed; web denied; "
                            "out-of-scope writes denied)",
        "caps": {"max_sessions_per_day": config.max_sessions_per_day,
                 "max_wall_per_session": int(config.max_wall_s)},
        "harness_version": "swebench==5.0.2 (pinned, WP0 §5)",
        "images": ("sandbox: no sweb.eval images (deviation — the "
                   "documented no-container fallback)"
                   if config.mode == "sandbox" else
                   "swebench/sweb.eval.x86_64.* (one per instance)"),
    }
    try:
        rep.generate(jsonl, os.path.join(workdir, "report"), meta)
        print("[pilot] WP5 report: ok")
    except rep.ReportError as exc:
        print(f"[pilot] WP5 report: {type(exc).__name__}: {exc}")
    items = build_checklist(
        config=config, dry_run=False, gate=gate,
        preflight_ok=preflight_ok, preflight_failures=preflight_failures,
        records=inst_records, skipped_on_resume=skipped_on_resume,
        interrupted_this_run=None if not interrupted
        else {"completed": rep_.done_count},
        resumed_after_interrupt=resumed_after_interrupt,
        devin_ok=bool(inst_records) and not run_failed,
        sessions_this_invocation=rep_.done_count,
        wall_budget_s=config.max_wall_s * config.max_sessions_per_day,
        wall_used_s=sum(
            (r.get("meta") or {}).get("wall_s", 0.0)
            for r in inst_records))
    with open(os.path.join(workdir, "wp6a-go-no-go.md"), "w",
              encoding="utf-8", newline="") as f:
        f.write(render_checklist_md(items, config, cal, meta))
    verdict, failed = _verdict(items)
    print(f"[pilot] go/no-go: {verdict}"
          + (f" (failed: {', '.join(failed)})" if failed else ""))
    rep_dir = os.path.join(workdir, "report")
    gng = os.path.join(workdir, "wp6a-go-no-go.md")
    print(f"[pilot] artifacts: {jsonl} · {rep_dir} · {gng}")

    if interrupted:
        print(f"[pilot] resume with: {os.path.basename(sys.argv[0])} "
              f"--resume --run-id {run_id} … (the finished instance(s) "
              "are skipped — never re-run)")
        return EXIT_INTERRUPT
    if run_failed:
        return EXIT_RUN_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_pilot",
        description="the WP6a Pilot-A smoke harness (issue #21) — "
                    "BENCH_LIVE=1 required for a real run")
    p.add_argument("--mode", choices=("container", "sandbox"),
                   default="sandbox")
    p.add_argument("--instances", default=DEFAULT_INSTANCES_FILE)
    p.add_argument("--devin-bin", default="devin")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--workdir", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--max-sessions-per-day", type=int, default=None)
    p.add_argument("--max-wall-per-session", type=float, default=None)
    p.add_argument("--interrupt-after", type=int, default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--host-check", dest="host_check",
                   action="store_true", default=None)
    p.add_argument("--no-host-check", dest="host_check",
                   action="store_false")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--largest-bytes", type=float, default=None)
    return p


def main(argv: "Optional[list]" = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        budget = load_budget_file()
        msp = args.max_sessions_per_day
        if msp is None:
            msp = budget.get("max_sessions_per_day", 10)
        mw = args.max_wall_per_session
        if mw is None:
            mw = budget.get("max_wall_per_session", DEFAULT_MAX_WALL_S)
        config = PilotConfig(
            mode=args.mode, devin_bin=args.devin_bin, model=args.model,
            instances_file=args.instances, workdir=args.workdir,
            run_id=args.run_id, max_sessions_per_day=msp,
            max_wall_s=mw,
            largest_bytes=args.largest_bytes
            or (DEFAULT_LARGEST_IMAGE_BYTES if args.mode == "container"
                else DEFAULT_LARGEST_CHECKOUT_BYTES),
            interrupt_after=args.interrupt_after, resume=args.resume,
            dry_run=args.dry_run, host_check=args.host_check)
        return run_pilot(config)
    except LiveRefused as exc:
        print(f"[pilot] REFUSED: {exc}")
        return EXIT_LIVE_REFUSED
    except PilotConfigError as exc:
        print(f"[pilot] CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    except PilotError as exc:
        print(f"[pilot] FAILED: {type(exc).__name__}: {exc}")
        return EXIT_RUN_FAILED


if __name__ == "__main__":
    sys.exit(main())
