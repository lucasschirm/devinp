"""``evaluate.py`` — WP4 (issue #17): the SWE-bench evaluation leg — a thin
delegate to the **official** SWE-bench harness.

Plan of record: ``bench/PLAN.md`` §5 WP4 · implementation plan:
``bench/PLAN-WP4.md``. One call = one instance (the WP3 runner is sequential
and writes one JSONL line per instance), so the module stays a 1:1 delegate
with no batch logic of its own.

**The whole design is "delegate, then read what the harness wrote."** This
module never applies a patch, never runs a test, never grades output, and
never imports ``swebench`` (no bespoke apply/grade logic — AC1/AC3). Its
*only* harness interaction is spawning the official CLI (WP0-verified, issue
#10; re-confirmed against the pinned ``swebench==5.0.2`` source):

    python -m swebench.harness.run_evaluation \
        -d SWE-bench/SWE-bench_Verified -s test \
        -p <predictions.jsonl> -i <instance_id> -id <run_id> \
        --max_workers 1 -t <timeout> --report_dir <workdir>

…with ``cwd = workdir`` (the per-instance artifact directory the caller owns;
the harness's log root is CWD-relative — ``logs/run_evaluation/…`` — so every
per-instance artifact the harness writes lands under one per-instance
directory, PLAN §5 WP2 "artifact atomicity"). The final run report is written
by the pin's ``make_run_report`` to ``<report_dir>/<model>.<run_id>.json``
(pinned ``reporting.py`` — ``--report_dir`` *is* honored in the 5.0.2 pin,
default ``.``), so passing ``--report_dir <workdir>`` lands it beside the
CWD-relative log tree. After the process exits, ``evaluate`` reads *only* the
harness's own artifacts to resolve the outcome:

* ``exit ≠ 0`` → a typed :class:`HarnessFailedError` **always** — even when a
  ``report.json`` exists: the harness writes the per-instance report before
  the final-report leg (``make_run_report``), so a crash after the report
  must not be read as a completed run.
* ``report.json`` (present — and fresh, see below) → ``resolved`` is the
  harness's value, and it must be a bool — a corrupted-but-parseable verdict
  is a typed failure, never a silent ``bool()`` coercion. The harness is the
  sole source of the resolved flag (AC3).
* ``report.json`` present with ``resolved: false`` +
  ``infra_failure: true`` → a typed :class:`HarnessFailedError` — the
  harness's *own* environment-fault classification (``grading.py:371-374`` —
  unparseable test output; the harness keeps ``resolved`` False but counts
  these separately in the final report's ``infra_failure_ids``). An
  environment fault is the WP3 ``eval_error`` retry class — never a terminal
  "not resolved" verdict.
* no ``report.json`` (the harness swallows per-instance failures and still
  exits 0) → triage on harness-written markers, **by the file the pinned
  harness writes / the test output it grades** (``run_evaluation.py``,
  ``grading.py:137``): ``>>>>> Patch Apply Failed`` in ``run_instance.log``
  → :class:`PatchCorruptionError` (the definitive corruption guard, AC4;
  wins over a timeout marker — a corrupt patch is terminal and a retry would
  just re-fail the apply); in ``test_output.txt``: the tail-anchored
  ``Timeout error: <n> seconds exceeded.`` the harness appends as the file's
  final write, or the dataset eval-script's ``>>>>> Tests Timed Out`` marker
  (graded *inside* the test output — never written to ``run_instance.log``);
  in ``run_instance.log``: the harness's own ``Test timed out after <n>
  seconds`` EvaluationError message → :class:`EvaluationTimeoutError`
  (*terminal* in WP3 — hence the loose markers are anchored and
  file-specific: a docker-client/network line containing ``Timeout error:``
  in the instance log must not convert a retryable infra flake into a
  terminal ``timeout``); a marker in a file this run did not update (the
  stale-log guard) is not this run's verdict → :class:`HarnessFailedError`;
  anything else → :class:`HarnessFailedError` (Docker/infra — the WP3
  ``eval_error`` retry class, PLAN §2.5). No marker is re-interpreted as a
  grade.

**Fresh container (AC2).** The harness creates the evaluation container
itself — ``containers.create(image=<instance eval image>, name=
"sweb.eval.<instance_id.lower()>.<run_id>")`` — a brand-new container from
the published eval image, force-removing any pre-existing container with
that exact name first (ghost cleanup; on a 409 create conflict it retries
once with a ``.{timestamp}`` suffix — the name to clean up is therefore the
base name, possibly suffixed). The agent's container (named
``sweb.eval.<instance_id>`` by the adapter, PLAN §1; its working tree,
submodules and installed packages are contaminated by the run) is never
referenced: there is no parameter for it. The per-call unique ``run_id``
default makes the eval container name ``sweb.eval.<iid>.<run_id>`` structurally
distinct from the agent's name. The harness logs the created container's id
into ``run_instance.log`` (``Container for … created/started: <id>``);
``evaluate`` extracts it into ``logs.container`` so the isolation assertion
(eval container id ≠ agent container id, AC4) is checkable from the result.

**Stale-report protection.** The harness's own ``get_dataset_from_preds``
treats a pre-existing per-run ``report.json`` as "already run"
(``exclude_completed`` — verified in the pinned source) and silently skips
the instance — a caller reusing the same (workdir, run_id) pair would
otherwise get the *previous* run's verdict for a patch that was never
evaluated (with the stale container id in ``logs.container``). ``evaluate``
never trusts a pre-existing report: it unlinks one before the spawn (best
effort; on an unlink failure it records the pre-run mtime and requires the
post-run report to be strictly newer — a missing pre-run mtime or an
unreadable post-run mtime makes freshness unverifiable → reject), and —
combined with the exit-≠ 0-always-error rule above — a result is only ever
produced from a report this run wrote. (The second skip path —
``run_instance`` itself early-returning a pre-existing report,
``run_evaluation.py:272`` — is covered by the same unlink.) The same
re-use risk for the *terminal-class triage markers*: a pre-existing
``run_instance.log`` / ``test_output.txt`` whose mtime this run did not
update is stale — its markers are not this run's verdict, and triage
downgrades them to the retryable infra class.

**Empty patches (the negative control).** The harness itself filters
empty/``None`` ``model_patch`` predictions (``get_dataset_from_preds`` —
verified in the pinned source): the instance is skipped, no container is
created, and the final report counts it in ``empty_patch_ids`` (never in
``resolved_ids``) — official semantics: empty patch ⇒ not resolved.
``evaluate`` mirrors that exactly by short-circuiting to
``{"resolved": False, …}`` with ``empty_patch: True`` — the *semantics* are
the harness's; the short-circuit only saves the HF dataset download and the
Docker handshake on a guaranteed no-op.

**Corruption guard (AC4)** — a clean typed error, never a crash: the
harness's own 4-command git-apply chain (``git apply`` / ``--3way`` /
``--reject`` + GNU ``patch --fuzz=5``) is the *sole authority* on patch
applicability — so no structural pre-check is made. The chain accepts patch
forms that carry no ``---``/``+++`` file headers (rename-only and mode-only
``diff --git`` hunks, ``GIT binary patch`` sections, tab-separated
``diff -u`` headers, context/normal diffs); a header-based pre-check would
reject applicable patches before they could ever reach the harness and score
``resolved``. A truncated/garbage patch reaches the harness, the apply chain
fails, the harness writes its own ``APPLY_PATCH_FAIL`` marker, and
``evaluate`` converts that marker into :class:`PatchCorruptionError`.

**Typed, never a traceback escape.** Every failure mode is one of the typed
errors below (a WP3 caller classifies them into retry decisions; the CLI
maps them to exit codes): bad input (:class:`InvalidInputError`, pre-harness,
zero cost), a corrupt patch (:class:`PatchCorruptionError`), the harness test
timeout (:class:`EvaluationTimeoutError`), or a harness/infra failure
(:class:`HarnessFailedError`) — the last covering a spawn failure (missing
interpreter, fork/exec ``OSError``), the outer watchdog firing, a non-zero
exit, a pre-spawn filesystem failure (ENOSPC/EACCES/ENAMETOOLONG on the
tempdir/workdir/predictions write — PLAN §7 disk management / §9 risk table:
"500 images exhaust disk"), an unreadable/non-object report, a non-bool
verdict, a harness ``infra_failure`` classification, a stale report, and (at
the CLI layer) any unexpected exception.

Acceptance criteria (issue #17) and where they live:

* **AC1 — delegates entirely to ``python -m swebench.harness.run_evaluation``
  on a standard predictions file** → :func:`_harness_argv` (exactly the
  official CLI), the subprocess is the *only* harness interaction, the
  predictions file carries exactly ``instance_id`` / ``model_name_or_path`` /
  ``model_patch``, and this module never imports ``swebench`` (pinned by
  ``tests/test_evaluate.py``: the argv test, the predictions-file test, the
  no-``swebench``-import source test).
* **AC2 — fresh container, never the agent's mutated one** → the harness's
  own ``containers.create`` on the published eval image (no agent-container
  parameter exists); unique per-call ``run_id`` ⇒ distinct container name;
  the container id comes from the harness's own log (pinned by the
  run-id / container-name unit tests and the integration isolation test).
* **AC3 — returns ``{resolved, logs}``, no bespoke apply/grade logic** → the
  return contract below — typed (:class:`EvaluationResult` /
  :class:`EvaluationLogs` — the repo's first `TypedDict`; the sibling
  structured results are `@dataclass`, and this JSON-shaped contract is the
  fit) so key-drift is test-enforced; `resolved` is only ever the harness's
  bool ``report.json`` value or the documented harness empty-patch
  semantics.
* **AC4 — positive / negative / corruption / isolation controls** → the
  integration tests in ``tests/test_evaluate.py`` (CI, public images, zero
  quota) + the unit-level layers above.

Return contract (AC3; :class:`EvaluationResult` — the key set is pinned by
``tests/test_evaluate.py`` for *both* the full and the empty-patch paths):

    {
      "resolved": bool,
      "logs": {
        "instance":     str | None,   # harness run_instance.log (path)
        "test_output":  str | None,   # harness test_output.txt (path)
        "report":       dict | None,  # harness report.json, verbatim
        "final_report": str | None,   # harness final run report (path)
        "container":    str | None,   # id of the container the harness created
        "workdir":      str | None,   # the artifact dir (the harness cwd); a
                                      #   fresh tempdir when the caller passed
                                      #   None — the caller owns its cleanup
        "run_id":       str | None,   # the -id passed to the harness
                                      #   (unique default per call)
        "predictions":  str | None,   # the standard predictions file written
        "empty_patch":  bool,         # short-circuit marker (see above)
      },
    }

Paths, not contents: the WP5 report / WP3 checkpoint consume artifact paths;
embedding multi-MB test output in memory is not the contract. Errors carry
``instance_id``, the log paths, the workdir (so an auto-created tempdir is
findable for the caller's cleanup on failure paths too) and a combined tail
of the harness output.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePath
from typing import Any, Callable, Optional, TypedDict

__all__ = [
    "DEFAULT_DATASET",
    "DEFAULT_SPLIT",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_TIMEOUT_S",
    "EvaluationError",
    "InvalidInputError",
    "PatchCorruptionError",
    "EvaluationTimeoutError",
    "HarnessFailedError",
    "EvaluationLogs",
    "EvaluationResult",
    "evaluate",
    "main",
]

_LOG = logging.getLogger(__name__)

# Official harness defaults (WP0-verified CLI, issue #10). The dataset is the
# Verified split — the benchmark this adapter exists for (PLAN.md goal).
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_MODEL_NAME = "devin"
DEFAULT_TIMEOUT_S = 1800  # the harness's own per-instance test timeout (-t)

# Harness log layout — verified against the pinned swebench==5.0.2 source
# (harness/constants/__init__.py: RUN_EVALUATION_LOG_DIR, LOG_REPORT,
# LOG_INSTANCE, LOG_TEST_OUTPUT; reporting.py: the final run report is
# written to <report_dir>/<model>.<run_id>.json — --report_dir is honored in
# this pin, default "."). The bench package pins swebench==5.0.2 exactly, so
# this layout is stable by pin.
_HARNESS_LOG_ROOT = Path("logs") / "run_evaluation"
_HARNESS_LOG_REPORT = "report.json"
_HARNESS_LOG_INSTANCE = "run_instance.log"
_HARNESS_LOG_TEST_OUTPUT = "test_output.txt"

# Harness-written markers (pinned swebench 5.0.2), by the file the harness
# writes / the test output the harness grades. Nothing is re-graded.
_APPLY_PATCH_FAIL_MARKER = ">>>>> Patch Apply Failed"  # run_instance.log
# test_output.txt markers: "Timeout error: <n> seconds exceeded." is
# appended by the harness as the file's final write (run_evaluation.py —
# hence tail-anchored), and ">>>>> Tests Timed Out" is the dataset
# eval-script's timeout marker, which grading reads *inside* the test output
# (grading.py:137) — it is never written to run_instance.log. The
# instance-log marker is the harness's own "Test timed out after <n> seconds"
# EvaluationError message via the logger. Markers are anchored and
# file-specific because EvaluationTimeoutError is terminal in WP3 while
# HarnessFailedError is the retried eval_error class — a loose substring in
# the wrong file must not convert a retryable infra flake into a terminal
# timeout.
_TIMEOUT_MARKER_TEST_OUTPUT_TAIL = re.compile(
    r"Timeout error: \d+ seconds exceeded\.\s*$"
)
_TIMEOUT_MARKER_TEST_OUTPUT = ">>>>> Tests Timed Out"
_TIMEOUT_MARKER_INSTANCE_LOG = "Test timed out after"

# The shared name character-set check (instance_id / run_id): the Docker
# container-name charset, starting alphanumeric, without ".." (they become
# container-name suffixes, tempdir prefixes and log-path components).
_NAME_CHARSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# A harness subprocess is the only I/O this module does; this tail is carried
# in typed errors so an operator can triage without re-reading the log files.
_ERROR_TAIL_CHARS = 4000


class EvaluationLogs(TypedDict):
    """The ``logs`` half of :class:`EvaluationResult` (AC3) — the harness
    artifact paths + the verbatim per-instance report + the run identity.
    The key set is pinned by ``tests/test_evaluate.py`` for both the full
    and the empty-patch paths (on the latter the run-identity values are
    None — no tempdir / run id / predictions file were created)."""

    instance: Optional[str]
    test_output: Optional[str]
    report: Optional[dict[str, Any]]
    final_report: Optional[str]
    container: Optional[str]
    workdir: Optional[str]
    run_id: Optional[str]
    predictions: Optional[str]
    empty_patch: bool


class EvaluationResult(TypedDict):
    """The ``{resolved, logs}`` return contract (AC3) — typed so key-drift is
    caught by the key-set tests (test-enforced; the repo has no type
    checker in CI); ``resolved`` is only ever the harness's bool value or
    the harness's own empty-patch semantics (False)."""

    resolved: bool
    logs: EvaluationLogs


