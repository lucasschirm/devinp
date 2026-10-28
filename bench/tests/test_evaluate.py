"""WP4 (issue #17) — ``evaluate.py``: the thin delegate to the official
SWE-bench harness.

All unit tests run with **zero quota, no Docker and no network**: the harness
subprocess is replaced by an injected ``harness_runner`` (the module's
documented test seam), and the harness's CWD-relative artifact layout is
fabricated on disk (the exact ``logs/run_evaluation/<run_id>/<model>/<iid>/``
tree the pinned swebench 5.0.2 writes). The acceptance controls that need a
container runtime (positive / corruption / isolation) are the two
integration tests at the bottom (isolation is asserted inside the positive
control) — CI (public images, no quota); they auto-skip where no Docker
daemon exists (e.g. this dev workspace).

Acceptance mapping (issue #17, ``bench/PLAN-WP4.md``):

* **AC1 — delegates entirely to the official CLI on a standard predictions
  file** → ``test_harness_argv_is_exactly_the_official_cli`` (the argv is
  *pinned* — any bespoke flag breaks it),
  ``test_harness_runs_with_cwd_as_workdir`` (the harness's CWD-relative log
  layout), ``test_predictions_file_is_exactly_the_standard_format``
  (exactly ``instance_id`` / ``model_name_or_path`` / ``model_patch``),
  ``test_no_swebench_import_in_evaluate_module`` (no bespoke apply/grade
  logic — the subprocess is the only harness interaction).
* **AC2 — fresh container, never the agent's mutated one** →
  ``test_default_run_id_is_unique_per_call`` (per-call unique run id),
  ``test_eval_container_name_never_equals_agent_container_name`` (the
  harness container-name formula with our run id vs the adapter's agent
  name), ``test_integration_positive_control`` (the eval container id comes
  from the harness's own log — the only container the module can name).
* **AC3 — returns ``{resolved, logs}``, no bespoke grading** →
  ``test_report_json_is_the_sole_source_of_resolved`` (true and false
  passthrough of the harness value), ``test_return_contract_shape`` (the
  exact key set), ``test_container_id_extracted_from_harness_log``
  (``logs.container`` from the harness's own line; absent → None, never a
  crash).
* **AC4 — the controls:**
  * negative control (no-op/empty patch → false, docker-free — runs in CI
    and locally): ``test_negative_control_no_op_patch_is_unresolved`` (``""``
    and ``None``; **no harness spawn** — the harness's own empty-patch
    filter semantics, verified in the pinned source; validation still
    precedes the short-circuit —
    ``test_empty_patch_with_invalid_input_is_invalid_input``);
  * corruption guard (the harness's own 4-command git-apply chain is the
    sole authority on applicability — no structural pre-check, so every
    non-empty patch reaches the harness; the chain's ``APPLY_PATCH_FAIL``
    marker becomes a clean typed error):
    ``test_nonempty_patch_always_reaches_harness_and_apply_fail_is_typed``
    (header-less garbage — the forms a header check would have wrongly
    rejected) and
    ``test_truncated_wellformed_patch_reaches_harness_and_is_corruption``
    (unit, fabricated tree) and
    ``test_integration_corruption_guard_truncated_gold_patch`` (real
    harness, real container);
  * triage edges (fabricated trees — markers by the file the pinned harness
    writes / the test output it grades): ``test_no_report_timeout_marker_is_evaluation_timeout``
    (all markers × the file placement),
    ``test_timeout_marker_in_wrong_file_is_not_terminal_timeout`` (anchored
    + file-specific), ``test_apply_fail_and_timeout_markers_corruption_wins``,
    ``test_no_report_no_marker_is_infra_error``,
    ``test_exit_nonzero_is_harness_failed_with_stderr_tail`` (the combined
    stdout + stderr tail), ``test_report_present_but_exit_nonzero_is_harness_failed``
    (the exit-≠ 0-always-error rule),
    ``test_exit_nonzero_with_apply_fail_marker_is_harness_failed``,
    ``test_stale_report_surviving_unlink_failure_is_not_trusted`` (the
    stale-report protection: the harness's exclude_completed would skip the
    instance; the pre-run report is unlinked / mtime-checked),
    ``test_preexisting_report_is_unlinked_and_not_trusted``,
    ``test_unreadable_report_is_harness_failed``,
    ``test_report_missing_instance_entry_is_harness_failed``,
    ``test_non_bool_resolved_verdict_is_harness_failed`` (AC3 — no
    ``bool()`` coercion of a malformed verdict),
    ``test_infra_failure_report_is_harness_failed`` (the harness's own
    environment-fault classification → the WP3 retry class, not a terminal
    verdict),
    ``test_stale_log_marker_is_not_a_terminal_verdict`` (a marker that
    survived from a previous run — the file was not updated by this run —
    is the retryable class, never a terminal verdict);
  * validation matrix (typed, before any side effect — the tests also assert
    zero filesystem side effects): ``test_input_validation_matrix``
    (instance id charset / patch / run_id charset / model name / timeout),
    ``test_extra_input_validation_matrix`` (dataset / split / workdir /
    python / harness_runner / overall_timeout — incl. nan/inf and NUL
    argv-carrying strings);
  * the delegation's robustness edges: ``test_relative_workdir_is_resolved_to_absolute_before_spawn``,
    ``test_workdir_none_creates_tempdir_and_surfaces_it`` (the
    caller-owned-cleanup contract), ``test_pre_spawn_filesystem_failure_is_typed``,
    ``test_runner_spawn_failure_is_typed_not_raw``,
    ``test_timeout_expired_from_runner_is_typed_with_partial_output`` (the
    outer watchdog — surfaces the eval container name + the partial
    output), ``test_overall_timeout_flows_to_the_runner``,
    ``test_injected_runner_typed_error_stays_typed``,
    ``test_process_object_without_returncode_is_typed``,
    ``test_non_object_report_is_harness_failed``,
    ``test_non_utf8_report_is_harness_failed``,
    ``test_default_harness_runner_watchdog_fires`` (the real runner's
    ``overall_timeout`` wiring),
    ``test_tail_decodes_bytes_readably`` (a bytes partial output is decoded
    readably, never a ``b'…'`` repr),
    ``test_auto_tempdir_is_surfaced_on_typed_failure`` (the auto tempdir is
    reachable via ``error.workdir`` on failure paths — the caller-owned
    cleanup stays reachable),
    ``test_pathlike_workdir_is_validated_like_a_str`` (an empty path-like
    would resolve to the caller's CWD — refused; a NUL-carrying PurePath
    is the same malformed input as the str case);
  * isolation: asserted inside ``test_integration_positive_control`` (eval
    container id ≠ agent container id; the eval container id is a real
    container id from the harness's own log).

Plus the operator CLI: ``test_cli_*`` (exit codes 0/2/3/4/5 — 0 = ran,
resolved or not — JSON on stdout; an unexpected exception is exit 5, the
documented contract, not a traceback).

The two Docker-gated integration tests carry a ``pytest.mark.timeout``
watchdog (a wedged pull must fail the test, not hang the job).
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePath
from typing import Callable, Optional

import pytest

import evaluate as ev

# a shared well-formed patch for the unit tests (small, deterministic)
_WELLFORMED_PATCH = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
)

# ---------------------------------------------------------------------------
# fakes — the injected harness_runner seam (documented in evaluate.evaluate)
# ---------------------------------------------------------------------------


class FakeHarness:
    """The injected harness_runner (seam: ``fn(argv, cwd, overall_timeout)``):
    records the call; optionally builds a fabricated harness tree under cwd
    (the harness's CWD-relative layout); returns a real
    ``subprocess.CompletedProcess`` — the type ``evaluate()`` actually
    consumes."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        tree: Optional[Callable[[Path], None]] = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.tree = tree
        self.calls: list[tuple[list[str], str, object]] = []

    def __call__(
        self, argv: list[str], cwd: str, overall_timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        self.calls.append((list(argv), cwd, overall_timeout))
        if self.tree is not None:
            self.tree(Path(cwd))
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


def _build_tree(
    cwd: Path,
    run_id: str,
    model: str,
    instance_id: str,
    *,
    report: Optional[dict] = None,
    instance_log: Optional[str] = None,
    test_output: Optional[str] = None,
    final_report: bool = False,
) -> None:
    """Fabricate the harness's exact CWD-relative artifact layout (the pinned
    swebench 5.0.2 log root / filenames; the final report is the pin's
    ``<report_dir>/<model>.<run_id>.json``)."""
    base = (
        cwd / "logs" / "run_evaluation" / run_id / model.replace("/", "__") / instance_id
    )
    base.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (base / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if instance_log is not None:
        (base / "run_instance.log").write_text(instance_log, encoding="utf-8")
    if test_output is not None:
        (base / "test_output.txt").write_text(test_output, encoding="utf-8")
    if final_report:
        (cwd / f"{model.replace('/', '__')}.{run_id}.json").write_text(
            json.dumps({"run_id": run_id}), encoding="utf-8"
        )


def _report_tree(
    run_id: str,
    model: str,
    instance_id: str,
    *,
    resolved: bool,
    instance_log: Optional[str] = None,
    test_output: Optional[str] = None,
) -> Callable[[Path], None]:
    return lambda cwd: _build_tree(
        cwd,
        run_id,
        model,
        instance_id,
        report={instance_id: {"resolved": resolved, "patch_successfully_applied": True}},
        instance_log=instance_log,
        test_output=test_output,
    )


# ---------------------------------------------------------------------------
# AC1 — the delegation is exactly the official CLI, on a standard predictions
# file, and the module imports no swebench at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("python", "dataset", "split"),
    [
        (None, None, None),  # defaults
        ("/usr/bin/custom-python", "org/dataset", "custom-split"),  # good values flow
    ],
)
def test_harness_argv_is_exactly_the_official_cli(tmp_path, python, dataset, split):
    """The argv is *pinned* to the official CLI (WP0-verified): any bespoke
    flag — or a missing official one — breaks this test, and good
    python/dataset/split values actually flow (the validation matrix proves
    bad ones are refused). AC1."""
    fake = FakeHarness(tree=_report_tree("rid1", "devin", "pallets__flask-5014", resolved=True))
    ev.evaluate(
        "pallets__flask-5014",
        _WELLFORMED_PATCH,
        dataset=dataset or ev.DEFAULT_DATASET,
        split=split or ev.DEFAULT_SPLIT,
        workdir=tmp_path,
        run_id="rid1",
        timeout=1234,
        python=python,
        harness_runner=fake,
    )
    (argv, _, _), = fake.calls
    expected_python = python if python is not None else sys.executable
    assert argv[0] == expected_python  # the interpreter with swebench installed
    assert argv[1:3] == ["-m", "swebench.harness.run_evaluation"]
    flags = dict(zip(argv[3::2], argv[4::2]))
    assert flags["-d"] == (dataset if dataset is not None else ev.DEFAULT_DATASET)
    assert flags["-s"] == (split if split is not None else ev.DEFAULT_SPLIT)
    assert flags["-p"].endswith("predictions.jsonl")
    assert flags["-i"] == "pallets__flask-5014"
    assert flags["-id"] == "rid1"
    assert flags["--max_workers"] == "1"
    assert flags["-t"] == "1234"
    assert flags["--report_dir"] == str(tmp_path)
    # exactly the official flag set — nothing of our own
    assert set(flags) == {"-d", "-s", "-p", "-i", "-id", "--max_workers", "-t", "--report_dir"}


def test_harness_runs_with_cwd_as_workdir(tmp_path):
    """The harness's log root is CWD-relative (logs/run_evaluation/…) — the
    run must happen with cwd = workdir or the artifacts land elsewhere."""
    fake = FakeHarness(tree=_report_tree("rid2", "devin", "iid", resolved=False))
    ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid2", harness_runner=fake)
    assert fake.calls[0][1] == str(tmp_path)


def test_predictions_file_is_exactly_the_standard_format(tmp_path):
    """WP0-verified input format: one JSONL line with *exactly* the three
    standard fields — nothing for the harness to ignore, nothing of ours for
    it to choke on."""
    fake = FakeHarness(tree=_report_tree("rid3", "m/with__slash", "iid", resolved=False))
    ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        model_name_or_path="m/with__slash",
        workdir=tmp_path,
        run_id="rid3",
        harness_runner=fake,
    )
    line = (tmp_path / "predictions.jsonl").read_text(encoding="utf-8")
    assert line.count("\n") == 1  # one JSONL line
    pred = json.loads(line)
    assert set(pred) == {"instance_id", "model_name_or_path", "model_patch"}
    assert pred == {
        "instance_id": "iid",
        "model_name_or_path": "m/with__slash",
        "model_patch": _WELLFORMED_PATCH,
    }


def test_no_swebench_import_in_evaluate_module():
    """AC1/AC3 — "no bespoke apply/grade logic inside": the subprocess is the
    *only* harness interaction, so the module must not import swebench (a
    direct import would be the seam for re-implementing the harness's
    apply/grade path). AST-based — a string match would also flag a string
    literal mentioning the import."""
    import ast

    tree = ast.parse(inspect.getsource(ev))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("swebench"), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("swebench"), node.module


def test_model_name_slash_is_escaped_like_the_harness(tmp_path):
    """The harness escapes ``/`` → ``__`` in the log dir / report name; our
    path derivation must match or the report is never found. (The final
    report is the pin's ``<report_dir>/<model>.<run_id>.json``;
    ``--report_dir = workdir``.)"""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid4", "devin/swe-2-high", "iid",
            report={"iid": {"resolved": True}}, final_report=True,
        )
    )
    result = ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        model_name_or_path="devin/swe-2-high",
        workdir=tmp_path,
        run_id="rid4",
        harness_runner=fake,
    )
    assert result["resolved"] is True  # the report was only found under devin__swe-2-high/
    assert result["logs"]["final_report"] == str(tmp_path / "devin__swe-2-high.rid4.json")
    assert result["logs"]["report"] is not None


