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
import report as rep
import runner as rr

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
        # the pruning record lands IN the persisted line (the round-3
        # item-10 fix — the hook ran before the line was merged +
        # persisted)
        assert isinstance(m.get("pruning"), dict)
        assert m["pruning"]["failed"] == []
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
    # the real ``-rA`` shapes (the review finding: the old parser keyed
    # on the raw line — a ``FAILED <id> - <reason>`` suffix landed in
    # the key, and a ``SKIPPED [N] <file>:<line>: <reason>`` line
    # never matched ``::`` at all):
    out = ("PASSED tests/a.py::test_a\n"
           "FAILED tests/b.py::test_b - AssertionError: 1 != 2\n"
           "ERROR tests/c.py::test_c - ImportError: cannot import\n"
           "SKIPPED [1] tests/d.py:45: needs network\n"
           "==================== 1 error, 1 failed, 1 passed in 0.1s "
           "====================\n")
    res = lp._parse_pytest_ra(
        out, ["tests/a.py::test_a", "tests/b.py::test_b",
              "tests/c.py::test_c", "tests/d.py::test_d"])
    assert res["tests/a.py::test_a"] == "passed"
    # the ``- <reason>`` suffix is not part of the key
    assert res["tests/b.py::test_b"] == "failed"
    assert "tests/b.py::test_b - AssertionError" not in res
    assert res["tests/c.py::test_c"] == "error"
    # the ``file:line`` SKIPPED is attributed to the node id under the
    # file (the ``file:line`` key itself is never a requested id)
    assert res["tests/d.py::test_d"] == "skipped"

    # a collection ``ERROR`` is a FILE-level line — it kills every
    # requested node id under the file (the old parser filed it under
    # ``missing`` instead)
    out2 = ("ERROR tests/e.py - NameError: name 'x' is not defined\n"
           "==================== 1 error in 0.1s ====================\n")
    res2 = lp._parse_pytest_ra(
        out2, ["tests/e.py::test_e1", "tests/e.py::test_e2"])
    assert res2["tests/e.py::test_e1"] == "error"
    assert res2["tests/e.py::test_e2"] == "error"

    # an unrequested node id is still parsed (the caller's missing
    # computation only looks at the requested set)
    res3 = lp._parse_pytest_ra(
        "PASSED tests/f.py::test_f\n", ["tests/g.py::test_g"])
    assert res3["tests/f.py::test_f"] == "passed"
    assert "tests/g.py::test_g" not in res3
    assert lp._parse_pytest_ra("no summary here", []) is None
    # a request with no own line and no file-level outcome is missing
    res4 = lp._parse_pytest_ra(
        "PASSED tests/h.py::test_h\n", ["tests/i.py::test_i"])
    assert "tests/i.py::test_i" not in res4


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
        resume_after_interrupt=False,
        devin_ok=True,
        sessions_used=1,
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
        resume_after_interrupt=True,
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
        resume_after_interrupt=False))
    item11 = [i for i in items if i["item"].startswith("11")][0]
    assert item11["pass"] is None
    # a resume that was NOT preceded by the intentional interrupt
    # (a crash/abort resume): item 11 fails — the AC3 path is not
    # exercised by a plain resume
    items = lp.build_checklist(**_checklist_setup(
        config=lp.PilotConfig(max_wall_s=2700.0, resume=True),
        resume_after_interrupt=False,
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
# the review round-2 regressions (the PR #37 second review)
# ---------------------------------------------------------------------------

def test_extract_patch_includes_nested_untracked_dirs(tmp_path, live):
    # the review finding: a collapsed untracked dir ≥2 components deep
    # (``?? src/newpkg/``) was dropped whole — the lexical gate ran on
    # the dir path (no source/test marker, not a ``.py``), so the
    # agent's new package never reached the patch. Now the collapsed
    # dir is expanded to its files and the guards run per file.
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    (repo / "src").mkdir()
    (repo / "src" / "newpkg").mkdir()
    (repo / "src" / "newpkg" / "__init__.py").write_text("# pkg\n")
    (repo / "src" / "newpkg" / "mod.py").write_text("x = 1\n")
    (repo / "src" / "tests").mkdir()
    (repo / "src" / "tests" / "test_new.py").write_text(
        "def test_new(): pass\n")
    ext = lp.extract_patch(str(repo))
    assert "src/newpkg/mod.py" in ext["diff"]
    assert "src/newpkg/__init__.py" in ext["diff"]
    assert "src/tests/test_new.py" in ext["diff"]


def test_extract_patch_excludes_generated_dirs(tmp_path, live):
    # the review finding: ``.venv/`` / ``node_modules/`` (and ``.tox``,
    # ``dist``, ``.mypy_cache``) passed the top-level gate, so every
    # ``.py`` under them was intent-to-added — a routine agent ``python
    # -m venv .venv`` blew the diff past the cap and the completed
    # session was discarded. Now the whole dir is excluded (a single
    # collapsed skip entry, no per-file walk of a 10k-file venv).
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    for d, fname in ((".venv", "lib/site-packages/site.py"),
                    ("node_modules", "pkg/index.js"),
                    (".tox", "py/test-x.py"),
                    ("dist", "out/build.py"),
                    (".mypy_cache", "cache/x.py")):
        dpath = repo / d
        fpath = dpath / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text("x = 1\n")
    ext = lp.extract_patch(str(repo))
    for d in (".venv", "node_modules", ".tox", "dist",
             ".mypy_cache"):
        assert f"{d}/" not in ext["diff"], d
        assert any(p == d + "/" and w == "exclusion pattern"
                   for p, w in ext["skipped"]), d


def test_extract_patch_unstages_pre_staged_junk(tmp_path, live):
    # the review finding: the pre-staged-junk path (the agent ran
    # ``git add`` on a junk file — porcelain ``"A "``) was untested.
    # The exclusion list applies regardless of staging state: the junk
    # is UNSTAGED, reported, and out of the diff.
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    (repo / "mod.py").write_text("print('changed')\n")
    (repo / "__pycache__").mkdir()
    junk = repo / "__pycache__" / "mod.pyc"
    junk.write_bytes(b"\x00x")
    _git(repo, "add", "--", str(junk.relative_to(repo)))
    (repo / "tests").mkdir()
    binf = repo / "tests" / "blob.bin"
    binf.write_bytes(b"\x00\x01\x02")
    _git(repo, "add", "--", str(binf.relative_to(repo)))
    ext = lp.extract_patch(str(repo))
    assert "mod.py" in ext["diff"]
    assert "mod.pyc" not in ext["diff"]
    assert "blob.bin" not in ext["diff"]
    why = {p: w for p, w in ext["skipped"]}
    assert any("pre-staged" in w for w in why.values())
    # unstaged: the working tree is back to a plain untracked state
    p = _git(repo, "status", "--porcelain")
    assert "A  " not in p.stdout.decode()


def test_dry_run_verdict_na_and_removes_stale_record(tmp_path, live):
    # the review finding: the dry-run skeleton rendered ``Verdict:
    # NO-GO`` (``_verdict`` counted ``pass=None`` as failed) and the
    # early exits left a stale verdict file standing. Now: a dry run
    # renders ``n/a`` (it decides nothing) and the stale record from a
    # previous invocation is removed, not clobbered-in-place.
    wd = tmp_path / "wd"
    wd.mkdir()
    stale = wd / "wp6a-go-no-go.md"
    stale.write_text("# stale\n**Verdict: GO**\n")
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    rc = lp.main(["--mode", "sandbox", "--devin-bin", MOCK_PATH,
                 "--instances", inst, "--workdir", str(wd),
                 "--run-id", "dry", "--dry-run"])
    assert rc == lp.EXIT_OK
    gng = stale.read_text()
    assert "**Verdict: n/a**" in gng
    assert "**Verdict: GO**" not in gng
    assert "stale" not in gng


def test_preflight_binary_leg_missing_devin(tmp_path, live):
    # the review finding: the docstring deferred to a ``which devin``
    # pre-flight leg that did not exist. Now the ``binary`` leg is
    # first: a missing binary is a pre-flight failure (exit 4, the
    # batch marker — no session burned).
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    rc = lp.main(["--mode", "sandbox",
                 "--devin-bin", str(tmp_path / "no-such-devin"),
                 "--instances", inst, "--workdir", str(wd),
                 "--run-id", "bin", "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_RUN_FAILED
    lines = _read_jsonl(wd / "run-bin.jsonl")
    assert any(l["status"] == "preflight_failed"
               and (l.get("meta") or {}).get("phase") == "batch"
               for l in lines)


def test_checklist_uses_deduped_view_on_resume(tmp_path, live):
    # the review finding: the checklist iterated RAW checkpoint records
    # — a superseded retry-class line (an instance-level
    # ``preflight_failed`` carries no orphan/leak/pruning meta) flipped
    # items 7/10/14 on the resumed run, a false NO-GO for an instance
    # that completed cleanly. Now the checklist uses the deduped
    # last-line-per-instance view (the WP5 contract).
    r = tmp_path / "src"
    commit = _make_repo(r)
    inst = _instances_file(tmp_path / "inst.json",
                          [_entry(0, str(r), commit)])
    wd = tmp_path / "wd"
    wd.mkdir()
    # the artifact files the cross-check resolves (a ``resolved`` line
    # requires a ``patch_diff`` reference)
    adir = wd / "runs" / "test__repo0-1"
    adir.mkdir(parents=True)
    (adir / "patch.diff").write_text("diff --git a/x b/x\n+x = 1\n")
    (adir / "trajectory.jsonl").write_text("")
    (adir / "eval.log").write_text("ok\n")
    iid = "test__repo0-1"
    stale = {"instance_id": iid, "status": "preflight_failed",
             "ts": 1000.0,
             "meta": {"reason": "infra", "wall_s": 0.5,
                     "artifacts_dir": None, "patch_diff": None,
                     "trajectory": None, "eval_log": None}}
    good = {"instance_id": iid, "status": "resolved",
            "ts": 1001.0,
            "meta": {"wall_s": 60.0,
                    "wall": {"setup_s": 1.0, "session_s": 60.0,
                            "eval_s": 5.0},
                    "usage": {"totalTokens": 100},
                    "permissions": {"requests": 0, "allowed": 0,
                                   "denied": 0},
                    "vendor_notifications": 0,
                    "orphan_check": True,
                    "pruning": {"removed": ["agent checkout"],
                               "failed": []},
                    "leak_guard": {"remotes_after": [],
                                  "warnings": []},
                    "leak_verify": {"remotes": [], "tags": [],
                                   "clean": True},
                    "artifacts_dir": "runs/test__repo0-1",
                    "patch_diff": "patch.diff",
                    "trajectory": "trajectory.jsonl",
                    "eval_log": "eval.log"}}
    jsonl = wd / "run-dedup.jsonl"
    jsonl.write_text(
        json.dumps(stale) + "\n" + json.dumps(good) + "\n")
    # the previous invocation was the intentional interrupt (the state
    # file) — item 11's resume-after-interrupt leg
    (wd / "pilot-dedup.state.json").write_text(
        json.dumps({"interrupted": True, "aborted": False}))
    rc = lp.main(["--mode", "sandbox", "--devin-bin", MOCK_PATH,
                 "--instances", str(inst), "--workdir", str(wd),
                 "--run-id", "dedup", "--resume",
                 "--no-host-check", "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_OK
    gng = (wd / "wp6a-go-no-go.md").read_text()
    # the deduped view (the ``resolved`` line) passes items 7/10/14 —
    # the raw view (the superseded ``preflight_failed`` line) would
    # have failed them
    assert "**Verdict: GO**" in gng
    assert "NOT PERFORMED" not in gng


def test_repeat_resume_keeps_go_record(tmp_path, live):
    # the review finding: a repeat ``--resume`` on a finished run pair
    # rewrote the record to NO-GO (run 2 reset the state file's
    # ``interrupted`` flag) — the GO was not durable. Now the AC3
    # exercise is cumulative in the state (``resume_exercised``): the
    # third invocation still renders GO and re-runs nothing.
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    # the pair is FINISHED in the resume sense: every instance lands
    # in a never-re-run terminal status (``no_patch`` — a mock mode;
    # the host-check-off ``eval_error`` would be the retry class and
    # legitimately re-run on the third invocation)
    modes = {"test__repo0-1": "no_patch",
            "test__repo1-1": "no_patch", "test__repo2-1": "no_patch"}
    cfg1 = _cfg(tmp_path, str(inst), run_id="rr", workdir=str(wd),
               interrupt_after=1)
    assert _run(cfg1, make_session=_mock_make_session(modes)) \
        == lp.EXIT_INTERRUPT
    cfg2 = _cfg(tmp_path, str(inst), run_id="rr", workdir=str(wd),
               resume=True)
    assert _run(cfg2, make_session=_mock_make_session(modes)) \
        == lp.EXIT_OK
    assert "**Verdict: GO**" in (wd / "wp6a-go-no-go.md").read_text()
    state2 = json.loads((wd / "pilot-rr.state.json").read_text())
    assert state2["resume_exercised"] is True
    # the third invocation: the finished pair — no instance session
    # re-runs (the JSONL may gain batch-level marker lines — the
    # instance lines are the quota-relevant count), the record stays GO
    n_before = sum(1 for l in _read_jsonl(wd / "run-rr.jsonl")
                  if l.get("instance_id"))
    cfg3 = _cfg(tmp_path, str(inst), run_id="rr", workdir=str(wd),
               resume=True)
    assert _run(cfg3, make_session=_mock_make_session(modes)) \
        == lp.EXIT_OK
    assert sum(1 for l in _read_jsonl(wd / "run-rr.jsonl")
              if l.get("instance_id")) == n_before
    gng = (wd / "wp6a-go-no-go.md").read_text()
    assert "**Verdict: GO**" in gng
    state3 = json.loads((wd / "pilot-rr.state.json").read_text())
    assert state3["resume_exercised"] is True


# ---------------------------------------------------------------------------
# the third review round's regressions (PR #37 pr-review round 3)
# ---------------------------------------------------------------------------

def test_container_mode_spawn_cwd_is_host_path(tmp_path, live):
    # the BLOCKER: ``spec.cwd="/testbed"`` was passed to the host
    # ``Popen(cwd=...)`` — ``/testbed`` exists only inside the
    # container, so every container-mode run died on the first
    # instance (``FileNotFoundError``) before the docker-exec shim
    # ever ran. The devin process is a HOST process: the spawn cwd
    # is a host-side scratch dir; the agent's session cwd stays
    # ``/testbed`` (the shim's ``docker exec -w /testbed``).
    inst = _entry(0, "/x", "abc")
    wd = tmp_path / "wd"
    spec = lp.SessionSpec(
        instance=inst, mode="container", devin_bin="shim",
        model="m", cwd="/testbed", env={}, wall_s=600.0,
        host_check=False, workdir=str(wd),
        container="sweb.eval.test__repo0-1")
    s = lp.PilotSession(spec, make_session=lambda sp: sp)
    out = s._default_make_session(spec)  # constructs; does not spawn
    assert out._cwd != "/testbed"
    assert str(wd) in out._cwd  # a host-side path
    assert out._session_cwd == "/testbed"  # the agent's cwd
    # sandbox mode: the spawn cwd IS the session cwd
    spec2 = lp.SessionSpec(
        instance=inst, mode="sandbox", devin_bin="shim", model="m",
        cwd="/x", env={}, wall_s=600.0, host_check=False,
        workdir=str(wd))
    out2 = lp.PilotSession(spec2, make_session=lambda sp: sp)
    o2 = out2._default_make_session(spec2)
    assert o2._cwd == "/x"


def test_session_cwd_wins_in_session_new():
    # the ``session_cwd`` seam: the session/new payload carries the
    # SESSION cwd (the container's /testbed), not the spawn cwd
    s = ac.AcpSession("devin", cwd="/host/spawn", session_cwd="/testbed")
    assert s._session_cwd == "/testbed"
    # the fallback chain: explicit arg > session_cwd > cwd > default
    assert s._cwd == "/host/spawn"


def test_leak_guard_deletes_local_branches(tmp_path):
    # MAJOR: the clone leaves the default branch at the upstream tip
    # — PAST base_commit, past the instance's merged fix commit (one
    # ``git log`` away); ``verify_leak_guard`` checked only remotes
    # and tags, so item 14 reported clean while the gold patch was
    # reachable
    src = tmp_path / "src"
    base = _make_repo(src)
    (src / "fix.py").write_text("fix = True\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "fix")
    fix = _git(src, "rev-parse", "HEAD").stdout.decode().strip()
    assert fix != base
    tb = tmp_path / "tb"
    _git(tmp_path, "clone", str(src), str(tb))
    _git(tb, "checkout", "--detach", base)
    lp.apply_leak_guard(str(tb))
    v = lp.verify_leak_guard(str(tb), base)
    assert v["clean"] is True
    assert v["branches"] == []  # the branch is gone
    # a branch recreated at the fix tip (the leak) is caught
    _git(tb, "branch", "leak", fix)
    v2 = lp.verify_leak_guard(str(tb), base)
    assert v2["clean"] is False
    assert v2["branches_ahead_of_base"] == ["leak"]
    # a branch AT the base is not a leak (reachable from base)
    _git(tb, "branch", "atbase", base)
    v3 = lp.verify_leak_guard(str(tb), base)
    assert "atbase" not in v3["branches_ahead_of_base"]


def test_container_setup_guard_strips_branches(tmp_path, live, monkeypatch):
    # the in-image guard has the same branch hole as the host guard
    vers = tmp_path / "versions"
    vers.mkdir()
    monkeypatch.setenv("DEVIN_VERSIONS_DIR", str(vers))
    guard_calls: list = []

    def fake_run(args, **kw):
        a = [str(x) for x in args]
        if a[0] == "docker" and a[1] == "exec" and "-c" in a:
            script = a[a.index("-c") + 1]
            guard_calls.append(script)
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    inst = _entry(0, "/tmp/x", "abc123")
    lp._container_setup(inst, str(tmp_path))
    guard = [c for c in guard_calls
            if c.startswith("cd /testbed")]
    assert len(guard) == 1
    g = guard[0]
    assert "git checkout --detach" in g
    assert "git branch -D" in g  # the branch strip


def test_is_binary_fifo_does_not_block(tmp_path):
    # ``open()`` on a FIFO blocks indefinitely — an agent-created
    # fifo left in the tree must not hang the extraction
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert lp._is_binary(str(fifo)) is False  # returns, does not hang
    # a regular file with NUL bytes is still detected
    f = tmp_path / "b.bin"
    f.write_bytes(b"x\x00y")
    assert lp._is_binary(str(f)) is True
    t = tmp_path / "t.txt"
    t.write_text("plain\n")
    assert lp._is_binary(str(t)) is False


def test_extract_patch_non_ascii_filename_included(tmp_path):
    # the review finding: the line-mode ``git status --porcelain``
    # QUOTES special paths (a non-ASCII filename printed as
    # ``"w\303\251ird \303\261ame.py"``) — the file would be
    # silently dropped from the diff. ``-z`` gives raw paths.
    repo = tmp_path / "repo"
    commit = _make_repo(repo, name="mod.py")
    (repo / "tests").mkdir()
    non_ascii = "w\u00e9ird \u00f6ame.py"
    (repo / "tests" / non_ascii).write_text("x = 1\n")
    out = lp.extract_patch(str(repo))
    assert f"tests/{non_ascii}" in out["included"], out["included"]
    assert non_ascii in out["diff"]  # raw, unquoted, in the diff


def test_extract_patch_add_n_failure_raises(tmp_path, monkeypatch):
    # an unchecked ``git add -N`` (a failing pathspec) would leave
    # the intent-to-add missing and ``git diff HEAD`` would quietly
    # drop the agent's files — it must be loud
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    (repo / "tests").mkdir()
    (repo / "tests" / "new.py").write_text("x = 1\n")
    real_run = lp.subprocess.run

    def fake_run(cmd, **kw):
        a = [str(x) for x in cmd]
        if a[0] == "git" and "-N" in a:
            return _fake_completed(128, err=b"fatal: pathspec failed")
        return real_run(cmd, **kw)

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    with pytest.raises(lp.PilotError, match="git add -N failed"):
        lp.extract_patch(str(repo))


def test_host_check_spawn_failure_is_eval_error(tmp_path, monkeypatch):
    # a spawned-tool failure that is NOT a returncode — a
    # ``FileNotFoundError`` (a venv without ``bin/pip``) or a
    # ``subprocess.TimeoutExpired`` (a hung pip/pytest) — is the
    # ``eval_error`` infra class, not a run-killing fatal
    inst = {
        "instance_id": "test__host-1", "repo": "test/host",
        "base_commit": "abc123", "problem_statement": "x",
        "FAIL_TO_PASS": ["tests/test_a.py::test_a"], "PASS_TO_PASS": [],
        "test_patch": "", "repo_url": "file:///nonexistent",
    }
    spec = lp.SessionSpec(
        instance=inst, mode="sandbox", devin_bin="x", model="m",
        cwd=str(tmp_path), env={}, wall_s=30.0, host_check=True,
        workdir=str(tmp_path))
    s = lp.PilotSession(spec)

    def fake_run(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", "pip")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    assert s._host_check("patch") == "eval_error"
    assert "FileNotFoundError" in s.meta["eval_skip_reason"]


def test_prune_removes_eval_workdir_in_both_modes(tmp_path, live,
                                                 monkeypatch):
    # the WP4 eval workdir (potentially GBs per instance) was
    # removed only in the sandbox branch — container mode left it
    # behind on every instance
    inst = _entry(0, "/tmp/x", "abc123")

    def fake_run(cmd, **kw):
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    for mode in ("container", "sandbox"):
        wd = tmp_path / f"eval-{mode}"
        d = wd / "eval" / "test__repo0-1"
        d.mkdir(parents=True)
        (d / "big").write_text("x")
        cfg = lp.PilotConfig(
            mode=mode, devin_bin=MOCK_PATH, instances_file="x",
            workdir=str(wd))
        out = lp.prune_instance(cfg, str(wd), inst)
        assert not d.is_dir(), f"eval workdir not removed ({mode})"
        assert "eval workdir" in out["removed"]


def test_preflight_versions_leg_container(tmp_path, live, monkeypatch):
    # a missing ``_versions`` tree must surface at the zero-quota
    # pre-flight gate (container mode), not mid-batch in the setup
    def _raise():
        raise lp.PilotError("no _versions tree")

    monkeypatch.setattr(lp, "_host_versions_dir", _raise)
    cfg = lp.PilotConfig(
        mode="container", devin_bin=MOCK_PATH, instances_file="x",
        workdir=str(tmp_path))
    pf = lp.make_preflight(cfg)
    with pytest.raises(rr.PreflightFailed) as excinfo:
        pf(None)
    assert excinfo.value.check == "versions"


def test_session_env_sanitizes_credentials():
    env = {"PATH": "/usr/bin", "HOME": "/root",
           "GITHUB_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "y",
           "OPENAI_API_KEY": "z", "SSH_AUTH_SOCK": "/tmp/ssh",
           "PYTHONPATH": ""}
    out, stripped = lp._sanitize_session_env(env)
    assert "GITHUB_TOKEN" not in out
    assert "AWS_SECRET_ACCESS_KEY" not in out
    assert "OPENAI_API_KEY" not in out
    assert "SSH_AUTH_SOCK" not in out
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/root"
    assert out["PYTHONPATH"] == ""
    assert sorted(stripped) == sorted(
        ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY",
         "SSH_AUTH_SOCK"])


def test_prepare_sandbox_strips_credentials(tmp_path, live, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    r = tmp_path / "src"
    commit = _make_repo(r)
    inst = _entry(0, str(r), commit)
    out = lp.prepare_sandbox_instance(inst, str(tmp_path / "wd"))
    assert "GITHUB_TOKEN" not in out["env"]
    assert "GITHUB_TOKEN" in out["env_stripped"]


def test_run_failed_state_file_says_run_failed(tmp_path, live):
    # a host-crash (run_failed) invocation must not record
    # ``final_state: done`` in the durable state file
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)

    def boom(spec):
        raise RuntimeError("the host crashed")

    cfg = _cfg(tmp_path, inst)
    rc = lp.run_pilot(cfg, make_session=boom)
    assert rc == lp.EXIT_RUN_FAILED
    state_files = list(tmp_path.glob("pilot-*.state.json"))
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text())
    assert state["final_state"] == "run_failed"


def test_interrupted_run_verdict_not_go(tmp_path, live):
    # an interrupted invocation is a PARTIAL record (1/3 instances) —
    # no gating item requires all pinned instances to have a
    # completed line, so the partial record must not render a GO
    # the resume invocation would silently supersede
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    cfg = _cfg(tmp_path, inst, interrupt_after=1)
    rc = lp.run_pilot(cfg, make_session=_mock_make_session("ok"))
    assert rc == lp.EXIT_INTERRUPT
    gng = (tmp_path / "wp6a-go-no-go.md").read_text()
    assert "**Verdict: GO**" not in gng
    assert "n/a (interrupted" in gng


def test_main_catch_all_is_exit_4(tmp_path, live, monkeypatch):
    # a non-typed failure (an OSError writing the state file / the
    # artifacts) stays inside the documented exit-code contract —
    # exit 4 (the resume class), never an exit-1 traceback
    def _raise(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(lp, "run_pilot", _raise)
    rc = lp.main(["--dry-run", "--instances",
                 _instances_file(tmp_path / "i.json", [_entry(0, "/x", "a")]),
                 "--workdir", str(tmp_path)])
    assert rc == lp.EXIT_RUN_FAILED


# ---------------------------------------------------------------------------
# the fourth review round's regressions (PR #37 pr-review round 4)
# ---------------------------------------------------------------------------

def test_session_env_authoritative_not_overlaid(tmp_path, monkeypatch):
    # the BLOCKER: ``AcpSession.start`` built the child env as
    # ``dict(os.environ)`` overlaid by ``self._env`` — every key the
    # pilot stripped (and the DEVIN_MODEL / refusal pops) was
    # re-inherited from the host. Now a caller-supplied env is
    # authoritative; the host env is the default only for a bare
    # session (container mode's shim — whose agent env comes from
    # the ``docker exec -e`` flags, not its own env)
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env")
            raise OSError("the spawn is stopped — the env is captured")

    monkeypatch.setattr(lp.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_leak_check")
    s = ac.AcpSession("devin", env={"PATH": "/x", "HOME": "/y"})
    try:
        s.start()
    except OSError:
        pass
    assert captured["env"].get("GITHUB_TOKEN") is None
    assert captured["env"] == {"PATH": "/x", "HOME": "/y"}
    # a bare session (no env) still gets the host env
    captured.clear()
    s2 = ac.AcpSession("devin")
    try:
        s2.start()
    except OSError:
        pass
    assert captured["env"].get("GITHUB_TOKEN") == "ghp_leak_check"
    # the refusal fallback stays out in every case
    monkeypatch.setenv("DEVIN_REFUSAL_FALLBACK", "1")
    s3 = ac.AcpSession("devin", env={"PATH": "/x"})
    try:
        s3.start()
    except OSError:
        pass
    assert captured["env"].get("DEVIN_REFUSAL_FALLBACK") is None


def test_report_failure_keeps_interrupted_state(tmp_path, live,
                                                 monkeypatch):
    # a report failure on an interrupted pair must not clobber the
    # durable interrupt record (the follow-up --resume's item-11
    # gate reads interrupted_before — the clobber made AC3
    # unprovable and the pair render NO-GO)
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)

    def _raise(*a, **k):
        raise rep.ReportError("the report pipeline failed")

    monkeypatch.setattr(lp.rep, "generate", _raise)
    cfg = _cfg(tmp_path, inst, interrupt_after=1, host_check=False)
    rc = lp.run_pilot(cfg, make_session=_mock_make_session("ok"))
    assert rc == lp.EXIT_RUN_FAILED
    state = json.loads(
        next(tmp_path.glob("pilot-*.state.json")).read_text())
    assert state["interrupted"] is True
    assert state["final_state"] == "report_failed"


def test_budget_counts_superseded_attempts(tmp_path, live):
    # a superseded attempt (eval_error → rerun) burned a session +
    # wall the deduped view would silently drop — the gate must
    # count ATTEMPTS (all instance-id lines), like WP5's session
    # arithmetic
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    # run 1: instance 0 runs (mock ok + host check off →
    # eval_error, the documented skip)
    cfg1 = _cfg(tmp_path, str(inst), run_id="att", workdir=str(wd),
               interrupt_after=1, host_check=False)
    assert _run(cfg1, _mock_make_session("ok")) == lp.EXIT_INTERRUPT
    # run 2 (resume): instance 0 is RETRY (eval_error) → re-run as
    # no_patch (superseded line); instances 1–2 run (eval_error)
    modes2 = {"test__repo0-1": "no_patch",
             "test__repo1-1": "ok", "test__repo2-1": "ok"}
    cfg2 = _cfg(tmp_path, str(inst), run_id="att", workdir=str(wd),
               resume=True, host_check=False)
    assert _run(cfg2, _mock_make_session(modes2)) == lp.EXIT_OK
    # 4 attempt lines: i0×2 (the superseded pair) + i1 + i2 — the
    # deduped view would say 3
    gng = (wd / "wp6a-go-no-go.md").read_text()
    row12 = [l for l in gng.splitlines() if l.startswith("| 12 |")][0]
    assert "sessions (attempts, incl. reruns): 4" in row12, row12


def test_instance_id_charset_validation(tmp_path, live):
    # the id is joined into workdir paths and interpolated into the
    # shim's shell script — ``..`` / metacharacters are a config
    # error at validation time
    base = _entry(0, "/x", "abc")
    for bad in ("evil/../../x", "a b", "id;rm -rf /", "id|whoami"):
        b = dict(base)
        b["instance_id"] = bad
        f = tmp_path / f"bad{abs(hash(bad))}.json"
        _instances_file(f, [b])
        with pytest.raises(lp.PilotConfigError):
            lp.load_instances(str(f))
    good = dict(base)
    good["instance_id"] = "owner__repo-123"
    f = tmp_path / "good.json"
    _instances_file(f, [good])
    out = lp.load_instances(str(f))
    assert out[0]["instance_id"] == "owner__repo-123"


def test_oversize_patch_is_eval_error_not_fatal(tmp_path, live,
                                                monkeypatch):
    # an oversize patch is the instance class (eval_error —
    # recorded, the queue not aborted, a resume may retry), not a
    # run-killing fatal (the fatal re-burns the full session at
    # quota on every resume before hitting the same cap)
    monkeypatch.setattr(lp, "PATCH_SIZE_CAP_BYTES", 1)
    repos = _three_repos(tmp_path)
    inst = _instances_file(tmp_path / "inst.json", repos)
    wd = tmp_path / "wd"
    cfg = _cfg(tmp_path, str(inst), run_id="big", workdir=str(wd),
              interrupt_after=1)
    # the run converges: the oversize patch is recorded (eval_error)
    # and the intentional interrupt fires — a FATAL (exit 4) would
    # have aborted before the interrupt and written no line
    rc = lp.run_pilot(cfg, make_session=_mock_make_session("ok"))
    assert rc == lp.EXIT_INTERRUPT
    lines = _read_jsonl(wd / "run-big.jsonl")
    assert len(lines) == 1
    assert lines[0]["status"] == "eval_error"
    assert "exceeds the cap" in (
        (lines[0]["meta"].get("eval_skip_reason") or ""))


def test_quota_429_word_boundary():
    # a bare ``429`` substring false-positives on incidental digits
    # (a 1429 byte count, a port 4290) and parks the batch in the
    # sleep-to-next-UTC-midnight backoff — the numeric marker must
    # match as a whole number
    assert lp._quota_marker_hit("http 429 too many requests")
    assert lp._quota_marker_hit("error: 429")
    assert lp._quota_marker_hit("rate limit reached")
    assert not lp._quota_marker_hit("transferred 1429 bytes")
    assert not lp._quota_marker_hit("listening on port 4290")
    assert not lp._quota_marker_hit("at 14:29:05 the build ran")


def test_largest_bytes_zero_is_config_error(tmp_path, monkeypatch):
    # an explicit ``--largest-bytes 0`` is a config error, not a
    # silent replacement by the default (the ``or`` fallback covers
    # absence only)
    monkeypatch.delenv(lp.LIVE_ENV, raising=False)
    rc = lp.main(["--mode", "sandbox", "--devin-bin", MOCK_PATH,
                 "--instances",
                 _instances_file(tmp_path / "i.json",
                                [_entry(0, "/x", "a")]),
                 "--workdir", str(tmp_path), "--largest-bytes", "0"])
    assert rc == lp.EXIT_CONFIG


def test_pinned_image_consumed(tmp_path, live, monkeypatch):
    # the instance file's PINNED ``image`` field must be what is
    # pulled (a custom file whose pinned image diverges from the
    # derivation must not silently pull the derived name)
    vers = tmp_path / "versions"
    vers.mkdir()
    monkeypatch.setenv("DEVIN_VERSIONS_DIR", str(vers))
    calls: list = []

    def fake_run(args, **kw):
        a = [str(x) for x in args]
        calls.append(a)
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    inst = _entry(0, "/tmp/x", "abc123")
    inst["image"] = "custom.registry/eval:pin-42"
    lp._container_setup(inst, str(tmp_path))
    pulls = [c for c in calls if c[0] == "docker" and c[1] == "pull"]
    assert pulls[0][2] == "custom.registry/eval:pin-42"


def test_container_guard_runs_in_testbed_group(tmp_path, live,
                                               monkeypatch):
    # only the first ``git remote remove`` was &&-chained to
    # ``cd /testbed`` — a cd failure ran the rest (incl. the
    # ``git remote`` probe) in the container's default cwd, reading
    # remotes from the wrong repository. The whole ref-stripping
    # sequence is now a { …; } group under the cd
    vers = tmp_path / "versions"
    vers.mkdir()
    monkeypatch.setenv("DEVIN_VERSIONS_DIR", str(vers))

    def fake_run(args, **kw):
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", fake_run)
    inst = _entry(0, "/tmp/x", "abc123")
    lp._container_setup(inst, str(tmp_path))
    shim = (tmp_path / "devin-shim-test__repo0-1").read_text()
    # the guard script is the sh -c payload of the docker exec —
    # verify the shape via a fresh capture
    guard: list = []

    def cap_run(args, **kw):
        a = [str(x) for x in args]
        if a[0] == "docker" and a[1] == "exec" and "-c" in a:
            guard.append(a[a.index("-c") + 1])
        return _fake_completed(0, b"")

    monkeypatch.setattr(lp.subprocess, "run", cap_run)
    lp._container_setup(inst, str(tmp_path))
    guards = [g for g in guard if g.startswith("cd /testbed")]
    assert len(guards) == 1
    g = guards[0]
    assert g.startswith("cd /testbed && { ")
    assert g.rstrip().endswith("git remote; }")


def test_add_n_separator_includes_leading_dash_file(tmp_path):
    # an agent-created leading-dash filename is a path, not an
    # option — without the ``--`` separator the ``git add -N``
    # would fail and the extraction would be a run-killing
    # PilotError (the ``git reset --`` below already guarded the
    # same list)
    repo = tmp_path / "repo"
    _make_repo(repo, name="mod.py")
    (repo / "tests").mkdir()
    (repo / "tests" / "-dash.py").write_text("x = 1\n")
    out = lp.extract_patch(str(repo))
    assert "tests/-dash.py" in out["included"], out["included"]


def test_early_exits_remove_stale_record(tmp_path, monkeypatch):
    # exit 2 (config) and exit 3 (live-refused) return before
    # run_pilot's removal could run — the stale verdict must be
    # gone on those paths too (a stale verdict is never kept)
    for args, env_state, expected in (
            (["--mode", "sandbox", "--devin-bin", MOCK_PATH,
              "--instances",
              _instances_file(tmp_path / "i.json",
                             [_entry(0, "/x", "a")]),
              "--largest-bytes", "0"],
             "unset", lp.EXIT_CONFIG),
            (["--mode", "sandbox", "--devin-bin", MOCK_PATH,
              "--instances",
              _instances_file(tmp_path / "i2.json",
                             [_entry(0, "/x", "a")])],
             "unset", lp.EXIT_LIVE_REFUSED)):
        wd = tmp_path / f"wd-{expected}"
        wd.mkdir(exist_ok=True)
        stale = wd / "wp6a-go-no-go.md"
        stale.write_text("**Verdict: GO** (stale)\n")
        if env_state == "unset":
            monkeypatch.delenv(lp.LIVE_ENV, raising=False)
        else:
            monkeypatch.setenv(lp.LIVE_ENV, "1")
        rc = lp.main([*args, "--workdir", str(wd)])
        assert rc == expected
        assert not stale.exists()


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