class EvaluationError(Exception):
    """Base of the evaluate.py error hierarchy — a clean typed error, never a
    traceback escape. Carries the instance, the harness log paths (where
    applicable) and a combined tail of the relevant harness output.

    The *type* is the structured discriminator: WP3 classifies by
    ``isinstance`` (``InvalidInputError`` / ``PatchCorruptionError`` /
    ``EvaluationTimeoutError`` / ``HarnessFailedError`` — the same classes
    the CLI maps to exit codes 2/3/4/5); the message text is for humans.

    ``workdir`` is set on every error raised after the workdir is resolved
    (with ``workdir=None`` that is the auto-created tempdir) — so the
    caller-owned cleanup stays reachable on failure paths, not just the
    success path (``logs.workdir``).

    Note the ``report`` key's two meanings: in ``log_paths`` (and in
    ``_instance_paths``) it is the report.json *path*; in the result's
    ``logs`` dict (``EvaluationLogs.report``) it is the *parsed, verbatim*
    per-instance report object.
    """

    def __init__(
        self,
        message: str,
        *,
        instance_id: Optional[str] = None,
        log_paths: Optional[dict[str, Optional[str]]] = None,
        output_tail: Optional[str] = None,
        workdir: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.instance_id = instance_id
        self.log_paths = dict(log_paths or {})
        self.output_tail = output_tail
        # the workdir (the harness cwd / artifact root) once it is known —
        # with workdir=None the module creates an auto tempdir, and on a
        # typed failure the caller must be able to *find* it to honor the
        # caller-owned-cleanup contract (an unreachable tempdir is a leak):
        # every typed error raised after the workdir is resolved carries it.
        self.workdir = workdir

    def __str__(self) -> str:
        if self.instance_id:
            parts: list[str] = [
                f"Error in evaluation for {self.instance_id}: {self.message}"
            ]
        else:
            parts = [self.message]
        shown = ", ".join(
            f"{k}={v}" for k, v in sorted(self.log_paths.items()) if v
        )
        if shown:
            parts.append(f"Check ({shown}) for more information.")
        if self.workdir:
            parts.append(
                f"Workdir: {self.workdir} (the caller owns its cleanup)."
            )
        if self.output_tail:
            parts.append(self.output_tail)
        return "\n".join(parts)


class InvalidInputError(EvaluationError):
    """The call's inputs are not evaluable (bad instance id / non-str patch /
    bad run_id / bad model name / bad timeout / bad dataset / split / workdir
    / python / overall_timeout / non-callable runner) — raised before any
    harness work happens, with zero filesystem side effects."""


class PatchCorruptionError(EvaluationError):
    """The harness's own git-apply chain — the sole authority on patch
    applicability (see the module docstring: it applies patch forms without
    ``---``/``+++`` file headers, which a structural pre-check would wrongly
    reject) — wrote its ``APPLY_PATCH_FAIL`` marker: the patch is
    corrupt/truncated. A clean typed error, never a crash (AC4 corruption
    guard)."""


class EvaluationTimeoutError(EvaluationError):
    """The harness's per-instance test timeout (-t) fired — the harness wrote
    its timeout marker in the file it writes it to; the instance is not
    graded. Terminal in WP3 (never re-run) — hence the anchored, file-
    specific markers."""


class HarnessFailedError(EvaluationError):
    """The harness process itself failed — a spawn failure (missing
    interpreter, fork/exec OSError), the outer watchdog firing, a non-zero
    exit (always an error, even when a report exists), a pre-spawn filesystem
    failure, an unreadable / non-object report, a non-bool verdict, a stale
    report — or it finished without a report and without a known marker
    (Docker/infra — the WP3 ``eval_error`` retry class, PLAN §2.5)."""


# ---------------------------------------------------------------------------
# small helpers — typed, never a traceback escape
# ---------------------------------------------------------------------------


def _tail(text: Any, limit: int = _ERROR_TAIL_CHARS) -> Optional[str]:
    """The last ``limit`` characters of a harness output. bytes (a
    ``TimeoutExpired.output`` / a runner's bytes stdout/stderr — bytes even
    under ``text=True`` on POSIX) is decoded with replacement, never a
    ``b'…'`` repr with escaped newlines; any other non-str is ``str()``
    coerced — never a TypeError inside a typed error's ``__str__``.
    Slicing clamps, so short text passes through as-is."""
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif not isinstance(text, str):
        text = str(text)
    text = text.strip()
    return text[-limit:] if text else None


def _combined_tail(
    stdout: Any,
    stderr: Any,
    stdout_label: str = "stdout",
    stderr_label: str = "stderr",
) -> Optional[str]:
    """A combined tail of *both* process outputs — when one is non-empty the
    other must not be discarded entirely (the harness's stdout carries the
    run's own diagnostics, e.g. the "already run, skipping" line). The
    labels name the source of each tail (the triage pass passes the
    per-file names)."""
    stdout_tail = _tail(stdout)
    stderr_tail = _tail(stderr)
    if stdout_tail and stderr_tail:
        return (
            f"[{stderr_label}]\n{stderr_tail}\n[{stdout_label}]\n{stdout_tail}"
        )
    return stderr_tail or stdout_tail


def _charset_ok(name: Any) -> bool:
    """The shared name character-set check (``instance_id`` / ``run_id``):
    a str of the Docker container-name charset ``[A-Za-z0-9._-]`` starting
    alphanumeric, without ``..`` (they become container-name suffixes,
    tempdir prefixes and log-path components — real SWE-bench ids
    ``owner__repo-N`` always satisfy it)."""
    return (
        isinstance(name, str)
        and ".." not in name
        and _NAME_CHARSET.fullmatch(name) is not None
    )


def _model_dir(model_name_or_path: str) -> str:
    """The harness's log-dir / report-name escape (``/`` → ``__``)."""
    return model_name_or_path.replace("/", "__")


def _instance_paths(
    workdir: Path, run_id: str, model_name_or_path: str, instance_id: str
) -> dict[str, Path]:
    """The harness's CWD-relative artifact layout (the pinned swebench 5.0.2
    log root / filenames); the final report is the pin's
    ``<report_dir>/<model>.<run_id>.json`` with ``--report_dir = workdir``."""
    base = (
        workdir
        / _HARNESS_LOG_ROOT
        / run_id
        / _model_dir(model_name_or_path)
        / instance_id
    )
    return {
        "report": base / _HARNESS_LOG_REPORT,
        "instance": base / _HARNESS_LOG_INSTANCE,
        "test_output": base / _HARNESS_LOG_TEST_OUTPUT,
        "final_report": workdir / f"{_model_dir(model_name_or_path)}.{run_id}.json",
    }


def _existing_log_paths(
    paths: dict[str, Path],
) -> dict[str, Optional[str]]:
    """Which of the harness's CWD-relative artifacts exist right now — the
    triage context every typed error carries. One helper, one spelling
    (computed before the spawn for the pre-spawn/runner errors and again
    after the run for the outcome errors)."""
    return {k: (str(p) if p.is_file() else None) for k, p in paths.items()}


def _extract_container_id(log_text: str, instance_id: str) -> Optional[str]:
    """The id of the container *the harness created* — from the harness's own
    log line (``Container for <iid> created/started: <id>``). The source of
    truth for the isolation assertion (AC4) is the harness, not us. Absent
    log / no line → None (never a crash; the caller logs a warning)."""
    pattern = re.compile(
        r"Container for " + re.escape(instance_id) + r" (?:created|started): (\S+)"
    )
    matches = pattern.findall(log_text)
    return matches[-1] if matches else None


def _read_log(path: Path) -> Optional[str]:
    """Read a harness log for marker triage. A missing file is a legitimate
    no-artifact (None); an *unreadable* existing file is an anomaly — it is
    logged, because a marker in it would be missed and the fallback error's
    "without a known failure marker" wording would otherwise be wrong."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        if path.is_file():
            _LOG.warning(
                "cannot read harness log %s during triage; markers in it "
                "will be missed",
                path,
            )
        return None


def _validate_inputs(
    instance_id: Any,
    model_patch: Any,
    *,
    run_id: Optional[str],
    model_name_or_path: Any,
    timeout: Any,
    dataset: Any,
    split: Any,
    workdir: Any,
    overall_timeout: Any,
    python: Any,
    harness_runner: Any,
) -> None:
    """Pre-harness validation — typed, zero-cost, no side effects.

    Raises :class:`InvalidInputError` (never a raw ``TypeError``) so a runner
    bug surfaces as a typed refusal before any dataset download or Docker
    work happens. ``instance_id`` and ``run_id`` are checked against the same
    character set (Docker container-name charset, no ``..``); NUL bytes are
    rejected for every string that lands in a shell/argv context (dataset,
    split, workdir, python, model_name_or_path — a NUL in an argv element
    would otherwise surface as a raw ``ValueError`` at Popen); ``timeout``
    must be a positive int (bools excluded) and ``overall_timeout`` a
    positive *finite* number (nan/inf would hang or immediately fire the
    watchdog); ``model_name_or_path`` is escaped ``/`` → ``__`` like the
    harness, but the *escaped* name must not be a log-path traversal
    (``.``/``..``).
    """
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise InvalidInputError(
            f"instance_id must be a non-empty str, got {instance_id!r}"
        )
    if not _charset_ok(instance_id):
        raise InvalidInputError(
            "instance_id must be a str of the SWE-bench id charset "
            "[A-Za-z0-9][A-Za-z0-9._-]* without '..' (it seeds the default "
            f"run_id — a container name — the tempdir prefix, and a log-path "
            f"component), got {instance_id!r}"
        )
    if model_patch is not None and not isinstance(model_patch, str):
        raise InvalidInputError(
            f"model_patch must be a str (or None for an empty patch), "
            f"got {type(model_patch).__name__}"
        )
    if run_id is not None and not _charset_ok(run_id):
        raise InvalidInputError(
            "run_id must be a non-empty str of Docker container-name charset "
            "[A-Za-z0-9._-] starting alphanumeric, without '..' (it becomes the "
            f"eval container name suffix and a log path component), got {run_id!r}"
        )
    if not isinstance(model_name_or_path, str) or not model_name_or_path:
        raise InvalidInputError(
            f"model_name_or_path must be a non-empty str, got {model_name_or_path!r}"
        )
    if "\x00" in model_name_or_path or _model_dir(model_name_or_path) in (".", ".."):
        raise InvalidInputError(
            "model_name_or_path is not a safe log-directory name (NUL or a "
            f"log-path traversal after the harness's '/' → '__' escape), "
            f"got {model_name_or_path!r}"
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise InvalidInputError(
            f"timeout must be a positive int (seconds), got {timeout!r}"
        )
    if not isinstance(dataset, str) or not dataset.strip() or "\x00" in dataset:
        raise InvalidInputError(
            f"dataset must be a non-empty NUL-free str, got {dataset!r}"
        )
    if not isinstance(split, str) or not split.strip() or "\x00" in split:
        raise InvalidInputError(
            f"split must be a non-empty NUL-free str, got {split!r}"
        )
    if workdir is not None:
        if isinstance(workdir, str):
            workdir_str = workdir
        elif isinstance(workdir, os.PathLike):
            # a path-like gets the *same* checks as a str: an *empty*
            # path-like (PurePath("") — its fsdecode is "." and would
            # silently resolve to the caller's CWD, the harness writing
            # its whole artifact tree into an unintended directory) is
            # refused explicitly; a broken PathLike's __fspath__ raising
            # is a typed refusal, not a raw escape; and a NUL-carrying
            # PurePath is the same malformed input as the str case.
            if isinstance(workdir, PurePath) and not workdir.parts:
                raise InvalidInputError(
                    "workdir must be a non-empty NUL-free str/path (or None "
                    "for a fresh tempdir) — an empty path-like resolves to "
                    f"the caller's CWD, got {workdir!r}"
                )
            try:
                workdir_str = os.fsdecode(workdir)
            except (OSError, ValueError, TypeError) as e:
                raise InvalidInputError(
                    f"workdir path-like is not usable: {e!r}"
                ) from e
        else:
            raise InvalidInputError(
                "workdir must be a str/path (or None for a fresh tempdir), "
                f"got {type(workdir).__name__}"
            )
        if not workdir_str.strip() or "\x00" in workdir_str:
            raise InvalidInputError(
                "workdir must be a non-empty NUL-free str/path (or None for "
                "a fresh tempdir) — an empty path resolves to the caller's "
                f"CWD and a NUL would surface as a raw ValueError, got "
                f"{workdir_str!r}"
            )
    if overall_timeout is not None and (
        isinstance(overall_timeout, bool)
        or not isinstance(overall_timeout, (int, float))
        or math.isnan(overall_timeout)
        or math.isinf(overall_timeout)
        or overall_timeout <= 0
    ):
        raise InvalidInputError(
            "overall_timeout must be a positive finite number of seconds (or "
            f"None for no outer watchdog), got {overall_timeout!r}"
        )
    if python is not None:
        if not isinstance(python, str) or not python.strip() or "\x00" in python:
            raise InvalidInputError(
                f"python must be a non-empty NUL-free str (or None for the "
                f"current interpreter), got {python!r}"
            )
    if harness_runner is not None and not callable(harness_runner):
        raise InvalidInputError(
            "harness_runner must be callable (or None for the real "
            f"subprocess), got {type(harness_runner).__name__}"
        )


def _write_predictions_file(
    workdir: Path, instance_id: str, model_name_or_path: str, model_patch: str
) -> Path:
    """The standard predictions file — exactly the three WP0-verified fields
    (``instance_id``, ``model_name_or_path``, ``model_patch``), one JSONL
    line. Nothing else is written: no extra fields for the harness to ignore,
    no fields of our own for it to choke on."""
    path = workdir / "predictions.jsonl"
    line = json.dumps(
        {
            "instance_id": instance_id,
            "model_name_or_path": model_name_or_path,
            "model_patch": model_patch,
        }
    )
    path.write_text(line + "\n", encoding="utf-8")
    return path


def _harness_argv(
    python: str,
    dataset: str,
    split: str,
    predictions_path: Path,
    instance_id: str,
    run_id: str,
    timeout: int,
    workdir: Path,
) -> list[str]:
    """Exactly the official CLI (WP0-verified; re-confirmed against the pinned
    ``swebench==5.0.2`` argparse). No custom flags: ``--max_workers 1`` (one
    instance per call — the sequential benchmark, PLAN §2.1), ``-t`` the
    per-instance test timeout, ``--report_dir workdir`` (the pinned
    ``make_run_report`` writes the final run report to
    ``<report_dir>/<model>.<run_id>.json``; with ``cwd = workdir`` the
    CWD-relative log root lands in the same directory)."""
    return [
        python,
        "-m",
        "swebench.harness.run_evaluation",
        "-d",
        dataset,
        "-s",
        split,
        "-p",
        str(predictions_path),
        "-i",
        instance_id,
        "-id",
        run_id,
        "--max_workers",
        "1",
        "-t",
        str(timeout),
        "--report_dir",
        str(workdir),
    ]


def _default_harness_runner(
    argv: list[str], cwd: str, overall_timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """The real delegation: spawn the official harness CLI. ``cwd = workdir``
    is load-bearing — the harness's log root is CWD-relative and
    ``--report_dir`` (which the 5.0.2 pin honors for the final report) is
    passed the same directory. ``overall_timeout`` is the optional outer
    watchdog — the project's hang policy (a hung run is a failure, not a
    wait, ``runner.py``): the harness's own ``-t`` bounds only the
    *in-container* test exec; the HF dataset download, the multi-GB image
    pull and the Docker daemon are not bound by ``-t``, so an unbounded
    default would let one stuck instance stall the whole sequential
    benchmark silently."""
    return subprocess.run(
        argv,
        cwd=cwd,
        # The harness never reads stdin — a stray read would block; and a
        # half-UTF-8 byte in the output must surface as replacement chars,
        # not kill the run with a decode error.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=overall_timeout,
    )


def _logs(
    *,
    instance: Optional[str] = None,
    test_output: Optional[str] = None,
    report: Optional[dict[str, Any]] = None,
    final_report: Optional[str] = None,
    container: Optional[str] = None,
    workdir: Optional[str] = None,
    run_id: Optional[str] = None,
    predictions: Optional[str] = None,
    empty_patch: bool = False,
) -> EvaluationLogs:
    """The single spelling of the 9-key ``logs`` dict (the full and the
    empty-patch results share it — one place for the key set)."""
    return {
        "instance": instance,
        "test_output": test_output,
        "report": report,
        "final_report": final_report,
        "container": container,
        "workdir": workdir,
        "run_id": run_id,
        "predictions": predictions,
        "empty_patch": empty_patch,
    }


# ---------------------------------------------------------------------------
# outcome resolution — read only what the harness wrote
# ---------------------------------------------------------------------------


def _result_from_report(
    *,
    instance_id: str,
    paths: dict[str, Path],
    workdir: Path,
    run_id: str,
    predictions_path: Path,
) -> EvaluationResult:
    """The report-present leg (a fresh report — see ``evaluate``'s stale-
    report protection): parse/validate the harness's ``report.json`` and build
    the ``{resolved, logs}`` result. AC3 — ``resolved`` is only ever the
    harness's value, and it must be a bool: a corrupted-but-parseable verdict
    (``"false"`` / ``None`` / ``1``) is a typed failure, never a silent
    ``bool()`` coercion."""
    log_paths = _existing_log_paths(paths)
    report_path = paths["report"]
    try:
        raw = report_path.read_bytes()
        report = json.loads(raw)
    except (OSError, ValueError, RecursionError) as e:
        # read_bytes + json.loads: a non-UTF-8 file is a JSONDecodeError (a
        # ValueError), not a UnicodeDecodeError escape; a pathological
        # deep-nested report is a RecursionError — neither may escape as
        # raw.
        raise HarnessFailedError(
            f"the harness report.json exists but is unreadable ({e}) — "
            "harness/infra failure",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir),
        ) from e
    if not isinstance(report, dict):
        raise HarnessFailedError(
            f"the harness report.json is not a JSON object "
            f"(got {type(report).__name__}) — harness/infra failure",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir),
        )
    entry = report.get(instance_id)
    if not isinstance(entry, dict) or "resolved" not in entry:
        raise HarnessFailedError(
            "the harness report.json lacks the instance's entry — "
            "harness/infra failure",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir),
        )
    resolved = entry["resolved"]
    if not isinstance(resolved, bool):
        raise HarnessFailedError(
            f"the harness report.json verdict for {instance_id!r} is not a "
            f"bool (got {resolved!r}) — refusing to coerce a malformed "
            "verdict",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir),
        )
    if resolved is False and entry.get("infra_failure"):
        # The harness's own classification, not a re-grade: on unparseable
        # test output from an environment fault, grading sets
        # infra_failure + reason (grading.py:371-374) while keeping
        # resolved False, and the final report tallies these separately
        # (infra_failure_ids, reporting.py). An environment fault is the
        # WP3 eval_error retry class — never a terminal verdict.
        reason = entry.get("infra_failure_reason")
        raise HarnessFailedError(
            "the harness classified the instance as an environment-fault "
            f"infra failure (infra_failure: True"
            f"{': ' + str(reason) if reason else ''}) — test output was "
            "unparseable; the harness keeps resolved False but counts these "
            "separately (infra_failure_ids), so this is the WP3 eval_error "
            "retry class, not a terminal 'not resolved' verdict",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_tail(_read_log(paths["instance"]))
            or _tail(_read_log(paths["test_output"])),
            workdir=str(workdir),
        )
    instance_log = _read_log(paths["instance"]) or ""
    container = _extract_container_id(instance_log, instance_id)
    if container is None:
        _LOG.warning(
            "no 'Container for %s created/started' line in %s — "
            "logs.container is None",
            instance_id,
            paths["instance"],
        )
    if log_paths["final_report"] is None:
        _LOG.warning(
            "the final run report was not found at %s (the harness writes "
            "<report_dir>/<model>.<run_id>.json)",
            paths["final_report"],
        )
    _LOG.info(
        "harness run %s for %s finished exit 0; resolved=%s",
        run_id,
        instance_id,
        resolved,
    )
    return {
        "resolved": resolved,
        "logs": _logs(
            instance=log_paths["instance"],
            test_output=log_paths["test_output"],
            report=report,
            final_report=log_paths["final_report"],
            container=container,
            workdir=str(workdir),
            run_id=run_id,
            predictions=str(predictions_path),
            empty_patch=False,
        ),
    }


def _triage_no_report(
    *,
    instance_id: str,
    paths: dict[str, Path],
    log_paths: dict[str, Optional[str]],
    stdout: Any,
    stderr: Any,
    pre_log_mtimes: dict[str, "int | bool"],
    workdir: Optional[str] = None,
) -> EvaluationError:
    """A harness run that exited 0 without producing a report.json (the
    harness swallows per-instance failures — it prints, it does not raise).
    Triage on harness-written markers only, **by the file the pinned harness
    writes them to** — no re-grading:

    * ``APPLY_PATCH_FAIL`` in run_instance.log → the harness's own git-apply
      chain failed → :class:`PatchCorruptionError` (definitive corruption
      guard, AC4; wins over the timeout marker — a corrupt patch is a
      terminal verdict and a retry would just re-fail the apply).
    * ``Timeout error: <n> seconds exceeded.`` in test_output.txt (tail-
      anchored — the harness appends it as the file's final write),
      ``>>>>> Tests Timed Out`` in test_output.txt (the dataset
      eval-script's timeout marker — graded inside the test output,
      ``grading.py:137``; never written to run_instance.log), or the
      harness's ``Test timed out after <n> seconds`` EvaluationError message
      in run_instance.log → :class:`EvaluationTimeoutError` (terminal in
      WP3 — hence the loose markers are anchored and file-specific: a
      docker-client/network line containing ``Timeout error:`` in the
      instance log must not convert a retryable infra flake into a terminal
      ``timeout``).
    * a marker that survived from a *previous* run (its file was not
      updated by this run — the stale-log guard) is not this run's verdict
      → the retryable :class:`HarnessFailedError` class, never a terminal
      verdict.
    * anything else → :class:`HarnessFailedError` (Docker/infra — the WP3
      ``eval_error`` retry class).
    """
    instance_log = _read_log(paths["instance"]) or ""
    test_output = _read_log(paths["test_output"]) or ""

    def _stale(file_key: str, path: Path) -> bool:
        """A marker in ``path`` may be from a *previous* run when the file
        existed pre-run and this run did not update it (mtime ≤ baseline).
        An unreadable baseline (False) or an unreadable post-run mtime is
        treated as stale — the safe direction (a wrong terminal verdict is
        worse than a retried infra class)."""
        baseline = pre_log_mtimes.get(file_key)
        if baseline is None:  # absent pre-run → any post-run file is fresh
            return False
        if baseline is False:  # existed pre-run, baseline unreadable
            return True
        try:
            return path.stat().st_mtime_ns <= baseline
        except OSError:
            return True

    apply_fail = _APPLY_PATCH_FAIL_MARKER in instance_log
    timeout_test_output = (
        _TIMEOUT_MARKER_TEST_OUTPUT_TAIL.search(test_output) is not None
        or _TIMEOUT_MARKER_TEST_OUTPUT in test_output
    )
    timeout_instance_log = _TIMEOUT_MARKER_INSTANCE_LOG in instance_log
    if apply_fail and not _stale("instance", paths["instance"]):
        return PatchCorruptionError(
            "the harness's git-apply chain rejected model_patch "
            f"({_APPLY_PATCH_FAIL_MARKER} in the instance log) — corrupt/truncated "
            "patch; see the harness output tail for the git-apply diagnostics",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_tail(instance_log),
            workdir=workdir,
        )
    if (
        (timeout_test_output and not _stale("test_output", paths["test_output"]))
        or (timeout_instance_log and not _stale("instance", paths["instance"]))
    ):
        return EvaluationTimeoutError(
            "the harness test run timed out (a harness timeout marker)",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_combined_tail(
                test_output,
                instance_log,
                stdout_label="test_output",
                stderr_label="run_instance.log",
            ),
            workdir=workdir,
        )
    if apply_fail or timeout_test_output or timeout_instance_log:
        # the marker(s) exist but survived from a previous run (the file was
        # not updated by this run) — not this run's verdict; the retryable
        # class, never a terminal one.
        return HarnessFailedError(
            "the harness exited 0 without a report; a harness failure marker "
            "survived in the log files but was not updated by this run "
            "(stale — a re-used workdir/run_id); the current run produced no "
            "report and no fresh marker — Docker/infra failure (the WP3 "
            "'eval_error' retry class)",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_combined_tail(stdout, stderr),
            workdir=workdir,
        )
    return HarnessFailedError(
        "the harness exited 0 without a report and without a known failure "
        "marker — Docker/infra failure (the WP3 'eval_error' retry class)",
        instance_id=instance_id,
        log_paths=log_paths,
        output_tail=_combined_tail(stdout, stderr),
        workdir=workdir,
    )


# ---------------------------------------------------------------------------
# the delegate
# ---------------------------------------------------------------------------


def evaluate(
    instance_id: str,
    model_patch: Optional[str],
    *,
    model_name_or_path: str = DEFAULT_MODEL_NAME,
    dataset: str = DEFAULT_DATASET,
    split: str = DEFAULT_SPLIT,
    workdir: Optional[str | os.PathLike] = None,
    run_id: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    overall_timeout: Optional[float] = None,
    python: Optional[str] = None,
    harness_runner: Optional[
        Callable[[list[str], str, Optional[float]], Any]
    ] = None,
) -> EvaluationResult:
    """Evaluate one instance by delegating *entirely* to the official
    SWE-bench harness (AC1) in a fresh container (AC2), returning
    ``{"resolved", "logs"}`` (AC3). See the module docstring for the full
    contract; the flow is:

    1. typed validation (zero-cost, no side effects; character-set checks for
       ``instance_id`` / ``run_id``, NUL checks for the argv-carrying strings,
       finite-numeric checks — :class:`InvalidInputError`),
    2. the harness's own empty-patch semantics (short-circuit — the negative
       control, no harness spawn, no tempdir),
    3. ``run_id`` (unique default per call) + ``workdir`` resolution (a
       relative workdir is resolved to an absolute path **before** the spawn
       — the harness resolves ``-p`` / ``--report_dir`` against its own cwd
       (= workdir), so an unresolved relative path would double-resolve
       (``workdir/workdir/…``)) + the pre-spawn filesystem work (tempdir /
       mkdir / predictions write) — typed, never a raw ``OSError`` escape,
       with the artifact paths known from here on so every typed error after
       this point carries them,
    4. stale-report protection — a pre-existing per-run ``report.json`` is
       unlinked (the harness's ``exclude_completed`` would otherwise skip
       the instance and a re-used (workdir, run_id) pair would return the
       previous run's verdict),
    5. the delegation — spawn the official CLI (``cwd = workdir``; the
       ``harness_runner`` injection seam is ``fn(argv, cwd,
       overall_timeout)``; a spawn failure, an injected runner failure, or
       the outer watchdog firing — which surfaces the possibly-still-running
       eval container name and the partial output — is a typed
       :class:`HarnessFailedError`; typed errors an injected runner raises
       stay typed),
    6. ``exit ≠ 0`` → a typed :class:`HarnessFailedError` **always** (even
       when a report exists — a crash after the report is not a completed
       run);
    7. fresh ``report.json`` → ``resolved`` (the harness's bool value, type-
       checked — the sole source of the resolved flag, AC3; a
       ``resolved: false`` + ``infra_failure: true`` report is the harness's
       environment-fault class → :class:`HarnessFailedError`, not a terminal
       verdict); the container id from the harness's own log;
    8. no report (exit 0) → marker triage (the corruption guard is the
       harness's own git-apply chain writing its ``APPLY_PATCH_FAIL``
       marker; a marker in a file this run did not update — the stale-log
       guard — is the retryable class, never a terminal verdict).

    ``workdir`` is the per-instance artifact directory (the harness cwd);
    with None a fresh ``swebench-eval-…`` directory is created and the
    *caller* owns its cleanup (the artifacts are the point) — the path is
    surfaced in ``logs.workdir`` (success) and in ``error.workdir``
    (every typed error raised after the workdir is resolved) so it is never
    unreachable. ``run_id`` defaults to a per-call unique id so the eval
    container name (``sweb.eval.<iid>.<run_id>``) is always distinct from
    the agent container's (``sweb.eval.<iid>``) — AC2. ``overall_timeout``
    is the optional outer watchdog in seconds (the project's hang policy: a
    hung run is a failure, not a wait — the harness's ``-t`` bounds only the
    in-container test exec). ``harness_runner`` is the test injection seam:
    a ``fn(argv, cwd, overall_timeout) -> CompletedProcess-like`` replacing
    the real subprocess (any failure it raises — or of the real spawn — is
    converted to a typed :class:`HarnessFailedError`, never a traceback).
    """
    _validate_inputs(
        instance_id,
        model_patch,
        run_id=run_id,
        model_name_or_path=model_name_or_path,
        timeout=timeout,
        dataset=dataset,
        split=split,
        workdir=workdir,
        overall_timeout=overall_timeout,
        python=python,
        harness_runner=harness_runner,
    )

    # AC4 negative control — the harness's *own* filter semantics (verified in
    # the pinned source, get_dataset_from_preds): an empty/None patch is
    # skipped by the harness (no container, no report, counted in
    # empty_patch_ids, never in resolved_ids). Mirror that exactly — no
    # harness spawn, no dataset download, no Docker, no tempdir (so there is
    # nothing for the caller to clean up; the run-identity log values are
    # None).
    if model_patch is None or model_patch == "":
        return {"resolved": False, "logs": _logs(empty_patch=True)}

    # Per-call unique run_id → the eval container name is always fresh and
    # can never collide with the agent container's (AC2).
    if run_id is None:
        run_id = f"eval-{instance_id}-{uuid.uuid4().hex[:8]}"

    # workdir resolution — before the harness is spawned (the harness
    # resolves -p / --report_dir against its own cwd = workdir, so an
    # unresolved relative path would double-resolve: workdir/workdir/…).
    # Pre-spawn filesystem work is typed (ENOSPC / EACCES / ENAMETOOLONG are
    # harness/infra failures the WP3 caller can classify), and the artifact
    # paths are known from here on, so every typed error after this point
    # carries them.
    if workdir is None:
        try:
            workdir = tempfile.mkdtemp(prefix=f"swebench-eval-{instance_id}-")
        except OSError as e:
            raise HarnessFailedError(
                f"pre-spawn filesystem failure creating the temp workdir: "
                f"{e!r}",
                instance_id=instance_id,
            ) from e
    try:
        workdir_abs = Path(workdir).resolve()
        paths = _instance_paths(workdir_abs, run_id, model_name_or_path, instance_id)
    except (OSError, ValueError, TypeError) as e:
        # OSError (ENOSPC / EACCES / ENAMETOOLONG), ValueError (a NUL byte
        # in a str path, or a broken PathLike whose fspath() raises) and
        # TypeError (a broken PathLike's fspath() returning a non-str) are
        # all pre-spawn infra/refusal failures — typed, never raw.
        raise HarnessFailedError(
            f"pre-spawn filesystem failure resolving workdir ({workdir!r}): "
            f"{e!r}",
            instance_id=instance_id,
        ) from e
    log_paths = _existing_log_paths(paths)

    # Stale-report protection — the harness's own get_dataset_from_preds
    # treats a pre-existing per-run report.json as "already run"
    # (exclude_completed) and silently skips the instance; a re-used
    # (workdir, run_id) pair would otherwise return the previous run's
    # verdict for a patch that was never evaluated (with the stale container
    # id in logs.container). Unlink a pre-existing report so a post-run
    # report is by definition from this run; if the unlink fails, remember
    # the pre-run mtime and require the post-run report to be newer.
    report_path = paths["report"]
    stale_guard = False
    pre_mtime_ns: Optional[int] = None
    if report_path.is_file():
        try:
            pre_mtime_ns = report_path.stat().st_mtime_ns
        except OSError:
            # mtime unreadable — stay None; the post-run freshness check
            # then rejects (a report whose prior mtime is unknown is
            # unverifiable, and the unlink below is the primary protection).
            pass
        try:
            report_path.unlink()
        except OSError as e:
            stale_guard = True
            _LOG.warning(
                "could not unlink pre-existing report.json %s (%s); a "
                "stale-report mtime check will be applied",
                report_path,
                e,
            )

    # Stale-log-marker protection — the same re-used (workdir, run_id)
    # risk, but for the *terminal-class* triage markers: on a re-use, the
    # previous run's run_instance.log / test_output.txt survive (the
    # harness truncates run_instance.log only at logger setup and
    # overwrites test_output.txt only when the run reaches the eval leg).
    # A run that exits 0 with no report and dies before writing new log
    # files would otherwise be classified by the *previous* run's markers —
    # on terminal classes. Record the pre-run mtime per file:
    # absent key = the file did not exist pre-run (a post-run file is then
    # fresh by definition); False = it existed but the baseline is
    # unreadable (freshness unverifiable → treated as stale — the safe
    # direction, since a wrong terminal verdict is worse than a retried
    # infra class).
    pre_log_mtimes: dict[str, "int | bool"] = {}
    for _key in ("instance", "test_output"):
        _p = paths[_key]
        if _p.is_file():
            try:
                pre_log_mtimes[_key] = _p.stat().st_mtime_ns
            except OSError:
                pre_log_mtimes[_key] = False

    try:
        workdir_abs.mkdir(parents=True, exist_ok=True)
        predictions_path = _write_predictions_file(
            workdir_abs, instance_id, model_name_or_path, model_patch
        )
    except OSError as e:
        raise HarnessFailedError(
            f"pre-spawn filesystem failure (workdir mkdir / predictions write "
            f"under {workdir_abs}): {e!r}",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir_abs),
        ) from e

    argv = _harness_argv(
        python if python is not None else sys.executable,
        dataset,
        split,
        predictions_path,
        instance_id,
        run_id,
        timeout,
        workdir_abs,
    )
    runner = (
        harness_runner if harness_runner is not None else _default_harness_runner
    )
    _LOG.info(
        "spawning the harness run %s for %s (cwd %s; eval container "
        "sweb.eval.%s.%s; outer watchdog %s)",
        run_id,
        instance_id,
        workdir_abs,
        instance_id.lower(),
        run_id,
        overall_timeout if overall_timeout is not None else "none",
    )
    try:
        proc = runner(argv, str(workdir_abs), overall_timeout)
    except EvaluationError:
        raise  # a typed error an injected runner raises stays typed
    except subprocess.TimeoutExpired as e:
        # The outer watchdog fired: the harness process was killed, but the
        # docker-managed eval container may still be running — surface its
        # name for cleanup — and the partial output must not be discarded.
        raise HarnessFailedError(
            f"the outer watchdog fired after {overall_timeout}s (the eval "
            f"container sweb.eval.{instance_id.lower()}.{run_id} — possibly "
            "with a .{timestamp} 409-retry suffix — may still be running; "
            "'docker rm -f' it)",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_combined_tail(
                getattr(e, "output", None),
                getattr(e, "stderr", None),
            ),
            workdir=str(workdir_abs),
        ) from e
    except Exception as e:
        # a spawn failure (missing python → FileNotFoundError, fork/exec
        # OSError), a harness CLI crash on the host side, or an injected
        # runner failing — a harness/infra failure, typed (never a raw
        # traceback escape).
        raise HarnessFailedError(
            f"the harness runner failed: {e!r}",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_tail(str(e)),
            workdir=str(workdir_abs),
        ) from e
    exit_code = getattr(proc, "returncode", None)
    if not isinstance(exit_code, int):
        raise HarnessFailedError(
            f"the harness process object has no usable returncode "
            f"({exit_code!r})",
            instance_id=instance_id,
            log_paths=log_paths,
            workdir=str(workdir_abs),
        )
    stdout = getattr(proc, "stdout", None)
    stderr = getattr(proc, "stderr", None)

    # A non-zero exit is an error even when a report.json exists: the
    # harness writes the per-instance report before the final-report leg
    # (make_run_report), so a crash after the report (e.g. inside
    # make_run_report) must not be read as a completed run.
    if exit_code != 0:
        raise HarnessFailedError(
            f"the harness exited {exit_code}",
            instance_id=instance_id,
            log_paths=_existing_log_paths(paths),
            output_tail=_combined_tail(stdout, stderr),
            workdir=str(workdir_abs),
        )

    if report_path.is_file():
        if stale_guard:
            # The unlink failed, so a pre-existing file may be under
            # report_path: accept it only if the harness demonstrably
            # updated it (post-run mtime strictly after the pre-run mtime).
            # A missing pre-run mtime (stat failed) or an unreadable post-run
            # mtime makes freshness unverifiable → reject.
            fresh = False
            if pre_mtime_ns is not None:
                try:
                    fresh = report_path.stat().st_mtime_ns > pre_mtime_ns
                except OSError:
                    fresh = False
            if not fresh:
                raise HarnessFailedError(
                    "stale report.json — a pre-existing report for this "
                    "(workdir, run_id) could not be removed and was not "
                    "demonstrably updated by this run (the harness would "
                    "have skipped the instance — its exclude_completed "
                    "semantics; and run_instance itself early-returns a "
                    "pre-existing report, run_evaluation.py:272)",
                    instance_id=instance_id,
                    log_paths=log_paths,
                    workdir=str(workdir_abs),
                )
        return _result_from_report(
            instance_id=instance_id,
            paths=paths,
            workdir=workdir_abs,
            run_id=run_id,
            predictions_path=predictions_path,
        )

    raise _triage_no_report(
        instance_id=instance_id,
        paths=paths,
        log_paths=_existing_log_paths(paths),
        stdout=stdout,
        stderr=stderr,
        pre_log_mtimes=pre_log_mtimes,
        workdir=str(workdir_abs),
    )


# ---------------------------------------------------------------------------
# the operator CLI
# ---------------------------------------------------------------------------


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate one SWE-bench instance by delegating to the official "
            "swebench.harness.run_evaluation (WP4). Exit codes: 0 = ran "
            "(resolved or not) — the JSON result on stdout; 2 "
            "InvalidInputError; 3 PatchCorruptionError; 4 "
            "EvaluationTimeoutError; 5 other harness/infra failure (including "
            "an unexpected exception — the documented contract, not a "
            "traceback)."
        )
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument(
        "--patch-file",
        required=True,
        help="path to the model patch text (an empty file = the empty-patch "
        "negative control)",
    )
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--overall-timeout",
        type=float,
        default=None,
        help="outer watchdog in seconds (default: none — the harness's -t "
        "bounds only the in-container test exec)",
    )
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--python",
        default=None,
        help="interpreter with swebench installed (default: the current one)",
    )
    return parser


def main(
    argv: Optional[list[str]] = None,
    evaluate_fn: Optional[Callable[..., dict]] = None,
) -> int:
    """Operator CLI seam (the test injects ``evaluate_fn``). The exit code is
    the typed-error class (2/3/4/5); a resolved *and* an unresolved outcome
    both exit 0 — the JSON on stdout carries the outcome. An unexpected
    exception exits 5 (the documented "other" class), never a traceback with
    exit 1."""
    args = _build_cli_parser().parse_args(argv)
    fn = evaluate_fn if evaluate_fn is not None else evaluate
    try:
        patch_text = Path(args.patch_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # OSError covers FileNotFoundError / IsADirectoryError /
        # PermissionError; a non-UTF-8 patch file is a bad input, not a
        # crash.
        print(f"Invalid input: {e}", file=sys.stderr)
        return 2
    try:
        result = fn(
            args.instance_id,
            patch_text,
            model_name_or_path=args.model_name,
            dataset=args.dataset,
            split=args.split,
            workdir=args.workdir,
            run_id=args.run_id,
            timeout=args.timeout,
            overall_timeout=args.overall_timeout,
            python=args.python,
        )
    except InvalidInputError as e:
        print(str(e), file=sys.stderr)
        return 2
    except PatchCorruptionError as e:
        print(str(e), file=sys.stderr)
        return 3
    except EvaluationTimeoutError as e:
        print(str(e), file=sys.stderr)
        return 4
    except EvaluationError as e:
        print(str(e), file=sys.stderr)
        return 5
    except Exception as e:
        # the documented "5 = other harness/infra failure": an unexpected bug
        # must exit 5 typed, not a traceback with exit 1.
        print(f"Unexpected failure: {e!r}", file=sys.stderr)
        return 5
    try:
        print(json.dumps(result, indent=2, default=str))
    except (BrokenPipeError, OSError) as e:
        # the JSON is on stdout for a consumer (`… | head`): a broken pipe
        # is the consumer going away, not a failure of the run — the typed
        # message + exit 5 keeps the documented contract (never a traceback
        # with exit 1) while the result was already computed.
        print(f"Unexpected failure: {e!r}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