# ---------------------------------------------------------------------------
# AC2 — fresh container: per-call unique run id ⇒ the eval container name can
# never equal the agent container's
# ---------------------------------------------------------------------------


def test_default_run_id_is_unique_per_call(tmp_path):
    """With no explicit run_id the default is per-call unique (AC2) — the
    argv is captured even though the run "fails" (no report fabricated for
    the random run id; the failure is the point of no consequence here)."""
    run_ids = []
    for _ in range(2):
        fake = FakeHarness()  # no tree: the random run id has no report → infra error
        with pytest.raises(ev.HarnessFailedError):
            ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, harness_runner=fake)
        argv, _, _ = fake.calls[0]
        run_ids.append(argv[argv.index("-id") + 1])
    assert run_ids[0] != run_ids[1]  # per-call unique (AC2)
    assert run_ids[0].startswith("eval-iid-")


def test_eval_container_name_never_equals_agent_container_name(tmp_path):
    """The harness names the eval container ``sweb.eval.<iid>.<run_id>``
    (verified in the pinned source, create_container); the adapter names the
    agent container ``sweb.eval.<iid>`` (PLAN §1). The guarantee lives in
    production code — the validated, per-call unique ``run_id`` flows into
    the name — so this test asserts it against the run id values the
    module *actually emits* (not against strings the test constructs)."""
    instance_id = "pallets__flask-5014"
    agent_name = f"sweb.eval.{instance_id}"
    # the default (unique, production-emitted) run id
    for _ in range(2):
        fake = FakeHarness()  # no tree: the random run id has no report → infra error
        with pytest.raises(ev.HarnessFailedError):
            ev.evaluate(instance_id, _WELLFORMED_PATCH, workdir=tmp_path, harness_runner=fake)
        argv, _, _ = fake.calls[0]
        run_id = argv[argv.index("-id") + 1]
        assert run_id  # non-empty — the whole name separation rests on this
        eval_name = f"sweb.eval.{instance_id.lower()}.{run_id}"
        assert eval_name != agent_name
        assert eval_name.endswith(f".{run_id}")  # the suffix the agent name lacks
    # an explicit run id: same guarantee, value taken from the emitted argv
    fake = FakeHarness(tree=_report_tree("rid-custom", "devin", instance_id, resolved=False))
    ev.evaluate(instance_id, _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid-custom", harness_runner=fake)
    argv, _, _ = fake.calls[0]
    run_id = argv[argv.index("-id") + 1]
    assert f"sweb.eval.{instance_id.lower()}.{run_id}" != agent_name


# ---------------------------------------------------------------------------
# AC3 — the {resolved, logs} contract
# ---------------------------------------------------------------------------


def test_report_json_is_the_sole_source_of_resolved(tmp_path):
    """resolved is only ever the harness's report.json value — true and
    false passthrough, no re-grading."""
    for expected in (True, False):
        fake = FakeHarness(
            tree=_report_tree(
                "rid",
                "devin",
                "iid",
                resolved=expected,
                instance_log="Container for iid created: abc123\nContainer for iid started: abc123\n",
            )
        )
        result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
        assert result["resolved"] is expected
        assert result["logs"]["report"]["iid"]["resolved"] is expected  # verbatim


def test_return_contract_shape(tmp_path):
    """The key set of both halves is pinned (the TypedDict contract —
    key-drift is unrepresentable, but the literals are pinned too, for
    both the full and the empty-patch paths — the latter in
    :func:`test_negative_control_no_op_patch_is_unresolved`). The run-identity
    values are surfaced (a caller-created tempdir must be reachable for
    cleanup; a failure must be debuggable)."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            report={"iid": {"resolved": False}},
            instance_log="Container for iid created: abc\n",
            test_output="ok\n",
            final_report=True,
        )
    )
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert set(result) == {"resolved", "logs"}
    assert set(result["logs"]) == {
        "instance",
        "test_output",
        "report",
        "final_report",
        "container",
        "workdir",
        "run_id",
        "predictions",
        "empty_patch",
    }
    assert result["logs"]["empty_patch"] is False
    # run identity — surfaced, never unreachable
    assert result["logs"]["workdir"] == str(tmp_path)
    assert result["logs"]["run_id"] == "rid"
    assert result["logs"]["predictions"] == str(tmp_path / "predictions.jsonl")
    # the harness artifact paths are asserted *verbatim* (a swapped key or a
    # wrong final-report filename formula must not pass silently)
    assert result["logs"]["instance"] == str(
        tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "run_instance.log"
    )
    assert result["logs"]["test_output"] == str(
        tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "test_output.txt"
    )
    assert result["logs"]["final_report"] == str(tmp_path / "devin.rid.json")


def test_container_id_extracted_from_harness_log(tmp_path):
    """logs.container is the id from the harness's *own* log line (the
    source of truth for the AC4 isolation assertion); absent log → None,
    never a crash."""
    fake = FakeHarness(
        tree=_report_tree("rid", "devin", "iid", resolved=True, instance_log="Container for iid created: 0123456789abcdef\nContainer for iid started: 0123456789abcdef\n")
    )
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert result["logs"]["container"] == "0123456789abcdef"

    # fresh workdir — the second part must not see the first part's log file
    fake = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=True))
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path / "b", run_id="rid", harness_runner=fake)
    assert result["logs"]["container"] is None


# ---------------------------------------------------------------------------
# AC4 — the controls: negative (docker-free), corruption (both layers)
# ---------------------------------------------------------------------------


def test_negative_control_no_op_patch_is_unresolved(tmp_path):
    """Empty / None patch → resolved False, empty_patch True, and **no
    harness spawn** — the harness's own filter semantics (verified in the
    pinned source: get_dataset_from_preds skips empty patches, no container,
    counted in empty_patch_ids, never in resolved_ids). Runs without Docker
    (CI and locally)."""
    for patch in ("", None):
        fake = FakeHarness()  # must not be called (records calls; asserted below)
        result = ev.evaluate("iid", patch, workdir=tmp_path, harness_runner=fake)
        assert result["resolved"] is False
        assert result["logs"]["empty_patch"] is True
        assert result["logs"]["container"] is None
        # the empty-patch literal carries the *same* key set (no run identity
        # — no tempdir / run id / predictions file were created)
        assert set(result["logs"]) == {
            "instance", "test_output", "report", "final_report", "container",
            "workdir", "run_id", "predictions", "empty_patch",
        }
        assert result["logs"]["workdir"] is None
        assert result["logs"]["run_id"] is None
        assert result["logs"]["predictions"] is None
        assert fake.calls == []  # no dataset download, no Docker


def test_nonempty_patch_always_reaches_harness_and_apply_fail_is_typed(tmp_path):
    """No structural pre-check is made (deliberate): the harness's own
    4-command apply chain is the *sole authority* on patch applicability —
    it accepts patch forms without ``---``/``+++`` file headers (rename-only
    and mode-only ``diff --git`` hunks, ``GIT binary patch`` sections,
    tab-separated ``diff -u`` headers, context/normal diffs), so a header
    check would reject applicable patches before they could ever reach the
    harness and score ``resolved``. Every non-empty patch — even header-less
    garbage — therefore reaches the harness, and the chain's
    ``APPLY_PATCH_FAIL`` marker becomes a clean typed error."""
    for garbage in ("hello world", "just some text\nno diff here", "---\n+++\n"):
        fake = FakeHarness(
            tree=lambda cwd: _build_tree(
                cwd,
                "rid",
                "devin",
                "iid",
                instance_log="applying patch…\n>>>>> Patch Apply Failed:\nerror: corrupt patch\n",
            )
        )
        with pytest.raises(ev.PatchCorruptionError):
            ev.evaluate("iid", garbage, workdir=tmp_path, run_id="rid", harness_runner=fake)
        assert len(fake.calls) == 1  # the harness *was* spawned


def test_truncated_wellformed_patch_reaches_harness_and_is_corruption(tmp_path):
    """A truncation that still leaves both diff headers is structurally
    well-formed — it reaches the harness, whose git-apply chain fails and
    writes its own APPLY_PATCH_FAIL marker; the marker becomes a clean typed
    error."""
    truncated = _truncated_gold_patch()
    assert "--- " in truncated and "+++ " in truncated  # well-formed: it reaches the harness
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "pallets__flask-5014",
            instance_log="applying patch…\n>>>>> Patch Apply Failed:\nerror: patch failed\n",
        )
    )
    with pytest.raises(ev.PatchCorruptionError) as e:
        ev.evaluate(
            "pallets__flask-5014",
            truncated,
            workdir=tmp_path,
            run_id="rid",
            harness_runner=fake,
        )
    assert len(fake.calls) == 1  # the harness *was* spawned (the chain is definitive)
    assert e.value.instance_id == "pallets__flask-5014"
    assert e.value.log_paths["instance"]  # the log paths travel with the error


def _timeout_marker_tree(marker_file: str, marker: str):
    """A no-report tree carrying exactly one timeout marker in exactly one
    file (the other harness artifact absent)."""
    kwargs = {marker_file: marker + "\n"}
    return lambda cwd: _build_tree(cwd, "rid", "devin", "iid", **kwargs)


@pytest.mark.parametrize(
    ("marker_file", "marker"),
    [
        # the markers, in the file the pinned harness writes / the test
        # output it grades (run_evaluation.py appends the tail-anchored
        # marker as the file's final write; grading.py:137 reads ">>>>>
        # Tests Timed Out" *inside* the test output; the instance-log
        # marker is the harness's own EvaluationError message via the
        # logger)
        ("test_output", "Timeout error: 1800 seconds exceeded."),
        ("test_output", ">>>>> Tests Timed Out"),
        ("instance_log", "Test timed out after 1800 seconds."),
    ],
)
def test_no_report_timeout_marker_is_evaluation_timeout(tmp_path, marker_file, marker):
    """A harness timeout marker **in the file the pinned harness writes it
    to** → EvaluationTimeoutError (parametrized over all markers × the
    file placement)."""
    fake = FakeHarness(tree=_timeout_marker_tree(marker_file, marker))
    with pytest.raises(ev.EvaluationTimeoutError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert e.value.instance_id == "iid"


@pytest.mark.parametrize(
    ("marker_file", "marker"),
    [
        # the same text in the *wrong* file is not a harness timeout marker:
        # EvaluationTimeoutError is terminal in WP3 while
        # HarnessFailedError is the retried eval_error class, so a
        # docker-client/network line containing "Timeout error:" in the
        # instance log (or test output echoing a suite's own timeout text)
        # must NOT convert a retryable infra flake into a terminal timeout.
        ("instance_log", "Timeout error: 1800 seconds exceeded."),
        ("test_output", "Test timed out after 1800 seconds."),
        ("instance_log", ">>>>> Tests Timed Out"),
    ],
)
def test_timeout_marker_in_wrong_file_is_not_terminal_timeout(tmp_path, marker_file, marker):
    """The loose markers are anchored and file-specific: the same text in
    the wrong file is not a timeout marker → the retryable infra class."""
    fake = FakeHarness(tree=_timeout_marker_tree(marker_file, marker))
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_bare_instance_log_timeout_substring_is_not_terminal(tmp_path):
    """The instance-log timeout marker is tied to this run's -t value and
    checked on the echo-masked log: a patch whose text merely contains the
    bare substring "Test timed out after" (no number — e.g. a docstring or
    comment, echoed by the harness into the "Git diff before:" region)
    must not be triaged as the harness's own timeout verdict when the run
    then exits 0 with no report (a swallowed post-apply infra error) → the
    retryable infra class, never a terminal EvaluationTimeoutError."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                "2026-10-28 04:12:34,123 - INFO - Git diff before:\n"
                "+    # Test timed out after the suite ran — see docs.\n"
                "2026-10-28 04:12:34,456 - INFO - error: post-apply infra failure swallowed by the harness\n"
            ),
        )
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_diff_echo_cannot_forge_either_terminal_marker(tmp_path):
    """The harness reflects the patch text into run_instance.log in exactly
    the "Git diff before:" / "Git diff after:" echoes (one logger call
    each, the diff is the multi-line message body — un-prefixed
    continuation lines). A diff carrying the *exact* timeout sentence for
    this run's -t (the harness's own message is
    f"Test timed out after {timeout} seconds.", run_evaluation.py:379)
    or the bare ">>>>> Patch Apply Failed" marker must not be triaged as
    the harness's verdict when the run exits 0 with no report → the
    retryable infra class."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                "2026-10-28 04:12:34,123 - INFO - Git diff before:\n"
                "+    assert output == \"Test timed out after 1800 seconds.\"\n"
                "+    MARKER = \">>>>> Patch Apply Failed\"\n"
                "2026-10-28 04:12:34,456 - INFO - error: post-apply infra failure swallowed by the harness\n"
            ),
        )
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_harness_written_markers_survive_echo_masking(tmp_path):
    """Control: the harness's own marker lines are outside the diff echo
    regions, so the masking must not hide them: the timeout
    EvaluationError — the traceback's last line (un-prefixed, but not
    inside an echo region) — with *this run's -t* (default 1800) →
    terminal timeout; the ">>>>> Patch Apply Failed:" line — the first
    line of its own logger call (run_evaluation.py:332, prefixed) →
    terminal corruption."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                "2026-10-28 04:12:34,123 - INFO - Git diff before:\n"
                "+    (harmless patch text)\n"
                "2026-10-28 04:14:34,123 - INFO - Traceback (most recent call last):\n"
                "  File \"/harness/run_evaluation.py\", line 380, in run_instance\n"
                "swebench.harness.utils.EvaluationError: Test timed out after 1800 seconds.\n"
            ),
        )
    )
    with pytest.raises(ev.EvaluationTimeoutError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)

    fake2 = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                "2026-10-28 04:12:34,123 - INFO - Git diff before:\n"
                "+    (harmless patch text)\n"
                "2026-10-28 04:12:35,456 - INFO - >>>>> Patch Apply Failed:\n"
                "error: patch failed: x; 1\n"
            ),
        )
    )
    with pytest.raises(ev.PatchCorruptionError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake2)


