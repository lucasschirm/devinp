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
artifact the harness writes lands under one per-instance directory, PLAN §5
WP2 "artifact atomicity"). After the process exits, ``evaluate`` reads *only*
the harness's own artifacts to resolve the outcome:

* ``report.json`` (present) → ``resolved`` is the harness's value — the
  harness is the sole source of the resolved flag (AC3).
* no ``report.json`` (the harness swallows per-instance failures and still
  exits 0) → triage on harness-written markers only: ``>>>>> Patch Apply
  Failed`` in ``run_instance.log`` → :class:`PatchCorruptionError` (the
  definitive corruption guard, AC4); the harness timeout marker in
  ``test_output.txt`` → :class:`EvaluationTimeoutError`; anything else →
  :class:`HarnessFailedError` (Docker/infra — the WP3 ``eval_error`` retry
  class, PLAN §2.5). No marker is re-interpreted as a grade.

**Fresh container (AC2).** The harness creates the evaluation container
itself — ``containers.create(image=<instance eval image>, name=
"sweb.eval.<instance_id>.<run_id>")`` — a brand-new container from the
published eval image, force-removing any pre-existing container with that
exact name first (ghost cleanup). The agent's container (named
``sweb.eval.<instance_id>`` by the adapter, PLAN §1; its working tree,
submodules and installed packages are contaminated by the run) is never
referenced: there is no parameter for it. The per-call unique ``run_id``
default makes the eval container name ``sweb.eval.<iid>.<run_id>`` structurally
distinct from the agent's name. The harness logs the created container's id
into ``run_instance.log`` (``Container for … created/started: <id>``);
``evaluate`` extracts it into ``logs.container`` so the isolation assertion
(eval container id ≠ agent container id, AC4) is checkable from the result.

**Empty patches (the negative control).** The harness itself filters
empty/``None`` ``model_patch`` predictions (``get_dataset_from_preds`` —
verified in the pinned source): the instance is skipped, no container is
created, and the final report counts it in ``empty_patch_ids`` (never in
``resolved_ids``) — official semantics: empty patch ⇒ not resolved.
``evaluate`` mirrors that exactly by short-circuiting to
``{"resolved": False, …}`` with ``empty_patch: True`` — the *semantics* are
the harness's; the short-circuit only saves the HF dataset download and the
Docker handshake on a guaranteed no-op.

**Corruption guard (AC4)** — two layers, both :class:`PatchCorruptionError`:
(1) a zero-cost pre-check — a non-empty patch must carry unified-diff file
headers (``--- `` / ``+++ ``), so garbage fails clean *before* any dataset
download or Docker work; (2) the definitive guard — a truncated patch that is
still structurally well-formed reaches the harness, whose 4-command git-apply
chain then fails and writes its own ``APPLY_PATCH_FAIL`` marker, which
``evaluate`` converts into the typed error (a clean typed error, never a
traceback escape).

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
  return contract below; ``resolved`` is only ever the harness's
  ``report.json`` value or the documented harness empty-patch semantics.
* **AC4 — positive / negative / corruption / isolation controls** → the
  integration tests in ``tests/test_evaluate.py`` (CI, public images, zero
  quota) + the unit-level layers above.

Return contract (AC3):

    {
      "resolved": bool,
      "logs": {
        "instance":     str | None,   # harness run_instance.log (path)
        "test_output":  str | None,   # harness test_output.txt (path)
        "report":       dict | None,  # harness report.json, verbatim
        "final_report": str | None,   # harness final run report (path)
        "container":    str | None,   # id of the container the harness created
        "empty_patch":  bool,         # short-circuit marker (see above)
      },
    }

Paths, not contents: the WP5 report / WP3 checkpoint consume artifact paths;
embedding multi-MB test output in memory is not the contract. Errors carry
``instance_id``, the log paths, and a tail of the relevant harness output.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

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
    "evaluate",
    "main",
]

