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

**Intentional interrupt (issue #21 AC3 / issue #22 AC4).**
``--interrupt-after N``: after the N-th instance's checkpoint line is
flushed, the session factory for the next instance raises
:class:`IntentionalInterrupt` (a host-crash-class abort — the
remaining instances have **no** checkpoint line; the WP3b resume
class). The follow-up ``--resume`` invocation goes through
``resume_queue``: finished instances are **skipped** (never re-run, no
session spent on them), the rest run. Exit code 42 marks the
interrupt.

**Pilot B (issue #22).** ``--pilot b`` selects the pilot-B record
(naming, the 17-item checklist, the record sections) over a 25-
instance, 12-repo stratified file (``pilot_b_instances.json``). The
3 Pilot A instances are **reused inside** the 25 through the
checkpoint **import**: ``--import-run <run-id>`` [+ ``--import-
workdir <path>``] reads the source run's ``run-<id>.jsonl`` and, per
shared instance, the **never-rerun** statuses (``resolved`` /
``failed_test`` / ``timeout`` / ``no_patch`` / ``agent_error``) are
imported — the line is copied into this run's checkpoint **with its
original ``ts``** (the source window — it never counts toward this
run's daily budget) + ``meta.imported_from``, and the instance is
skipped (never re-run, no session spent); the **retry-class**
statuses (``quota_limited`` / ``eval_error`` / ``preflight_failed``)
are **not** imported — the instance runs (the only pass@1 retry
class, PLAN §2.5). The import is fail-closed (a missing / corrupt
source is a config error before the pre-flight) and idempotent (a
repeat ``--resume`` never re-seeds). Checklist item 16 (gating) is
the 2-run overlap/variance check: for each imported instance the
source line and this run's final line must agree on status / session
wall / total tokens / patch bytes (the import is faithful **and**
the reused instance was not re-run) + the session arithmetic
(imported + new == unique). Item 15 (gating) is the stratification
(every repo in the instance file's ``stratified_repos`` meta has a
final line); item 17 (gating) is the root-caused ``eval_error``
(every ``eval_error`` line carries ``meta.eval_error_root_cause`` ∈
{``env``, ``parser``, ``patch``} — the mapping is in
PLAN-WP6B.md). Item 12 gates **per window** (the checkpoint lines
bucketed by their ``ts``'s UTC day — a multi-window run never fails
by construction; a single-window run renders the verbatim WP6a text).

**Exit codes:** 0 = complete (incl. a finished resume) · 2 = config
error · 3 = ``BENCH_LIVE`` refused · 4 = pre-flight / runner / other
failure · 42 = intentional interrupt.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
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
DEFAULT_B_INSTANCES_FILE = str(_BENCH_DIR / "pilot_b_instances.json")
BUDGET_FILE = str(_BENCH_DIR / "live_pilot_budget.json")
#: The go/no-go record names per pilot kind (the early-exit invariant
#: removes BOTH — a stale verdict from a previous kind in the same
#: workdir is never kept).
RECORD_NAMES = {"a": "wp6a-go-no-go.md", "b": "wp6b-pilot-b.md"}
# The ``--import-run`` id is interpolated into ``run-<id>.jsonl`` —
# the same charset contract as the SWE-bench instance id (the WP5
# ``_IID_CHARSET`` / container-name charset, no ``..``): a run id
# outside it could point the import outside the workdir (path
# traversal) — the codebase charset-restricts ids for exactly this
# reason.
_RUN_ID_CHARSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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
#: diff (never from the working tree). The directory patterns use
#: ``(/|$)`` (not a bare ``$``): the porcelain status collapses an
#: untracked directory to a trailing-slash entry (``build/``), and files
#: inside an excluded dir end in the filename (``build/lib/foo.py``) —
#: a ``$``-anchored pattern matches neither, and the generated ``.py``
#: artifacts would leak into the patch.
_EXCLUDE_PATTERNS = (
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"(^|/)\.pytest_cache(/|$)"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)\.coverage(/|$)"),
    re.compile(r"(^|/)htmlcov(/|$)"),
    re.compile(r"\.egg-info(/|$)"),
    re.compile(r"(^|/)build(/|$)"),
    # the generated-dir class (PLAN §4.2): a routine agent ``python -m
    # venv .venv`` / ``npm install`` would otherwise intent-to-add every
    # ``.py`` under the tree — the diff blows past the size cap and the
    # completed session is discarded (retried at full quota on resume,
    # hitting the same cap)
    re.compile(r"(^|/)\.venv(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)\.tox(/|$)"),
    re.compile(r"(^|/)dist(/|$)"),
    re.compile(r"(^|/)\.mypy_cache(/|$)"),
)

#: Quota / rate-limit signal markers (PLAN §2.3: the ACP stop reason,
#: the CLI stderr, or a non-zero exit with quota text). The numeric
#: ``429`` marker is matched as a whole number (a bare substring would
#: false-positive on incidental digits — a ``1429`` byte count, a port
#: ``4290`` — and park the batch in the sleep-to-next-UTC-midnight
#: backoff for no reason).
_QUOTA_MARKERS = (
    "quota", "rate limit", "rate_limit", "rate-limit",
    "too many requests", "usage limit",
)
_QUOTA_429_RE = re.compile(r"(?<!\d)429(?!\d)")


def _quota_marker_hit(text: str) -> bool:
    """The marker match over one text (stop reason or the stderr
    tail, lowercased): the text markers as substrings, the numeric
    ``429`` as a whole number."""
    if _QUOTA_429_RE.search(text):
        return True
    return any(m in text for m in _QUOTA_MARKERS)


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
    """A host-check infrastructure failure (the ``eval_error``
    class). Carries the AC3 root-cause class (issue #22):
    ``env`` (the default — the host / venv / install leg), ``patch``
    (the model patch does not apply), or ``parser`` (the test run
    happened but no parseable results came back)."""

    def __init__(self, msg: str, root_cause: str = "env") -> None:
        super().__init__(msg)
        self.root_cause = root_cause


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
    pilot: str = "a"  # "a" (the WP6a 3-instance gate) | "b" (issue #22)
    import_run: Optional[str] = None  # the source run id (issue #22 AC2)
    import_workdir: Optional[str] = None  # its workdir (default: this run's)

    def __post_init__(self) -> None:
        if self.mode not in ("container", "sandbox"):
            raise PilotConfigError(
                f"mode must be 'container' or 'sandbox' (got {self.mode!r})")
        if self.pilot not in ("a", "b"):
            raise PilotConfigError(
                f"pilot must be 'a' or 'b' (got {self.pilot!r})")
        if self.import_run is not None and not isinstance(
                self.import_run, str):
            raise PilotConfigError(
                f"import_run must be a string (got "
                f"{type(self.import_run).__name__})")
        if self.import_workdir is not None and not isinstance(
                self.import_workdir, str):
            raise PilotConfigError(
                f"import_workdir must be a string (got "
                f"{type(self.import_workdir).__name__})")
        if self.import_run is not None:
            if (".." in self.import_run
                    or _RUN_ID_CHARSET.fullmatch(self.import_run)
                    is None):
                raise PilotConfigError(
                    "--import-run must be a run id of the SWE-bench "
                    "id charset [A-Za-z0-9][A-Za-z0-9._-]* without "
                    f"'..' (got {self.import_run!r})")
        if self.run_id is not None and not isinstance(
                self.run_id, str):
            raise PilotConfigError(
                f"run_id must be a string (got "
                f"{type(self.run_id).__name__})")
        # the run id (the --run-id flag) is joined into the checkpoint
        # / state filenames (run-<id>.jsonl / pilot-<id>.state.json)
        # and reaches ev.evaluate (a non-conforming id would degrade
        # to a per-instance eval_error instead of a fail-closed
        # config error) — the same charset as --import-run
        if self.run_id is not None:
            if (".." in self.run_id
                    or _RUN_ID_CHARSET.fullmatch(self.run_id)
                    is None):
                raise PilotConfigError(
                    "--run-id must be a run id of the SWE-bench id "
                    "charset [A-Za-z0-9][A-Za-z0-9._-]* without "
                    f"'..' (got {self.run_id!r})")
        # the pairing (fail-closed): --import-workdir is meaningless
        # without --import-run; --import-run is the pilot-B import
        # (checklist items 15–17 verify it — they never render
        # under --pilot a, where a seeded import would go
        # unverified)
        if (self.import_workdir is not None
                and self.import_run is None):
            raise PilotConfigError(
                "--import-workdir requires --import-run")
        if (self.import_run is not None and self.pilot != "b"):
            raise PilotConfigError(
                "--import-run is the pilot-B import (items 15–17 "
                "verify it — they do not render under --pilot a)")
        # ``--host-check`` is a sandbox-mode flag (the documented no-
        # container fallback) — in container mode the eval leg is the
        # official WP4 harness, and the flag would be accepted but
        # inert (run() takes the container branch first). Reject it
        # loud instead
        if self.host_check is True and self.mode == "container":
            raise PilotConfigError(
                "--host-check is a sandbox-mode flag — the container "
                "mode's eval leg is the official WP4 harness")
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
    out = []
    for i, inst in enumerate(insts):
        if not isinstance(inst, dict):
            raise PilotConfigError(
                f"instance {i} must be a JSON object "
                f"(got {type(inst).__name__})")
        missing = []
        for k in ("instance_id", "repo", "base_commit",
                  "problem_statement"):
            v = inst.get(k)
            # a strict string (a bool / a bare number / an empty
            # string is a config error — not a raw TypeError inside
            # the git argv later)
            if not isinstance(v, str) or not v:
                missing.append(k)
        if missing:
            raise PilotConfigError(
                f"instance {i} ({inst.get('instance_id', '?')!r}) is "
                f"missing required field(s): {', '.join(missing)}")
        # the instance_id is joined into workdir paths
        # (``runs/<iid>``, ``devin-shim-<iid>``) and interpolated into
        # the docker-exec shim's shell script — a charset restriction
        # (the WP4 ``_charset_ok`` / WP5 ``_IID_CHARSET`` analogue)
        # keeps a custom ``--instances`` file from writing outside the
        # workdir (``..``) or injecting shell commands
        iid = inst["instance_id"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", iid):
            raise PilotConfigError(
                f"instance {i}: instance_id {iid!r} has characters "
                "outside [A-Za-z0-9._-] (the id is joined into "
                "workdir paths and interpolated into the docker-exec "
                "shim script — no metacharacters)")
        if ".." in iid:
            raise PilotConfigError(
                f"instance {i}: instance_id {iid!r} must not contain "
                "'..' (path traversal)")
        # the two list fields: an **empty** list is valid (an instance
        # with no PASS_TO_PASS); only absence / a wrong type fails
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            if k not in inst or not isinstance(inst[k], list):
                raise PilotConfigError(
                    f"instance {i}: {k} must be a list "
                    f"(got {type(inst.get(k)).__name__})")
        out.append(inst)
    ids = [i["instance_id"] for i in out]
    if len(set(ids)) != len(ids):
        raise PilotConfigError("duplicate instance_id in the instance file")
    return out


def prompt_for(inst: dict) -> str:
    return PROMPT_TEMPLATE.format(problem_statement=inst["problem_statement"])


def load_instances_meta(path: str) -> dict:
    """The instance file's ``_meta`` (the WP6b ``stratified_repos``
    provenance; ``{}`` for a bare list / a meta-less file)."""
    data = _load_json_file(path)
    if isinstance(data, dict):
        meta = data.get("_meta")
        return meta if isinstance(meta, dict) else {}
    return {}


def _validate_imported_line(rec: dict, iid: str,
                           origin: str) -> None:
    """The fail-closed reference validation shared by the
    ``--import-run`` path (:func:`load_imported`) and the
    state / checkpoint re-derivation (the round-20 Medium fix,
    issue #22, 4450828209): the re-derivation rebuilds
    ``imported`` from our own state / checkpoint lines and
    bypasses ``load_imported`` — a checkpoint line carrying
    ``meta.imported_from`` plus a ``..`` / absolute
    ``artifacts_dir`` would otherwise reach
    ``_copy_imported_artifacts`` (the value joins into the
    copy's destination paths — a write outside the workdir),
    while the identical value is a fail-closed config error on
    the ``--import-run`` path. The WP5 ``_parse_ts`` contract
    (the round-8 Medium fix) is re-checked too (a re-derived
    line is written back by the seeding; a non-conforming ts
    would wedge every later ``--resume`` in ``report_failed``).
    ``origin`` is the error-message label (the source-line vs.
    re-derivation phrasing).
    """
    meta = _meta_of(rec)
    ts = rec.get("ts")
    if "ts" in rec:
        if (ts is None
                or isinstance(ts, bool)
                or not isinstance(ts, (int, float))
                or not math.isfinite(ts) or ts <= 0):
            raise PilotConfigError(
                f"{origin} for {iid!r} carries a ts the WP5 "
                f"_parse_ts contract rejects ({ts!r}) — fail-closed "
                f"(a seeded non-finite / non-positive / bool / null "
                f"ts would wedge every later --resume in "
                f"report_failed)")
    for k in ("artifacts_dir", "patch_diff", "trajectory",
              "eval_log"):
        v = meta.get(k)
        if v is not None and not (isinstance(v, str) and v):
            raise PilotConfigError(
                f"{origin} for {iid!r} carries a {k} the WP5 "
                f"_ref_path contract rejects ({v!r}) — fail-closed "
                f"(a seeded non-string reference would wedge every "
                f"later --resume in report_failed)")
        if isinstance(v, str) and v and (
                os.path.isabs(v) or ".." in v.split("/")):
            raise PilotConfigError(
                f"{origin} for {iid!r} carries a {k} with a "
                f"path traversal or an absolute reference "
                f"({v!r}) — fail-closed: the value joins into "
                f"the artifact copy's paths (a '../../x' "
                f"reference would write outside this run's "
                f"workdir)")


def load_imported(config: PilotConfig, workdir: str,
                  insts: list) -> "tuple[dict, dict]":
    """The WP6b checkpoint import (issue #22 AC2 — the 2-run overlap
    reuse). Reads the source run's checkpoint, dedupes (last line per
    instance, the WP3 contract), and classifies each shared instance:

    * a **never-rerun** status → ``imported[iid]`` = the source line
      (it is seeded into this run's checkpoint with its original
      ``ts`` + ``meta.imported_from`` and skipped by ``resume_queue``
      — never re-run, no session spent);
    * a **retry-class** status → ``retry_class[iid]`` = the status
      (NOT imported — the instance runs in this run, the only
      pass@1 retry class, PLAN §2.5);
    * a source line whose ``instance_id`` is not in the current
      file → ignored (stale).

    Fail-closed: a missing source file, or a corrupt one, is a
    ``PilotConfigError`` (exit 2) — the validation happens before the
    pre-flight, so no session is ever spent against an import that
    did not validate.
    """
    src = os.path.join(config.import_workdir or workdir,
                      f"run-{config.import_run}.jsonl")
    # fail-closed: a SELF-IMPORT is rejected — with the default
    # import-workdir (this run's workdir) and --import-run equal to
    # this run's id, the source IS this run's own checkpoint: a
    # later-window --resume would re-derive ``imported`` from the
    # run's own seeded lines, overwrite ``state.import_source`` /
    # ``imported_run``, and item 16 would pass on a self-comparison
    # (the real 2-run overlap evidence clobbered)
    if (config.import_run == config.run_id
            and (config.import_workdir is None
                 or os.path.realpath(config.import_workdir)
                 == os.path.realpath(workdir))):
        raise PilotConfigError(
            f"--import-run {config.import_run!r} is this run's own "
            "run id with this run's workdir — a self-import is "
            "rejected (it would re-derive the import from the run's "
            "own seeded lines; item 16 would pass on a "
            "self-comparison)")
    if not os.path.isfile(src):
        raise PilotConfigError(
            f"--import-run source not found: {src!r} (the import is "
            "fail-closed — a missing / corrupt source is a config "
            "error before the pre-flight)")
    try:
        src_records, _ = rr.load_checkpoint(src)
    except rr.RunnerProtocolError as exc:
        raise PilotConfigError(
            f"--import-run source {src!r} is corrupt: {exc}") from exc
    try:
        finals, _ = rep.dedupe([r for r in src_records
                              if isinstance(r, dict)])
    except rep.ReportProtocolError as exc:
        # a charset-invalid instance_id in the source (the WP5
        # dedupe is fail-loud on it) is a corrupt source — the
        # documented exit-2 config error, not an exit-4 run failure
        raise PilotConfigError(
            f"--import-run source {src!r} is corrupt: {exc}") from exc
    known = {i["instance_id"] for i in insts}
    imported: dict = {}
    retry_class: dict = {}
    for iid, rec in finals.items():
        if iid not in known:
            continue  # stale — not in this run's instance file
        # the fail-closed source-line validation (the round-7
        # Medium fix, issue #22): a non-dict ``meta`` (or a
        # non-numeric ``ts``) passes the load_checkpoint / dedupe
        # validation (they only check instance_id / status) and
        # would crash the seeding loop MID-SEED (exit 4) instead
        # of the documented fail-closed config error (exit 2,
        # before any session) — and the persisted early import
        # record would make every later --resume hit the same
        # crash. Validate here, before any session.
        meta = rec.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise PilotConfigError(
                f"--import-run source line for {iid!r} carries a "
                f"non-dict meta ({type(meta).__name__}) — a "
                "corrupt source (fail-closed before any session)")
        # the round-12 Major fix (issue #22 AC2): a missing / null
        # meta is schema-conformant (all references absent) —
        # normalize it to {} for the guards below. The old code let
        # None reach the _ref_path schema loop — meta.get(k)
        # crashed with an uncaught AttributeError that escaped
        # run_pilot to main's generic handler (exit 4),
        # contradicting this function's documented fail-closed
        # contract (a corrupt source is a config error, exit 2,
        # before any session) — and every --resume re-invoked
        # load_imported and re-hit the same crash, the exact wedge
        # class the meta / ts / ref guards were added to close
        if meta is None:
            meta = {}
        # the round-20 Medium fix (issue #22): the ts / reference
        # schema / traversal validation is the SHARED helper
        # (also applied to the state / checkpoint re-derivation —
        # the identical fail-closed contract on both import
        # paths)
        _validate_imported_line(
            rec, iid, "--import-run source line")
        # the SOURCE-ARTIFACT LAYOUT normalization (the round-11
        # Blocker fix, issue #22 AC2): the pre-commit Pilot-A run
        # (2026-10-28 20:46–20:56 UTC — before commit 4259460)
        # wrote ``meta.artifacts_dir = <iid>`` while its artifacts
        # lived under ``runs/<iid>/`` — the import resolution
        # (``<src>/<iid>/...``) finds nothing, the copy no-ops,
        # and the seeded durable lines reference paths that can
        # NEVER exist: the completing invocation's ``rep.generate``
        # raises ArtifactCheckError (report_failed, exit 4) and
        # every --resume re-hits the same wedge (the self-heal
        # can never find the sources — the AC2 claim "the
        # referenced artifacts are copied so this run's WP5
        # cross-check holds" was false on the real pair). The
        # normalization: if the line's references resolve MISSING
        # under its own (relative) artifacts_dir but ALL exist
        # under ``runs/<artifacts_dir>/`` in the source workdir
        # (the current harness' ``_artifact_dir`` layout), rewrite
        # the imported line's meta to the current layout. Item 16's
        # faithfulness is unaffected (it reads status / wall /
        # tokens / patch_bytes from the meta — all preserved).
        refs = [meta.get(k) for k in ("patch_diff", "trajectory",
                                     "eval_log")
                if isinstance(meta.get(k), str) and meta.get(k)
                and not os.path.isabs(meta.get(k))]
        ad = meta.get("artifacts_dir")
        if (isinstance(ad, str) and ad and not os.path.isabs(ad)
                and refs):
            base = os.path.join(os.path.dirname(src) or ".", ad)
            if any(not os.path.isfile(os.path.join(base, r))
                   for r in refs):
                alt_base = os.path.join(
                    os.path.dirname(src) or ".", "runs", ad)
                if all(os.path.isfile(os.path.join(alt_base, r))
                       for r in refs):
                    meta["artifacts_dir"] = "runs/" + ad
        status = rec.get("status")
        if status in rr.NEVER_RERUN_STATUSES:
            imported[iid] = rec
        elif status in rr.RETRY_STATUSES:
            retry_class[iid] = status
        else:
            # a non-terminal status on a source line (impossible after
            # the load_checkpoint validation, but fail loud anyway)
            raise PilotConfigError(
                f"--import-run source line for {iid!r} carries "
                f"unknown status {status!r}")
    # the zero-overlap fail-closed guard (the round-12 Medium fix,
    # issue #22 AC2): a --import-run source that shares NO instance
    # ids with this run's instance file (an existing but unrelated
    # run id — it passes every other fail-closed check: the source
    # exists, is readable, well-formed) previously produced an empty
    # import: item 16 rendered n/a (non-gating) and the verdict-
    # bearing record could GO with zero AC2 overlap evidence, with
    # nothing in the record distinguishing it from a legitimate
    # non-import run. Fail-closed: a config error before any
    # session (a source / instance mismatch is an operator error —
    # the wrong --import-run or the wrong --instances)
    if (config.import_run and not imported
            and not retry_class):
        raise PilotConfigError(
            f"--import-run {config.import_run!r} shared no "
            "instance ids with this run's instance file — a "
            "source / instance mismatch (fail-closed: a "
            "zero-overlap import has no AC2 evidence and would "
            "let the record GO with item 16 n/a)")
    return imported, retry_class


def _copy_imported_artifacts(rec: dict, src_workdir: str,
                             dst_workdir: str) -> None:
    """The imported line's referenced artifacts (the WP5 reference
    contract: ``patch_diff`` / ``trajectory`` / ``eval_log`` resolve
    against ``artifacts_dir`` → the run JSONL's directory) are copied
    from the source run's workdir into this run's — the imported
    line is part of THIS run's record, and the WP5 cross-check
    (``report.crosscheck``) resolves every reference against the JSONL's
    own directory: a ``resolved`` / ``failed_test`` final must
    reference an existing ``patch.diff`` *here* (the leaderboard
    artifact's ``all_preds.jsonl`` patch content is cross-checked
    against this run's copy). A source file that is missing is left
    as-is (the cross-check flags it — fail loud, the WP5
    semantics)."""
    meta = rec.get("meta") or {}
    ad = meta.get("artifacts_dir")
    if isinstance(ad, str) and ad:
        src_base = (ad if os.path.isabs(ad)
                    else os.path.join(src_workdir, ad))
        dst_base = (ad if os.path.isabs(ad)
                    else os.path.join(dst_workdir, ad))
    else:
        # the WP5 ``_ref_path`` contract: a reference without an
        # ``artifacts_dir`` resolves against the run JSONL's
        # directory (the workdir) — the copy must land there too
        # (a None base would make the same-path guard a trivial
        # no-op and the artifact would never be copied — the
        # cross-check then reports it missing at report time)
        src_base, dst_base = src_workdir, dst_workdir
    for key in ("patch_diff", "trajectory", "eval_log"):
        val = meta.get(key)
        if not isinstance(val, str) or not val:
            continue
        if os.path.isabs(val):
            continue  # an absolute reference is not a workdir-relative
            # artifact — not an import-time concern
        src = os.path.join(src_base, val) if src_base else val
        dst = os.path.join(dst_base, val) if dst_base else val
        if os.path.abspath(src) == os.path.abspath(dst):
            continue  # a same-workdir import (the --import-workdir
            # default is this run's workdir): the artifact is
            # already in place (shutil.copyfile onto itself would
            # raise SameFileError)
        if os.path.isfile(dst):
            # the idempotent fast path — with a size check: a
            # PARTIALLY-COPIED dst (shutil.copyfile is not atomic —
            # a mid-copy crash leaves a truncated file) must not be
            # treated as complete: the WP5 check_artifacts R2
            # byte-compare then fails the cross-check at report
            # time (a permanent report_failed wedge — the same
            # class rounds 3–4 were fixing). The source file is
            # immutable (a completed run's artifact), so size
            # equality IS completeness; a size-mismatched (or
            # pre-fix truncated) dst is re-copied below (the
            # atomic copy — a new mid-copy crash leaves no
            # partial dst at all, only an orphan .part that the
            # next copy overwrites)
            if not (os.path.isfile(src)
                   and os.path.getsize(src) == os.path.getsize(dst)):
                pass  # fall through to the (atomic) copy
            else:
                continue
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # the atomic copy: <dst>.part + os.replace (the rename
            # is atomic on POSIX) — see the fast-path note above
            shutil.copyfile(src, dst + ".part")
            os.replace(dst + ".part", dst)


def seed_import_lines(jsonl_path: str, imported: dict,
                      import_run: str,
                      src_workdir: "Optional[str]" = None) -> list:
    """Idempotently seeds the imported lines into this run's
    checkpoint (issue #22 AC2). The lines are raw-written (the source
    ``ts`` is **preserved** — the imported session counts toward the
    source run's window, never this run's daily budget) and are
    skipped when the file already carries a line for the instance (a
    repeat ``--resume`` never re-seeds — no duplicate import lines).
    The write-side repair (the ``JsonlCheckpoint`` open — the
    torn-tail truncation) runs **before** the seeding, so a crash-
    resume never glues an import line onto a torn tail (which the
    next open would drop — a silent re-run of a never-rerun
    instance); the post-repair idempotency read then sees the clean
    file. The referenced artifacts are copied from ``src_workdir``
    for **every** imported id on every invocation (the WP5
    cross-check — see :func:`_copy_imported_artifacts` — idempotent
    per file: a crash between a line's fsync and its artifact copy
    self-heals on the next invocation). A mid-file corruption is a
    :class:`PilotConfigError` (exit 2, before any session). Returns
    the instance ids actually seeded."""
    if not imported:
        return []
    # the write-side repair FIRST (the WP3a torn-tail contract):
    # if this file carries a torn tail (the host-crash class), it
    # is truncated to the last clean boundary BEFORE the import
    # lines are appended — otherwise the import lines glue onto the
    # fragment and the next checkpoint open drops them, and the
    # never-rerun instance silently re-runs (a pass@1 violation).
    # The idempotency read happens AFTER the repair: a pre-repair
    # read of a torn file would fail and empty the ``existing``
    # set, and already-seeded lines would be duplicated
    try:
        with rr.JsonlCheckpoint(jsonl_path):
            pass  # the __init__ IS the repair
    except rr.RunnerProtocolError as exc:
        raise PilotConfigError(
            f"checkpoint {jsonl_path!r} is corrupt before the "
            f"import seeding: {exc}") from exc
    existing = set()
    if os.path.isfile(jsonl_path):
        # post-repair: the file ends on a clean boundary, so the
        # read is well-defined (a mid-file corruption is still
        # fail-loud — the config error class, before any session:
        # the tail repair covers only the tail, so a mid-file
        # corrupt line still makes load_checkpoint raise
        # RunnerProtocolError — the round-9 fix: catch it here and
        # fail closed (exit 2) as the comment promises, instead of
        # letting it propagate to the exit-4 runner-error path)
        try:
            recs, _ = rr.load_checkpoint(jsonl_path)
        except rr.RunnerProtocolError as exc:
            raise PilotConfigError(
                f"checkpoint {jsonl_path!r} is corrupt (a "
                f"mid-file corruption — the tail repair covers "
                f"only the tail; the config error class, before "
                f"any session): {exc}") from exc
        existing = {r.get("instance_id") for r in recs
                   if isinstance(r, dict) and r.get("instance_id")}
    seeded = []
    dst_workdir = os.path.dirname(jsonl_path) or "."
    for iid in sorted(imported):
        if iid in existing:
            continue
        rec = imported[iid]
        m = dict(rec.get("meta") or {})
        m["imported_from"] = import_run or "imported"
        line = {"instance_id": iid, "status": rec.get("status"),
                "meta": m, "ts": rec.get("ts", time.time())}
        with open(jsonl_path, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(line) + "\n")
            f.flush()
            os.fsync(f.fileno())
        seeded.append(iid)
    # the referenced-artifact copy for EVERY imported id (not just
    # the newly seeded ones — a crash between a line's fsync and
    # its artifact copy leaves a durable line with missing
    # artifacts that every later resume skips; the idempotent
    # per-file copy self-heals that on this invocation instead of
    # leaving the WP5 cross-check to fail report_failed with no
    # automated recovery)
    if src_workdir:
        for iid in sorted(imported):
            _copy_imported_artifacts(imported[iid], src_workdir,
                                    dst_workdir)
    return seeded


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


class PilotPatchTooLarge(PilotError):
    """A patch that exceeds the size cap (PLAN §4) — an
    *instance*-class failure (the ``eval_error`` route: recorded in
    the checkpoint, the queue is not aborted, a resume may retry with
    a fresh session that could produce a smaller patch), not a
    run-killing fatal (the fatal writes no checkpoint line, so every
    ``--resume`` re-runs the full session at quota before hitting the
    same deterministic cap — and aborts the whole queue, leaving the
    run unconvergeable)."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__(
            f"patch size {size} exceeds the cap {cap} bytes (PLAN §4)")
        self.size = size
        self.cap = cap


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
    # the local branches are the remaining leak vector: the clone
    # leaves the default branch (``main``/``master``) at the
    # upstream tip — PAST ``base_commit``, past the instance's
    # merged fix commit (one ``git log`` away). The setup
    # ``checkout --detach``'d first, so every local branch can be
    # deleted safely
    # ``for-each-ref`` (not ``git branch``): the latter prints a
    # ``(HEAD detached at …)`` decoration line on a detached checkout,
    # which would contaminate the branch list and make ``-D`` fail
    p = _git(testbed, "for-each-ref",
             "--format=%(refname:short)", "refs/heads/")
    branches = [b for b in p.stdout.decode("utf-8", "replace").split()
               if b]
    if branches:
        p = _git(testbed, "branch", "-D", *branches)
        if p.returncode != 0:
            res["warnings"].append(
                "branch delete: "
                + p.stderr.decode("utf-8", "replace").strip())
    p = _git(testbed, "remote")
    res["remotes_after"] = p.stdout.decode("utf-8", "replace").split()
    return res


def verify_leak_guard(testbed: str,
                      base_commit: "Optional[str]" = None) -> dict:
    """The post-session half of item 14: no remote survives, no tags
    survive (a remote the agent *re-added* would show up here), and —
    when ``base_commit`` is given — no local branch points PAST
    ``base_commit`` (a branch tip that is not an ancestor of the
    base carries the merged fix commit: a leak the remotes/tags
    check alone would miss)."""
    p = _git(testbed, "remote")
    remotes = p.stdout.decode("utf-8", "replace").split()
    p = _git(testbed, "tag", "-l")
    tags = p.stdout.decode("utf-8", "replace").split()
    branches: list = []
    ahead: list = []
    p = _git(testbed, "for-each-ref",
             "--format=%(refname:short) %(objectname)", "refs/heads/")
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[0]:
            branches.append(parts[0])
            if base_commit:
                # a LEAK = a branch tip NOT reachable from the base
                # (past or diverged — it carries the merged fix
                # commit). Clean = the tip is an ancestor of (or
                # equals) the base
                p2 = _git(testbed, "merge-base", "--is-ancestor",
                         parts[1], base_commit)
                if p2.returncode != 0:
                    # past/diverged (or an unresolvable object —
                    # fail-closed: report it)
                    ahead.append(parts[0])
    return {"remotes": remotes, "tags": tags, "branches": branches,
            "branches_ahead_of_base": ahead,
            "clean": not remotes and not tags and not ahead}


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
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        # a FIFO / socket / device: ``open()`` on a FIFO BLOCKS
        # indefinitely — an agent-created fifo left in the tree must
        # not hang the extraction (treat as non-regular: it is not a
        # binary payload)
        return False
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

    # ``-z`` (NUL-separated): the default line mode QUOTES special
    # paths (a non-ASCII filename prints as ``"w\303\251ird\n"``),
    # which would silently drop the agent's files from the diff —
    # ``-z`` gives raw, unquoted paths (even with newlines in them)
    p = _git(cwd, "status", "--porcelain", "-z")
    if p.returncode != 0:
        raise PilotError(
            "git status --porcelain -z failed: "
            + p.stderr.decode("utf-8", "replace").strip())
    porcelain: dict = {}
    untracked: list = []
    for entry in p.stdout.split(b"\x00"):
        # a well-formed entry is ``XY <path>`` (the 3rd byte is a
        # space) — a rename/copy source path is a bare field without
        # the status prefix (skipped: only the new name matters)
        if len(entry) < 4 or entry[2:3] != b" ":
            continue
        xy = entry[:2].decode("utf-8", "replace")
        path = entry[3:].decode("utf-8", "replace")
        if xy.startswith("??"):
            untracked.append(path)
        else:
            porcelain[path] = xy

    def _guard(path: str) -> "Optional[str]":
        """The per-path verdict: the skip reason, or None (include)."""
        if _exclude(path):
            return "exclusion pattern"
        if _is_binary(os.path.join(cwd, path)):
            return "binary content"
        if not _looks_source_or_test(path):
            return "outside source/test dirs"
        return None

    included: list = []
    skipped: list = []
    for path in untracked:
        full = os.path.join(cwd, path)
        if os.path.isdir(full):
            # a collapsed untracked dir (any depth — ``?? tests/``,
            # ``?? src/newpkg/``). A dir that is excluded as a whole
            # (``.venv/``, ``node_modules/``, ``__pycache__/``) is
            # reported as a single dir entry (no per-file walk of a
            # 10k-file venv); otherwise the dir is expanded to its
            # actual files and the guards run per file — a nested
            # source/test dir (``src/newpkg/``, ``src/tests/``) is
            # found at the FILE level (the lexical gate on the
            # collapsed dir path would drop a ≥2-component dir whose
            # own name is not a source/test marker, silently losing
            # the agent's new package)
            dpath = path if path.endswith("/") else path + "/"
            if _exclude(dpath):
                skipped.append([path, "exclusion pattern"])
                continue
            found = False
            for root, dirs, files in os.walk(full):
                for f in files:
                    fp = os.path.relpath(os.path.join(root, f), cwd)
                    why = _guard(fp)
                    if why:
                        skipped.append([fp, why])
                        continue
                    included.append(fp)
                    found = True
            if not found:
                # an empty dir, or one with only excluded / non-source
                # files: report the collapsed entry itself
                skipped.append([path, _guard(dpath) or
                               "outside source/test dirs"])
            continue
        why = _guard(path)
        if why:
            skipped.append([path, why])
            continue
        included.append(path)

    # the staged entries (the agent ran ``git add -N`` — porcelain
    # ``" A"`` — a real ``git add`` — ``"A "`` — or staged-then-
    # modified — ``"AM"``): the FULL guard applies regardless of the
    # staging state (the exclusion list, the binary guard, AND the
    # source/test lexical gate — ``git diff HEAD`` captures staged
    # entries, so a staged non-source file or an excluded-dir file
    # must not reach the patch): a rejected entry is UNSTAGED and
    # reported; a legitimate staged source file is left staged
    for path, status in porcelain.items():
        if status in ("A ", " A", "AM"):
            why = _guard(path)
            if why:
                _git(cwd, "reset", "--", path)
                skipped.append([path, f"pre-staged entry: {why} — unstaged"])

    # the included set is files only (the collapsed dirs were expanded
    # above) — the staging is a flat ``git add -N`` (never on a dir:
    # git would recursively stage everything under it)
    add_args: list = list(included)
    if add_args:
        # a failing pathspec must not be silent: an unchecked
        # ``git add -N`` would leave the intent-to-add missing and
        # ``git diff HEAD`` would quietly drop the agent's files.
        # The ``--`` separator: an agent-created leading-dash
        # filename is a path, not an option (without it the add
        # would fail and this would be a run-killing PilotError —
        # the ``git reset --`` directly below guards the same list)
        p = _git(cwd, "add", "-N", "--", *add_args)
        if p.returncode != 0:
            raise PilotError(
                "git add -N failed: "
                + p.stderr.decode("utf-8", "replace").strip())
    try:
        # ``-c core.quotepath=false``: the diff output must carry RAW
        # paths (a non-ASCII filename would otherwise be quoted in the
        # diff while the porcelain ``-z`` list is raw — the two would
        # disagree and the agent's files would not round-trip to the
        # eval leg's ``git apply``)
        p = _git(cwd, "-c", "core.quotepath=false", "diff", "HEAD")
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
        raise PilotPatchTooLarge(total, PATCH_SIZE_CAP_BYTES)

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
    inject via ``make_session``). ``container`` names the agent
    container (container mode) — the host-side git ops (patch
    extraction, the leak verification) then run against a host **copy**
    of ``/testbed`` (``docker cp``), because the in-container path does
    not exist on the host filesystem."""
    instance: dict
    mode: str
    devin_bin: str
    model: str
    cwd: str
    env: dict
    wall_s: float
    host_check: bool
    workdir: str
    container: "Optional[str]" = None
    # the run's label for the eval-leg artifact names (issue #22 —
    # the WP4 artifacts are per-run; a pilot-B run must not label its
    # artifacts ``pilotA-...``). The default is a neutral label for a
    # direct spec construction — run_pilot always passes
    # run_id=config.run_id (the pilot's default run id is
    # pilot-<timestamp>), so a pilot run's WP4 artifact / container
    # labels are run_id-labelled by construction. The round-18
    # Minor fix (issue #22): the old "pilotA" default was a stale
    # literal — any direct (non-run_pilot) SessionSpec construction,
    # including a pilot-B one, silently labelled its WP4 eval
    # artifacts / container names ``pilotA``; the neutral "run"
    # label makes the direct-construction case visible instead
    run_id: str = "run"


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

        # the patch (PLAN §4) — an oversize patch is the
        # ``eval_error`` instance class, not a run fatal (see
        # PilotPatchTooLarge): the checkpoint line is written, the
        # queue is not aborted, a resume may retry
        try:
            ext = extract_patch(self._extract_repo_dir())
        except PilotPatchTooLarge as exc:
            self.meta["eval_skip_reason"] = str(exc)[:200]
            # the AC3 root cause (issue #22): an oversize patch is
            # unusable by definition — the ``patch`` class (the
            # agent's output, not the env / not the parser)
            self.meta["eval_error_root_cause"] = "patch"
            self.meta["eval_error_root_cause_detail"] = str(exc)[:200]
            self._write_eval_log(f"eval skipped: {exc}")
            self._write_trajectory()
            return "eval_error"
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
        # the AC3 root cause (issue #22): the documented skip is the
        # ``env`` class (no eval leg available in this environment)
        self.meta["eval_error_root_cause"] = "env"
        self.meta["eval_error_root_cause_detail"] = (
            "no container runtime (sandbox mode, host check off)")
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
                self._extract_repo_dir(),
                self._spec.instance.get("base_commit"))
        except Exception as exc:
            # close is best-effort (the WP3 contract) — but a failed
            # close is RECORDED (the checklist item 7 must not render
            # "pass" for a check that did not run)
            self.meta["close_error"] = f"{type(exc).__name__}: {exc}"

    # -- helpers ----------------------------------------------------------

    def _default_make_session(self, spec: SessionSpec) -> ac.AcpSession:
        if spec.mode == "container" and spec.container:
            # the devin process is spawned on the HOST (the shim
            # forwards into the container with ``docker exec -w
            # /testbed``) — the host ``Popen`` needs a host path;
            # ``spec.cwd`` (``/testbed``) exists only inside the
            # container, so it is the SESSION cwd (what the agent
            # works in, inside the container) while the spawn cwd is
            # a host-side scratch dir
            spawn = os.path.join(self._artifact_dir(), "spawn")
            os.makedirs(spawn, exist_ok=True)
            return ac.AcpSession(
                spec.devin_bin, model=spec.model, cwd=spawn,
                session_cwd=spec.cwd,
                env=spec.env, policy=ac.SwebenchPolicy(workdir=spec.cwd),
                prompt_timeout_s=spec.wall_s)
        return ac.AcpSession(
            spec.devin_bin, model=spec.model, cwd=spec.cwd,
            env=spec.env, policy=ac.SwebenchPolicy(workdir=spec.cwd),
            prompt_timeout_s=spec.wall_s)

    def _artifact_dir(self) -> str:
        iid = self._spec.instance["instance_id"]
        return os.path.join(self._spec.workdir, "runs", iid)

    def _extract_repo_dir(self) -> str:
        """The repo dir the host-side git ops run against. Sandbox:
        the agent checkout itself (``spec.cwd``). Container: the
        in-container ``/testbed`` does not exist on the host — the
        working tree is copied out (``docker cp``) and the host-side
        extraction / leak verification run on the copy (the copy is
        scratch: the intent-to-add staging touches only the copy's
        index; the agent's container tree is never modified)."""
        if self._spec.mode != "container" or not self._spec.container:
            return self._spec.cwd
        copy = os.path.join(self._artifact_dir(), "testbed-copy")
        if not os.path.isdir(os.path.join(copy, ".git")):
            if os.path.isdir(copy):
                shutil.rmtree(copy)
            p = subprocess.run(
                ["docker", "cp",
                 f"{self._spec.container}:testbed", copy],
                capture_output=True, timeout=3600, check=False)
            if p.returncode != 0 or not os.path.isdir(
                    os.path.join(copy, ".git")):
                raise PilotError(
                    "docker cp of /testbed failed: "
                    + p.stderr.decode("utf-8", "replace").strip()[:500])
        return copy

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
        # the permission responses the client could not send (a
        # non-zero count = a request the server never heard — item 6
        # must be able to fail on it)
        self.meta["permission_send_failures"] = getattr(
            self._acp, "permission_send_failures", 0)
        # the mid-session stderr tail (the client's drain thread —
        # ``stderr_tail`` itself is only populated at close(), which is
        # too late for the error paths + the quota signal)
        self.meta["stderr_tail"] = self._stderr_tail()[-2000:]

    def _stderr_tail(self) -> str:
        """The session stderr tail — the live accessor (the drain
        thread's chunks) during the session, falling back to the
        close()-captured ``stderr_tail`` afterwards."""
        if self._acp is None:
            return ""
        fn = getattr(self._acp, "stderr_tail_now", None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
        return self._acp.stderr_tail or ""

    def _quota_signal(self, result: ac.PromptResult) -> bool:
        stop = (result.stop_reason or "").lower()
        if _quota_marker_hit(stop):
            return True
        tail = self._stderr_tail().lower()
        return _quota_marker_hit(tail)

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
                run_id=self._spec.run_id,
            )
        except ev.EvaluationError as exc:
            self.meta["wall"]["eval_s"] = round(
                time.monotonic() - t0, 3)
            # the AC3 root cause (issue #22): the WP4 typed errors
            # classify cleanly — the harness's own git-apply chain
            # wrote its APPLY_PATCH_FAIL marker (the patch is
            # corrupt/truncated) is the ``patch`` class; every other
            # harness failure (spawn / watchdog / non-zero exit /
            # input validation / a timeout) is ``env`` (the harness
            # container / the harness itself)
            self.meta["eval_error_root_cause"] = (
                "patch" if isinstance(exc, ev.PatchCorruptionError)
                else "env")
            self.meta["eval_error_root_cause_detail"] = (
                f"{type(exc).__name__}: {exc}"[:200])
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
        base = os.path.abspath(os.path.join(self._spec.workdir,
                                           "host-check", iid))
        os.makedirs(base, exist_ok=True)
        repo_url = inst.get("repo_url",
                           "https://github.com/" + inst["repo"] + ".git")
        log_lines = [f"host check (the documented no-container "
                     f"fallback) for {iid}",
                     f"  base_commit: {inst['base_commit']}",
                     f"  repo_url:    {repo_url}"]
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
                    # the AC3 root cause (issue #22): the MODEL patch
                    # not applying to the fresh checkout is the
                    # ``patch`` class; the TEST patch not applying is
                    # the ``env`` class (the harness test fixture /
                    # the pinned base — not the agent's patch). A
                    # "can't open patch"-class failure is a HARNESS
                    # path failure (the file is missing — e.g. the
                    # workdir-relative path resolved against the
                    # clone by ``git -C``): the ``env`` class — the
                    # model patch content never got a chance, and
                    # classifying it ``patch`` would misgate the AC3
                    # taxonomy
                    err = p.stderr.decode("utf-8", "replace").strip()
                    if "can't open" in err.lower():
                        cause = "env"
                    else:
                        cause = ("patch" if name == "model_patch"
                                else "env")
                    raise _HostCheckInfra(
                        f"git apply {name}: {err}",
                        root_cause=cause)
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
            results = _parse_pytest_ra(out, ids)
            if results is None:
                # the AC3 root cause (issue #22): the tests ran but
                # the log-parser leg produced no parseable results —
                # the ``parser`` class
                raise _HostCheckInfra(
                    f"pytest exited {p.returncode} with no parseable "
                    "-rA results", root_cause="parser")
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
            # the AC3 root cause (issue #22) — the class the failed
            # phase maps to (the PLAN-WP6B.md table)
            self.meta["eval_error_root_cause"] = getattr(
                exc, "root_cause", "env")
            self.meta["eval_error_root_cause_detail"] = str(exc)[:200]
            return "eval_error"
        except (OSError, subprocess.SubprocessError) as exc:
            # a spawned-tool failure that is not a returncode — a
            # ``FileNotFoundError`` (a venv without ``bin/pip``) or a
            # ``subprocess.TimeoutExpired`` (a hung pip/pytest) — is
            # likewise the ``eval_error`` infra class, not a
            # run-killing fatal (it would otherwise escape the
            # documented ``eval_error`` handling); the root cause is
            # ``env`` (the host infra — a missing tool / a hang)
            self.meta["wall"]["eval_s"] = round(
                time.monotonic() - t0, 3)
            log_lines.append(
                f"  infra failure: {type(exc).__name__}: {exc}")
            self._write_eval_log("\n".join(log_lines))
            self.meta["eval_skip_reason"] = (
                f"{type(exc).__name__}: {exc}"[:200])
            self.meta["eval_error_root_cause"] = "env"
            self.meta["eval_error_root_cause_detail"] = (
                f"{type(exc).__name__}: {exc}"[:200])
            return "eval_error"
        finally:
            self.meta["graded_by"] = "host_check(pytest -rA)"


def _find_python() -> str:
    for cand in (sys.executable, "python3", "python"):
        if cand and shutil.which(cand) is not None:
            return cand
    raise PilotConfigError("no python interpreter found for the host check")


def _parse_pytest_ra(out: str, requested: list) -> "Optional[dict]":
    """The per-node-id results from the ``-rA`` short summary lines.
    Real pytest shapes: ``PASSED <id>``, ``FAILED <id> - <reason>``,
    ``ERROR <id-or-file> - <reason>`` (a collection error is a
    FILE-level line — the suffix would otherwise land in the key), and
    ``SKIPPED [N] <file>:<line>: <reason>`` (a ``file:line`` key, no
    ``::`` — it never matches a node id lexically). A file-level
    outcome is attributed to every requested node id under that file
    (a collection error kills every test in the file). Returns the
    id→state map (``passed``/``failed``/``error``/``skipped``/
    ``xfailed``/``xpassed``) or None when nothing parsed (an infra
    failure, not a verdict)."""
    # ``XFAIL`` is its own state: the official SWE-bench grading
    # requires a ``PASSED`` line, so an xfailed FAIL_TO_PASS does NOT
    # count as passed (it lands in ``failed_f2p``); ``XPASS``
    # (unexpectedly passed) is likewise not a ``PASSED`` line
    # (fail-closed)
    out_map = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error",
              "SKIPPED": "skipped", "XFAIL": "xfailed", "XPASS": "failed"}
    node_outcomes: dict = {}
    file_outcomes: dict = {}
    for line in out.splitlines():
        m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(.*)$",
                     line.strip())
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        # ``SKIPPED [N] …`` — strip the count token
        rest = re.sub(r"^\[\d+\]\s*", "", rest)
        # ``FAILED <id> - <reason>`` / ``ERROR <id> - <reason>`` — the
        # reason is not part of the id
        node = re.split(r"\s+-\s+", rest, 1)[0].strip()
        # ``XFAIL <id> [reason]`` / ``XPASS <id> [reason]`` — some
        # pytest versions bracket the reason with a space separator
        # (a parameterized id's ``[args]`` is attached to the name
        # without a space, so the space-requirement keeps it intact)
        node = re.sub(r"\s+\[[^\]]*\]$", "", node).strip()
        if "::" in node:
            node_outcomes.setdefault(node, out_map[kind])
        else:
            # a file-level id (a collection ``ERROR``) or a ``file:line``
            # (``SKIPPED``) — attribute to the requested node ids under
            # the file
            filepart = node.split(":", 1)[0]
            file_outcomes.setdefault(filepart, out_map[kind])
    res: dict = {}
    for rid in requested:
        if rid in node_outcomes:
            res[rid] = node_outcomes[rid]
        elif rid.split("::", 1)[0] in file_outcomes:
            res[rid] = file_outcomes[rid.split("::", 1)[0]]
    # any parsed line that is not a requested id is kept too (the
    # caller's ``missing`` computation only looks at the requested set)
    res.update(node_outcomes)
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
        # ``copyfile`` drops the permission bits — a mode-600
        # ``credentials.toml`` would land at the umask default
        # (typically 0644), leaving the devin auth token
        # world-readable for the session's duration. Preserve the
        # source's mode
        shutil.copymode(host_creds,
                       os.path.join(xdg_data, "devin",
                                   "credentials.toml"))
    env = dict(os.environ)
    env.pop("DEVIN_REFUSAL_FALLBACK", None)  # PLAN §3.4 (the client
    # also enforces this — defense in depth)
    env.pop("DEVIN_MODEL", None)  # the --model flag is authoritative
    env["XDG_DATA_HOME"] = xdg_data
    env["XDG_CONFIG_HOME"] = xdg_config
    env, stripped = _sanitize_session_env(env)
    return {"cwd": testbed, "env": env, "env_stripped": stripped,
            "leak_guard": guard}