def test_timeout_marker_with_wrong_t_value_is_not_terminal(tmp_path):
    """The instance-log timeout marker is tied to *this run's* -t value:
    a "Test timed out after 9999 seconds." line for a run with the default
    -t (1800) is not the harness's verdict for this run → the retryable
    infra class, never a terminal timeout."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                "2026-10-28 04:14:34,123 - INFO - Traceback (most recent call last):\n"
                "swebench.harness.utils.EvaluationError: Test timed out after 9999 seconds.\n"
            ),
        )
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_apply_fail_and_timeout_markers_corruption_wins(tmp_path):
    """Both markers present (apply-fail + the timeout message, both in the
    instance log): corruption wins — a corrupt patch is a terminal verdict
    and a retry would just re-fail the apply."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            instance_log=(
                ">>>>> Patch Apply Failed:\nerror: patch failed\n"
                "Test timed out after 1800 seconds.\n"
            ),
        )
    )
    with pytest.raises(ev.PatchCorruptionError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_apply_fail_in_wrong_file_is_not_corruption(tmp_path):
    """The ``APPLY_PATCH_FAIL`` marker in test_output.txt (the wrong file —
    the harness writes it to run_instance.log) is not the harness's
    apply-failed verdict → the retryable infra class, not corruption."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            test_output=(
                ">>>>> Patch Apply Failed:\nerror: patch failed\n"
            ),
        )
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_no_report_no_marker_is_infra_error(tmp_path):
    """No report and no known marker → Docker/infra failure (the WP3
    eval_error retry class) — typed, not a crash."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid", instance_log="creating container…\n"
        )
    )
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert e.value.instance_id == "iid"