# Official harness defaults (WP0-verified CLI, issue #10). The dataset is the
# Verified split — the benchmark this adapter exists for (PLAN.md goal).
DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_SPLIT = "test"
DEFAULT_MODEL_NAME = "devin"
DEFAULT_TIMEOUT_S = 1800  # the harness's own per-instance test timeout (-t)

# Harness log layout — verified against the pinned swebench==5.0.2 source
# (harness/constants/__init__.py: RUN_EVALUATION_LOG_DIR, LOG_REPORT,
# LOG_INSTANCE, LOG_TEST_OUTPUT; reporting.py: the final-report filename
# ``<model>.<run_id>.json``). The bench package pins swebench==5.0.2 exactly,
# so this layout is stable by pin.
_HARNESS_LOG_ROOT = Path("logs") / "run_evaluation"
_HARNESS_LOG_REPORT = "report.json"
_HARNESS_LOG_INSTANCE = "run_instance.log"
_HARNESS_LOG_TEST_OUTPUT = "test_output.txt"

# Harness-written markers (swebench 5.0.2 harness/constants) — triage reads
# only these; nothing is re-graded.
_APPLY_PATCH_FAIL_MARKER = ">>>>> Patch Apply Failed"
_TIMEOUT_MARKERS = ("Timeout error:", ">>>>> Tests Timed Out", "Test timed out after")

# A harness subprocess is the only I/O this module does; this tail is carried
# in typed errors so an operator can triage without re-reading the log files.
_ERROR_TAIL_CHARS = 4000