def _sanitize_session_env(env: dict) -> "tuple[dict, list]":
    """PLAN §3.5 (extended): strip host credentials from the agent
    environment — a denylist of secret-ish keys (token/secret/key/
    password/passwd/credential/apikey/auth, case-insensitive) so the
    agent process (and its in-scope exec tool — ``env``/``printenv``
    dump them) never inherits the host's ``GITHUB_TOKEN`` / cloud API
    keys. Returns the filtered env + the stripped key names (recorded
    in the session meta)."""
    secret = re.compile(
        r"(?i)(token|secret|passwd|password|credential|apikey|_key|"
        r"api_key|auth)")
    out: dict = {}
    stripped: list = []
    for k, v in env.items():
        if secret.search(k):
            stripped.append(k)
        else:
            out[k] = v
    return out, stripped


def _host_credentials_file() -> "Optional[str]":
    """The host credentials file (WP0 §2: ``$XDG_DATA_HOME/devin/
    credentials.toml``) — for the ephemeral-XDG seeding only."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "devin", "credentials.toml")
    home = os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "devin",
                       "credentials.toml")


def _host_versions_dir() -> str:
    """The host ``_versions`` tree (the real-binary source for the
    in-container injection, WP0 §2) — env override, then the XDG
    locations; a clear error when absent (the container-mode setup must
    not bind a non-existent path)."""
    env = os.environ.get("DEVIN_VERSIONS_DIR")
    if env:
        if not os.path.isdir(env):
            raise PilotError(
                f"DEVIN_VERSIONS_DIR={env!r} is not a directory "
                "(the _versions tree for the real-binary injection)")
        return env
    candidates: list = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "devin", "cli", "_versions"))
    candidates.append(os.path.expanduser(
        "~/.local/share/devin/cli/_versions"))
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    raise PilotError(
        "the devin _versions tree was not found (tried: "
        + ", ".join(candidates) + ") — set DEVIN_VERSIONS_DIR to the "
        "_versions directory (container mode, WP0 §2)")


def _image_for(inst: dict) -> str:
    """The instance's ``sweb.eval`` image — the instance file's
    **pinned** ``image`` field when present (a custom instances file
    whose pinned image diverges from the derivation must pull the
    pinned one, not the derived name), otherwise the WP3b default
    derivation (the Docker-Hub ``__`` → ``_1776_`` escape)."""
    img = inst.get("image")
    if isinstance(img, str) and img:
        return img
    return ("swebench/sweb.eval.x86_64."
            + inst["instance_id"].replace("__", "_1776_"))


def _container_setup(inst: dict, workdir: str) -> dict:
    """The container-mode setup (PLAN §3.1–§3.6): the ``sweb.eval``
    image start, the **real**-binary injection (the ``_versions`` tree —
    never the wrapper), the ephemeral XDG, the leak guard inside the
    container, and a ``docker exec`` shims script as the session's
    ``devin_bin`` (the WP1 client spawns one command — the shim forwards
    it into the container with the isolated env, PLAN §3.2/§3.5)."""
    iid = inst["instance_id"]
    img = _image_for(inst)
    versions = _host_versions_dir()
    subprocess.run(["docker", "pull", img], check=True, timeout=7200)
    # a failure anywhere after ``docker run -d`` (the injection, the
    # XDG seeding, the guard, the shim write) would leave
    # ``sweb.eval.<iid>`` running — the instance gets no checkpoint
    # line, so ``prune_instance`` would never remove the named
    # container (a leak). Best-effort cleanup on any failure
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", f"sweb.eval.{iid}",
             "-v", f"{versions}:/devin-inject:ro",
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
        # the leak guard (PLAN §3.6) — the record is kept (the
        # checklist item 14 evidence)
        p = subprocess.run(
            ["docker", "exec", f"sweb.eval.{iid}", "sh", "-c",
             "cd /testbed && { "
             "git remote remove origin 2>/dev/null; "
             "git checkout --detach 2>/dev/null; "
             "git for-each-ref --format='%(refname:short)' refs/heads/ "
             "| xargs -r -n1 git branch -D 2>/dev/null; "
             "git tag -l | xargs -r git tag -d 2>/dev/null; "
             "git reflog expire --expire=now --all 2>/dev/null; "
             "git remote; }"],
            capture_output=True, timeout=300)
    except Exception:
        subprocess.run(
            ["docker", "rm", "-f", f"sweb.eval.{iid}"],
            capture_output=True, timeout=300, check=False)
        raise
    leak_guard = {
        "warnings": ([] if p.returncode == 0 else
                    ["leak guard: "
                     + p.stderr.decode("utf-8", "replace").strip()]),
        "remotes_after": (p.stdout.decode("utf-8", "replace").split()
                          if p.returncode == 0 else []),
    }
    # the docker-exec shim (the session's devin_bin) — ONE line: the
    # ``exec`` and every flag must form a single command (a multi-line
    # shim would exec ``docker exec -i`` with no container and die)
    shim = os.path.join(workdir, f"devin-shim-{iid}")
    with open(shim, "w", encoding="utf-8", newline="") as f:
        f.write(
            "#!/bin/sh\n"
            f"exec docker exec -i "
            "-e XDG_DATA_HOME=/tmp/xdg-data "
            "-e XDG_CONFIG_HOME=/tmp/xdg-config "
            "-e PATH=/opt/miniconda3/envs/testbed/bin:"
            "/usr/local/bin:/usr/bin:/bin "
            "-e CONDA_DEFAULT_ENV=testbed "
            "-w /testbed "
            f"sweb.eval.{iid} /usr/local/bin/devin \"$@\"\n")
    os.chmod(shim, 0o755)
    env, stripped = _sanitize_session_env({})
    return {"devin_bin": shim, "env": env, "env_stripped": stripped,
            "container": f"sweb.eval.{iid}", "image": img,
            "leak_guard": leak_guard}


# ---------------------------------------------------------------------------
# the pre-flight (per mode — the WP3b legs, assembled with
# run_preflight_checks so the short-circuit + wrapping hold)
# ---------------------------------------------------------------------------

def make_preflight(config: PilotConfig,
                   instances: "Optional[list]" = None) -> Callable:
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

    def _binary() -> None:
        # the missing-binary gate (a missing ``devin`` binary is a
        # pre-flight failure — the loud exit-4 path; the ``devin
        # version`` probe above is informational only)
        if shutil.which(config.devin_bin) is None:
            raise rr.PreflightFailed(
                "binary", f"the {config.devin_bin!r} binary is not on "
                         f"PATH (or not executable)")

    def _ensure_docker_image(img: str) -> None:
        # pull-if-missing: a cold host has never pulled the instance
        # image — ``docker image inspect`` would fail and the per-
        # instance pre-flight would abort before the setup's own pull
        # could run. The pull is zero-quota (no agent session), so it
        # belongs in the pre-flight (the WP3b per-instance leg); the
        # setup's pull then becomes a no-op
        p = subprocess.run(["docker", "image", "inspect", img],
                          capture_output=True, timeout=timeout_s)
        if p.returncode == 0:
            return
        try:
            p = subprocess.run(["docker", "pull", img],
                              capture_output=True, timeout=7200)
        except Exception as exc:
            raise rr.PreflightFailed(
                "docker_image", f"docker pull could not be run: "
                     f"{type(exc).__name__}: {exc}") from exc
        if p.returncode != 0:
            tail = (p.stderr or b"")[-200:].decode(
                "utf-8", "replace").strip()
            raise rr.PreflightFailed(
                "docker_image", f"docker pull exited {p.returncode}: {tail!r}")
        p = subprocess.run(["docker", "image", "inspect", img],
                          capture_output=True, timeout=timeout_s)
        if p.returncode != 0:
            raise rr.PreflightFailed(
                "docker_image", f"the image {img!r} is still absent "
                                f"after the pull")

    def _check(instance_id: "Optional[str]") -> None:
        checks = [
            ("binary", _binary),
            ("auth",
             lambda: _cmd("auth", [config.devin_bin, "auth", "status"])),
        ]
        if config.mode == "container":
            def _versions() -> None:
                try:
                    vdir = _host_versions_dir()
                except PilotError as exc:
                    # a missing ``_versions`` tree would be the
                    # setup's mid-batch fatal — surface it at the
                    # zero-quota gate instead
                    raise rr.PreflightFailed(
                        "versions", str(exc)) from exc
                # the tree must actually carry the injected binary —
                # a dir that exists but lacks ``current/bin/devin``
                # would still fail the setup's ``cp`` mid-batch
                if not os.path.isfile(
                        os.path.join(vdir, "current", "bin", "devin")):
                    raise rr.PreflightFailed(
                        "versions",
                        f"the _versions tree {vdir!r} has no "
                        "current/bin/devin (the setup's binary "
                        "injection would fail)")
            checks.append(("versions", _versions))
            checks.append(
                ("docker_daemon",
                 lambda: _cmd("docker_daemon", ["docker", "info"])))
            if instance_id is not None:
                # pull-if-missing (a cold host pre-flights the pull;
                # the setup's pull then no-ops). The image is the
                # instance file's PINNED ``image`` field when present
                # (a custom file whose pinned image diverges from the
                # derivation must pre-flight the pinned one)
                inst = next((i for i in (instances or [])
                            if i["instance_id"] == instance_id), None)
                img = (_image_for(inst) if inst is not None else
                       "swebench/sweb.eval.x86_64."
                       + instance_id.replace("__", "_1776_"))
                checks.append(
                    ("docker_image",
                     lambda: _ensure_docker_image(img)))
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
        # the post-instance hook (the pilot's prune) runs BEFORE the
        # line is merged + persisted — its meta write (the pruning
        # record) must land in the same JSONL line, not a later one
        if instance_id is not None and self._on_instance_done is not None:
            self._on_instance_done(instance_id, status)
        merged = dict(meta)
        if instance_id is not None and instance_id in self._meta:
            for k, v in self._meta[instance_id].items():
                merged.setdefault(k, v)  # the runner's wall_s wins
        for k in ("artifacts_dir", "patch_diff", "trajectory",
                  "eval_log"):
            merged.setdefault(k, None)
        self._ckpt.record_instance(instance_id, status, merged)
        # the session arithmetic: a ``preflight_failed`` mark consumed
        # no session (the budget claim is released — the WP3b contract)
        # and does not count toward the --interrupt-after check
        if instance_id is not None and status != "preflight_failed":
            self.done_count += 1

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
        img = _image_for(inst)
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
                 "host-check tree")):
            if os.path.isdir(path):
                try:
                    shutil.rmtree(path)
                    out["removed"].append(label)
                except Exception as exc:
                    out["failed"].append(f"{label}: {exc}")
    # the WP4 eval workdir (harness trees — potentially GBs per
    # instance) is removed in BOTH modes (the host check / the
    # container eval leg both work under ``eval/<iid>``)
    eval_wd = os.path.join(workdir, "eval", iid)
    if os.path.isdir(eval_wd):
        try:
            shutil.rmtree(eval_wd)
            out["removed"].append("eval workdir")
        except Exception as exc:
            out["failed"].append(f"eval workdir: {exc}")
    # the container-mode /testbed host copy (the scratch tree the
    # host-side extraction ran on — removed in both modes; a no-op in
    # sandbox mode where it was never created)
    copy = os.path.join(workdir, "runs", iid, "testbed-copy")
    if os.path.isdir(copy):
        try:
            shutil.rmtree(copy)
            out["removed"].append("testbed copy")
        except Exception as exc:
            out["failed"].append(f"testbed copy: {exc}")
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
        meta = _meta_of(rec)
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
                   attempt_records: "Optional[list]" = None,
                   skipped_on_resume: dict,
                   interrupted_this_run: "Optional[dict]",
                   resume_after_interrupt: bool,
                   devin_ok: "Optional[bool]",
                   sessions_used: int,
                   wall_budget_s: float,
                   wall_used_s: float,
                   pilot_b: bool = False,
                   stratified_repos: "Optional[list]" = None,
                   imported: "Optional[dict]" = None,
                   import_attempted: bool = False,
                   repo_by_iid: "Optional[dict]" = None,
                   arithmetic_total: int = 3,
                   window_buckets: "Optional[dict]" = None) -> list:
    """The go/no-go checklist (PLAN-WP6A.md — 14 items; PLAN-WP6B.md
    — 17 for a ``--pilot b`` run: 15 stratification (AC1), 16 the
    2-run overlap/variance (AC2), 17 the root-caused ``eval_error``
    (AC3)). ``pass`` is None in a dry run (the skeleton). Item 13
    (the eval leg) is deviation-tolerant, **non-gating**.

    Items 15/16 render ``n/a`` (non-gating) when their data is
    absent (a pilot-B run over a non-stratified file / without an
    import — the import re-derives from the state file on the
    continuation windows, so a multi-window run still gates item 16
    on the completing record); item 17 is always evaluable (zero
    ``eval_error`` lines pass vacuously). Item 12 gates **per
    window** when ``window_buckets`` is given (the quota window is
    the next UTC midnight — a multi-window run never fails the
    per-day cap by construction); a run that spans one window
    renders the verbatim WP6a text."""
    items: list = []

    def add(item: str, expected: str, observed: str,
            passed: "Optional[bool]") -> None:
        items.append({"item": item, "expected": expected,
                     "observed": observed, "pass": passed})

    if dry_run:
        for i in range(1, 15 + (3 if pilot_b else 0)):
            add(f"{i} (dry run)", "see PLAN-WP6A.md", "n/a (dry run)",
                None)
        return items

    perms_req = 0
    perms_all_answered = True
    perms_send_failures = 0
    vendor_seen = 0
    orphan_checked = True
    orphan_unchecked = False
    untrusted = False
    pruning_ok = True
    pruning_obs: list = []
    leak_ok = True
    leak_obs: list = []
    eval_legs: list = []
    # the round-17 Minor fix (issue #22 AC2): the item-4 / item-7
    # observations count THIS RUN'S sessions only — imported
    # (never-rerun) lines were never initialized / closed by this
    # run (the source run's sessions), so the gating record must
    # not claim "ok on 25 instance(s)" for 2 it never exercised
    n_own = sum(1 for r in records
                if not _meta_of(r).get("imported_from"))
    n_imp = len(records) - n_own
    # the session-level stats (item 8's token totals, item 9's wall,
    # the calibration) count **ATTEMPTS** — every line with an
    # instance id: a superseded ``eval_error``→rerun attempt's wall
    # and tokens must not silently drop from the record (the same
    # basis as item 12). The per-instance items below (7, 10, 14,
    # permissions) use the deduped last-line-per-instance view
    att = attempt_records if attempt_records is not None else records
    attempt_wall: list = []
    attempt_tokens = 0
    attempt_usage_seen = False
    for r in att:
        m = _meta_of(r)
        w = (m.get("wall") or {}).get("session_s")
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            attempt_wall.append((r.get("status"), w))
        u = m.get("usage")
        if isinstance(u, dict):
            attempt_usage_seen = True
            for k in ("totalTokens", "total_tokens"):
                v = u.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    attempt_tokens += int(v)
                    break
    for rec in records:
        m = _meta_of(rec)
        # the per-instance session-derived gating checks (items 5,
        # 6, 7, 10, 14) apply to THIS RUN'S sessions only (the
        # round-15 Major fix, issue #22 AC2 — extending the
        # round-13 item-10 exclusion): imported (never-rerun) lines
        # carry the source run's meta verbatim — a source whose
        # lines lack these keys (or carry a send failure / a
        # permissions mismatch / a non-clean leak verify / the
        # pre-harness missing pruning record) would fail the
        # gating items on evidence from the source run's sessions —
        # a permanent NO-GO on this run's record for sessions it
        # never ran. The source run's own record gates its own
        # sessions. Items 8/9 (the attempt stats) and 12/16 (the
        # budget / overlap arithmetic) exclude the imported lines by
        # their own filters; item 13 (the eval leg verdict) is
        # non-gating and renders every final line
        if m.get("imported_from"):
            pruning_obs.append(
                f"{rec.get('instance_id')}: imported (the source "
                f"run's record gates the pruning)")
            leak_obs.append(
                f"{rec.get('instance_id')}: imported (the source "
                f"run's record gates the leak guard)")
        else:
            p = m.get("permissions") or {}
            perms_req += int(p.get("requests", 0))
            if (int(p.get("requests", 0)) != int(p.get("allowed", 0))
                    + int(p.get("denied", 0))):
                perms_all_answered = False
            perms_send_failures += int(
                m.get("permission_send_failures", 0))
            vendor_seen += int(m.get("vendor_notifications", 0))
            # item 7: a check that did not run (a close() failure —
            # ``orphan_check`` stays None) is a FAIL, not a pass
            oc = m.get("orphan_check")
            if oc is None:
                orphan_unchecked = True
            elif oc is False:
                orphan_checked = False
            # item 10: the pruning record must be present in the
            # line and free of failures (written before the line
            # is persisted)
            pr = m.get("pruning")
            if not isinstance(pr, dict) or pr.get("failed"):
                pruning_ok = False
            pruning_obs.append(
                f"{rec.get('instance_id')}: "
                + (f"removed {pr.get('removed')}" if isinstance(pr, dict)
                   else "no record in the checkpoint line")
                + (f"; FAILED {pr.get('failed')}" if isinstance(pr, dict)
                   and pr.get("failed") else ""))
            # item 14: the recorded guard + post-session
            # verification
            lg = m.get("leak_guard") or {}
            lv = m.get("leak_verify") or {}
            if (lv.get("clean") is not True
                    or lg.get("remotes_after")):
                leak_ok = False
            leak_obs.append(
                f"{rec.get('instance_id')}: verify="
                + (f"clean={lv.get('clean')} remotes={lv.get('remotes')} "
                   f"tags={lv.get('tags')}" if lv else "no record")
                + (f"; guard remotes_after={lg.get('remotes_after')} "
                   f"warnings={lg.get('warnings')}" if lg else ""))
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
        "auth + runtime + disk legs pass before any session; the "
        "runner re-runs the legs before each batch and each instance",
        "; ".join(preflight_failures)
        or "all legs passed (re-run by the runner per batch + per "
           "instance)",
        bool(preflight_ok))
    add("4 NDJSON framing + vendor notifications (WP0 §1)",
        "the initialize result is received; vendor notifications are "
        "logged, never errors",
        f"initialize ok on {n_own} instance(s)"
        + (f" (+ {n_imp} imported — not initialized this run)"
           if n_imp else "")
        + f"; vendor notifications observed: {vendor_seen}",
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
        + (f"; {perms_send_failures} response(s) the client could not "
           "send (the server never heard them)"
           if perms_send_failures else "")
        + (" (zero observed — the WP0 trivial-task caveat)"
           if perms_req == 0 and records else ""),
        (perms_all_answered and perms_send_failures == 0)
        if records else devin_ok)
    add("7 session/close + group kill + no orphans (WP0 §1)",
        "close sent best-effort; the process group killed; no orphan "
        "devin survives",
        (f"orphan check: "
         + ("PASS on all " + str(n_own) + " instance(s)"
            if orphan_checked and not orphan_unchecked
            else ("FAILED (an orphan survived)" if not orphan_checked
                 else "NOT PERFORMED (a close() failure — recorded in "
                      "the checkpoint meta close_error)")))
        + (f" (+ {n_imp} imported — not closed this run)"
           if n_imp else "")
        if records else "no instances",
        (orphan_checked and not orphan_unchecked)
        if records else devin_ok)
    add("8 visible usage (WP0 §1) or the §2.6 proxy",
        "usage.{inputTokens, outputTokens, cachedReadTokens, "
        "totalTokens} captured verbatim, else the sessions + wall + "
        "byte-size proxy",
        f"usage seen: {attempt_usage_seen}; total visible tokens "
        f"(attempts, incl. reruns): {attempt_tokens}; proxy (wall + "
        "prompt/patch bytes) recorded per instance",
        attempt_usage_seen or bool(attempt_wall))
    # a ``timeout`` session's wall is measured AFTER the deadline +
    # the cancel/close grace + the group-kill wait — deterministically
    # cap + ~1s or more, so the comparison would fail by
    # construction. The cap contract for a timeout is the kill
    # itself (status == timeout is the evidence); the comparison
    # applies to the non-timeout sessions
    over_cap = [w for st, w in attempt_wall
                if st != "timeout" and w > config.max_wall_s]
    cap_ok = not over_cap
    timeouts = sum(1 for st, _ in attempt_wall if st == "timeout")
    add("9 per-instance wall ≤ cap",
        f"≤ {config.max_wall_s:.0f}s (the 45-min cap)",
        f"max session wall (attempts): "
        f"{max(w for _, w in attempt_wall) if attempt_wall else 0:.1f}s"
        + (f" — {timeouts} timeout session(s) excluded from the "
           f"comparison (killed at the cap by construction)"
           if timeouts else "")
        + (" — suspected_untrusted_cwd on at least one instance (WP0 "
           "§8: the sandbox must live under a trusted workspace "
           "root)" if untrusted else ""),
        cap_ok if attempt_wall else devin_ok)
    add("10 pruning (PLAN §7)",
        "the per-instance state removed after eval; the shared base "
        "retained",
        "; ".join(pruning_obs) or "no instances",
        (pruning_ok and bool(records)) if records else devin_ok)
    if (skipped_on_resume and config.resume and resume_after_interrupt):
        obs = "resumed after the intentional interrupt: " + ", ".join(
            f"{k} skipped ({v})" for k, v in sorted(
                skipped_on_resume.items()))
        p = True
    # an interrupted invocation IS the interrupt leg of the
    # pair — whether or not it is ALSO a resume with skipped
    # instances (the pilot-B window-1 flow: --resume + the
    # import + the interrupt): the pair's AC3 evidence
    # completes on the FOLLOWING resume invocation (the
    # first branch). Ordered BEFORE the plain
    # resume-with-skip branch (the round-9 fix): a
    # pilot-B window-1 interrupted record previously
    # rendered "the pair was not the intentional
    # interrupt (AC3 not exercised)" + FAIL on the very
    # invocation that just performed the interrupt (its
    # skipped_on_resume was non-empty — the seeded import /
    # prior lines — while resume_after_interrupt was still
    # False)
    elif interrupted_this_run:
        obs = (f"interrupted after "
               f"{interrupted_this_run.get('completed')} instance(s); "
               "the resume invocation must follow")
        p = None
    elif config.resume and skipped_on_resume:
        # a resume with skipped instances, but the pair was not the
        # intentional interrupt (a crash/abort resume — the AC3 path
        # is not exercised)
        obs = ("resumed (skipped: "
               + ", ".join(f"{k} ({v})"
                          for k, v in sorted(skipped_on_resume.items()))
               + ") — but the pair was not the intentional interrupt "
                 "(AC3 not exercised)")
        p = False
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
        f"({arithmetic_total} total across the run pair)",
        obs, p)
    # the AC4 per-window budget gate (issue #22): a multi-window run
    # (the WP3 runner sleeps to the next UTC-midnight window at the
    # local daily cap) is gated PER WINDOW — the total would exceed
    # the per-day cap by construction; a single-window run renders
    # the verbatim WP6a text (the WP6a contract holds)
    if window_buckets is not None and len(window_buckets) > 1:
        over_s = [d for d, w in window_buckets.items()
                 if w["sessions"] > config.max_sessions_per_day]
        over_w = [d for d, w in window_buckets.items()
                 if w["wall_s"] > wall_budget_s]
        obs12 = (f"windows: " + "; ".join(
            f"{_window_day_str(d)}: {w['sessions']} sessions / "
            f"{w['wall_s']:.1f}s" for d, w in
            sorted(window_buckets.items()))
            + f"; max per window: "
            f"{max(w['sessions'] for w in window_buckets.values())} "
            f"sessions, "
            f"{max(w['wall_s'] for w in window_buckets.values()):.1f}s"
            + (f" — the WP5 report's sessions-used also counts "
               f"the {len(imported or {})} imported line(s) (the "
               f"source run's sessions — the WP5 record-level "
               f"methodology); this run's per-window budget "
               f"arithmetic (item 12 / the calibrate basis) "
               f"excludes them (the round-14 Minor fix: the "
               f"annotation was missing on the multi-window "
               f"branch, so the record no longer stated why its "
               f"session count differs from the WP5 report's)"
               if imported else ""))
        p12 = not over_s and not over_w
    else:
        # an EMPTY bucket view (a host crash before any line — no
        # ``ts`` to bucket) falls back to the legacy totals (0 / 0)
        if window_buckets:
            only = next(iter(window_buckets.values()))
            n12 = only["sessions"]
            w12 = only["wall_s"]
        else:
            n12 = sessions_used
            w12 = wall_used_s
        obs12 = (f"sessions (attempts, incl. reruns): {n12}; "
                f"wall used: {w12:.1f}s"
                + (f" — the WP5 report's sessions-used also "
                   f"counts the {len(imported or {})} imported "
                   f"line(s) (the source run's sessions — the "
                   f"WP5 record-level methodology); this run's "
                   f"budget arithmetic (item 12 / the calibrate "
                   f"basis) excludes them"
                   if imported else ""))
        p12 = (n12 <= config.max_sessions_per_day
              and w12 <= wall_budget_s)
    add("12 within the calibrated budget",
        (f"every window: sessions ≤ {config.max_sessions_per_day}; "
         f"wall ≤ {wall_budget_s:.0f}s")
        if (window_buckets is not None
            and len(window_buckets) > 1)
        else (f"sessions ≤ {config.max_sessions_per_day}; wall ≤ "
              f"{wall_budget_s:.0f}s"),
        obs12,
        p12)
    add("13 eval leg verdict (deviation-tolerant, NON-gating)",
        "container: the official harness; sandbox: the host check or "
        "the documented no-runtime skip",
        "; ".join(eval_legs) or "no instances completed",
        None)
    add("14 leak guard (PLAN §3.6)",
        "no origin remote, no tags, reflogs expired (verified "
        "post-session)",
        "; ".join(leak_obs) or "no instances",
        (leak_ok and bool(records)) if records else devin_ok)

    # the WP6b items (issue #22 — appended for a ``--pilot b`` run
    # only; the WP6a 14-item contract + the
    # test_checklist_verdict_gating invariants hold). Each is ``n/a``
    # (non-gating — ``pass=None``) when its data is absent.
    if pilot_b:
        # 15 — the stratification (AC1)
        if stratified_repos:
            repo_by_iid = repo_by_iid or {}
            covered = {repo_by_iid.get(r.get("instance_id"))
                      for r in records}
            covered.discard(None)
            n_cov = sum(1 for repo in stratified_repos
                       if repo in covered)
            add("15 stratification (WP6b AC1)",
                f"all {len(stratified_repos)} Verified repos covered "
                "by a final line (the instance file's meta)",
                f"{n_cov}/{len(stratified_repos)} repos covered",
                n_cov == len(stratified_repos))
        else:
            add("15 stratification (WP6b AC1)",
                "the instance file's ``stratified_repos`` meta",
                "n/a (no stratification meta — non-gating)",
                None)
        # 16 — the 2-run overlap/variance (AC2)
        if imported:
            ok16 = True
            frag = []
            for iid in sorted(imported):
                src = imported[iid]
                fin = next((r for r in records
                           if r.get("instance_id") == iid), None)
                sm = _meta_of(src)
                fm = _meta_of(fin) if fin else {}
                s_wall = (sm.get("wall") or {}).get("session_s")
                f_wall = (fm.get("wall") or {}).get("session_s")
                s_tok = _usage_total(sm.get("usage"))
                f_tok = _usage_total(fm.get("usage"))
                s_pb = sm.get("patch_bytes")
                f_pb = fm.get("patch_bytes")
                m = (fin is not None
                    and src.get("status") == fin.get("status")
                    and s_wall == f_wall and s_tok == f_tok
                    and s_pb == f_pb)
                ok16 = ok16 and m
                frag.append(f"{iid}: "
                          + ("match" if m else "MISMATCH")
                          + f" ({src.get('status')}/"
                             f"{fin.get('status') if fin else '-'}, "
                             f"wall {s_wall}/{f_wall}, tokens "
                             f"{s_tok}/{f_tok}, patch {s_pb}/{f_pb})")
            # the session arithmetic (the unique-instance basis):
            # imported + the unique NEW instances == unique — a legal
            # pass@1 retry (several attempt lines for one instance)
            # is the item-12 budget basis, not an arithmetic break
            new_iids = {r.get("instance_id") for r in records
                       if r.get("instance_id")
                       and not _meta_of(r).get(
                           "imported_from")
                       and r.get("status") != "preflight_failed"}
            unique = len({r.get("instance_id") for r in records
                         if r.get("instance_id")})
            arith_holds = (len(imported) + len(new_iids) == unique)
            ok16 = ok16 and arith_holds
            frag.append(f"arithmetic {len(imported)} + "
                        f"{len(new_iids)} = {unique} "
                        + ("holds" if arith_holds else "BROKEN"))
            add("16 2-run overlap/variance (WP6b AC2)",
                "each imported (reused) instance: the source line == "
                "this run's final line (status / session wall / "
                "tokens / patch bytes) — never re-run, no extra "
                "session spent; the session arithmetic holds "
                "(imported + new == unique)",
                "; ".join(frag) or "no imported instances",
                ok16)
        else:
            if import_attempted:
                # the round-12 Medium fix (issue #22 AC2): an
                # --import-run (this or an earlier window — the
                # state re-derivation) was attempted but zero
                # never-rerun instances were imported (the shared
                # ids were all retry-class — the zero-id case is a
                # fail-closed config error in load_imported). Item
                # 16 renders the explicit vacuous state (the AC2
                # overlap check is vacuous for an empty imported
                # set; the retry-class re-runs are recorded
                # separately) instead of a bare "n/a (no import)"
                # indistinguishable from a legitimate non-import
                # run
                add("16 2-run overlap/variance (WP6b AC2)",
                    "an ``--import-run`` source (the 3 Pilot A "
                    "instances)",
                    "n/a (import attempted — zero imported "
                    "instances; the AC2 overlap check is vacuous; "
                    "the retry-class re-runs are recorded)",
                    None)
            else:
                add("16 2-run overlap/variance (WP6b AC2)",
                    "an ``--import-run`` source (the 3 Pilot A "
                    "instances)",
                    "n/a (no import — non-gating)",
                    None)
        # 17 — the root-caused eval_error (AC3)
        ee = [r for r in records if r.get("status") == "eval_error"]
        unclassed = [r.get("instance_id") for r in ee
                    if _meta_of(r).get(
                        "eval_error_root_cause")
                    not in ("env", "parser", "patch")]
        unclassified_obs = (
            f" — UNCLASSIFIED: "
            + ", ".join(sorted(str(u) for u in unclassed))
            if unclassed else "")
        obs17 = (f"{len(ee)} eval_error line(s), "
                f"{len(ee) - len(unclassed)} classified"
                + unclassified_obs)
        if ee:
            by_class: dict = {}
            for r in ee:
                c = _meta_of(r).get(
                    "eval_error_root_cause")
                if c in ("env", "parser", "patch"):
                    by_class[c] = by_class.get(c, 0) + 1
            obs17 += " (" + ", ".join(f"{c}={n}"
                                     for c, n in sorted(by_class.items())) + ")"
        add("17 eval_error root-caused (WP6b AC3)",
            "every eval_error line carries a root cause (env / "
            "parser / patch — the PLAN-WP6B.md mapping)",
            obs17,
            not unclassed)
    return items


def _verdict(items: list, interrupted: bool = False,
             gating: "Optional[frozenset]" = None) -> "tuple[str, list]":
    """GO ⟺ all gating items pass (the WP6a 14-item set by default —
    1–12 + 14, item 13 non-gating; the ``gating`` parameter extends
    it for a pilot-B run: + 15/16/17, 13 still non-gating). A
    rendering with no decided items at all (the dry-run skeleton —
    every item ``pass=None``) is ``n/a``, not NO-GO (a dry run
    decides nothing). An **interrupted** run decides nothing either:
    it is a PARTIAL record (e.g. 1/3 instances) — no gating item
    requires all pinned instances to have a completed line, so
    without this guard the partial record would render a GO the
    resume invocation would silently supersede. The pair's verdict
    is the resume invocation's."""
    if interrupted:
        return ("n/a (interrupted — resume to complete)", [])
    if gating is None:
        gating = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                 "11", "12", "14"}  # 13 excluded (non-gating)
    decided = [i for i in items
              if i["item"].split(" ", 1)[0] in gating
              and i["pass"] is not None]
    if not decided:
        return ("n/a", [])
    failed = [i["item"] for i in decided if i["pass"] is not True]
    return ("NO-GO" if failed else "GO", failed)


