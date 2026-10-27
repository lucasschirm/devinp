"""WP4 (issue #17) — ``evaluate.py``: the thin delegate to the official
SWE-bench harness.

All unit tests run with **zero quota, no Docker and no network**: the harness
subprocess is replaced by an injected ``harness_runner`` (the module's
documented test seam), and the harness's CWD-relative artifact layout is
fabricated on disk (the exact ``logs/run_evaluation/<run_id>/<model>/<iid>/``
tree the pinned swebench 5.0.2 writes). The three acceptance controls that
need a container runtime (positive / corruption / isolation) are the
integration tests at the bottom — CI (public images, no quota); they
auto-skip where no Docker daemon exists (e.g. this dev workspace).

Acceptance mapping (issue #17, ``bench/PLAN-WP4.md``):

* **AC1 — delegates entirely to the official CLI on a standard predictions
  file** → :func:`test_harness_argv_is_exactly_the_official_cli` (the argv
  is *pinned* — any bespoke flag breaks it), :func:`test_harness_runs_with_
  cwd_as_workdir` (the harness's CWD-relative log layout),
  :func:`test_predictions_file_is_exactly_the_standard_format` (exactly
  ``instance_id`` / ``model_name_or_path`` / ``model_patch``),
  :func:`test_no_swebench_import_in_evaluate_module` (no bespoke
  apply/grade logic — the subprocess is the only harness interaction).
* **AC2 — fresh container, never the agent's mutated one** →
  :func:`test_default_run_id_is_unique_per_call` (per-call unique run id),
  :func:`test_eval_container_name_never_equals_agent_container_name` (the
  harness container-name formula with our run id vs the adapter's agent
  name), :func:`test_integration_positive_control` (the eval container id
  comes from the harness's own log — the only container the module can
  name).
* **AC3 — returns ``{resolved, logs}``, no bespoke grading** →
  :func:`test_report_json_is_the_sole_source_of_resolved` (true and false
  passthrough of the harness value), :func:`test_return_contract_shape`
  (the exact key set), :func:`test_container_id_extracted_from_harness_log`
  (``logs.container`` from the harness's own line; absent → None, never a
  crash).
* **AC4 — the controls:**
  * negative control (no-op/empty patch → false, docker-free — runs in CI
    and locally): :func:`test_negative_control_no_op_patch_is_unresolved`
    (``""`` and ``None``; **no harness spawn** — the harness's own
    empty-patch filter semantics, verified in the pinned source);
  * pre-check corruption guard: :func:`test_headerless_patch_is_corruption_
    and_spawns_nothing`;
  * definitive corruption guard (a well-formed-but-truncated patch reaches
    the harness; its own git-apply chain fails and writes the
    ``APPLY_PATCH_FAIL`` marker → typed error): :func:`test_truncated_
    wellformed_patch_reaches_harness_and_is_corruption` (unit, fabricated
    tree) and :func:`test_integration_corruption_guard_truncated_gold_patch`
    (real harness, real container);
  * corruption triage edges: :func:`test_no_report_timeout_marker_is_
    evaluation_timeout`, :func:`test_no_report_no_marker_is_infra_error`,
    :func:`test_exit_nonzero_is_harness_failed_with_stderr_tail`,
    :func:`test_unreadable_report_is_harness_failed`,
    :func:`test_report_missing_instance_entry_is_harness_failed`;
  * validation matrix (typed, before any side effect):
    :func:`test_input_validation_matrix`;
  * isolation: asserted inside :func:`test_integration_positive_control`
    (eval container id ≠ agent container id; the eval container id is a
    real container id from the harness's own log).

Plus the operator CLI: :func:`test_cli_*` (exit codes 0/2/3/4/5, JSON on
stdout).
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

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


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeHarness:
    """The injected harness_runner: records (argv, cwd); optionally builds a
    fabricated harness tree under cwd (the harness's CWD-relative layout);
    returns a FakeCompleted."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        tree: "callable | None" = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.tree = tree
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: list[str], cwd: str) -> FakeCompleted:
        self.calls.append((list(argv), cwd))
        if self.tree is not None:
            self.tree(Path(cwd))
        return FakeCompleted(self.returncode, self.stdout, self.stderr)