def test_exit_nonzero_is_harness_failed_with_stderr_tail(tmp_path):
    """A non-zero harness exit (unknown instance id, dataset fetch failure,
    unreachable daemon) is a typed HarnessFailedError carrying the
    *combined* output tail (when one stream is non-empty the other is not
    discarded entirely) — the operator can triage without re-reading the
    logs."""
    stderr = "Traceback (most recent call last):\nValueError: Some prediction IDs not found in dataset!\n"
    stdout = "running 1 instance…\n"
    fake = FakeHarness(returncode=1, stdout=stdout, stderr=stderr)
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "ValueError" in e.value.output_tail  # the stderr tail
    assert "running 1 instance" in e.value.output_tail  # the stdout tail, not discarded


def test_report_present_but_exit_nonzero_is_harness_failed(tmp_path):
    """The exit-≠ 0-always-error rule: the harness writes the per-instance
    report before the final-report leg (make_run_report), so a crash *after*
    the report (report present + exit ≠ 0) must not be read as a completed
    run — the stderr tail must not be silently dropped."""
    fake = FakeHarness(
        returncode=1,
        stderr="crashed inside make_run_report\n",
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            report={"iid": {"resolved": True}},
        ),
    )
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "crashed inside make_run_report" in e.value.output_tail


def test_exit_nonzero_with_apply_fail_marker_is_harness_failed(tmp_path):
    """Exit ≠ 0 wins over the markers (checked before the no-report
    triage): a non-zero exit with an apply-fail line in the log is a
    harness failure, not a corruption verdict — the instance's outcome is
    unknown when the run itself failed."""
    fake = FakeHarness(
        returncode=1,
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            instance_log=">>>>> Patch Apply Failed:\nerror: patch failed\n",
        ),
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_unreadable_report_is_harness_failed(tmp_path):
    def tree(cwd: Path) -> None:
        _build_tree(cwd, "rid", "devin", "iid")
        (cwd / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "report.json").write_text(
            "{ not json", encoding="utf-8"
        )

    fake = FakeHarness(tree=tree)
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_report_missing_instance_entry_is_harness_failed(tmp_path):
    """A report that *exists at the instance path* but lacks the instance's
    entry (a foreign key only, or an entry without "resolved") is a typed
    HarnessFailedError — the guard at the report-present leg (exercises the
    entry validation branch, not the no-report triage)."""
    for report in (
        {"other-instance": {"resolved": True}},  # foreign key only
        {"iid": {"patch_successfully_applied": True}},  # missing "resolved"
    ):
        fake = FakeHarness(
            tree=lambda cwd, r=report: _build_tree(cwd, "rid", "devin", "iid", report=r)
        )
        with pytest.raises(ev.HarnessFailedError):
            ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


@pytest.mark.parametrize("bad_resolved", ["false", None, 1, 0, "true"])
def test_non_bool_resolved_verdict_is_harness_failed(tmp_path, bad_resolved):
    """AC3 — a corrupted-but-parseable verdict (a non-bool "resolved") is a
    typed HarnessFailedError, never a silent bool() coercion (bool("false")
    would be True; bool(None) would hide the corruption as False)."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            report={"iid": {"resolved": bad_resolved}},
        )
    )
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


# ---------------------------------------------------------------------------
# robustness edges — typed, never a traceback escape
# ---------------------------------------------------------------------------


def test_relative_workdir_is_resolved_to_absolute_before_spawn(tmp_path, monkeypatch):
    """The harness resolves -p / --report_dir against its own cwd (= workdir),
    so a relative workdir must be resolved before the spawn or the paths
    double-resolve (workdir/workdir/…) and every relative-workdir call
    fails."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel-workdir").mkdir()
    fake = FakeHarness(tree=lambda cwd: _build_tree(cwd, "rid", "devin", "iid", report={"iid": {"resolved": False}}))
    ev.evaluate("iid", _WELLFORMED_PATCH, workdir="rel-workdir", run_id="rid", harness_runner=fake)
    argv, cwd, _ = fake.calls[0]
    assert cwd == str(tmp_path / "rel-workdir")  # absolute cwd
    assert argv[argv.index("-p") + 1] == str(tmp_path / "rel-workdir" / "predictions.jsonl")
    assert argv[argv.index("--report_dir") + 1] == str(tmp_path / "rel-workdir")