def _render_pilot_b_sections(wp6b: dict) -> list:
    """The pilot-B record sections (issue #22) — the per-repo
    stratification table, the 2-run overlap/variance table, the
    failure taxonomy + root-cause table, the per-window budget
    table."""
    lines: list = []
    pr = wp6b.get("per_repo") or {}
    expected = wp6b.get("expected_repos") or sorted(pr)
    if pr:
        lines += ["## Stratification (issue #22 AC1)", "",
                  "| repo | observed | status |",
                  "|---|---|---|"]
        for repo in expected:
            d = pr.get(repo, {"count": 0, "statuses": {}})
            st = ", ".join(f"{k} {v}" for k, v in
                          sorted(d["statuses"].items())) or "-"
            lines.append(f"| {repo} | {d['count']} | {st} |")
        lines += [f"\n**{sum(1 for r in pr.values() if r['count'] >= 1)}/"
                  f"{len(expected)} repos covered.**", ""]
    imp = wp6b.get("import") or {}
    rows = imp.get("rows")
    if rows is not None:
        lines += ["## 2-run overlap/variance (issue #22 AC2)", "",
                  "| instance | source | final | session wall (s) "
                  "| tokens | patch bytes | match |",
                  "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['instance_id']} | {r['src_status']} | "
                f"{r['final_status']} | {r['src_wall']} → "
                f"{r['final_wall']} | {r['src_tokens']} → "
                f"{r['final_tokens']} | {r['src_patch']} → "
                f"{r['final_patch']} | "
                f"{'PASS' if r['match'] else 'FAIL'} |")
        match_label = ("PASS" if imp.get("all_match")
                      else ("FAIL" if imp.get("rows")
                           else "n/a (no import)"))
        lines += ["",
                  f"Session arithmetic: {imp.get('imported')} imported "
                  f"+ {imp.get('new_instances')} new = "
                  f"{imp.get('unique')} unique — "
                  f"{'holds' if imp.get('arithmetic_holds') else 'BROKEN'} "
                  f"({imp.get('new_sessions')} attempt lines — the "
                  f"budget basis, item 12). Overlap match: "
                  f"{match_label}.", ""]
    tax = wp6b.get("taxonomy") or {}
    rc = wp6b.get("root_causes") or []
    if tax or rc:
        lines += ["## Failure taxonomy (issue #22 AC3)", "",
                  "| status | count | instances |",
                  "|---|---|---|"]
        for s in sorted(tax):
            t = tax[s]
            lines.append(f"| {s} | {t['count']} | "
                        f"{', '.join(t['instances']) or '-'} |")
        if rc:
            lines += ["",
                     "**eval_error root causes (env / parser / patch):**",
                     "",
                     "| instance | class | detail |",
                     "|---|---|---|"]
            for r in rc:
                # the detail is operator text (a root-cause detail
                # — e.g. a git/pytest stderr line): a ``|`` or a
                # newline in it would corrupt the markdown table
                # (the checklist rows escape the same way)
                detail = str(r["detail"] or "-")
                detail = (detail.replace("|", "\\|")
                         .replace("\n", " ")[:160])
                lines.append(f"| {r['instance_id']} | "
                            f"{r['root_cause'] or 'UNCLASSIFIED'} | "
                            f"{detail} |")
        lines.append("")
    win = wp6b.get("windows") or []
    if win:
        lines += ["## Budget windows (issue #22 AC4)", "",
                  "| window (UTC day) | sessions | wall (s) |",
                  "|---|---|---|"]
        for w in win:
            lines.append(f"| {w['day']} | {w['sessions']} | "
                        f"{w['wall_s']} |")
        lines.append("")
    return lines