class EvaluationError(Exception):
    """Base of the evaluate.py error hierarchy — a clean typed error, never a
    traceback escape. Carries the instance, the harness log paths (where
    applicable) and a tail of the relevant harness output."""

    def __init__(
        self,
        message: str,
        *,
        instance_id: Optional[str] = None,
        log_paths: Optional[dict[str, Optional[str]]] = None,
        output_tail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.instance_id = instance_id
        self.log_paths = dict(log_paths or {})
        self.output_tail = output_tail

    def __str__(self) -> str:
        parts = [f"Error in evaluation for {self.instance_id}: {self.message}"
                 if self.instance_id else self.message]
        if self.log_paths:
            shown = ", ".join(f"{k}={v}" for k, v in sorted(self.log_paths.items()) if v)
            if shown:
                parts.append(f"Check ({shown}) for more information.")
        if self.output_tail:
            parts.append(self.output_tail)
        return "\n".join(parts)


class InvalidInputError(EvaluationError):
    """The call's inputs are not evaluable (bad instance id / non-str patch /
    bad run_id / bad timeout) — raised before any harness work happens."""


class PatchCorruptionError(EvaluationError):
    """The model patch is corrupt (no unified-diff structure — pre-check; or
    the harness's own git-apply chain failed and wrote its APPLY_PATCH_FAIL
    marker — definitive). A clean typed error for a truncated/garbage patch,
    never a crash (AC4 corruption guard)."""


class EvaluationTimeoutError(EvaluationError):
    """The harness's per-instance test timeout (-t) fired — the harness wrote
    its timeout marker; the instance is not graded."""


class HarnessFailedError(EvaluationError):
    """The harness process itself failed (non-zero exit — e.g. unknown
    instance id, dataset fetch failure, unreachable Docker daemon) or finished
    without a report and without a known marker (Docker/infra failure — the
    WP3 ``eval_error`` retry class, PLAN §2.5)."""


def _tail(text: Optional[str], limit: int = _ERROR_TAIL_CHARS) -> Optional[str]:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text[-limit:] if len(text) > limit else text


def _validate_inputs(
    instance_id: Any,
    model_patch: Any,
    *,
    run_id: Optional[str],
    model_name_or_path: str,
    timeout: Any,
) -> None:
    """AC1/AC4 pre-harness validation — typed, zero-cost, no side effects.

    Raises :class:`InvalidInputError` (never a raw ``TypeError``) so a runner
    bug surfaces as a typed refusal before any dataset download or Docker
    work happens.
    """
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise InvalidInputError(
            f"instance_id must be a non-empty str, got {instance_id!r}"
        )
    if model_patch is not None and not isinstance(model_patch, str):
        raise InvalidInputError(
            f"model_patch must be a str (or None for an empty patch), "
            f"got {type(model_patch).__name__}"
        )
    if run_id is not None and (
        not isinstance(run_id, str) or not run_id.strip() or " " in run_id
    ):
        raise InvalidInputError(
            "run_id must be a non-empty str without whitespace (it becomes the "
            f"eval container name suffix), got {run_id!r}"
        )
    if not isinstance(model_name_or_path, str) or not model_name_or_path:
        raise InvalidInputError(
            f"model_name_or_path must be a non-empty str, got {model_name_or_path!r}"
        )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise InvalidInputError(
            f"timeout must be a positive int (seconds), got {timeout!r}"
        )


def _check_patch_structure(
    model_patch: str, *, instance_id: str
) -> None:
    """Corruption guard, layer 1 (zero-cost, pre-harness): a non-empty patch
    must carry unified-diff file headers. A truncated patch that lost its
    ``+++`` line, or non-patch garbage, fails clean before any dataset
    download or Docker work. (A truncation that still leaves both headers is
    structurally well-formed and reaches the harness — its git-apply chain is
    the *definitive* guard, layer 2.)"""
    has_minus = re.search(r"^--- \S", model_patch, re.MULTILINE)
    has_plus = re.search(r"^\+\+\+ \S", model_patch, re.MULTILINE)
    if not (has_minus and has_plus):
        raise PatchCorruptionError(
            "model_patch is not a unified diff (missing '--- ' / '+++ ' file "
            f"headers) — truncated or not a patch at all",
            instance_id=instance_id,
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
    per-instance test timeout, ``--report_dir workdir`` (the final report
    lands beside the per-instance logs)."""
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


def _default_harness_runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess:
    """The real delegation: spawn the official harness CLI. ``cwd = workdir``
    is load-bearing — the harness's log root is CWD-relative. No outer
    watchdog: the per-instance test timeout is the harness's own ``-t`` (and
    its Docker client timeout), not ours to re-implement."""
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=False
    )


def _model_dir(model_name_or_path: str) -> str:
    """The harness's log-dir / report-name escape (``/`` → ``__``)."""
    return model_name_or_path.replace("/", "__")


def _instance_paths(
    workdir: Path, run_id: str, model_name_or_path: str, instance_id: str
) -> dict[str, Path]:
    """The harness's CWD-relative artifact layout (fact 2 of PLAN-WP4.md)."""
    base = workdir / _HARNESS_LOG_ROOT / run_id / _model_dir(model_name_or_path) / instance_id
    return {
        "report": base / _HARNESS_LOG_REPORT,
        "instance": base / _HARNESS_LOG_INSTANCE,
        "test_output": base / _HARNESS_LOG_TEST_OUTPUT,
        "final_report": workdir / f"{_model_dir(model_name_or_path)}.{run_id}.json",
    }


def _extract_container_id(log_text: str, instance_id: str) -> Optional[str]:
    """The id of the container *the harness created* — from the harness's own
    log line (``Container for <iid> created/started: <id>``). The source of
    truth for the isolation assertion (AC4) is the harness, not us. Absent
    log / no line → None (never a crash)."""
    pattern = re.compile(
        r"Container for " + re.escape(instance_id) + r" (?:created|started): (\S+)"
    )
    matches = pattern.findall(log_text)
    return matches[-1] if matches else None


def _read_log(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _triage_no_report(
    *,
    instance_id: str,
    paths: dict[str, Path],
    exit_code: int,
    stdout: Optional[str],
    stderr: Optional[str],
) -> EvaluationError:
    """A harness run that produced no report.json: with a non-zero exit the
    harness itself failed (typed with the stderr tail); with an exit-0 the
    harness swallowed a per-instance failure (it prints, it does not raise).
    Triage on harness-written markers only — no re-grading:

    * ``APPLY_PATCH_FAIL`` in run_instance.log → the harness's own git-apply
      chain failed → :class:`PatchCorruptionError` (definitive corruption
      guard, AC4).
    * a timeout marker in test_output.txt (or the instance log) →
      :class:`EvaluationTimeoutError`.
    * anything else → :class:`HarnessFailedError` (Docker/infra — the WP3
      ``eval_error`` retry class).
    """
    log_paths = {k: (str(p) if p.is_file() else None) for k, p in paths.items()}
    tail = _tail(stderr or stdout)

    if exit_code != 0:
        return HarnessFailedError(
            f"harness exited {exit_code}",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=tail,
        )

    instance_log = _read_log(paths["instance"]) or ""
    if _APPLY_PATCH_FAIL_MARKER in instance_log:
        return PatchCorruptionError(
            "the harness's git-apply chain rejected model_patch "
            f"({_APPLY_PATCH_FAIL_MARKER} in the instance log) — corrupt/truncated "
            "patch; see the harness output tail for the git-apply diagnostics",
            instance_id=instance_id,
            log_paths=log_paths,
            output_tail=_tail(instance_log),
        )

    test_output = _read_log(paths["test_output"]) or ""
    for marker in _TIMEOUT_MARKERS:
        if marker in test_output or marker in instance_log:
            return EvaluationTimeoutError(
                f"the harness test run timed out ({marker!r} marker)",
                instance_id=instance_id,
                log_paths=log_paths,
                output_tail=_tail(test_output or instance_log),
            )

    return HarnessFailedError(
        "the harness finished without a report and without a known failure "
        "marker — Docker/infra failure (the WP3 'eval_error' retry class)",
        instance_id=instance_id,
        log_paths=log_paths,
        output_tail=tail,
    )


def evaluate(
    instance_id: str,
    model_patch: Optional[str],
    *,
    model_name_or_path: str = DEFAULT_MODEL_NAME,
    dataset: str = DEFAULT_DATASET,
    split: str = DEFAULT_SPLIT,
    workdir: "Path | str | None" = None,
    run_id: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    python: Optional[str] = None,
    harness_runner: Optional[Callable[[list[str], str], Any]] = None,
) -> dict:
    """Evaluate one instance by delegating *entirely* to the official
    SWE-bench harness (AC1) in a fresh container (AC2), returning
    ``{"resolved", "logs"}`` (AC3). See the module docstring for the full
    contract; the flow is:

    1. typed validation (zero-cost; :class:`InvalidInputError`),
    2. the harness's own empty-patch semantics (short-circuit — the negative
       control, no harness spawn),
    3. the corruption pre-check (:class:`PatchCorruptionError`, layer 1),
    4. write the standard predictions file + spawn the official CLI
       (``cwd = workdir``),
    5. read only what the harness wrote: ``report.json`` → ``resolved``; no
       report → marker triage (typed error); the container id from the
       harness's own log.

    ``workdir`` is the per-instance artifact directory (the harness cwd);
    with None a fresh ``swebench-eval-…`` directory is created and the
    *caller* owns its cleanup (the artifacts are the point). ``run_id``
    defaults to a per-call unique id so the eval container name
    (``sweb.eval.<iid>.<run_id>``) is always distinct from the agent
    container's (``sweb.eval.<iid>``) — AC2. ``harness_runner`` is the test
    injection seam: a ``fn(argv, cwd) -> CompletedProcess-like`` replacing the
    real subprocess.
    """
    _validate_inputs(
        instance_id,
        model_patch,
        run_id=run_id,
        model_name_or_path=model_name_or_path,
        timeout=timeout,
    )

    # AC4 negative control — the harness's *own* filter semantics (verified in
    # the pinned source, get_dataset_from_preds): an empty/None patch is
    # skipped by the harness (no container, no report, counted in
    # empty_patch_ids, never in resolved_ids). Mirror that exactly — no
    # harness spawn, no dataset download, no Docker.
    if model_patch is None or model_patch == "":
        return {
            "resolved": False,
            "logs": {
                "instance": None,
                "test_output": None,
                "report": None,
                "final_report": None,
                "container": None,
                "empty_patch": True,
            },
        }

    # AC4 corruption guard, layer 1 (zero-cost, pre-harness).
    _check_patch_structure(model_patch, instance_id=instance_id)

    if run_id is None:
        # Per-call unique → the eval container name is always fresh and can
        # never collide with the agent container's (AC2).
        run_id = f"eval-{instance_id}-{uuid.uuid4().hex[:8]}"
    if workdir is None:
        workdir = Path(tempfile.mkdtemp(prefix=f"swebench-eval-{instance_id}-"))
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    predictions_path = _write_predictions_file(
        workdir, instance_id, model_name_or_path, model_patch
    )
    argv = _harness_argv(
        python if python is not None else sys.executable,
        dataset,
        split,
        predictions_path,
        instance_id,
        run_id,
        timeout,
        workdir,
    )

    runner = harness_runner if harness_runner is not None else _default_harness_runner
    proc = runner(argv, str(workdir))
    exit_code = proc.returncode
    stdout = getattr(proc, "stdout", None)
    stderr = getattr(proc, "stderr", None)

    paths = _instance_paths(workdir, run_id, model_name_or_path, instance_id)
    log_paths = {k: (str(p) if p.is_file() else None) for k, p in paths.items()}

    report_path = paths["report"]
    if report_path.is_file():
        # AC3: the harness is the sole source of the resolved flag.
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise HarnessFailedError(
                f"the harness report.json exists but is unreadable ({e}) — "
                "harness/infra failure",
                instance_id=instance_id,
                log_paths=log_paths,
            ) from e
        entry = report.get(instance_id)
        if not isinstance(entry, dict) or "resolved" not in entry:
            raise HarnessFailedError(
                "the harness report.json lacks the instance entry — "
                "harness/infra failure",
                instance_id=instance_id,
                log_paths=log_paths,
            )
        instance_log = _read_log(paths["instance"]) or ""
        return {
            "resolved": bool(entry["resolved"]),
            "logs": {
                "instance": log_paths["instance"],
                "test_output": log_paths["test_output"],
                "report": report,
                "final_report": log_paths["final_report"],
                "container": _extract_container_id(instance_log, instance_id),
                "empty_patch": False,
            },
        }

    raise _triage_no_report(
        instance_id=instance_id,
        paths=paths,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate one SWE-bench instance by delegating to the official "
            "swebench.harness.run_evaluation (WP4). Exit codes: 0 resolved or "
            "unresolved (the JSON result on stdout), 2 InvalidInputError, 3 "
            "PatchCorruptionError, 4 EvaluationTimeoutError, 5 other "
            "harness/infra failure."
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
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--python", default=None, help="interpreter with swebench installed (default: the current one)")
    return parser


def main(
    argv: Optional[list[str]] = None,
    evaluate_fn: Optional[Callable[..., dict]] = None,
) -> int:
    """Operator CLI seam (the test injects ``evaluate_fn``). The exit code is
    the typed-error class (2/3/4/5); a resolved *and* an unresolved outcome
    both exit 0 — the JSON on stdout carries the outcome."""
    args = _build_cli_parser().parse_args(argv)
    fn = evaluate_fn if evaluate_fn is not None else evaluate
    try:
        patch_text = Path(args.patch_file).read_text(encoding="utf-8")
        result = fn(
            args.instance_id,
            patch_text,
            model_name_or_path=args.model_name,
            dataset=args.dataset,
            split=args.split,
            workdir=args.workdir,
            run_id=args.run_id,
            timeout=args.timeout,
            python=args.python,
        )
    except FileNotFoundError as e:
        print(f"Invalid input: {e}", file=sys.stderr)
        return 2
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
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