def test_runner_spawn_failure_is_typed_not_raw(tmp_path):
    """A spawn failure of the harness subprocess (missing python →
    FileNotFoundError; fork/exec limits → OSError) is a typed
    HarnessFailedError, never a raw traceback escape."""
    def boom(argv, cwd, overall_timeout=None):
        raise FileNotFoundError(2, "No such file or directory", "/nope")

    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=boom)
    assert "harness runner failed" in e.value.message


def test_process_object_without_returncode_is_typed(tmp_path):
    """A process object without a usable returncode (a malformed runner
    result) is a typed HarnessFailedError — proc.returncode is never
    accessed bare."""
    class NoReturncode:
        stdout = ""
        stderr = ""

    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid",
                    harness_runner=lambda a, c, t=None: NoReturncode())


def test_pre_spawn_filesystem_failure_is_typed(tmp_path):
    """Pre-spawn filesystem work (workdir mkdir / predictions write) is typed
    — a workdir whose parent is a *file* (deterministic regardless of
    privileges) fails with an OSError that must not escape raw (PLAN §7
    disk management / §9 risk table: "500 images exhaust disk")."""
    (tmp_path / "blocker").write_text("i am a file", encoding="utf-8")
    fake = FakeHarness()
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate(
            "iid", _WELLFORMED_PATCH,
            workdir=tmp_path / "blocker" / "sub",
            run_id="rid",
            harness_runner=fake,
        )
    assert "pre-spawn filesystem failure" in e.value.message
    assert fake.calls == []  # failed before the spawn


def test_workdir_none_creates_tempdir_and_surfaces_it(tmp_path):
    """workdir=None (the auto-tempdir path) creates a fresh
    swebench-eval-… directory and *surfaces* it — logs.workdir / logs.
    predictions make the caller-owned cleanup reachable (a silent tempdir
    leak is not an option) and failures debuggable."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            report={"iid": {"resolved": False}}, final_report=True,
        )
    )
    result = ev.evaluate("iid", _WELLFORMED_PATCH, run_id="rid", harness_runner=fake)
    wd = result["logs"]["workdir"]
    assert wd is not None
    created = Path(wd)
    assert created.is_dir()
    assert created.name.startswith("swebench-eval-iid-")
    assert created != tmp_path  # not the caller's directory
    assert result["logs"]["predictions"] == str(created / "predictions.jsonl")
    assert (created / "predictions.jsonl").is_file()
    shutil.rmtree(wd)  # the caller owns the cleanup — the contract under test


def test_preexisting_report_is_unlinked_and_not_trusted(tmp_path):
    """Stale-report protection: the harness's own exclude_completed treats a
    pre-existing per-run report.json as 'already run' and silently skips
    the instance — a re-used (workdir, run_id) pair must not return the
    previous run's verdict for a patch that was never evaluated. The fake
    harness 'skips' the instance like the real one would (writes nothing
    new) → the result must be a typed no-report triage, not the stale
    verdict."""
    run_dir = tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid"
    run_dir.mkdir(parents=True)
    stale = run_dir / "report.json"
    stale.write_text(json.dumps({"iid": {"resolved": True}}), encoding="utf-8")
    fake = FakeHarness()  # returncode 0, no tree — the instance was "skipped"
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert not stale.exists()  # the pre-existing report was removed before the spawn


def test_stale_report_surviving_unlink_failure_is_not_trusted(tmp_path, monkeypatch):
    """Stale-report protection, unlink-failure fallback: if the pre-existing
    report cannot be removed (permissions), the pre-run mtime is recorded and
    a post-run report that was not updated is a typed stale-report failure
    (the harness's exclude_completed would have skipped the instance)."""
    run_dir = tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid"
    run_dir.mkdir(parents=True)
    stale = run_dir / "report.json"
    stale.write_text(json.dumps({"iid": {"resolved": True}}), encoding="utf-8")
    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(PermissionError("locked")),
    )
    # the fake harness "skips" the instance (writes nothing new) — the stale
    # report survives the failed unlink with its pre-run mtime.
    fake = FakeHarness()
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "stale" in e.value.message.lower()


def test_stale_guard_accepts_a_fresh_report_when_unlink_fails(tmp_path, monkeypatch):
    """Stale-report protection, unlink-failure fallback — the positive leg:
    when the pre-existing report cannot be removed but the harness
    *demonstrably updated* it (post-run mtime strictly after the pre-run
    mtime), it is this run's report and is accepted."""
    import os
    import time

    run_dir = tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid"
    run_dir.mkdir(parents=True)
    stale = run_dir / "report.json"
    stale.write_text(json.dumps({"iid": {"resolved": True}}), encoding="utf-8")
    t_past = time.time() - 10
    os.utime(stale, (t_past, t_past))  # the pre-run mtime, clearly in the past
    monkeypatch.setattr(
        Path, "unlink",
        lambda self: (_ for _ in ()).throw(PermissionError("locked")),
    )

    def tree(cwd: Path) -> None:
        _build_tree(cwd, "rid", "devin", "iid", report={"iid": {"resolved": False}})
        t_future = time.time() + 1
        os.utime(cwd / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "report.json", (t_future, t_future))

    fake = FakeHarness(tree=tree)
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert result["resolved"] is False  # the *new* report's verdict, not the stale one


def test_timeout_expired_from_runner_is_typed_with_partial_output(tmp_path):
    """The outer watchdog firing (subprocess.TimeoutExpired — the default
    runner or an injected one) is a typed HarnessFailedError that surfaces
    the possibly-still-running eval container name (for 'docker rm -f')
    and the partial output — never a raw escape."""
    def boom(argv, cwd, overall_timeout=None):
        raise subprocess.TimeoutExpired(
            cmd="harness", timeout=overall_timeout,
            output=b"partial stdout line one\npartial stdout line two\n",
            stderr=b"partial stderr line one\npartial stderr line two\n",
        )

    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid9", harness_runner=boom)
    m = e.value.message.lower()
    assert "watchdog" in m
    assert "sweb.eval.iid.rid9" in m  # the eval container name for cleanup
    tail = e.value.output_tail or ""
    assert "partial stdout" in tail and "partial stderr" in tail  # both tails, labeled
    assert "\n" in tail  # decoded readably — not a single-line b'…' repr
    assert "b'" not in tail


def test_watchdog_kill_error_carries_fresh_log_paths(tmp_path):
    """A mid-run watchdog kill must surface the artifacts the harness
    already wrote before the kill (run_instance.log / test_output.txt) —
    not the pre-spawn log_paths snapshot, which would be empty for a
    fresh workdir (or the previous run's stale files for a re-used
    workdir)."""
    def boom(argv, cwd, overall_timeout=None):
        # the harness writes its logs early, then wedges (e.g. a stuck
        # image pull) until the outer watchdog kills it
        base = Path(cwd) / "logs" / "run_evaluation" / "rid9" / "devin" / "iid"
        base.mkdir(parents=True, exist_ok=True)
        (base / "run_instance.log").write_text("pulled 3/12 image layers...\n", encoding="utf-8")
        (base / "test_output.txt").write_text("\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(
            cmd="harness", timeout=overall_timeout,
            output=b"partial stdout", stderr=b"partial stderr",
        )

    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid9", harness_runner=boom)
    assert "watchdog" in e.value.message.lower()
    assert e.value.log_paths["instance"]  # re-read post-spawn — the harness-written log
    assert e.value.log_paths["test_output"]


@pytest.mark.parametrize(
    "exc",
    [
        lambda: ev.InvalidInputError("the runner refuses"),
        lambda: ev.EvaluationTimeoutError("runner timeout", instance_id="iid"),
    ],
)
def test_injected_runner_typed_error_stays_typed(tmp_path, exc):
    """A typed error an injected runner raises stays typed — the generic
    runner-failure clause must not re-wrap an EvaluationError (a
    runner-raised EvaluationTimeoutError would be a *terminal* error
    silently becoming the retried eval_error class; an InvalidInputError
    would be the wrong CLI exit class)."""
    def boom(argv, cwd, overall_timeout=None):
        raise exc()

    with pytest.raises(ev.EvaluationError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=boom)
    assert type(e.value) is type(exc())  # the type is preserved, not re-wrapped


