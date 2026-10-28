"""WP6a (issue #21) — the ``live_pilot`` harness tests.

Zero-quota CI: the full pilot path is driven through the WP2
``mock_devin`` double (the ``--devin-bin`` CLI leg and the injected
``make_session`` seam); the one test that touches the real ``devin``
binary is ``@pytest.mark.bench_live`` (skipped unless ``BENCH_LIVE=1``
— PLAN §6: CI never consumes quota).

Acceptance mapping (issue #21, PLAN-WP6A.md):

* the ``BENCH_LIVE`` gate (the issue's "testable by" leg): a real (non
  dry-run) invocation without the flag is refused (exit 3); a dry run
  is zero-quota (config + instance-file validation, budget math, the
  14-item checklist skeleton) → :func:`test_dry_run_is_zero_quota_and
  _writes_skeleton`, :func:`test_live_refused_without_bench_live`
* the 3-instance sequential pilot (AC1, mock-driven — the real-binary
  conformance leg rides on the delegated live run + the ``bench_live``
  smoke below): the full pipeline (pre-flight → model gate → sessions →
  patch extraction → checkpoint → pruning → WP5 report) →
  :func:`test_mock_driven_full_pilot_sandbox`
* the failure-mode mapping (the WP2 modes through the harness) →
  :func:`test_mock_failure_mode_mapping`
* the pre-flight / resume paths (AC3 — one intentional interrupt): the
  ``--interrupt-after`` abort (exit 42, the host-crash class) + the
  ``--resume`` run skips the finished instance (never re-run — the
  pass@1 semantics), no duplicate checkpoint lines, the session
  arithmetic (1 + 2 = 3 across the run pair) →
  :func:`test_interrupt_and_resume_pass1_semantics` (+
  :func:`test_resume_without_flag_refuses_silent_rerun`)
* the pre-flight failure → the batch-level ``preflight_failed`` marker
  (no session burned) → :func:`test_pre_flight_failure_batch_marker`
* the fail-closed model gate (a paid model is refused before any
  session) → :func:`test_model_gate_refuses_paid_model`
* the calibration + checklist math (AC2 / AC4) →
  :func:`test_calibrate_math`, :func:`test_checklist_verdict_gating`
* the PLAN §4 patch guards → :func:`test_extract_patch_guards`
* the host-check verdict leg (the documented no-container fallback) →
  :func:`test_host_check_verdicts` (stubbed subprocess — zero quota, no
  network)
* the sandbox hygiene (the leak guard + the ephemeral XDG + the env
  hygiene, PLAN §3.2–§3.6) → :func:`test_prepare_sandbox_instance`
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import acp_client as ac
import live_pilot as lp

MOCK_PATH = str(Path(__file__).resolve().parent.parent / "mock_devin.py")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _git(p: Path, *args: str) -> "subprocess.CompletedProcess":
    return subprocess.run(["git", "-C", str(p), *args],
                         capture_output=True, check=False, timeout=120)


def _make_repo(path: Path, name: str = "base.py") -> str:
    """A minimal git repo (one commit); returns the commit hash."""
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(f"# {name}\nprint('base')\n")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "pilot@test.local")
    _git(path, "config", "user.name", "Pilot")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    return _git(path, "rev-parse", "HEAD").stdout.decode().strip()


def _instances_file(path: Path, entries: list) -> str:
    doc = {"_meta": {"source": "test fixture"},
           "instances": entries}
    path.write_text(json.dumps(doc))
    return str(path)


def _entry(i: int, repo_path: str, base_commit: str,
          f2p=None, test_patch: str = "") -> dict:
    return {
        "instance_id": f"test__repo{i}-1",
        "repo": f"test/repo{i}",
        "repo_url": repo_path,
        "base_commit": base_commit,
        "problem_statement": "Fix the thing.",
        "FAIL_TO_PASS": f2p or [f"tests/test_r{i}.py::test_a"],
        "PASS_TO_PASS": [],
        "test_patch": test_patch,
    }


def _mock_pilot(tmp: Path, mode: str = "sandbox", **kw) -> int:
    """The CLI path with the mock as the devin binary (zero quota)."""
    args = ["--mode", mode, "--devin-bin", MOCK_PATH,
            "--largest-bytes", "1048576"]
    args += kw.pop("args", [])
    return lp.main(args + kw.pop("cli", []))


def _run(cfg: lp.PilotConfig, make_session=None) -> int:
    return lp.run_pilot(cfg, make_session=make_session)


def _cfg(tmp: Path, inst_file: str, **kw) -> lp.PilotConfig:
    base = dict(mode="sandbox", devin_bin=MOCK_PATH,
                instances_file=inst_file, workdir=str(tmp),
                largest_bytes=1048576)
    base.update(kw)
    return lp.PilotConfig(**base)


def _mock_make_session(modes: dict):
    """The injected seam: per-instance mock mode (keyed by instance id;
    a single string = every instance)."""
    def make(spec):
        mode = spec.instance["instance_id"]
        m = (modes.get(mode, "ok") if isinstance(modes, dict)
             else modes)
        return ac.AcpSession(
            spec.devin_bin, model=spec.model, cwd=spec.cwd,
            env=spec.env,
            policy=ac.SwebenchPolicy(workdir=spec.cwd),
            prompt_timeout_s=spec.wall_s,
            extra_args=["--mode", m])
    return make


def _read_jsonl(path: Path) -> list:
    out = []
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _three_repos(tmp: Path) -> "tuple[list, list, str]":
    repos = []
    for i in range(3):
        r = tmp / f"srcrepo{i}"
        commit = _make_repo(r)
        repos.append(_entry(i, str(r), commit))
    return repos


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    return monkeypatch


# ---------------------------------------------------------------------------
# the gate + the dry run
# ---------------------------------------------------------------------------

def test_dry_run_is_zero_quota_and_writes_skeleton(tmp_path, live,
                                                   monkeypatch):
    """A dry run never requires the flag's *effect* (it validates the
    config + the instance file + the budget math + the checklist
    skeleton) — and never spawns devin (the mock is passed only so the
    devin_bin field is a real path; it is never executed)."""
    monkeypatch.delenv(lp.LIVE_ENV, raising=False)
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    rc = lp.main(["--dry-run", "--devin-bin", MOCK_PATH,
                  "--instances", inst,
                  "--workdir", str(tmp_path / "wd"),
                  "--run-id", "dry"])
    assert rc == lp.EXIT_OK
    md = (tmp_path / "wd" / "wp6a-go-no-go.md").read_text()
    # the 14-item skeleton
    for i in range(1, 15):
        assert f"| {i} |" in md, md
    assert "dry-run" in md


def test_live_refused_without_bench_live(tmp_path, monkeypatch):
    monkeypatch.delenv(lp.LIVE_ENV, raising=False)
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    rc = lp.main(["--devin-bin", MOCK_PATH, "--instances", inst,
                  "--workdir", str(tmp_path / "wd"),
                  "--run-id", "x", "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_LIVE_REFUSED


def test_config_error_bad_mode(tmp_path, live, monkeypatch):
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    with pytest.raises(lp.PilotConfigError):
        lp.PilotConfig(mode="warp")


def test_instances_file_validation(tmp_path, live):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"instances": []}))
    with pytest.raises(lp.PilotConfigError):
        lp.load_instances(str(good))  # empty
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"instances": [
        {"instance_id": "a", "repo": "a/b", "base_commit": "x"}]}))
    with pytest.raises(lp.PilotConfigError):
        lp.load_instances(str(bad))  # missing required fields
    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps({"instances": [
        {"instance_id": "a", "repo": "a/b", "base_commit": "x",
         "problem_statement": "p", "FAIL_TO_PASS": [],
         "PASS_TO_PASS": []},
        {"instance_id": "a", "repo": "a/b", "base_commit": "x",
         "problem_statement": "p", "FAIL_TO_PASS": [],
         "PASS_TO_PASS": []}]}))
    with pytest.raises(lp.PilotConfigError):
        lp.load_instances(str(dup))


# ---------------------------------------------------------------------------
# the mock-driven full pilot (AC1's pipeline, zero quota)
# ---------------------------------------------------------------------------

def test_mock_driven_full_pilot_sandbox(tmp_path, live):
    """The full 3-instance sequential pipeline against the mock:
    pre-flight → model gate → 3 sessions → patch extraction → the
    checkpoint (3 lines) → pruning (the agent checkout removed, the
    artifacts + the clone cache retained) → the WP5 report
    (report.md + all_preds.jsonl). With the host check off (the
    documented no-container skip), the terminal status is the retry
    class ``eval_error`` with the ``skip_reason`` — the gate does not
    depend on the eval verdict."""
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    rc = lp.main(["--mode", "sandbox", "--devin-bin", MOCK_PATH,
                  "--instances", inst, "--workdir", str(wd),
                  "--run-id", "t1", "--no-host-check",
                  "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_OK

    lines = _read_jsonl(wd / "run-t1.jsonl")
    assert len(lines) == 3
    ids = [l["instance_id"] for l in lines]
    assert len(set(ids)) == 3
    for l in lines:
        assert l["status"] == "eval_error", l
        m = l["meta"]
        assert m["eval_skip_reason"] == "no_container_runtime"
        # the WP5 reference contract on every line
        for k in ("artifacts_dir", "patch_diff", "trajectory",
                  "eval_log"):
            assert k in m
        assert m["patch_diff"] == "patch.diff"
        assert (wd / "runs" / l["instance_id"] / "patch.diff").is_file()
        assert (wd / "runs" / l["instance_id"] / "trajectory.jsonl").is_file()
        # the mock's canned usage (the WP0-shape object, captured
        # verbatim by the WP1 client)
        assert m["usage"]["totalTokens"] == 12026
        # pruning (PLAN §7): the agent checkout is removed; the bare
        # clone cache is retained (the shared base layer)
        assert not (wd / "runs" / l["instance_id"] / "testbed").exists()
    assert (wd / "clone-cache").is_dir()
    # the WP5 report + leaderboard artifacts
    assert (wd / "report" / "report.md").is_file()
    # the WP5 cross-check (regression: the artifacts_dir reference once
    # pointed at <workdir>/<iid>/ while the files were written under
    # <workdir>/runs/<iid>/ — the live pilot's ArtifactCheckError)
    import report as rp
    assert rp.crosscheck(lines, wd) == []
    finals = {l["instance_id"]: l for l in lines}
    assert rp.check_artifacts(
        str(wd / "report" / "all_preds.jsonl"), finals, wd) == []
    preds = (wd / "report" / "all_preds.jsonl").read_text().splitlines()
    assert len(preds) == 3
    for p in preds:
        row = json.loads(p)
        assert set(row) == {"instance_id", "model_name_or_path",
                           "model_patch"}
    # the go/no-go record
    gng = (wd / "wp6a-go-no-go.md").read_text()
    assert "**Verdict:" in gng
    # the state file (the interrupt/resume bookkeeping)
    state = json.loads((wd / "pilot-t1.state.json").read_text())
    assert state["interrupted"] is False


def test_mock_failure_mode_mapping(tmp_path, live):
    """The WP2 failure modes through the harness: ``hang`` →
    ``timeout`` (the 45-min cap analogue — here a 3 s cap; the WP1
    client already sent session/cancel + group-killed), ``crash`` →
    ``agent_error``, ``quota`` → ``quota_limited``, ``no_patch`` →
    ``no_patch``."""
    cases = (("hang", "timeout", 3.0),
             ("crash", "agent_error", 30.0),
             ("quota", "quota_limited", 30.0),
             ("no_patch", "no_patch", 30.0))
    for i, (mode, want, wall) in enumerate(cases):
        r = tmp_path / f"src{i}"
        commit = _make_repo(r)
        inst = _instances_file(
            tmp_path / f"inst{i}.json",
            [_entry(i, str(r), commit)])
        wd = tmp_path / f"wd{i}"
        cfg = _cfg(tmp_path, str(inst), run_id=f"f{i}",
                   workdir=str(wd), max_wall_s=wall)
        rc = _run(cfg, make_session=_mock_make_session({
            f"test__repo{i}-1": mode}))
        assert rc == lp.EXIT_OK, (mode, rc)
        lines = _read_jsonl(wd / f"run-f{i}.jsonl")
        assert len(lines) == 1, (mode, lines)
        assert lines[0]["status"] == want, (mode, lines)
        # the trajectory is recorded on every terminal path (the
        # stream-ordered transcript)
        assert (wd / "runs" / f"test__repo{i}-1" / "trajectory.jsonl") \
            .is_file()


def test_interrupt_and_resume_pass1_semantics(tmp_path, live):
    """AC3 — the intentional interrupt + the resume, zero quota (mock):
    run 1 (``interrupt_after=1``) finishes instance 1 (the mock
    ``no_patch`` mode — a **never-re-run** terminal status) and aborts
    before instance 2 (exit 42; the remaining instances have no
    checkpoint line — the WP3b host-crash class); run 2 (``--resume``)
    skips instance 1 (never re-run — no session spent on it) and runs
    2–3. Assertions: exactly one line per finished instance (no
    duplicates), instance 1's line byte-identical across the pair, the
    session arithmetic (1 + 2 = 3), the checklist item 11 PASS on the
    resume run."""
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    modes = {"test__repo0-1": "no_patch",
             "test__repo1-1": "ok",
             "test__repo2-1": "ok"}

    cfg1 = _cfg(tmp_path, str(inst), run_id="pair", workdir=str(wd),
               interrupt_after=1)
    rc1 = _run(cfg1, make_session=_mock_make_session(modes))
    assert rc1 == lp.EXIT_INTERRUPT
    lines1 = _read_jsonl(wd / "run-pair.jsonl")
    assert len(lines1) == 1
    assert lines1[0]["instance_id"] == "test__repo0-1"
    assert lines1[0]["status"] == "no_patch"
    first_line_bytes = (wd / "run-pair.jsonl").read_text()
    state1 = json.loads((wd / "pilot-pair.state.json").read_text())
    assert state1["interrupted"] is True
    assert state1["completed_this_invocation"] == 1

    cfg2 = _cfg(tmp_path, str(inst), run_id="pair", workdir=str(wd),
               resume=True)
    rc2 = _run(cfg2, make_session=_mock_make_session(modes))
    assert rc2 == lp.EXIT_OK
    lines2 = _read_jsonl(wd / "run-pair.jsonl")
    assert len(lines2) == 3, lines2
    assert [l["instance_id"] for l in lines2] == [
        "test__repo0-1", "test__repo1-1", "test__repo2-1"]
    # instance 1 was NOT re-run: its line is byte-identical and there
    # is exactly one of them
    assert (wd / "run-pair.jsonl").read_text().startswith(first_line_bytes)
    assert sum(1 for l in lines2
               if l["instance_id"] == "test__repo0-1") == 1
    # the session arithmetic: 1 (run 1) + 2 (run 2) = 3
    state2 = json.loads((wd / "pilot-pair.state.json").read_text())
    assert state2["completed_this_invocation"] == 2
    assert state2["interrupted"] is False
    # the checklist (item 11) on the resume run
    gng = (wd / "wp6a-go-no-go.md").read_text()
    row11 = [l for l in gng.splitlines()
            if l.startswith("| 11 |")][0]
    assert "PASS" in row11, row11


def test_resume_without_flag_refuses_silent_rerun(tmp_path, live):
    """A second invocation over an existing checkpoint WITHOUT
    ``--resume`` refuses (exit 2) — a finished (or partially finished)
    run is never silently re-run (the pass@1 contract). Two
    instances: the interrupt fires after the first (one left to run).
    """
    r0 = tmp_path / "src0"
    c0 = _make_repo(r0)
    r1 = tmp_path / "src1"
    c1 = _make_repo(r1)
    inst = _instances_file(tmp_path / "inst.json",
                          [_entry(0, str(r0), c0),
                           _entry(1, str(r1), c1)])
    wd = tmp_path / "wd"
    cfg1 = _cfg(tmp_path, str(inst), run_id="rr", workdir=str(wd),
               interrupt_after=1)
    assert _run(cfg1, make_session=_mock_make_session(
        "no_patch")) == lp.EXIT_INTERRUPT
    cfg2 = _cfg(tmp_path, str(inst), run_id="rr", workdir=str(wd))
    assert _run(cfg2, make_session=_mock_make_session("no_patch")) \
        == lp.EXIT_CONFIG


def test_pre_flight_failure_batch_marker(tmp_path, live):
    """A failed pre-flight leg (the auth leg — a devin stand-in that
    serves the mock's models list but fails ``auth status``) aborts
    before any session is burned and records the batch-level
    ``preflight_failed`` marker (``instance_id: null`` — it consumes no
    queue slot on resume)."""
    r = tmp_path / "src"
    commit = _make_repo(r)
    inst = _instances_file(tmp_path / "inst.json",
                          [_entry(0, str(r), commit)])
    wd = tmp_path / "wd"
    bad = tmp_path / "bad-devin"
    bad.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"models\" ]; then exec " + sys.executable + " "
        + MOCK_PATH + " \"$@\"; fi\n"
        "exit 3\n")
    bad.chmod(0o755)
    cfg = _cfg(tmp_path, str(inst), run_id="pf", workdir=str(wd),
              devin_bin=str(bad))
    rc = _run(cfg)
    assert rc == lp.EXIT_RUN_FAILED
    lines = _read_jsonl(wd / "run-pf.jsonl")
    assert len(lines) == 1
    assert lines[0]["instance_id"] is None
    assert lines[0]["status"] == "preflight_failed"
    assert "auth" in lines[0]["meta"]["reason"]


def test_model_gate_refuses_paid_model(tmp_path, live):
    """The fail-closed gate (WP3b): a paid model (``swe-1-6`` in the
    mock's models-list fixture) is refused **before any session is
    built** — no quota, no instance line."""
    r = tmp_path / "src"
    commit = _make_repo(r)
    inst = _instances_file(tmp_path / "inst.json",
                          [_entry(0, str(r), commit)])
    wd = tmp_path / "wd"
    cfg = _cfg(tmp_path, str(inst), run_id="gate", workdir=str(wd),
              model="swe-1-6")
    rc = _run(cfg, make_session=_mock_make_session("ok"))
    assert rc == lp.EXIT_RUN_FAILED
    assert not (wd / "run-gate.jsonl").exists() or \
        len(_read_jsonl(wd / "run-gate.jsonl")) == 0


# ---------------------------------------------------------------------------
# the PLAN §4 patch guards
# ---------------------------------------------------------------------------

def test_extract_patch_guards(tmp_path, live):
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    # a tracked edit
    (repo / "mod.py").write_text("# mod.py\nprint('changed')\n")
    # an included untracked (a test dir)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_new.py").write_text("def test_new(): pass\n")
    # an excluded untracked (the exclusion patterns)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"\x00x")
    # a binary untracked (the binary guard)
    (repo / "tests" / "blob.bin").write_bytes(b"\x00\x01\x02")
    ext = lp.extract_patch(str(repo))
    assert "mod.py" in ext["diff"]
    assert "test_new.py" in ext["diff"]
    assert "mod.cpython-311.pyc" not in ext["diff"]
    assert "blob.bin" not in ext["diff"]
    skipped_paths = [p for p, _ in ext["skipped"]]
    # the excluded dir is matched at the directory level (the porcelain
    # collapses untracked dirs to a trailing-slash entry — a
    # ``$``-anchored pattern would miss it, and the generated ``.py``
    # artifacts inside would leak into the patch); the binary is the
    # per-file verdict
    assert "__pycache__/" in skipped_paths
    assert "tests/blob.bin" in skipped_paths
    skipped_why = {p: w for p, w in ext["skipped"]}
    assert "exclusion" in skipped_why["__pycache__/"]
    assert "binary" in skipped_why["tests/blob.bin"]
    # the intent-to-add staging is reverted (the working tree is not
    # modified beyond the agent's own edits)
    p = _git(repo, "status", "--porcelain")
    assert "mod.py" in p.stdout.decode()
    assert "tests/" in p.stdout.decode()  # collapsed untracked dir
    assert "A  " not in p.stdout.decode()  # no staged intent remains


def test_extract_patch_size_cap(tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    (repo / "tests").mkdir()
    (repo / "tests" / "big.py").write_text("x = 1\n" * 200_000)
    with pytest.raises(lp.PilotError, match="cap"):
        lp.extract_patch(str(repo))


# ---------------------------------------------------------------------------
# the host-check verdict leg (stubbed subprocess — zero quota)
# ---------------------------------------------------------------------------

def _fake_completed(rc, out=b"", err=b""):
    class _P:
        pass
    p = _P()
    p.returncode = rc
    p.stdout = out
    p.stderr = err
    return p


def _host_check_case(tmp_path, monkeypatch, f2p, p2p, pytest_out,
                     pip_rc=0, apply_rc=0, install_label="pip"):
    """One host-check verdict: the real method, the stubbed subprocess
    (the git/venv/pip/pytest legs), the real venv-less paths (all the
    spawned processes are fakes — zero quota, no network)."""
    inst = {
        "instance_id": "test__host-1", "repo": "test/host",
        "base_commit": "abc123", "problem_statement": "x",
        "FAIL_TO_PASS": f2p, "PASS_TO_PASS": p2p,
        "test_patch": "diff --git a/t.py b/t.py\n",
        "repo_url": "file:///nonexistent",
    }
    spec = lp.SessionSpec(
        instance=inst, mode="sandbox", devin_bin="x", model="m",
        cwd=str(tmp_path), env={}, wall_s=30.0, host_check=True,
        workdir=str(tmp_path))
    s = lp.PilotSession(spec)

    def fake_run(cmd, **kw):
        a = [str(x) for x in cmd]
        if a[0] == "git":
            if "--bare" in a:
                return _fake_completed(0)
            if "apply" in a:
                return _fake_completed(apply_rc)
            return _fake_completed(0)  # clone / checkout
        if a[0].endswith("pip"):
            # check the ARGS, not the join — the tmp path itself contains
            # the substring "pytest" (/tmp/pytest-of-.../)
            if "pytest" in a[1:]:
                return _fake_completed(0)
            return _fake_completed(pip_rc)
        if a[0].endswith("python") and "-m" in a \
                and "pytest" in a:
            return _fake_completed(1 if b"FAILED" in pytest_out else 0,
                                  out=pytest_out)
        return _fake_completed(0)  # venv

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    status = s._host_check("patch body")
    return status, s


def test_host_check_verdicts_all_pass(tmp_path, monkeypatch):
    out = b"PASSED tests/test_a.py::test_a\n" \
         b"PASSED tests/test_b.py::test_b\n" \
         b"==================== 2 passed in 0.1s ====================\n"
    status, s = _host_check_case(
        tmp_path, monkeypatch, ["tests/test_a.py::test_a"],
        ["tests/test_b.py::test_b"], out)
    assert status == "resolved"
    assert s.meta["graded_by"] == "host_check(pytest -rA)"
    assert s.meta["eval_log"] == "eval.log"
    assert s.meta["patch_diff"] is None  # set by run(), not here
    assert s.meta["eval_skip_reason"] is None


def test_host_check_verdicts_f2p_fail(tmp_path, monkeypatch):
    out = b"FAILED tests/test_a.py::test_a\n" \
         b"PASSED tests/test_b.py::test_b\n" \
         b"================== 1 failed, 1 passed in 0.1s ==============\n"
    status, _ = _host_check_case(
        tmp_path, monkeypatch, ["tests/test_a.py::test_a"],
        ["tests/test_b.py::test_b"], out)
    assert status == "failed_test"


def test_host_check_verdicts_p2p_fail(tmp_path, monkeypatch):
    out = b"PASSED tests/test_a.py::test_a\n" \
         b"FAILED tests/test_b.py::test_b\n" \
         b"================== 1 failed, 1 passed in 0.1s ==============\n"
    status, _ = _host_check_case(
        tmp_path, monkeypatch, ["tests/test_a.py::test_a"],
        ["tests/test_b.py::test_b"], out)
    assert status == "failed_test"


def test_host_check_infra_failure_is_eval_error(tmp_path, monkeypatch):
    status, s = _host_check_case(
        tmp_path, monkeypatch, ["tests/test_a.py::test_a"], [], b"",
        pip_rc=1)
    assert status == "eval_error"
    assert s.meta["eval_skip_reason"]
    assert "pip install" in s.meta["eval_skip_reason"]


def test_parse_pytest_ra():
    out = ("PASSED tests/a.py::test_a\n"
           "FAILED tests/b.py::test_b\n"
           "ERROR tests/c.py::test_c\n"
           "SKIPPED tests/d.py::test_d\n"
           "==================== 1 error, 1 failed, 1 passed in 0.1s "
           "====================\n")
    res = lp._parse_pytest_ra(out)
    assert res == {"tests/a.py::test_a": "passed",
                  "tests/b.py::test_b": "failed",
                  "tests/c.py::test_c": "error",
                  "tests/d.py::test_d": "skipped"}
    assert lp._parse_pytest_ra("no summary here") is None


# ---------------------------------------------------------------------------
# the calibration + the checklist math (AC2 / AC4)
# ---------------------------------------------------------------------------

def test_calibrate_math():
    cfg = lp.PilotConfig()
    recs = [
        {"instance_id": "a", "meta": {
            "wall": {"session_s": 60.0},
            "usage": {"totalTokens": 100}}},
        {"instance_id": "b", "meta": {
            "wall": {"session_s": 120.0},
            "usage": {"totalTokens": 200}}},
        {"instance_id": "c", "meta": {
            "wall": {"session_s": 180.0}}},
    ]
    cal = lp.calibrate(recs, cfg)
    assert cal["per_session_wall_s"] == [60.0, 120.0, 180.0]
    assert cal["median_session_wall_s"] == 120.0
    assert cal["mean_session_wall_s"] == 120.0
    assert cal["visible_usage_seen"] is True
    assert cal["total_visible_tokens"] == 300
    assert cal["wall_limited_sessions_per_day"] == 86400 // 120
    assert cal["chosen_max_sessions_per_day"] == 10


def _checklist_setup(**over):
    base = dict(
        config=lp.PilotConfig(max_wall_s=2700.0),
        dry_run=False,
        gate={"allowed": True, "cost_tier": "Free", "reason": None},
        preflight_ok=True,
        preflight_failures=[],
        records=[
            {"instance_id": "a", "status": "resolved", "meta": {
                "wall": {"session_s": 60.0},
                "usage": {"totalTokens": 100},
                "permissions": {"requests": 1, "allowed": 1, "denied": 0},
                "vendor_notifications": 2, "orphan_check": True,
                "graded_by": "host_check(pytest -rA)",
                "patch_diff": "patch.diff",
                "pruning": {"removed": ["agent checkout"],
                           "failed": []},
                "leak_guard": {"remotes_after": [], "warnings": []},
                "leak_verify": {"remotes": [], "tags": [],
                               "clean": True}}},
        ],
        skipped_on_resume={},
        interrupted_this_run=None,
        interrupted_before=False,
        devin_ok=True,
        sessions_this_invocation=1,
        wall_budget_s=2700.0 * 10,
        wall_used_s=60.0,
    )
    base.update(over)
    return base


def test_checklist_verdict_gating(tmp_path, monkeypatch):
    # item 1 records the BENCH_LIVE state — the run-pair scenario below
    # is a post-run rendering of an opted-in pilot
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    # a completed run pair (the interrupt + the resume exercised):
    # item 11 PASS → GO
    items = lp.build_checklist(**_checklist_setup(
        config=lp.PilotConfig(max_wall_s=2700.0, resume=True),
        interrupted_before=True,
        skipped_on_resume={"a": "no_patch"}))
    assert len(items) == 14
    verdict, failed = lp._verdict(items)
    assert verdict == "GO" and not failed
    # item 13 is None (deviation-tolerant) yet the verdict holds
    item13 = [i for i in items if i["item"].startswith("13")][0]
    assert item13["pass"] is None
    # a failed orphan check → NO-GO (item 7 gates)
    items = lp.build_checklist(**_checklist_setup(
        records=[
            {"instance_id": "a", "status": "resolved", "meta": {
                "wall": {"session_s": 60.0},
                "permissions": {"requests": 0, "allowed": 0,
                               "denied": 0},
                "orphan_check": False}}]))
    verdict, failed = lp._verdict(items)
    assert verdict == "NO-GO"
    assert any("7" == f.split(" ", 1)[0] for f in failed)
    # the interrupted (pre-resume) run: item 11 is pending (None), not
    # a failure
    items = lp.build_checklist(**_checklist_setup(
        interrupted_this_run={"completed": 1},
        interrupted_before=False))
    item11 = [i for i in items if i["item"].startswith("11")][0]
    assert item11["pass"] is None
    # a resume that was NOT preceded by the intentional interrupt
    # (a crash/abort resume): item 11 fails — the AC3 path is not
    # exercised by a plain resume
    items = lp.build_checklist(**_checklist_setup(
        config=lp.PilotConfig(max_wall_s=2700.0, resume=True),
        interrupted_before=False,
        skipped_on_resume={"a": "no_patch"}))
    item11 = [i for i in items if i["item"].startswith("11")][0]
    assert item11["pass"] is False
    # item 10: a record without the pruning record (or with a failed
    # prune) → NO-GO
    items = lp.build_checklist(**_checklist_setup(
        records=[{
            "instance_id": "a", "status": "resolved", "meta": {
                "wall": {"session_s": 60.0},
                "permissions": {"requests": 0, "allowed": 0,
                               "denied": 0},
                "orphan_check": True,
                "leak_guard": {"remotes_after": [], "warnings": []},
                "leak_verify": {"remotes": [], "tags": [],
                               "clean": True}}}]))
    verdict, failed = lp._verdict(items)
    assert verdict == "NO-GO"
    assert any("10" == f.split(" ", 1)[0] for f in failed)
    # item 14: a dirty post-session verification → NO-GO (the guard
    # result gates — it is not assumed from devin_ok)
    items = lp.build_checklist(**_checklist_setup(
        records=[{
            "instance_id": "a", "status": "resolved", "meta": {
                "wall": {"session_s": 60.0},
                "permissions": {"requests": 0, "allowed": 0,
                               "denied": 0},
                "orphan_check": True,
                "pruning": {"removed": [], "failed": []},
                "leak_guard": {"remotes_after": [], "warnings": []},
                "leak_verify": {"remotes": ["origin"], "tags": [],
                               "clean": False}}}]))
    verdict, failed = lp._verdict(items)
    assert verdict == "NO-GO"
    assert any("14" == f.split(" ", 1)[0] for f in failed)
    # item 7: a close() failure (the orphan check never ran —
    # ``orphan_check`` is None) → NO-GO, not a pass
    items = lp.build_checklist(**_checklist_setup(
        records=[{
            "instance_id": "a", "status": "resolved", "meta": {
                "wall": {"session_s": 60.0},
                "permissions": {"requests": 0, "allowed": 0,
                               "denied": 0},
                "orphan_check": None, "close_error": "boom",
                "pruning": {"removed": [], "failed": []},
                "leak_guard": {"remotes_after": [], "warnings": []},
                "leak_verify": {"remotes": [], "tags": [],
                               "clean": True}}}]))
    verdict, failed = lp._verdict(items)
    assert verdict == "NO-GO"
    assert any("7" == f.split(" ", 1)[0] for f in failed)


# ---------------------------------------------------------------------------
# the sandbox hygiene (PLAN §3.2–§3.6)
# ---------------------------------------------------------------------------

def test_prepare_sandbox_instance(tmp_path, live):
    r = tmp_path / "src"
    commit = _make_repo(r)
    inst = _entry(0, str(r), commit)
    wd = tmp_path / "wd"
    out = lp.prepare_sandbox_instance(inst, str(wd))
    testbed = wd / "runs" / "test__repo0-1" / "testbed"
    assert (testbed / ".git").is_dir()
    # at the base commit
    p = _git(testbed, "rev-parse", "HEAD")
    assert p.stdout.decode().strip() == commit
    # the leak guard: no remote, no tags
    assert out["leak_guard"]["remotes_after"] == []
    # the ephemeral XDG (the host config isolated; the credentials
    # seeded when present)
    assert (wd / "runs" / "test__repo0-1" / "xdg" / "data" / "devin") \
        .is_dir()
    assert (wd / "runs" / "test__repo0-1" / "xdg" / "config").is_dir()
    # the env hygiene (PLAN §3.2/§3.4)
    env = out["env"]
    assert env["XDG_DATA_HOME"] == str(
        wd / "runs" / "test__repo0-1" / "xdg" / "data")
    assert env["XDG_CONFIG_HOME"] == str(
        wd / "runs" / "test__repo0-1" / "xdg" / "config")
    assert "DEVIN_REFUSAL_FALLBACK" not in env
    assert "DEVIN_MODEL" not in env


# ---------------------------------------------------------------------------
# the review findings' regressions (the PR #37 review round)
# ---------------------------------------------------------------------------

def test_exclusion_patterns_match_collapsed_and_nested():
    # the directory patterns must match the collapsed porcelain entry
    # (trailing slash) and files inside an excluded dir (a generated
    # ``.py`` under ``build/`` would otherwise leak into the patch)
    assert lp._exclude("build/")
    assert lp._exclude("build/lib/foo.py")
    assert lp._exclude("__pycache__/")
    assert lp._exclude("pkg/__pycache__/x.pyc")
    assert lp._exclude("pkg.egg-info/")
    assert lp._exclude(".coverage")
    assert lp._exclude("a/.coverage/")
    assert lp._exclude("htmlcov/")
    assert lp._exclude(".pytest_cache/")
    # non-excluded paths stay
    assert not lp._exclude("buildout.py")  # name match, not a dir
    assert not lp._exclude("src/foo.py")
    assert not lp._exclude("buildings/")  # no component boundary


def test_container_setup_writes_single_line_shim_and_captures_guard(
        tmp_path, live, monkeypatch):
    vers = tmp_path / "versions"
    vers.mkdir()
    monkeypatch.setenv("DEVIN_VERSIONS_DIR", str(vers))
    calls: list = []

    def fake_run(args, **kw):
        calls.append(list(args))
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    inst = _entry(0, "/tmp/x", "abc123")
    out = lp._container_setup(inst, str(tmp_path))
    shim = tmp_path / "devin-shim-test__repo0-1"
    assert shim.is_file()
    lines = shim.read_text().splitlines()
    assert lines[0] == "#!/bin/sh"
    # ONE command line (the review finding: a multi-line shim exec'd
    # ``docker exec -i`` with no container and died — the session
    # could never start in container mode)
    assert len(lines) == 2
    cmd = lines[1]
    assert cmd.startswith("exec docker exec -i ")
    assert "\\n" not in cmd
    assert "  -e" not in cmd  # no continuation-line flags
    assert "sweb.eval.test__repo0-1 /usr/local/bin/devin \"$@\"" in cmd
    assert "/home/lucas" not in cmd  # portable (the review finding)
    # the _versions bind uses the resolved host path (not a hardcoded
    # operator home)
    run_call = [a for a in calls if a[0] == "docker" and a[1] == "run"][0]
    assert f"{vers}:/devin-inject:ro" in run_call
    # the leak-guard record is captured (the checklist item 14
    # evidence — previously discarded)
    assert out["leak_guard"] == {"warnings": [], "remotes_after": []}


def test_container_mode_host_side_ops_use_host_copy(
        tmp_path, live, monkeypatch):
    # the agent runs in the container (cwd /testbed); the host-side
    # extraction + leak verification must run on a host COPY (the
    # review finding: ``git -C /testbed`` on the host failed)
    copy = tmp_path / "runs" / "test__repo0-1" / "testbed-copy"
    commit = _make_repo(copy, name="base.py")

    import subprocess as _sp
    real_run = _sp.run

    def fake_run(args, **kw):
        if args[0] == "docker":
            assert args[1] == "cp"
            return _fake_completed(0, b"")
        return real_run(args, **kw)

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    inst = _entry(0, "/src", commit)
    spec = lp.SessionSpec(
        instance=inst, mode="container", devin_bin="devin",
        model="swebench-swe-2-high", cwd="/testbed", env={},
        wall_s=600.0, host_check=False, workdir=str(tmp_path),
        container="sweb.eval.test__repo0-1")

    class _P:  # the prompt result (the WP0-shape object)
        stop_reason = "end_turn"
        usage = {"totalTokens": 1}
        updates = 1
        duration_s = 1.0
        raw = {}

    class FakeAcp:
        def __init__(self):
            self.stderr_tail = ""

        def start(self):
            pass

        def initialize(self):
            pass

        def create_session(self, cwd=None):
            pass

        def prompt(self, prompt, timeout=None):
            return _P()

        def close(self):
            pass

        def group_alive(self):
            return False

        def stderr_tail_now(self):
            return ""

        transcript = []
        vendor_notifications = []

    s = lp.PilotSession(spec, make_session=lambda sp: FakeAcp())
    s.start()
    # the "agent" edits the tree inside the container — the host copy
    # sees the same change (docker cp semantics, simplified)
    (copy / "base.py").write_text("# base.py\nprint('changed')\n")
    monkeypatch.setattr(lp.PilotSession, "_eval_container",
                        lambda self, patch: "resolved")
    status = s.run()
    s.close()
    assert status == "resolved"
    assert s._extract_repo_dir() == str(copy)
    assert s.meta["patch_diff"] == "patch.diff"
    assert (tmp_path / "runs" / "test__repo0-1" / "patch.diff") \
        .is_file()
    assert s.meta["leak_verify"]["clean"] is True
    assert s.meta["orphan_check"] is True


def test_aborted_run_records_state_and_fails(tmp_path, live):
    # the WP3 terminal abort (2 consecutive agent_error): the state is
    # recorded durably and the invocation FAILS — no GO checklist
    # renders for an aborted run (the review finding: an aborted
    # final state exited 0 and could still produce a GO verdict)
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    cfg = _cfg(tmp_path, inst)
    rc = lp.run_pilot(cfg, make_session=_mock_make_session("crash"))
    assert rc == lp.EXIT_RUN_FAILED
    state_files = list(tmp_path.glob("pilot-*.state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text())
    assert state["aborted"] is True
    assert state["final_state"] == "aborted"
    assert not (tmp_path / "wp6a-go-no-go.md").exists()
    # the abort is visible to the next (resume) invocation
    state2_src = state_files[0].read_text()
    assert json.loads(state2_src)["aborted"] is True


def test_stderr_tail_live_accessor(tmp_path, live):
    # ``stderr_tail`` is only populated at close(); the mid-session
    # read (the quota signal, the error paths) uses the drain
    # thread's live accessor (the review finding: the signal could
    # never fire)
    inst = _entry(0, "/x", "abc")
    spec = lp.SessionSpec(
        instance=inst, mode="sandbox", devin_bin="devin",
        model="m", cwd="/x", env={}, wall_s=600.0,
        host_check=False, workdir=str(tmp_path))

    class WithLive:
        stderr_tail = "post-close value"

        def stderr_tail_now(self):
            return "live tail"

    s = lp.PilotSession(spec)
    s._acp = WithLive()
    assert s._stderr_tail() == "live tail"

    class NoLive:
        stderr_tail = "captured at close"

    s._acp = NoLive()
    assert s._stderr_tail() == "captured at close"
    # the quota signal sees the live tail (not the empty pre-close
    # ``stderr_tail``)

    class QuotaLive(WithLive):
        def stderr_tail_now(self):
            return "Rate limit exceeded, try again later"

    s._acp = QuotaLive()
    res = ac.PromptResult(stop_reason="end_turn", usage=None, updates=1,
                         duration_s=1.0, raw={})
    assert s._quota_signal(res) is True


def test_devin_version_guards_missing_binary(tmp_path, live):
    # a missing binary / a probe failure is not a traceback: the
    # pre-flight's ``which devin`` is the loud gate that follows
    assert lp.devin_version(str(tmp_path / "no-such-binary")) \
        .startswith("unknown")


# ---------------------------------------------------------------------------
# the real-binary smoke (BENCH_LIVE — the WP0 probe analogue)
# ---------------------------------------------------------------------------

@pytest.mark.bench_live
def test_real_devin_smoke(tmp_path):
    """The one real-quota leg: a minimal ``devin acp`` handshake
    (initialize → session/new → a trivial prompt → close + group kill)
    in a scratch dir under the repo root (a trusted workspace root —
    WP0 §8: the untrusted-cwd dead turn is the failure mode this
    exercises against). Quota-cheap (one trivial ``swe-2-high``
    session, the WP0 run-2 analogue)."""
    if not (Path(__file__).resolve().parent.parent / "pilot_instances.json").is_file():
        pytest.skip("the pinned instance file is absent")
    if __import__("shutil").which("devin") is None:
        pytest.skip("the real devin binary is not on PATH")
    scratch = Path(__file__).resolve().parent.parent.parent / \
        "pilot-a-smoke"
    scratch.mkdir(exist_ok=True)
    marker = scratch / "wp6a_smoke.txt"
    if marker.exists():
        marker.unlink()
    s = ac.AcpSession("devin", model="swe-2-high", cwd=str(scratch),
                     prompt_timeout_s=180.0)
    s.start()
    info = s.initialize()
    assert info  # the agent info (the WP0 §1 surface)
    s.create_session(cwd=str(scratch))
    res = s.prompt(
        "Create a file named wp6a_smoke.txt containing exactly the "
        "text: wp6a ok — no other changes. Then stop.",
        timeout=180.0)
    assert res.stop_reason == "end_turn", res.stop_reason
    s.close()
    assert not s.group_alive()  # the no-orphan check (WP0 §1)
    assert marker.exists(), "the trivial task did not complete (the " \
        "WP0 §8 untrusted-cwd dead turn? check the cwd trust)"