def render_checklist_md(items: list, config: PilotConfig,
                       calibration: "Optional[dict]",
                       meta: dict,
                       interrupted: bool = False,
                       title: str = "WP6a Pilot A — go/no-go record "
                                   "(auto-generated)",
                       gating: "Optional[frozenset]" = None) -> str:
    verdict, failed = _verdict(items, interrupted=interrupted,
                              gating=gating)
    lines = [f"# {title}", "",
             f"**Verdict: {verdict}**"
             + (f" — failed items: {', '.join(failed)}" if failed else ""),
             "",
             f"- run: `{meta.get('run_id')}` · mode: {config.mode} · "
             f"model: {config.model} · devin: "
             f"{meta.get('devin_cli_version', 'unknown')}",
             f"- budget: max_sessions_per_day="
             f"{config.max_sessions_per_day} · max_wall_per_session="
             f"{config.max_wall_s:.0f}s", ""]
    ac_label = "issue #22" if config.pilot == "b" else "issue #21"
    # the Calibration and Checklist sections are the INHERITED
    # WP6a sections — #21's AC2 / AC4 (the round-19 Minor fix,
    # issue #22): pilot-B's AC numbers (PLAN-WP6B) map AC2 to the
    # 2-run overlap/variance and AC4 to the mid-batch interrupt,
    # so labelling these sections with #22's numbers misattributes
    # the record's own sections (the pilot-B-specific sections —
    # 15/16/17 — carry the #22 labels)
    if calibration:
        lines += [
            f"## Calibration (issue #21 AC2 — the inherited WP6a "
            f"section)", "",
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
    lines += [f"## Checklist (issue #21 AC4 — the inherited WP6a "
              f"section)", "",
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
    if meta.get("wp6b"):
        lines += _render_pilot_b_sections(meta["wp6b"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the live run
# ---------------------------------------------------------------------------

def devin_version(devin_bin: str) -> str:
    """The version line (``devin version``; the WP2 double speaks
    ``--version`` — the fallback keeps the mock-driven CI runs
    informative). A missing binary / a hang is NOT a hard failure here
    (the pre-flight's ``which devin`` check is the loud exit-4 gate
    that follows — this probe only feeds the record)."""
    for args in (("version",), ("--version",)):
        try:
            p = subprocess.run([devin_bin, *args], capture_output=True,
                              timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "unknown (devin binary not found / probe failed)"
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


def _meta_of(rec: dict) -> dict:
    """The record's meta as a dict (the round-10 fix, issue #22): a
    corrupt (non-dict) meta is normalized to ``{}`` — a truthy
    non-dict meta (the same corrupt-line class :func:`load_imported`
    validates on the source side) must not crash the read-side
    accounting (the checkpoint re-derivation, the window buckets,
    the calibrate exclusion, the checklist math) with an uncaught
    ``AttributeError`` (exit 4) — the dict guard keeps the read
    side robust (the line simply lacks a meta for the accounting).
    """
    m = rec.get("meta")
    return m if isinstance(m, dict) else {}


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
    """Crash-atomic (the round-7 Major fix, issue #22): the state
    write is tmp + fsync + ``os.replace`` — a mid-write crash (a
    process kill / power loss during the write) leaves the
    previous state file intact (or NO file on a fresh workdir — a
    visible anomaly the operator resolves by re-invoking the import
    window; the seeding is idempotent), so the resume's import
    re-derivation (the state's full source set — the round-3 early
    record — + the checkpoint fallback, round 2) is never clobbered
    by a torn write. The old ``open("w")`` truncated in place: a
    mid-write crash left a torn JSON that :func:`_load_state`
    swallowed to ``{}`` — the early import record (the round-3
    recovery) could be destroyed by its own write path (zero seeds
    + a torn state → the resume derives nothing, the never-rerun
    instances run fresh, item 16 renders n/a — the same silent
    under-derivation the round-3 early record was added to close).
    """
    os.makedirs(workdir, exist_ok=True)
    p = _state_path(workdir, run_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)  # atomic on POSIX (the same filesystem)
    # best-effort directory fsync (the rename's durability — a
    # power loss immediately after the rename can drop it without
    # this; best-effort: not every platform exposes a directory fd)
    try:
        dfd = os.open(os.path.dirname(p) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _remove_stale_record(workdir: str) -> None:
    """The record file is the last *completed* run's verdict — an early
    exit (dry-run skeleton, pre-flight / model-gate refusal, an abort)
    removes it so a stale verdict from a previous invocation is never
    mistaken for this run's (consistent record handling across exits).
    Both pilot kinds' names are removed (a stale verdict from the
    other kind in the same workdir is equally stale)."""
    for name in RECORD_NAMES.values():
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass


def _pilot_title(config: PilotConfig) -> str:
    return ("WP6b Pilot B (issue #22)" if config.pilot == "b"
            else "WP6a Pilot A (issue #21)")


def _stratified_repos(config: PilotConfig) -> "Optional[list]":
    """The AC1 expected repo set: the instance file's
    ``_meta.stratified_repos`` (None for a meta-less / bare-list file
    — the item renders ``n/a`` and is non-gating)."""
    try:
        meta = load_instances_meta(config.instances_file)
    except PilotConfigError:
        return None
    repos = meta.get("stratified_repos")
    if isinstance(repos, list) and repos and all(
            isinstance(r, str) for r in repos):
        return list(repos)
    return None


def _repo_by_iid(insts: list) -> dict:
    return {i["instance_id"]: i.get("repo", "?") for i in insts}


def _window_buckets(records: list) -> dict:
    """The AC4 per-window budget view (issue #22): the checkpoint
    lines (instance id, non-``preflight_failed`` — the WP3 contract:
    it consumed no session) bucketed by their ``ts``'s **UTC day**
    (the quota window boundary, ``next_quota_window`` — the next UTC
    midnight; the WP3 ``Runner`` — the pilot harness' queue engine,
    default sleeper — sleeps there at the per-process daily cap,
    resets the budget counters (a new window is a fresh budget), and
    continues the remaining queue in the same process, the 500-run
    model: one process spans days across windows). Imported lines
    are EXCLUDED from this run's accounting — a never-rerun reuse
    burned no session of this run's budget; the source run
    accounted for it (bucketing them by their source ``ts`` — e.g.
    a same-UTC-day source — would fold the source run's sessions
    into this run's window and falsely fail the gate). A run that
    spans one window renders the verbatim WP6a item-12 text."""
    out: dict = {}
    for r in records:
        if not r.get("instance_id"):
            continue
        if _meta_of(r).get("imported_from"):
            continue  # the import — the source run's accounting
        ts = r.get("ts") or 0.0
        day = int(ts // 86400)
        w = out.setdefault(day, {"sessions": 0, "wall_s": 0.0})
        # a preflight_failed line is not a SESSION (no session ran —
        # the pre-flight leg failed), so it does not count toward
        # the session count — but its REAL wall (a failed pre-flight
        # leg — the image-pull class — can be minutes) counts
        # toward the wall gate (the WP6a wall_used_s basis counted
        # it; dropping the line dropped the wall — the round-8
        # fix)
        if r.get("status") != "preflight_failed":
            w["sessions"] += 1
        wall = _meta_of(r).get("wall_s", 0.0)
        if isinstance(wall, (int, float)) and not isinstance(wall, bool):
            w["wall_s"] += float(wall)
    return out


def _window_day_str(day: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day * 86400))


def _pilot_gating(config: PilotConfig) -> frozenset:
    """The gating item set (the WP6a 14-item set; pilot B adds the
    AC1–AC3 items 15/16/17 — item 13 stays non-gating in both)."""
    base = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
            "11", "12", "14"}
    if config.pilot == "b":
        base |= {"15", "16", "17"}
    return frozenset(base)


def _pilot_b_sections(config: PilotConfig, insts: list, records: list,
                     inst_records: list, imported: dict,
                     buckets: dict) -> dict:
    """The pilot-B record-section data (issue #22) — the
    ``meta['wp6b']`` the renderer turns into markdown tables.

    * ``per_repo`` — the AC1 stratification table (the instance
      file's ``stratified_repos`` meta vs the finals' repo counts);
    * ``import`` — the AC2 2-run overlap/variance table (per shared
      instance: the source line vs this run's final line) + the
      session arithmetic;
    * ``taxonomy`` — the AC3 full failure taxonomy (every terminal
      status: count + instance ids) + the root-cause table (every
      ``eval_error`` line: class + detail);
    * ``windows`` — the per-window budget table (the item-12 view).
    """
    repo_by_iid = _repo_by_iid(insts)
    final_by_iid = {r.get("instance_id"): r for r in inst_records}
    strat = _stratified_repos(config) or sorted(
        {i.get("repo", "?") for i in insts})
    per_repo: dict = {}
    for repo in strat:
        ids = [iid for iid, r in final_by_iid.items()
              if repo_by_iid.get(iid) == repo]
        statuses: dict = {}
        for iid in ids:
            s = final_by_iid[iid].get("status")
            statuses[s] = statuses.get(s, 0) + 1
        per_repo[repo] = {"count": len(ids), "statuses": statuses,
                         "instances": sorted(ids)}
    # the AC2 overlap/variance: per imported instance, the source line
    # vs the final line (status / session wall / total tokens / patch
    # bytes). A re-run of an imported instance supersedes the seeded
    # line with a divergent final → a mismatch → the item fails
    import_rows: list = []
    for iid in sorted(imported):
        src = imported[iid]
        fin = final_by_iid.get(iid)
        sm = _meta_of(src)
        fm = _meta_of(fin) if fin else {}
        wall_s = (lambda m: (m.get("wall") or {}).get("session_s"))
        toks = (lambda m: _usage_total(m.get("usage")))
        s_wall, f_wall = wall_s(sm), wall_s(fm)
        s_tok, f_tok = toks(sm), toks(fm)
        s_pb, f_pb = sm.get("patch_bytes"), fm.get("patch_bytes")
        match = (fin is not None
                and src.get("status") == fin.get("status")
                and s_wall == f_wall and s_tok == f_tok
                and s_pb == f_pb)
        import_rows.append({
            "instance_id": iid, "repo": repo_by_iid.get(iid),
            "src_status": src.get("status"), "src_wall": s_wall,
            "src_tokens": s_tok, "src_patch": s_pb,
            "final_status": fin.get("status") if fin else None,
            "final_wall": f_wall, "final_tokens": f_tok,
            "final_patch": f_pb, "match": match})
    new_lines = [r for r in records if r.get("instance_id")
                and not _meta_of(r).get("imported_from")
                and r.get("status") != "preflight_failed"]
    # the session arithmetic is on the DEDUPED-FINALS basis
    # (the round-16 Medium fix, issue #22 AC2 — it must match
    # item 16, which gates on the same equation over the deduped
    # finals): an instance whose final line is preflight_failed
    # (an eval_error attempt superseded by it) must not count as
    # "new" here yet be excluded in item 16 — the record would
    # render "holds" and "BROKEN" for the same run. new_sessions
    # stays on the raw (attempt) basis — it is a session count
    fin_new = [r for r in inst_records if r.get("instance_id")
               and not _meta_of(r).get("imported_from")
               and r.get("status") != "preflight_failed"]
    # the session arithmetic counts UNIQUE new instances (a legal
    # pass@1 retry of one instance is several attempt lines — the
    # attempts are the item-12 budget basis, not the arithmetic
    # basis)
    new_iids = {r.get("instance_id") for r in fin_new}
    unique = len({r.get("instance_id") for r in inst_records
                 if r.get("instance_id")})
    import_section = {
        "rows": import_rows,
        "imported": len(imported),
        "new_instances": len(new_iids),
        "new_sessions": len(new_lines),
        "unique": unique,
        "arithmetic_holds": (len(imported) + len(new_iids) == unique),
        # the overlap match is only defined for a run that has an
        # import (zero rows is not a FAIL — item 16 renders n/a on
        # a non-import run, and the record must not contradict
        # itself)
        "all_match": all(r["match"] for r in import_rows)
        if import_rows else None,
    }
    # the AC3 taxonomy + the root-cause table (over the DEDUPED view
    # — a superseded attempt's eval_error is not the instance's
    # verdict; the attempts basis lives in the calibration + item 12)
    taxonomy: dict = {}
    for r in inst_records:
        s = r.get("status")
        t = taxonomy.setdefault(s, {"count": 0, "instances": []})
        t["count"] += 1
        t["instances"].append(r.get("instance_id"))
    for t in taxonomy.values():
        t["instances"].sort()
    root_rows: list = []
    for r in inst_records:
        if r.get("status") != "eval_error":
            continue
        m = _meta_of(r)
        rc = m.get("eval_error_root_cause")
        root_rows.append({
            "instance_id": r.get("instance_id"),
            "root_cause": rc if rc in ("env", "parser", "patch")
            else None,
            "detail": (m.get("eval_error_root_cause_detail")
                      or m.get("eval_skip_reason") or "")[:200]})
    return {
        # the round-16 Minor fix (issue #22): all_repos_covered /
        # all_root_caused were computed here but never consumed by
        # the renderer or the checklist (the gating items 15 / 17
        # compute their own coverage / classification over the same
        # data) — dead record keys, removed
        "per_repo": per_repo,
        "expected_repos": strat,
        "import": import_section,
        "taxonomy": taxonomy,
        "root_causes": root_rows,
        "windows": [{"day": _window_day_str(d), "sessions": w["sessions"],
                    "wall_s": round(w["wall_s"], 1)}
                   for d, w in sorted(buckets.items())],
    }


def _usage_total(usage: object) -> "Optional[int]":
    """The verbatim visible-usage total (the WP0 §1
    ``usage.totalTokens``; the ``total_tokens`` alias) — None when
    the leg did not expose it (the comparison is still well-defined:
    the import and the final line must agree, including on absence)."""
    if not isinstance(usage, dict):
        return None
    for k in ("totalTokens", "total_tokens"):
        v = usage.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def _check_pilot_b_requirements(config: PilotConfig, state: dict,
                               imported: "Optional[dict]" = None) -> None:
    """The round-13 Medium fix (issue #22 AC2): a pilot-B run
    requires the 2-run overlap evidence (AC2) — the ``--import-run``
    source (this or an earlier window — the state or checkpoint
    re-derivation). A ``--pilot b`` invocation with no import at
    all rendered item 16 ``n/a (no import — non-gating)`` and could
    GO with zero AC2 overlap evidence — the same under-evidenced-
    verdict hole the zero-overlap guard closed for a mismatched
    source. Fail-closed: a config error before any session (pilot B
    is defined as reusing the Pilot A checkpoint — a no-import
    pilot-B run is not the documented pilot B; run it as a pilot
    A). The round-14 Medium fix (issue #22 AC1): a pilot-B run
    also requires the AC1 stratification evidence — the instance
    file's ``_meta.stratified_repos``. Without it, item 15 renders
    n/a (non-gating) and the record could GO with zero AC1
    stratification evidence — the same under-evidenced-verdict
    hole left open for item 15 (e.g. ``--instances
    pilot_instances.json --import-run pilotA`` — a valid source
    with a meta-less instance file). Fail-closed: a config error
    before any session (pilot B is defined as the stratified
    12-repo set — a non-stratified instance file is not the
    documented pilot B; run it as a pilot A)."""
    if (config.pilot == "b"
            and not config.import_run
            and not imported
            and not state.get("imported_run")
            and not state.get("import_source")):
        raise PilotConfigError(
            "--pilot b requires --import-run (AC2 — the 2-run "
            "overlap/variance evidence is pilot B's definition; a "
            "no-import pilot-B run would GO with item 16 n/a and "
            "zero AC2 evidence) — or a --resume on a workdir whose "
            "state carries the import bookkeeping")
    if config.pilot == "b" and _stratified_repos(config) is None:
        raise PilotConfigError(
            "--pilot b requires the instance file's "
            "_meta.stratified_repos (AC1 — the 12-repo "
            "stratification is pilot B's definition; a "
            "non-stratified instance file would GO with item 15 "
            "n/a and zero AC1 evidence) — use --pilot a for a "
            "non-stratified run")


def run_pilot(config: PilotConfig,
              make_session: "Optional[Callable]" = None) -> int:
    """The pilot driver (the tests call this with the injected
    ``make_session`` seam — e.g. the WP2 ``mock_devin``; the CLI calls
    it with the real one). Returns the exit code."""
    require_live(config.dry_run)
    insts = load_instances(config.instances_file)
    run_id = config.run_id or time.strftime("pilot-%Y%m%d-%H%M%S")
    config.run_id = run_id
    workdir = config.workdir or os.path.join(
        os.getcwd(), "pilot-b" if config.pilot == "b" else "pilot-a")
    config.workdir = workdir
    os.makedirs(workdir, exist_ok=True)
    jsonl = os.path.join(workdir, f"run-{run_id}.jsonl")
    record_name = RECORD_NAMES[config.pilot]

    # the WP6b import (issue #22 AC2) — fail-closed validation for
    # BOTH the dry run and the live run: a missing / corrupt source
    # is a config error before anything else (no session, no report)
    imported: dict = {}
    import_retry: dict = {}
    if config.import_run:
        imported, import_retry = load_imported(config, workdir, insts)
        print(f"[pilot] import from {config.import_run}: reused "
              f"{len(imported)} (never-rerun — skipped, no session "
              "spent); retry-class (the pass@1 re-run): "
              f"{sorted(import_retry)}")

    # -- dry run: the zero-quota validation ------------------------------
    if config.dry_run:
        # the round-13 Medium fix (issue #22 AC2): the pilot-B
        # import requirement in the dry run too — the dry run
        # renders no verdict (all n/a), but fail-closed
        # consistency: a dry run that passes must be an invocation
        # that would actually work (the round-14 AC1 stratification
        # requirement too — the same check). The round-16 Minor
        # fix (issue #22 AC2): the check applies the checkpoint
        # re-derivation the live path performs — a --pilot b
        # --dry-run --resume on a workdir whose checkpoint carries
        # seeded import lines but whose state file is absent must
        # be treated like the equivalent live --resume (it used to
        # be rejected while the live path proceeded)
        dry_state = _load_state(workdir, run_id)
        dry_imported = None
        if (config.resume and not config.import_run
                and not dry_state.get("imported_run")
                and not dry_state.get("import_source")
                and os.path.isfile(
                    os.path.join(workdir,
                                f"run-{run_id}.jsonl"))):
            try:
                pre_recs, _ = rr.load_checkpoint(
                    os.path.join(workdir, f"run-{run_id}.jsonl"))
            except rr.RunnerProtocolError:
                pre_recs = []
            known = {i["instance_id"] for i in insts}
            der: dict = {}
            for r in pre_recs:
                m = _meta_of(r)
                if m.get("imported_from"):
                    line = dict(r)
                    line["meta"] = m
                    m.pop("imported_from", None)
                    der[r.get("instance_id")] = line
            dry_imported = {iid: l for iid, l in der.items()
                           if iid in known}
        # the round-19 Minor fix (issue #22, 4450720403): the dry
        # run reproduces the LIVE guard's exact predicate — the
        # live path rejects a non-resume --import-run only when a
        # checkpoint EXISTS after the import seeding (the
        # existing-checkpoint guard), which never fires when the
        # import seeds nothing (an all-retry-class source) and
        # the workdir has no checkpoint yet (the repair
        # early-returns on a missing file; an empty seeding
        # creates no file) — the identical live invocation
        # proceeds. Rejecting unconditionally let the dry run
        # refuse an invocation the live path accepts (the 'a dry
        # run that passes is an invocation that would actually
        # work' contract, both directions)
        if (config.import_run is not None
                and not config.resume):
            checkpoint_exists = os.path.isfile(
                os.path.join(config.workdir,
                             "run-" + config.run_id + ".jsonl"))
            # a non-empty validated import would seed (and
            # thereby create) the checkpoint — the live guard
            # fires then
            import_would_seed = bool(imported)
            if checkpoint_exists or import_would_seed:
                raise PilotConfigError(
                    "--import-run seeds this run's checkpoint "
                    "(existing checkpoint / a non-empty import "
                    "would create one); pass --resume so the "
                    "import is the first window of a resumable "
                    "pilot (the live path's existing-checkpoint "
                    "guard)")
        _check_pilot_b_requirements(config, dry_state,
                                   dry_imported)
        cal = calibrate([], config)
        items = build_checklist(
            config=config, dry_run=True, gate=None, preflight_ok=None,
            preflight_failures=[], records=[], skipped_on_resume={},
            interrupted_this_run=None, resume_after_interrupt=False,
            devin_ok=None, sessions_used=0,
            wall_budget_s=config.max_wall_s
            * config.max_sessions_per_day, wall_used_s=0.0,
            pilot_b=config.pilot == "b",
            stratified_repos=_stratified_repos(config),
            imported=imported, repo_by_iid=_repo_by_iid(insts))
        _remove_stale_record(workdir)  # the skeleton replaces the last
        # completed run's verdict — a stale file is never kept
        with open(os.path.join(workdir, record_name), "w",
                  encoding="utf-8", newline="") as f:
            f.write(render_checklist_md(items, config, cal, {
                "run_id": run_id, "devin_cli_version": "dry-run"},
                title=_pilot_title(config)))
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

    # the resume (WP3b) — the interrupted / partial state (the state
    # file is the previous invocation's — it is overwritten at the end
    # of this run). Read **before** the pre-flight: the per-instance
    # pre-flight legs must only inspect the instances this invocation
    # will RUN — a --resume must not pre-flight finished instances'
    # container images (the prune docker rmi'd them) or re-pull images
    # a finished run already consumed
    state = _load_state(workdir, run_id)
    interrupted_before = bool(state.get("interrupted"))
    aborted_before = bool(state.get("aborted"))
    resume_exercised_before = bool(state.get("resume_exercised"))
    # the early import record (issue #22 AC2 — the mid-seed crash
    # class): a host crash mid-seeding leaves only PART of the
    # import in the checkpoint — a checkpoint re-derivation on the
    # follow-up --resume would treat that subset as the complete
    # import, and the unseeded never-rerun instances would re-run
    # (a silent AC2 violation item 16 cannot see, its comparison
    # set being the same truncated subset). Persisting the FULL
    # import bookkeeping BEFORE the seeding makes the state source
    # (the full set) take precedence on that resume — the missing
    # lines re-seed (the idempotent seeding skips the present
    # ones), and item 16's comparison set is the true source set
    if imported and config.import_run:
        state["imported"] = sorted(imported)
        state["import_source"] = imported
        state["imported_run"] = config.import_run
        # the round-17 Medium fix (issue #22 AC2): persist the
        # import workdir ABSOLUTE — a relative --import-workdir
        # resolved against this invocation's cwd at import time;
        # a continuation window (a different cwd — the operator
        # re-invokes from wherever) must re-resolve it to the
        # SAME source workdir, or the artifact self-heal copy
        # finds missing sources and the WP5 cross-check fails —
        # a report_failed wedge every subsequent resume re-hits
        # (when the flag's workdir is absent the source is this
        # run's workdir — persist that, absolute)
        state["import_workdir"] = os.path.abspath(
            config.import_workdir or workdir)
        state["import_retry_class"] = {
            k: v for k, v in sorted(import_retry.items())}
        _write_state(workdir, run_id, state)
    # the WP6b import re-derivation (issue #22 AC2): the documented
    # multi-window flow drops --import-run on the continuation
    # windows — but item 16 must still gate on the completing
    # (verdict-producing) record. Two sources: the state file's
    # ``import_source`` (persisted by the early import record —
    # BEFORE the seeding — so a mid-seed crash resume sees the FULL
    # set; the end-of-run write repeats it), or — when a graceful
    # early exit (the pre-flight failure, the model-gate refusal, a
    # load_checkpoint protocol error) left no state — the
    # checkpoint's own seeded lines (they carry
    # ``meta.imported_from`` + the original ``ts``; the source line
    # is the line minus the marker). Both filtered to the current
    # instance file (a stale id is ignored — the load_imported
    # semantics). Crash-atomic contract (the round-7 Major fix,
    # issue #22): the state write is tmp + fsync + os.replace
    # (:func:`_write_state`) — a mid-write crash leaves the
    # previous state intact (or NO file on a fresh workdir — a
    # VISIBLE anomaly, never a torn JSON swallowed to {} by
    # :func:`_load_state`): the fallback trusts the seeded lines
    # as the complete import BECAUSE a torn state can no longer
    # destroy the recovery info — and a completely lost state (a
    # fresh-workdir crash before the first state write lands) is
    # the visible anomaly the operator resolves by re-invoking the
    # import window (the seeding is idempotent; the state is
    # rewritten).
    if (not imported and config.resume):
        known = {i["instance_id"] for i in insts}
        if (state.get("imported")
                and isinstance(state.get("import_source"), dict)
                and state["import_source"]):
            imported = {iid: line for iid, line in
                        state["import_source"].items()
                        if iid in known}
            if imported:
                label = state.get("imported_run") or "?"
                print(f"[pilot] import re-derived from the state "
                      f"file ({len(imported)} line(s) from "
                      f"{label})")
        elif os.path.isfile(jsonl):
            try:
                pre_recs, _ = rr.load_checkpoint(jsonl)
            except rr.RunnerProtocolError:
                pre_recs = []  # a torn tail (the host-crash class)
                # makes the read fail — the state source is the only
                # re-derivation available on that path
            for r in pre_recs:
                m = _meta_of(r)
                if m.get("imported_from"):
                    line = dict(r)
                    line["meta"] = m
                    m.pop("imported_from", None)
                    imported[r.get("instance_id")] = line
            imported = {iid: line for iid, line in imported.items()
                       if iid in known}
            if imported:
                label = state.get("imported_run") or "?"
                print(f"[pilot] import re-derived from the "
                      f"checkpoint's seeded lines ({len(imported)} "
                      f"line(s) from {label})")
        # the round-18 Medium fix (issue #22 AC2): the
        # re-derivation must fail-closed on ZERO overlap with the
        # current instance file — the state's import bookkeeping is
        # truthy but the re-derived set (imported + the state's
        # retry class, filtered to the current file) is empty: a
        # --resume pointed at a different (still stratified)
        # --instances file would otherwise proceed with zero
        # overlap and item 16 renders the vacuous non-gating n/a
        # (the completing record can GO with no AC2 evidence — the
        # same under-evidenced-verdict class the flag-path
        # zero-overlap guard made a config error)
        # the round-20 Medium fix (issue #22, 4450828209): the
        # re-derivation rebuilds ``imported`` from our own state
        # / checkpoint lines and bypasses load_imported — apply
        # the SHARED fail-closed validation (the ts contract +
        # the reference schema + the round-15 ref traversal
        # guard) so a checkpoint line carrying meta.imported_from
        # plus a .. / absolute reference is a config error here
        # too (instead of reaching _copy_imported_artifacts and
        # writing outside the workdir — the identical value is
        # fail-closed on the --import-run path)
        for _iid, _rec in imported.items():
            _validate_imported_line(
                _rec, _iid,
                "the import re-derivation (state / checkpoint "
                "line)")
        state_retry = {k for k in
                      (state.get("import_retry_class") or {})
                      if k in known}
        if (not imported and not state_retry
                and (state.get("imported_run")
                     or state.get("import_source"))
                and (state.get("imported")
                     or state.get("import_retry_class"))):
            raise PilotConfigError(
                f"the --resume import re-derivation "
                f"({state.get('imported_run')!r}) shares no "
                "instance ids with this invocation's instance "
                "file — a source / instance mismatch "
                "(fail-closed: a zero-overlap re-derivation "
                "would let the record GO with item 16 n/a and "
                "zero AC2 evidence)")
    # the round-13 Medium fix (issue #22 AC2): the pilot-B import
    # requirement (this or an earlier window — the re-derivation
    # above satisfies it; a no-import pilot-B run is a config
    # error before any session) + the round-14 Medium fix
    # (issue #22 AC1): the stratification requirement
    _check_pilot_b_requirements(config, state, imported)
    # the WP6b import seeding (issue #22 AC2) — BEFORE the resume
    # read: the imported (never-rerun) lines must be in the
    # checkpoint when ``resume_queue`` decides what to skip. The
    # referenced-artifact copy runs for EVERY imported id (not just
    # the newly seeded ones — a crash between a line's fsync and
    # its artifact copy leaves a durable line with missing
    # artifacts; the idempotent per-file copy self-heals that on
    # the next invocation). The source workdir is the flag's, or
    # the state file's on the continuation windows (the flag is
    # dropped there; the state record persists it)
    if imported:
        # the import marker: the flag's source run id, or the state
        # record's on the continuation windows (the flag is dropped
        # there) — a None marker would break the re-derivation +
        # the window/calibration exclusion (they key on it)
        marker = (config.import_run
                 or state.get("imported_run")
                 or "imported")
        seeded = seed_import_lines(
            jsonl, imported, marker,
            src_workdir=(config.import_workdir
                        or state.get("import_workdir")
                        or workdir))
        if seeded:
            label = (config.import_run
                    or state.get("imported_run")
                    or "state/checkpoint")
            print(f"[pilot] imported {len(seeded)} instance "
                  f"line(s) from {label} (skipped — never re-run, "
                  "no session spent)")
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
            _remove_stale_record(workdir)
            return EXIT_CONFIG
        print(f"[pilot] resuming: skipped {skipped_on_resume}; "
              f"to run: {to_run}")
    if aborted_before:
        print("[pilot] the previous run ended ABORTED (the state "
              "file) — the resume continues the retry-eligible "
              "instances")
    if not to_run:
        print("[pilot] nothing to run (all instances finished in a "
              "previous invocation)")
    # the cumulative AC3 exercise (the state is overwritten every run —
    # a repeat --resume on a finished pair must not lose the record
    # that the pair exercised the intentional-interrupt resume)
    resume_exercised = resume_exercised_before or (
        bool(config.resume) and bool(skipped_on_resume)
        and interrupted_before)

    # the documented gate order (PLAN-WP6A.md): opt-in (above) →
    # pre-flight (zero quota, before any session; the runner also
    # re-runs it per batch + per instance) → model gate (fail-closed)
    pf = make_preflight(config, insts)
    preflight_ok = True
    preflight_failures: list = []
    try:
        pf(None)
        for iid in to_run:
            pf(iid)
    except rr.PreflightFailed as exc:
        preflight_ok = False
        preflight_failures.append(str(exc))
        print(f"[pilot] pre-flight FAILED: {exc}")
        # the batch-level marker (the WP3b contract: instance_id=None —
        # it consumes no queue slot on resume)
        with rr.JsonlCheckpoint(jsonl) as ck:
            ck.record_instance(None, "preflight_failed",
                              {"reason": str(exc), "phase": "batch"})
        _remove_stale_record(workdir)
        return EXIT_RUN_FAILED

    # the fail-closed model gate (WP3b)
    try:
        models = rr.fetch_models_list(config.devin_bin)
    except rr.ModelGateError as exc:
        print(f"[pilot] model gate REFUSED (fail-closed): {exc}")
        _remove_stale_record(workdir)
        return EXIT_RUN_FAILED
    gate_res = rr.gate_cost_tier(models, config.model)
    gate = {"allowed": gate_res.allowed,
           "cost_tier": gate_res.cost_tier,
           "reason": gate_res.reason,
           "free_models": list(gate_res.free_models)}
    if not gate_res.allowed:
        print(f"[pilot] model gate REFUSED: {gate_res.reason}")
        _remove_stale_record(workdir)
        return EXIT_RUN_FAILED

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
            # a clean attempt, every time: on a resume after a
            # mid-session crash the previous attempt's
            # ``runs/<iid>/testbed`` survives (the prune runs only for
            # a completed record) — reusing it would make the retried
            # session inherit the previous attempt's edits and the
            # extracted patch the union of two attempts (pass@1
            # contamination). The agent tree is removed (the shared
            # clone cache is retained) before the re-clone
            for stale in ("testbed", "xdg"):
                sp = os.path.join(workdir, "runs", iid, stale)
                if os.path.isdir(sp):
                    shutil.rmtree(sp, ignore_errors=True)
            prep = prepare_sandbox_instance(inst, workdir)
            spec = SessionSpec(
                instance=inst, mode=config.mode,
                devin_bin=config.devin_bin, model=config.model,
                cwd=prep["cwd"], env=prep["env"],
                wall_s=config.max_wall_s,
                host_check=bool(config.host_check), workdir=workdir,
                run_id=config.run_id)
        else:
            # a crashed attempt's agent container may still be up on a
            # crash-resume — ``docker run --name`` would fail with
            # "name already in use" (best-effort removal)
            subprocess.run(
                ["docker", "rm", "-f",
                 f"sweb.eval.{inst['instance_id']}"],
                capture_output=True, timeout=300, check=False)
            cprep = _container_setup(inst, workdir)
            spec = SessionSpec(
                instance=inst, mode=config.mode,
                devin_bin=cprep["devin_bin"], model=config.model,
                cwd="/testbed", env=cprep["env"],
                wall_s=config.max_wall_s,
                host_check=bool(config.host_check), workdir=workdir,
                container=cprep["container"],
                run_id=config.run_id)
        s = PilotSession(spec, make_session=make_session)
        # the pre-session guard record (the checklist item 14 evidence —
        # kept in the session meta so the line merger persists it)
        src = prep if config.mode == "sandbox" else cprep
        s.meta["leak_guard"] = src["leak_guard"]
        # the stripped credential key names (the env hygiene record)
        s.meta["env_sanitized"] = src.get("env_stripped", [])
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
    aborted = False
    try:
        result = runner.run()
        print(f"[pilot] run finished: {result.final_state}")
        if result.final_state == "aborted":
            # the WP3 terminal abort (2 consecutive agent_error, or a
            # per-instance pre-flight abort): recorded durably (the
            # state file) and the invocation FAILS — no GO checklist
            # renders for an aborted run (an abort is the host-crash
            # class: the operator fixes and resumes)
            aborted = True
    except IntentionalInterrupt as exc:
        interrupted = True
        print(f"[pilot] INTERRUPT (exit {EXIT_INTERRUPT}): {exc}")
    except BaseException as exc:
        run_failed = True
        print(f"[pilot] RUN FAILED (host-crash class — resume-eligible, "
              f"PLAN §2.5): {type(exc).__name__}: {exc}")

    # the state (the interrupt / abort / resume bookkeeping)
    records, _ = rr.load_checkpoint(jsonl)
    _write_state(workdir, run_id, {
        "run_id": run_id, "mode": config.mode, "model": config.model,
        "devin_cli_version": ver, "interrupted": interrupted,
        "aborted": aborted,
        "resume_exercised": resume_exercised,
        "final_state": ("aborted" if aborted
                       else ("interrupted" if interrupted
                           else ("run_failed" if run_failed
                                else "done"))),
        "completed_this_invocation": rep_.done_count,
        # the import bookkeeping: the current invocation's flag
        # values, or — on a continuation window (the flag is
        # dropped there; the source re-derived from this record) —
        # the previous invocation's (without this, the end-of-run
        # write nulls imported_run / import_workdir and the next
        # window's seeding cannot self-heal the referenced
        # artifacts from the source workdir)
        "imported_run": (config.import_run
                        if config.import_run
                        else state.get("imported_run")),
        "imported": sorted(imported) or state.get("imported", []),
        "import_source": (imported
                         or state.get("import_source", {})),
        # the round-17 Medium fix (issue #22 AC2): persist the
        # import workdir ABSOLUTE here too (the same contract as
        # the early import record — a relative flag value must
        # not survive into the state: a continuation window with
        # a different cwd would re-resolve it against the wrong
        # cwd — missing sources → the cross-check fails → a
        # report_failed wedge)
        "import_workdir": (os.path.abspath(
            config.import_workdir or workdir)
            if config.import_workdir
            else state.get("import_workdir")),
        "import_retry_class": ({k: v
                               for k, v in
                               sorted(import_retry.items())}
                              or state.get("import_retry_class",
                                          {})),
        "ts": time.time(),
    })
    ckpt.close()
    if aborted:
        print(f"[pilot] ABORTED (exit {EXIT_RUN_FAILED}): the run "
              "aborted — recorded in the state file; the "
              "retry-eligible instances are the resume class")
        _remove_stale_record(workdir)
        return EXIT_RUN_FAILED

    # the WP5 report + the go/no-go record
    # the deduped last-line-per-instance view (the WP5 contract —
    # ``rep.dedupe``): a superseded retry-class line (e.g. a stale
    # instance-level ``preflight_failed`` shadowed by the resumed
    # ``resolved`` line) must not flip the checklist items
    finals, _ = rep.dedupe(records)
    inst_records = list(finals.values())
    # the calibration + item 8/9 stats count ATTEMPTS (every line
    # with an instance id) — a superseded eval_error→rerun attempt
    # measured wall + tokens that must not drop out of the record
    # (the same basis as the item-12 budget gate). Imported lines
    # are EXCLUDED: they are the source run's sessions (never-rerun
    # reuse — no session of this run spent), and folding their
    # source-run wall/usage into this run's per-session stats would
    # misstate the run the calibration measures
    attempts = [r for r in records if r.get("instance_id")
               and not _meta_of(r).get("imported_from")]
    cal = calibrate(attempts, config)
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
    report_failed = False
    try:
        rep.generate(jsonl, os.path.join(workdir, "report"), meta)
        print("[pilot] WP5 report: ok")
    except rep.ReportError as exc:
        # a failed report pipeline (ArtifactCheckError / generate
        # failure) is a run failure — a corrupt report can never yield
        # a GO: the state is recorded and no checklist renders (the
        # resume class — fix the artifact reference and resume)
        report_failed = True
        print(f"[pilot] WP5 report FAILED: {type(exc).__name__}: {exc}")
    if report_failed:
        _write_state(workdir, run_id, {
            "run_id": run_id, "mode": config.mode, "model": config.model,
            "devin_cli_version": ver,
            # the flags from the state written before the report —
            # a report failure on an interrupted pair must not
            # clobber the durable interrupt record (the follow-up
            # --resume's item-11 gate reads interrupted_before)
            "interrupted": interrupted, "aborted": aborted,
            "resume_exercised": resume_exercised,
            "final_state": "report_failed",
            "report_error": "the WP5 report pipeline failed",
            "completed_this_invocation": rep_.done_count,
            # the import bookkeeping: the same preserve-on-
            # continuation fallback as the main write (a report
            # failure on the documented no-flag continuation window
            # must not null the import fields — the next resume's
            # artifact self-heal resolves the source workdir from
            # them; without the fallback a missing referenced
            # artifact — a plausible cause of the report failure
            # itself — could never be self-healed and every
            # subsequent resume would stay wedged in report_failed)
            "imported_run": (config.import_run
                            if config.import_run
                            else state.get("imported_run")),
            "imported": sorted(imported) or state.get("imported", []),
            "import_source": (imported
                             or state.get("import_source", {})),
            "import_workdir": (os.path.abspath(
                config.import_workdir or workdir)
                if config.import_workdir
                else state.get("import_workdir")),
            "import_retry_class": ({k: v
                                   for k, v in
                                   sorted(import_retry.items())}
                                  or state.get("import_retry_class",
                                              {})),
            "ts": time.time(),
        })
        _remove_stale_record(workdir)
        print(f"[pilot] REPORT FAILED (exit {EXIT_RUN_FAILED}): the "
              "WP5 report pipeline failed — no verdict rendered; "
              "the run is the resume class")
        return EXIT_RUN_FAILED
    # the AC4 per-window budget view (issue #22) — the item-12 gate
    # buckets the checkpoint lines by their ts's UTC day; a
    # multi-window run never fails the per-day cap by construction,
    # and a single-window run renders the verbatim WP6a text
    buckets = _window_buckets(records)
    sessions_used = sum(w["sessions"] for w in buckets.values())
    wall_used_s = sum(w["wall_s"] for w in buckets.values())
    items = build_checklist(
        config=config, dry_run=False, gate=gate,
        preflight_ok=preflight_ok, preflight_failures=preflight_failures,
        records=inst_records, attempt_records=attempts,
        skipped_on_resume=skipped_on_resume,
        interrupted_this_run=None if not interrupted
        else {"completed": rep_.done_count},
        resume_after_interrupt=(interrupted_before
                               or resume_exercised_before),
        devin_ok=bool(inst_records) and not run_failed,
        # the budget gate counts ATTEMPTS (every checkpoint line
        # with an instance id, EXCEPT a preflight_failed line — the
        # WP3 contract: the pre-flight failed before the session, so
        # it burned nothing; _window_buckets skips those lines),
        # consistent with the wall sum and with WP5's session
        # arithmetic
        sessions_used=sessions_used,
        wall_budget_s=config.max_wall_s * config.max_sessions_per_day,
        wall_used_s=wall_used_s,
        pilot_b=config.pilot == "b",
        stratified_repos=_stratified_repos(config),
        imported=imported,
        # the round-12 Medium fix (issue #22 AC2): item 16 must
        # distinguish an attempted-but-zero-import from a
        # legitimate non-import run — the attempt is this
        # invocation's --import-run or an earlier window's
        # (the state re-derivation)
        import_attempted=(bool(config.import_run)
                         or bool(state.get("imported_run")
                                or state.get("import_source"))),
        repo_by_iid=_repo_by_iid(insts),
        arithmetic_total=len(insts),
        window_buckets=buckets)
    # the pilot-B record sections (issue #22) — the per-repo
    # stratification table, the 2-run overlap/variance table, the
    # failure taxonomy + root-cause table, the per-window budget
    # table
    if config.pilot == "b":
        meta["wp6b"] = _pilot_b_sections(
            config, insts, records, inst_records, imported, buckets)
    with open(os.path.join(workdir, record_name), "w",
              encoding="utf-8", newline="") as f:
        f.write(render_checklist_md(items, config, cal, meta,
                                   interrupted=interrupted,
                                   title=_pilot_title(config),
                                   gating=_pilot_gating(config)))
    verdict, failed = _verdict(items, interrupted=interrupted,
                              gating=_pilot_gating(config))
    print(f"[pilot] go/no-go: {verdict}"
          + (f" (failed: {', '.join(failed)})" if failed else ""))
    rep_dir = os.path.join(workdir, "report")
    gng = os.path.join(workdir, record_name)
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
    p.add_argument("--instances", default=None,
                   help="the instances file (default: "
                        f"{os.path.basename(DEFAULT_INSTANCES_FILE)} "
                        "for --pilot a, "
                        f"{os.path.basename(DEFAULT_B_INSTANCES_FILE)} "
                        "for --pilot b)")
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
    p.add_argument("--pilot", choices=("a", "b"), default="a",
                   help="the pilot kind (issue #22: 'b' = the 25-"
                        "instance, 12-repo stratified pilot; the "
                        "default workdir/record naming follows)")
    p.add_argument("--import-run", default=None,
               help="the WP6b checkpoint import: the source run's "
                    "id (issue #22 AC2 — the 3 Pilot A instances "
                    "are reused inside the 25, never re-run; "
                    "a pilot-B flag, verified by items 15–17; "
                    "the run id charset "
                    "[A-Za-z0-9][A-Za-z0-9._-]*). The first "
                    "import window must also pass --resume: the "
                    "import seeds this run's checkpoint, and a "
                    "non-resume invocation with an existing "
                    "checkpoint is rejected (exit 2) — the "
                    "imported lines are the expected contents, "
                    "not a silent re-run")
    p.add_argument("--import-workdir", default=None,
                   help="the source run's workdir (default: this "
                        "run's workdir; requires --import-run)")
    return p


def main(argv: "Optional[list]" = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        # the record-file invariant holds on **every** early exit
        # (exit 2 config / exit 3 live-refused / exit 4 run): the
        # workdir is resolved exactly as run_pilot does, and the
        # previous run's verdict is removed before the first thing
        # that can fail (a stale verdict is never kept)
        wd = args.workdir or os.path.join(
            os.getcwd(),
            "pilot-b" if args.pilot == "b" else "pilot-a")
        os.makedirs(wd, exist_ok=True)
        _remove_stale_record(wd)
        budget = load_budget_file()
        msp = args.max_sessions_per_day
        if msp is None:
            msp = budget.get("max_sessions_per_day", 10)
        mw = args.max_wall_per_session
        if mw is None:
            mw = budget.get("max_wall_per_session", DEFAULT_MAX_WALL_S)
        # an explicit zero/negative cap is a config error, not a
        # silent replacement by the default (the ``or`` fallback
        # covers absence only)
        if msp <= 0:
            raise PilotConfigError(
                "max_sessions_per_day must be > 0")
        if mw <= 0:
            raise PilotConfigError(
                "max_wall_per_session must be > 0")
        if (args.largest_bytes is not None
                and args.largest_bytes <= 0):
            raise PilotConfigError("--largest-bytes must be > 0")
        config = PilotConfig(
            mode=args.mode, devin_bin=args.devin_bin, model=args.model,
            instances_file=args.instances
            or (DEFAULT_B_INSTANCES_FILE if args.pilot == "b"
                else DEFAULT_INSTANCES_FILE),
            workdir=args.workdir,
            run_id=args.run_id, max_sessions_per_day=msp,
            max_wall_s=mw,
            largest_bytes=args.largest_bytes
            or (DEFAULT_LARGEST_IMAGE_BYTES if args.mode == "container"
                else DEFAULT_LARGEST_CHECKOUT_BYTES),
            interrupt_after=args.interrupt_after, resume=args.resume,
            dry_run=args.dry_run, host_check=args.host_check,
            pilot=args.pilot, import_run=args.import_run,
            import_workdir=args.import_workdir)
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
    except rr.RunnerError as exc:
        # the WP3 protocol layer (RunnerProtocolError and friends) must
        # not escape as a traceback: a protocol failure is a run
        # failure (exit 4, the resume class)
        print(f"[pilot] RUNNER ERROR: {type(exc).__name__}: {exc}")
        return EXIT_RUN_FAILED
    except Exception as exc:
        # any non-typed failure (an ``OSError`` writing the state file
        # or the artifacts, a harness bug) stays inside the documented
        # 0/2/3/4/42 exit-code contract — exit 4 (the resume class),
        # never an exit-1 traceback
        print(f"[pilot] FAILED: {type(exc).__name__}: {exc}")
        return EXIT_RUN_FAILED


if __name__ == "__main__":
    sys.exit(main())