def test_default_harness_runner_success_watchdog_fires():
    """The default runner's own wiring (previously untested — only the
    injected seam was): a trivial subprocess succeeds, and
    ``overall_timeout`` really bounds the run (a hung command raises
    TimeoutExpired, not hangs the benchmark)."""
    proc = ev._default_harness_runner(
        [sys.executable, "-c", "print('hello harness')"],
        cwd=".",
        overall_timeout=30,
    )
    assert proc.returncode == 0
    assert "hello harness" in proc.stdout

    with pytest.raises(subprocess.TimeoutExpired):
        ev._default_harness_runner(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=".",
            overall_timeout=0.5,
        )


def test_tail_decodes_bytes_readably():
    """A bytes output (a TimeoutExpired.output / a runner's bytes
    stdout/stderr — bytes even under text=True on POSIX) is decoded
    readably in _tail, never a b'…' repr with escaped newlines."""
    assert ev._tail(b"line one\nline two") == "line one\nline two"
    long_tail = ev._tail(b"a\n" * 100000)
    assert long_tail is not None and "b'" not in long_tail
    assert ev._tail(None) is None


def test_auto_tempdir_is_surfaced_on_typed_failure():
    """workdir=None: the auto-created tempdir must be reachable on the
    typed *failure* paths too (error.workdir + __str__) — the documented
    caller-owned-cleanup contract can't be honored for a directory the
    caller can neither find nor remove (a silent tempdir leak)."""
    fake = FakeHarness()  # no tree → exit 0, no report → no-report triage
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, harness_runner=fake)
    wd = e.value.workdir
    assert wd is not None
    assert Path(wd).name.startswith("swebench-eval-iid-")
    assert wd in str(e.value)  # the directory is in the error text
    assert (Path(wd) / "predictions.jsonl").is_file()  # the workdir was used
    shutil.rmtree(wd)  # the caller owns the cleanup — now reachable


def test_pathlike_workdir_is_validated_like_a_str(tmp_path):
    """A path-like workdir gets the same validation as a str: a valid
    Path works; an *empty* path-like (which would resolve to the caller's
    CWD — the harness writing its whole artifact tree into an unintended
    directory) is refused; and a NUL-carrying PurePath is the same
    malformed input as the str case (InvalidInputError, not a raw
    ValueError at resolve)."""
    fake = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=False))
    ev.evaluate("iid", _WELLFORMED_PATCH, workdir=Path(tmp_path), run_id="rid", harness_runner=fake)
    assert fake.calls  # a valid Path flows through

    for bad in (Path(""), PurePath("w\x00d")):
        fake2 = FakeHarness()
        with pytest.raises(ev.InvalidInputError):
            ev.evaluate("iid", _WELLFORMED_PATCH, workdir=bad, run_id="rid", harness_runner=fake2)
        assert fake2.calls == []  # typed before any side effect


def test_pathlike_fspath_raising_runtime_error_is_typed_refusal(tmp_path):
    """A path-like workdir whose __fspath__ raises RuntimeError (a symlink
    loop the OS can surface that way) is a typed refusal: the
    validation's os.fsdecode guard covers RuntimeError in addition to
    OSError/ValueError/TypeError — never a raw escape."""
    class _LoopingPath:
        def __fspath__(self) -> str:
            raise RuntimeError("Maximum symlink depth exceeded")

    fake = FakeHarness()
    with pytest.raises(ev.InvalidInputError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=_LoopingPath(), run_id="rid", harness_runner=fake)
    assert fake.calls == []  # typed before any side effect


def test_symlink_loop_workdir_is_typed_not_raw(tmp_path):
    """Path(workdir).resolve() raises RuntimeError ("Maximum symlink depth
    exceeded") on a symlink loop — a pre-spawn filesystem condition. It
    must be typed (HarnessFailedError), never a raw traceback escape:
    the module's "typed, never a traceback escape" contract covers it.
    (Platform probe: this Python's resolve() only raises on a loop on the
    versions where CPython enforces the symlink-depth limit — skipped
    elsewhere, where the pre-spawn clause is unreachable for a loop.)"""
    loop_a, loop_b = tmp_path / "loop-a", tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    try:
        loop_a.resolve()
    except RuntimeError:
        pass  # this Python raises on a loop — the contract is under test
    except Exception:
        pytest.skip("this Python's Path.resolve() raises a non-RuntimeError on a symlink loop")
    else:
        pytest.skip("this Python's Path.resolve() does not raise on a symlink loop")
    fake = FakeHarness()
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=loop_a, run_id="rid", harness_runner=fake)
    assert "pre-spawn" in e.value.message.lower()
    assert fake.calls == []  # typed before the spawn


def test_overall_timeout_flows_to_the_runner(tmp_path):
    """An explicit outer watchdog value flows through the runner seam to
    the default subprocess unchanged."""
    fake = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=False))
    ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        workdir=tmp_path,
        run_id="rid",
        overall_timeout=120.5,
        harness_runner=fake,
    )
    _, _, overall_timeout = fake.calls[0]
    assert overall_timeout == 120.5


def test_default_overall_watchdog_is_bounded_and_scales_with_timeout(tmp_path):
    """overall_timeout=None (the default, including the CLI default) must
    NOT leave the spawned harness unbounded: a wedged dataset download,
    multi-GB image pull, or Docker daemon must not hang the sequential
    benchmark forever (the rest of the package uses bounded defaults for
    subprocess calls — runner.py timeout_s=30/15). The bounded default is
    timeout + DEFAULT_OVERALL_TIMEOUT_MARGIN_S — it scales with the
    in-container test cap the caller chose, so a raised -t cannot make the
    watchdog fire before the test exec can finish."""
    default_case = FakeHarness(
        tree=_report_tree("rid", "devin", "iid", resolved=False)
    )
    ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=default_case)
    _, _, default_watchdog = default_case.calls[0]
    assert default_watchdog == ev.DEFAULT_TIMEOUT_S + ev.DEFAULT_OVERALL_TIMEOUT_MARGIN_S

    raised_case = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=False))
    ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        workdir=tmp_path,
        run_id="rid",
        timeout=600,
        harness_runner=raised_case,
    )
    _, _, raised_watchdog = raised_case.calls[0]
    assert raised_watchdog == 600 + ev.DEFAULT_OVERALL_TIMEOUT_MARGIN_S
    # …and a *larger* explicit watchdog still flows through unchanged.
    explicit = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=False))
    ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        workdir=tmp_path,
        run_id="rid",
        timeout=600,
        overall_timeout=9999.0,
        harness_runner=explicit,
    )
    assert explicit.calls[0][2] == 9999.0


def test_huge_int_timeouts_are_typed_not_overflow(tmp_path):
    """A huge-int timeout / overall_timeout overflows the float conversion
    (math.isnan/isinf in the validation, the float conversion at spawn for
    the bounded default watchdog) — a raw OverflowError the validation
    must not let out → typed refusals, before any side effect."""
    for kwargs in ({"timeout": 10**400}, {"overall_timeout": 10**400}):
        fake = FakeHarness()
        with pytest.raises(ev.InvalidInputError):
            ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake, **kwargs)
        assert fake.calls == []  # typed before any side effect


def test_non_object_report_is_harness_failed(tmp_path):
    """A valid-JSON-but-not-an-object report.json is a typed
    HarnessFailedError (not an AttributeError escape)."""
    def tree(cwd: Path) -> None:
        _build_tree(cwd, "rid", "devin", "iid")
        (cwd / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "report.json").write_text(
            '["a", "list"]', encoding="utf-8"
        )

    fake = FakeHarness(tree=tree)
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


def test_non_utf8_report_is_harness_failed(tmp_path):
    """A non-UTF-8 report.json is a typed HarnessFailedError (not a
    UnicodeDecodeError escape)."""
    def tree(cwd: Path) -> None:
        _build_tree(cwd, "rid", "devin", "iid")
        (cwd / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "report.json").write_bytes(
            b"\xff\xfe{invalid"
        )

    fake = FakeHarness(tree=tree)
    with pytest.raises(ev.HarnessFailedError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


@pytest.mark.parametrize("with_reason", [True, False])
def test_infra_failure_report_is_harness_failed(tmp_path, with_reason):
    """The harness's own environment-fault classification (infra_failure —
    grading.py:371-374: unparseable test output; the harness keeps
    resolved False but counts these separately in the final report's
    infra_failure_ids) is the WP3 eval_error retry class — never a terminal
    'not resolved' verdict."""
    entry = {"resolved": False, "infra_failure": True}
    if with_reason:
        entry["infra_failure_reason"] = "conda env broken"
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid", report={"iid": entry}
        )
    )
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "infra" in e.value.message.lower()