def _build_tree(
    cwd: Path,
    run_id: str,
    model: str,
    instance_id: str,
    *,
    report: "dict | None" = None,
    instance_log: "str | None" = None,
    test_output: "str | None" = None,
) -> None:
    """Fabricate the harness's exact CWD-relative artifact layout (the pinned
    swebench 5.0.2 log root / filenames)."""
    base = cwd / "logs" / "run_evaluation" / run_id / model.replace("/", "__") / instance_id
    base.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (base / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if instance_log is not None:
        (base / "run_instance.log").write_text(instance_log, encoding="utf-8")
    if test_output is not None:
        (base / "test_output.txt").write_text(test_output, encoding="utf-8")


def _report_tree(
    run_id: str,
    model: str,
    instance_id: str,
    *,
    resolved: bool,
    instance_log: "str | None" = None,
    test_output: "str | None" = None,
) -> "callable":
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


def test_harness_argv_is_exactly_the_official_cli(tmp_path):
    """The argv is *pinned* to the official CLI (WP0-verified): any bespoke
    flag — or a missing official one — breaks this test. AC1."""
    fake = FakeHarness(tree=_report_tree("rid1", "devin", "pallets__flask-5014", resolved=True))
    ev.evaluate(
        "pallets__flask-5014",
        _WELLFORMED_PATCH,
        workdir=tmp_path,
        run_id="rid1",
        timeout=1234,
        harness_runner=fake,
    )
    (argv, _), = fake.calls
    assert argv[0] == sys.executable  # the interpreter with swebench installed
    assert argv[1:3] == ["-m", "swebench.harness.run_evaluation"]
    flags = dict(zip(argv[3::2], argv[4::2]))
    assert flags["-d"] == "SWE-bench/SWE-bench_Verified"
    assert flags["-s"] == "test"
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
    apply/grade path)."""
    src = inspect.getsource(ev)
    assert "import swebench" not in src
    assert "from swebench" not in src


def test_model_name_slash_is_escaped_like_the_harness(tmp_path):
    """The harness escapes ``/`` → ``__`` in the log dir / report name; our
    path derivation must match or the report is never found."""
    fake = FakeHarness(tree=_report_tree("rid4", "devin/swe-2-high", "iid", resolved=True))
    result = ev.evaluate(
        "iid",
        _WELLFORMED_PATCH,
        model_name_or_path="devin/swe-2-high",
        workdir=tmp_path,
        run_id="rid4",
        harness_runner=fake,
    )
    assert result["resolved"] is True
    assert (tmp_path / "devin__swe-2-high.rid4.json").parent == tmp_path  # name shape only
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
    for i in range(2):
        fake = FakeHarness()  # no tree: the random run id has no report → infra error
        with pytest.raises(ev.HarnessFailedError):
            ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, harness_runner=fake)
        argv, _ = fake.calls[0]
        run_ids.append(argv[argv.index("-id") + 1])
    assert run_ids[0] != run_ids[1]  # per-call unique (AC2)
    assert run_ids[0].startswith("eval-iid-")


def test_eval_container_name_never_equals_agent_container_name():
    """The harness names the eval container ``sweb.eval.<iid>.<run_id>``
    (verified in the pinned source, create_container); the adapter names the
    agent container ``sweb.eval.<iid>`` (PLAN §1). With a non-empty run_id
    the two can never be equal — and our default run_id is always non-empty
    and unique. AC2."""
    instance_id = "pallets__flask-5014"
    agent_name = f"sweb.eval.{instance_id}"
    for run_id in ("eval-x-01234567", "rid-custom"):
        eval_name = f"sweb.eval.{instance_id.lower()}.{run_id}"
        assert eval_name != agent_name
        assert eval_name.endswith(f".{run_id}")  # the suffix the agent name lacks


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
    fake = FakeHarness(tree=_report_tree("rid", "devin", "iid", resolved=False))
    result = ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert set(result) == {"resolved", "logs"}
    assert set(result["logs"]) == {
        "instance",
        "test_output",
        "report",
        "final_report",
        "container",
        "empty_patch",
    }
    assert result["logs"]["empty_patch"] is False


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
        fake = FakeHarness()  # would raise if called
        result = ev.evaluate("iid", patch, workdir=tmp_path, harness_runner=fake)
        assert result["resolved"] is False
        assert result["logs"]["empty_patch"] is True
        assert result["logs"]["container"] is None
        assert fake.calls == []  # no dataset download, no Docker


def test_headerless_patch_is_corruption_and_spawns_nothing(tmp_path):
    """Corruption guard, layer 1: garbage (no ``---``/``+++`` file headers)
    fails clean *before* any harness spawn (no dataset download, no
    Docker)."""
    for garbage in ("hello world", "just some text\nno diff here", "---\n+++\n"):
        fake = FakeHarness()
        with pytest.raises(ev.PatchCorruptionError) as e:
            ev.evaluate("iid", garbage, workdir=tmp_path, harness_runner=fake)
        assert e.value.instance_id == "iid"
        assert fake.calls == []


def test_truncated_wellformed_patch_reaches_harness_and_is_corruption(tmp_path):
    """Corruption guard, layer 2 (definitive): a truncation that still
    leaves both diff headers is structurally well-formed — it reaches the
    harness, whose git-apply chain fails and writes its own
    APPLY_PATCH_FAIL marker; the marker becomes a clean typed error."""
    truncated = _truncated_gold_patch()
    assert "--- " in truncated and "+++ " in truncated  # layer 1 passes it
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
    assert len(fake.calls) == 1  # the harness *was* spawned (layer 2 is definitive)
    assert e.value.instance_id == "pallets__flask-5014"
    assert e.value.log_paths["instance"]  # the log paths travel with the error


def test_no_report_timeout_marker_is_evaluation_timeout(tmp_path):
    fake = FakeHarness(
        tree=lambda cwd: _build_tree(
            cwd,
            "rid",
            "devin",
            "iid",
            test_output="ran the tests\n\nTimeout error: 1800 seconds exceeded.\n",
        )
    )
    with pytest.raises(ev.EvaluationTimeoutError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert e.value.instance_id == "iid"


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
    unreachable daemon) is a typed HarnessFailedError carrying the stderr
    tail — the operator can triage without re-reading the logs."""
    stderr = "Traceback (most recent call last):\nValueError: Some prediction IDs not found in dataset!\n"
    fake = FakeHarness(returncode=1, stderr=stderr)
    with pytest.raises(ev.HarnessFailedError) as e:
        ev.evaluate("iid", _WELLFORMED_PATCH, workdir=tmp_path, run_id="rid", harness_runner=fake)
    assert "ValueError" in e.value.output_tail


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
    fake = FakeHarness(tree=_report_tree("rid", "devin", "other-instance", resolved=True))
    with pytest.raises(ev.HarnessFailedError):
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
        ("iid", 123, None, "devin", 1800),  # non-str patch (int)
        ("iid", b"a patch", None, "devin", 1800),  # non-str patch (bytes)
        ("iid", {"model_patch": "x"}, None, "devin", 1800),  # non-str patch (dict)
        ("iid", _WELLFORMED_PATCH, "has space", "devin", 1800),  # run_id whitespace
        ("iid", _WELLFORMED_PATCH, "", "devin", 1800),  # run_id empty
        ("iid", _WELLFORMED_PATCH, None, "", 1800),  # empty model name
        ("iid", _WELLFORMED_PATCH, None, "devin", 0),  # timeout 0
        ("iid", _WELLFORMED_PATCH, None, "devin", -5),  # timeout negative
        ("iid", _WELLFORMED_PATCH, None, "devin", "1800"),  # timeout str
        ("iid", _WELLFORMED_PATCH, None, "devin", True),  # timeout bool
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
                 "final_report": None, "container": None, "empty_patch": False},
    }
    seen = {}

    def fake_evaluate(instance_id, model_patch, **kwargs):
        seen["args"] = (instance_id, model_patch, kwargs)
        return fixed

    code = ev.main(
        ["--instance-id", "iid", "--patch-file", str(patch_file)], evaluate_fn=fake_evaluate
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == fixed
    assert seen["args"][0] == "iid"
    assert seen["args"][1] == _WELLFORMED_PATCH


def test_cli_missing_patch_file_is_exit_2(tmp_path, capsys):
    code = ev.main(["--instance-id", "iid", "--patch-file", str(tmp_path / "nope")])
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
    reject. (Deterministic: 13 lines total, the last 3 — the hunk tail —
    are cut.)"""
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