def test_plain_unresolved_is_not_infra_failed(tmp_path):
    """Control: a plain resolved=False report (no infra_failure flag) is a
    legitimate 'not resolved' verdict, not an error."""
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd, "rid", "devin", "iid",
            report={"iid": {"resolved": False, "infra_failure": False}},
        )
    )
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert result["resolved"] is False


def test_empty_patch_with_invalid_input_is_invalid_input(tmp_path):
    """Validation precedes the empty-patch short-circuit: an empty patch
    with a bad instance_id is an InvalidInputError (typed before any
    side effect), not the empty-patch result."""
    fake = FakeHarness()
    with pytest.raises(ev.InvalidInputError):
        ev.evaluate("a/b", "", workdir=tmp_path, harness_runner=fake)
    assert fake.calls == []


def test_stale_log_marker_is_not_a_terminal_verdict(tmp_path):
    """Stale-log-marker protection: a pre-existing run_instance.log (from a
    re-used workdir/run_id) carrying APPLY_PATCH_FAIL, which this run does
    not update (the run dies before the harness reaches logger setup —
    no report, no fresh log), must not be triaged as this run's verdict →
    the retryable infra class, never the terminal corruption class."""
    import os
    import time

    log = tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "run_instance.log"
    log.parent.mkdir(parents=True)
    log.write_text(">>>>> Patch Apply Failed:\nerror: patch failed\n", encoding="utf-8")
    t_past = time.time() - 10
    os.utime(log, (t_past, t_past))  # the pre-run mtime, clearly in the past
    fake = FakeHarness()  # no tree: the run writes nothing new
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "stale" in e.value.message.lower()


def test_updated_log_marker_is_this_runs_verdict(tmp_path):
    """Control: the same pre-existing log file, but *updated by this run*
    (the harness truncates/rewrites it — newer mtime), is this run's
    verdict → the terminal corruption class."""
    import os
    import time

    log = tmp_path / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "run_instance.log"
    log.parent.mkdir(parents=True)
    log.write_text(">>>>> Patch Apply Failed:\nerror: patch failed\n", encoding="utf-8")
    t_past = time.time() - 10
    os.utime(log, (t_past, t_past))

    def tree(cwd: Path) -> None:
        _build_tree(
            cwd, "rid", "devin", "iid",
            instance_log=(
                ">>>>> Patch Apply Failed:\nerror: patch failed\n"
            ),
        )
        t_future = time.time() + 1
        os.utime(cwd / "logs" / "run_evaluation" / "rid" / "devin" / "iid" / "run_instance.log", (t_future, t_future))

    fake = FakeHarness(tree=tree)
    with pytest.raises(ev.PatchCorruptionError):
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)


# ---------------------------------------------------------------------------
# validation matrix — typed, before any side effect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("instance_id", "model_patch", "run_id", "model_name", "timeout"),
    [
        ("", _WELLFORMED_PATCH, None, "devin", 1800),  # empty id
        (None, _WELLFORMED_PATCH, None, "devin", 1800),  # non-str id
        (42, _WELLFORMED_PATCH, None, "devin", 1800),  # non-str id
        ("a/b", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id slash (charset)
        ("../x", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id traversal
        ("a\tb", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id tab (charset)
        ("a b", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id space (charset)
        ("a\x00b", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id NUL (charset)
        (".lead", _WELLFORMED_PATCH, None, "devin", 1800),  # instance_id leading dot (charset)
        ("iid", 123, None, "devin", 1800),  # non-str patch (int)
        ("iid", b"a patch", None, "devin", 1800),  # non-str patch (bytes)
        ("iid", {"model_patch": "x"}, None, "devin", 1800),  # non-str patch (dict)
        ("iid", _WELLFORMED_PATCH, "has space", "devin", 1800),  # run_id whitespace
        ("iid", _WELLFORMED_PATCH, "", "devin", 1800),  # run_id empty
        ("iid", _WELLFORMED_PATCH, None, "", 1800),  # empty model name
        ("iid", _WELLFORMED_PATCH, None, "..", 1800),  # model name log-path traversal
        ("iid", _WELLFORMED_PATCH, None, ".", 1800),  # model name log-path traversal
        ("iid", _WELLFORMED_PATCH, None, "a\x00b", 1800),  # model name NUL
        ("iid", _WELLFORMED_PATCH, None, "devin", 0),  # timeout 0
        ("iid", _WELLFORMED_PATCH, None, "devin", -5),  # timeout negative
        ("iid", _WELLFORMED_PATCH, None, "devin", "1800"),  # timeout str
        ("iid", _WELLFORMED_PATCH, None, "devin", True),  # timeout bool
        ("iid", _WELLFORMED_PATCH, "has\ttab", "devin", 1800),  # run_id tab
        ("iid", _WELLFORMED_PATCH, "has\nnewline", "devin", 1800),  # run_id newline
        ("iid", _WELLFORMED_PATCH, "with/slash", "devin", 1800),  # run_id slash
        ("iid", _WELLFORMED_PATCH, "..", "devin", 1800),  # run_id traversal
        ("iid", _WELLFORMED_PATCH, ".leading", "devin", 1800),  # run_id leading dot
        ("iid", _WELLFORMED_PATCH, 42, "devin", 1800),  # non-str run_id
    ],
)
def test_input_validation_matrix(
    instance_id, model_patch, run_id, model_name, timeout, tmp_path
):
    fake = FakeHarness()
    with pytest.raises(ev.InvalidInputError):
        ev.evaluate(
            instance_id,
            model_patch,
            model_name_or_path=model_name,
            run_id=run_id,
            timeout=timeout,
            workdir=tmp_path,
            harness_runner=fake,
        )
    assert fake.calls == []  # typed before any side effect
    assert not (tmp_path / "predictions.jsonl").exists()  # zero FS side effects


@pytest.mark.parametrize(
    ("dataset", "split", "workdir", "python", "harness_runner", "overall_timeout"),
    [
        ("", "test", None, None, None, None),  # empty dataset
        (None, "test", None, None, None, None),  # non-str dataset
        ("SWE-bench/SWE-bench_Verified", "", None, None, None, None),  # empty split
        ("SWE-bench/SWE-bench_Verified", 42, None, None, None, None),  # non-str split
        ("SWE-bench/SWE-bench_Verified", "test", 42, None, None, None),  # non-path workdir
        ("SWE-bench/SWE-bench_Verified", "test", "", None, None, None),  # empty workdir
        ("SWE-bench/SWE-bench_Verified", "test", None, "", None, None),  # empty python
        ("SWE-bench/SWE-bench_Verified", "test", None, 42, None, None),  # non-str python
        ("SWE-bench/SWE-bench_Verified", "test", None, None, "not-callable", None),  # non-callable runner
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, 0),  # overall_timeout 0
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, -5),  # overall_timeout negative
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, "600"),  # overall_timeout str
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, True),  # overall_timeout bool
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, float("nan")),  # overall_timeout nan (a nan deadline hangs / fires unpredictably)
        ("SWE-bench/SWE-bench_Verified", "test", None, None, None, float("inf")),  # overall_timeout inf (no outer bound = no watchdog)
        ("SWE-bench\x00/SWE-bench_Verified", "test", None, None, None, None),  # dataset NUL (an argv context)
        ("SWE-bench/SWE-bench_Verified", "test\x00", None, None, None, None),  # split NUL (an argv context)
        ("SWE-bench/SWE-bench_Verified", "test", "w\x00d", None, None, None),  # workdir NUL (an argv context)
        ("SWE-bench/SWE-bench_Verified", "test", None, "/py\x00", None, None),  # python NUL (an argv context)
    ],
)
def test_extra_input_validation_matrix(
    dataset, split, workdir, python, harness_runner, overall_timeout, tmp_path
):
    """The remaining call inputs are validated typed too — the same
    'never a raw TypeError' contract as the primary matrix."""
    fake = FakeHarness()
    with pytest.raises(ev.InvalidInputError):
        ev.evaluate(
            "iid",
            _WELLFORMED_PATCH,
            dataset=dataset,
            split=split,
            workdir=workdir if workdir is not None else tmp_path,
            overall_timeout=overall_timeout,
            python=python,
            harness_runner=harness_runner if harness_runner is not None else fake,
        )
    assert fake.calls == []  # typed before any side effect
    assert not (tmp_path / "predictions.jsonl").exists()  # zero FS side effects


# ---------------------------------------------------------------------------
# the operator CLI
# ---------------------------------------------------------------------------


def test_cli_help():
    with pytest.raises(SystemExit) as e:
        ev.main(["--help"])
    assert e.value.code == 0


def test_cli_success_prints_json_on_stdout(tmp_path, capsys):
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(_WELLFORMED_PATCH, encoding="utf-8")
    fixed = {
        "resolved": True,
        "logs": {"instance": None, "test_output": None, "report": None,
                 "final_report": None, "container": None, "workdir": None,
                 "run_id": None, "predictions": None, "empty_patch": False},
    }
    seen = {}

    def fake_evaluate(instance_id, model_patch, **kwargs):
        seen["args"] = (instance_id, model_patch, kwargs)
        return fixed

    code = ev.main(
        ["--instance-id", "iid", "--patch-file", str(patch_file),
         "--model-name", "devin-cli", "--dataset", "org/ds", "--split", "cs",
         "--workdir", str(tmp_path / "wd"), "--run-id", "rid-cli",
         "--python", "/custom/python", "--timeout", "999",
         "--overall-timeout", "600"],
        evaluate_fn=fake_evaluate,
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == fixed
    assert seen["args"][0] == "iid"
    assert seen["args"][1] == _WELLFORMED_PATCH
    seen_kwargs = seen["args"][2]
    assert seen_kwargs["overall_timeout"] == 600.0  # the flag flows through
    assert seen_kwargs["model_name_or_path"] == "devin-cli"
    assert seen_kwargs["dataset"] == "org/ds"
    assert seen_kwargs["split"] == "cs"
    assert seen_kwargs["workdir"] == str(tmp_path / "wd")
    assert seen_kwargs["run_id"] == "rid-cli"
    assert seen_kwargs["python"] == "/custom/python"
    assert seen_kwargs["timeout"] == 999


def test_cli_missing_patch_file_is_exit_2(tmp_path, capsys):
    code = ev.main(["--instance-id", "iid", "--patch-file", str(tmp_path / "nope")])
    assert code == 2
    assert "Invalid input" in capsys.readouterr().err


def test_cli_directory_patch_file_is_exit_2(tmp_path, capsys):
    code = ev.main(["--instance-id", "iid", "--patch-file", str(tmp_path)])  # a directory
    assert code == 2
    assert "Invalid input" in capsys.readouterr().err


def test_cli_non_utf8_patch_file_is_exit_2(tmp_path, capsys):
    patch_file = tmp_path / "bad.patch"
    patch_file.write_bytes(b"\xff\xfe\x00garbage")
    code = ev.main(["--instance-id", "iid", "--patch-file", str(patch_file)])
    assert code == 2
    assert "Invalid input" in capsys.readouterr().err


def test_cli_error_exit_codes(tmp_path, capsys):
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(_WELLFORMED_PATCH, encoding="utf-8")

    def raiser(exc: BaseException):
        def _fn(*a, **k):
            raise exc

        return _fn

    cases = [
        (ev.InvalidInputError("bad input"), 2),
        (ev.PatchCorruptionError("bad patch", instance_id="iid"), 3),
        (ev.EvaluationTimeoutError("timed out", instance_id="iid"), 4),
        (ev.HarnessFailedError("infra", instance_id="iid"), 5),
    ]
    for exc, expected in cases:
        code = ev.main(
            ["--instance-id", "iid", "--patch-file", str(patch_file)],
            evaluate_fn=raiser(exc),
        )
        assert code == expected, f"{type(exc).__name__} → {code}"
        assert capsys.readouterr().err  # the typed message on stderr


def test_cli_unexpected_exception_is_exit_5(tmp_path, capsys):
    """The CLI catch-all: an *unexpected* exception (a bug, not a documented
    failure class) is exit 5 with a typed message — the documented
    contract, not a traceback."""
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(_WELLFORMED_PATCH, encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("a bug")

    code = ev.main(
        ["--instance-id", "iid", "--patch-file", str(patch_file)],
        evaluate_fn=boom,
    )
    assert code == 5
    err = capsys.readouterr().err
    assert "Unexpected failure" in err
    assert "RuntimeError" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# integration (CI — public images, zero quota; auto-skip without a Docker
# daemon, which this dev workspace lacks)
# ---------------------------------------------------------------------------

# The positive control: pallets__flask-5014 — the single flask Verified
# instance (smallest eval image + suite: 1 FAIL_TO_PASS, 59 PASS_TO_PASS).
# The gold patch below is the published Verified run's patch verbatim (the
# merged PR's fix; dataset SWE-bench/SWE-bench_Verified test split, base
# commit 7ee9ceb71e86, image
# swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest). Fallback if the
# 2026 image rebuild flakes on it: psf__requests-1142 (1 F2P, 5 P2P).
GOLD_PATCH_FLASK_5014 = (
    "diff --git a/src/flask/blueprints.py b/src/flask/blueprints.py\n"
    "--- a/src/flask/blueprints.py\n"
    "+++ b/src/flask/blueprints.py\n"
    "@@ -190,6 +190,9 @@ def __init__(\n"
    "             root_path=root_path,\n"
    "         )\n"
    " \n"
    "+        if not name:\n"
    '+            raise ValueError("\'name\' may not be empty.")\n'
    "+\n"
    '         if "." in name:\n'
    '             raise ValueError("\'name\' may not contain a dot \'.\' character.")\n'
    " \n"
)

_FLASK_INSTANCE_ID = "pallets__flask-5014"


def _truncated_gold_patch() -> str:
    """The gold patch cut mid-hunk: the hunk header declares 9 new-side
    lines, the truncation leaves 5 — a structurally well-formed truncation
    (both file headers intact) that only the harness's git-apply chain can
    reject. (Deterministic: 13 lines total; ``lines[:9]`` keeps 9, so the
    last 4 — the `+` added line + 3 context lines — are cut; the remaining
    5 = 3 context + 2 added.)"""
    lines = GOLD_PATCH_FLASK_5014.splitlines(keepends=True)
    assert len(lines) == 13, "the embedded gold patch changed — re-pin the truncation"
    return "".join(lines[:9])


def _docker_daemon_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


_HAS_DOCKER = _docker_daemon_ok()
_DOCKER_SKIP = pytest.mark.skipif(
    not _HAS_DOCKER,
    reason="no Docker daemon (these run in CI; this dev workspace has no container runtime)",
)


@_DOCKER_SKIP
@pytest.mark.timeout(2700)  # the integration tests' watchdog: a wedged Docker
# pull must fail the test, not hang until the job-level timeout (CI)
def test_integration_positive_control(tmp_path):
    """AC4 positive control — the known-resolved gold patch of the published
    Verified run resolves (public image, zero quota). Also the AC2/AC4
    *isolation* assertion: the eval container id (from the harness's own
    log — the only container this module can name) ≠ the agent container
    id; the agent's container (sweb.eval.<iid>, never present here) is
    never referenced — there is not even a parameter for it."""
    import re

    agent_container_id = "a" * 64  # stand-in id of the agent's sweb.eval.<iid> container
    result = ev.evaluate(
        _FLASK_INSTANCE_ID,
        GOLD_PATCH_FLASK_5014,
        model_name_or_path="devin-wp4-positive",
        workdir=tmp_path,
    )
    assert result["resolved"] is True
    # the harness actually ran a fresh container and graded the fresh checkout
    assert result["logs"]["container"] is not None
    assert re.fullmatch(r"[0-9a-f]{12,}", result["logs"]["container"]), (
        "logs.container must be the id of a real container (from the harness log)"
    )
    # isolation (AC4): eval container id ≠ agent container id
    assert result["logs"]["container"] != agent_container_id
    # the report is the harness's verbatim artifact; the final report exists
    assert result["logs"]["report"][_FLASK_INSTANCE_ID]["resolved"] is True
    assert Path(result["logs"]["final_report"]).is_file()


@_DOCKER_SKIP
@pytest.mark.timeout(2700)  # the integration tests' watchdog (see above)
def test_integration_corruption_guard_truncated_gold_patch(tmp_path):
    """AC4 corruption guard, full harness: the gold patch truncated mid-hunk
    → a clean typed error (the harness's own git-apply chain fails and
    writes its marker), never a crash."""
    with pytest.raises(ev.PatchCorruptionError):
        ev.evaluate(
            _FLASK_INSTANCE_ID,
            _truncated_gold_patch(),
            model_name_or_path="devin-wp4-corrupt",
            workdir=tmp_path,
        )
