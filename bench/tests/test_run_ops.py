"""WP6c pre-flight E5 (issue #44) — multi-window operation tooling and
the resume drill.

* :func:`test_run_window_*` — ``scripts/run_window.sh``'s exit-code
  contract (0 done / 42 window cap / 4 stop-never-retry / 2 config / 3 the
  live gate) and its per-window log, driven by a stub harness so the
  contract is tested without the pilot (and so "exactly one invocation" is
  provable, not asserted by reading the script);
* :func:`test_status_*` — ``scripts/run_status.py``'s read-only math at
  500-instance scale (windows, finals by status, the pending queue, the
  ETA from measured walls, disk) and its refusal of an ambiguous workdir;
* :func:`test_resume_drill_*` — the drill the issue asks for: a capped
  window → resume → completion, plus the kill-mid-instance class (the
  ``--interrupt-after`` host-crash class and a torn last checkpoint line)
  and the retry-class re-run, asserting the pass@1 semantics (§2.5: a
  decided outcome is never re-run, an undecided one always is) across
  every window of the run.

Zero quota: the mock ``devin`` double and a stubbed eval leg.
"""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import live_pilot as lp
import report as rep

_BENCH = Path(__file__).resolve().parent.parent
_SCRIPTS = _BENCH / "scripts"
MOCK_PATH = str(_BENCH / "mock_devin.py")


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias,
                                                 str(_SCRIPTS /
                                                     f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


run_status = _load("run_status", "run_status_under_test")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    return monkeypatch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _git(p: Path, *args: str) -> "subprocess.CompletedProcess":
    return subprocess.run(["git", "-C", str(p), *args],
                         capture_output=True, check=False, timeout=120)


def _repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "base.py").write_text("# base.py\nprint('base')\n")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "drill@test.local")
    _git(path, "config", "user.name", "Drill")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    return _git(path, "rev-parse", "HEAD").stdout.decode().strip()


def _instances(tmp: Path, n: int) -> str:
    entries = []
    for i in range(n):
        r = tmp / f"srcrepo{i}"
        head = _repo(r)
        entries.append({
            "instance_id": f"test__repo{i}-1",
            "repo": f"test/repo{i}",
            "repo_url": str(r),
            "base_commit": head,
            "problem_statement": "Fix the thing.",
            "FAIL_TO_PASS": [f"tests/test_r{i}.py::test_a"],
            "PASS_TO_PASS": [],
            "test_patch": "",
        })
    f = tmp / "inst.json"
    f.write_text(json.dumps({"_meta": {"source": "drill fixture"},
                            "instances": entries}))
    return str(f)


def _lines(path) -> list:
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()
           if l.strip()]


def _state(path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _rec(iid: str, status: str, ts: float, *, wall: float = 1200.0,
        **meta) -> str:
    m = {"wall_s": wall, "artifacts_dir": f"runs/{iid}"}
    m.update(meta)
    return json.dumps({"instance_id": iid, "status": status, "ts": ts,
                      "meta": m})


# ---------------------------------------------------------------------------
# run_window.sh — the exit-code contract
# ---------------------------------------------------------------------------

STUB = '''
import json, os, sys
n = int(os.environ.get("STUB_N", "0")) + 1
with open(os.environ["STUB_CALLS"], "w", encoding="utf-8") as f:
    json.dump({"n": n, "argv": sys.argv[1:], "workdir_arg":
               ("--workdir" in sys.argv)}, f)
print("stub harness output")
print("second line")
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''


def _window(tmp_path, *, rc=0, args=None, env=None, script="run_window.sh"):
    stub = tmp_path / "stub_harness.py"
    stub.write_text(STUB)
    calls = tmp_path / "calls.json"
    e = dict(os.environ)
    e.update({"PYTHON": sys.executable, "LIVE_PILOT": str(stub),
             "STUB_RC": str(rc), "STUB_CALLS": str(calls)})
    e.update(env or {})
    p = subprocess.run(["bash", str(_SCRIPTS / script),
                       *(args if args is not None else
                         ["--workdir", str(tmp_path / "wd")])],
                      capture_output=True, text=True, env=e, timeout=120,
                      cwd=str(tmp_path))
    return p, calls


def test_run_window_passthrough_and_log(tmp_path):
    p, calls = _window(tmp_path, rc=0)
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(calls.read_text())["n"] == 1
    logs = list((tmp_path / "wd" / "logs").glob("window-*.log"))
    assert len(logs) == 1, logs
    body = logs[0].read_text()
    assert "stub harness output" in body and "second line" in body
    # the interpretation of exit 0: verify, then package
    assert "COMPLETION" in p.stdout
    assert "verify_submission.py" in p.stdout


def test_run_window_42_is_the_window_cap_and_prints_the_resume_cmd(tmp_path):
    p, _ = _window(tmp_path, rc=42)
    assert p.returncode == 42
    assert "WINDOW CAP" in p.stdout
    # the operator must not have to reconstruct the invocation
    assert "--workdir" in p.stdout and "run_window.sh" in p.stdout
    assert "run_status.py" in p.stdout


def test_run_window_4_stops_and_never_loops(tmp_path):
    """The issue's hard rule: exit 4 = stop, never loop-retry."""
    p, calls = _window(tmp_path, rc=4)
    assert p.returncode == 4
    assert json.loads(calls.read_text())["n"] == 1
    assert "STOP" in p.stdout and "Do not loop-retry" in p.stdout
    assert "no verdict record was written" in p.stdout


@pytest.mark.parametrize("rc,needle", [
    (3, "live-run gate"),
    (2, "configuration error"),
    (7, "unexpected exit 7"),
])
def test_run_window_other_codes(tmp_path, rc, needle):
    p, calls = _window(tmp_path, rc=rc)
    assert p.returncode == rc
    assert needle in p.stdout
    assert json.loads(calls.read_text())["n"] == 1


def test_run_window_requires_workdir(tmp_path):
    p, _calls = _window(tmp_path, args=["--pilot", "c"])
    assert p.returncode == 2
    assert "--workdir" in p.stderr


def test_run_window_help_is_zero_quota(tmp_path):
    p, _ = _window(tmp_path, args=["--help"])
    assert p.returncode == 0
    assert "WINDOW" in p.stdout or "window" in p.stdout


def test_run_window_log_dir_override(tmp_path):
    p, _ = _window(tmp_path, rc=0,
                  env={"LOG_DIR": str(tmp_path / "elsewhere")})
    assert p.returncode == 0
    assert list((tmp_path / "elsewhere").glob("window-*.log"))


def test_run_window_second_invocation_gets_its_own_log(tmp_path):
    p1, _ = _window(tmp_path, rc=42)
    p2, calls = _window(tmp_path, rc=42)
    assert p1.returncode == p2.returncode == 42
    assert json.loads(calls.read_text())["n"] == 1  # a fresh process
    assert len(list((tmp_path / "wd" / "logs").glob("window-*.log"
                                                  ))) == 2


def test_run_window_refuses_a_missing_harness(tmp_path):
    p, _ = _window(tmp_path, env={"LIVE_PILOT": str(tmp_path / "no.py")})
    assert p.returncode == 2
    assert "no harness" in p.stderr


# ---------------------------------------------------------------------------
# run_status.py — the read-only view
# ---------------------------------------------------------------------------

DAY = 86400.0
T0 = 1_700_000_000.0  # a UTC day boundary-ish anchor


def _mk_run(tmp_path, lines, *, name="run-full500.jsonl", state=None):
    work = tmp_path / "wd"
    work.mkdir(exist_ok=True)
    (work / name).write_text("".join(l + "\n" for l in lines))
    if state is not None:
        (work / "pilot-full500.state.json").write_text(json.dumps(state))
    return work


def _manifest(tmp_path, ids, name="manifest.json") -> str:
    f = tmp_path / name
    f.write_text(json.dumps({"instances": [{"instance_id": i}
                                          for i in ids]}))
    return str(f)


def test_status_at_500_instance_scale(tmp_path):
    """The issue's 'at 500-instance scale' leg for the *tooling*: 460
    terminal + 12 retry-class + 28 never-attempted, across 47 windows."""
    ids = [f"r__repo-{i}" for i in range(500)]
    lines = []
    for i, iid in enumerate(ids[:460]):
        st = "resolved" if i % 5 == 0 else "failed_test"
        lines.append(_rec(iid, st, T0 + (i // 10) * DAY * 2 + i * 60))
    for i, iid in enumerate(ids[460:472]):
        lines.append(_rec(iid, "eval_error",
                         T0 + (46 + i) * DAY * 2 + 60))
    work = _mk_run(tmp_path, lines)
    s = run_status.status(str(work / "run-full500.jsonl"), ids, cap
                          =10)
    assert s["run_id"] == "full500"
    assert s["lines"] == 472 and s["distinct"] == 472
    assert s["by_status"]["resolved"] == 92  # every 5th of 460
    assert s["by_status"]["failed_test"] == 368
    assert s["by_status"]["eval_error"] == 12
    assert s["terminal"] == 460  # the retry class is excluded
    assert s["pending_retry"] == ids[460:472]
    # the queue: the 28 never-attempted + the 12 undecided
    assert s["queue_count"] == 40
    assert s["outside_manifest"] == []
    assert s["manifest_total"] == 500
    # windows: every 10 sessions share a UTC day, 2 days apart
    assert s["windows_used"] == 59
    assert s["sessions_total"] == 472
    # ETA: 40 remaining / 10 per window = 4 windows, from measured walls
    assert s["eta"]["windows"] == 4
    assert s["eta"]["basis"].startswith("median measured session wall")
    assert s["eta"]["session_hours"] == round(40 * 1200 / 3600, 1)
    out = run_status.render(s, run_status.disk_view(str(work)))
    assert "pass@1 SO FAR" in out and "provisional" in out
    assert "queue: 40 of 500" in out
    assert "ETA: 4 more window(s)" in out


def test_status_queue_is_the_harness_resume_decision(tmp_path):
    """The queue is not a guess and not a second implementation of pass@1:
    it is ``runner.resume_queue`` — the decision the next window makes
    (never-rerun skipped; a retry-class final and a never-attempted id
    both re-run). The state file contributes the previous window's flags,
    never the math."""
    ids = ["a__a-1", "a__a-2", "a__a-3", "a__a-4"]
    lines = [_rec("a__a-1", "resolved", T0),
            _rec("a__a-2", "no_patch", T0 + 60),
            _rec("a__a-3", "quota_limited", T0 + 120)]
    work = _mk_run(tmp_path, lines, state={"final_state": "interrupted",
                                          "interrupted": True,
                                          "skipped": {"zz": {}}})
    s = run_status.status(str(work / "run-full500.jsonl"), ids, 10)
    assert s["skipped"] == 2
    assert s["queue"] == ["a__a-3", "a__a-4"]
    assert s["skipped_by_status"] == {"resolved": 1, "no_patch": 1}
    assert s["state_flags"] == {"final_state": "interrupted",
                                "interrupted": True}
    assert "last window: interrupted" in run_status.render(s, None)


def test_status_unknown_status_line_is_never_read_as_complete(tmp_path):
    """A line the WP5 loader cannot accept (an out-of-set status) is
    DROPPED by the loader — and the dropped-line WARNING must be visible,
    with that id back in the queue. The failure mode this guards is the
    silent one: a checkpoint the dashboard reads as 'nothing queued'."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "banana", T0)])
    s = run_status.status(str(work / "run-full500.jsonl"),
                         ["a__a-1", "a__a-2"], 2)
    assert s["dropped_line"] is True
    assert s["queue"] == ["a__a-1", "a__a-2"]
    out = run_status.render(s, None)
    assert "malformed line was dropped" in out
    # queue_error stays the second line of defence (a status the loader
    # accepts but the WP3 queue contract does not)
    assert s["queue_error"] == ""


def test_status_corrupt_state_file_warns(tmp_path):
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    (work / "pilot-full500.state.json").write_text("{torn")
    s = run_status.status(str(work / "run-full500.jsonl"), ["a__a-1"], 2)
    assert s["state_error"]
    assert "state file is unreadable" in run_status.render(s, None)
    assert s["queue"] == []      # the checkpoint still answers
    assert s["skipped"] == 1


def test_status_missing_state_file_is_not_an_error(tmp_path):
    ids = ["a__a-1"]
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    s = run_status.status(str(work / "run-full500.jsonl"), ids, 10)
    assert s["skipped"] == 1 and s["state_error"] == ""
    assert s["state_flags"] == {}
    # the checkpoint alone says the outcome is terminal → nothing queued
    assert s["queue"] == []


def test_status_excludes_imported_lines_from_the_windows(tmp_path):
    """An imported (never-rerun) line burned the SOURCE run's budget —
    counting it would make the window look fuller than it is."""
    ids = ["a__a-1", "a__a-2"]
    lines = [_rec("a__a-1", "resolved", T0, imported_from="pilotA"),
            _rec("a__a-2", "resolved", T0 + 60)]
    work = _mk_run(tmp_path, lines)
    s = run_status.status(str(work / "run-full500.jsonl"), ids, 10)
    assert s["sessions_total"] == 1
    assert len(s["windows"]) == 1
    assert s["measure"]["n"] == 1
    assert s["distinct"] == 2  # both still count as outcomes


def test_status_preflight_failed_counts_wall_not_a_session(tmp_path):
    work = _mk_run(tmp_path, [_rec("a__a-1", "preflight_failed", T0,
                                  wall=900.0, instance=None)])
    s = run_status.status(str(work / "run-full500.jsonl"), None, 10)
    assert s["sessions_total"] == 0
    assert s["windows"] and list(s["windows"].values())[0]["wall_s"] \
        == 900.0
    assert s["measure"]["n"] == 0  # no session wall to average
    assert s["eta"]["basis"] == "no measurement yet"


def test_status_batch_marker_line_is_not_an_instance(tmp_path):
    work = _mk_run(tmp_path, ['{"instance_id": null, "status": '
                             '"preflight_failed", "ts": %r, "meta": {}}'
                             % T0])
    s = run_status.status(str(work / "run-full500.jsonl"), None, 10)
    assert s["distinct"] == 0 and s["markers"] == 1
    assert s["last_line"] is None


def test_status_warns_on_a_torn_line(tmp_path):
    """A SIGKILL mid-write leaves a partial last line — the operator must
    see it (it means the last instance's outcome is unknown)."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    with open(work / "run-full500.jsonl", "a", encoding="utf-8") as f:
        f.write('{"instance_id": "a__a-2", "stat')
    s = run_status.status(str(work / "run-full500.jsonl"), None, 10)
    assert s["dropped_line"] is True
    assert "WARNING" in run_status.render(s, None)


def test_status_warns_on_ids_outside_the_manifest(tmp_path):
    work = _mk_run(tmp_path, [_rec("ghost__g-1", "resolved", T0)])
    s = run_status.status(str(work / "run-full500.jsonl"), ["a__a-1"],
                         10)
    assert s["outside_manifest"] == ["ghost__g-1"]
    assert "outside the manifest" in run_status.render(s, None)


def test_find_jsonl(tmp_path):
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    path, rid = run_status.find_jsonl(str(work), None)
    assert rid == "full500" and path.endswith("run-full500.jsonl")
    # an ambiguous workdir is refused, never guessed
    (work / "run-other.jsonl").write_text("")
    with pytest.raises(SystemExit, match="pass --run-id"):
        run_status.find_jsonl(str(work), None)
    p2, r2 = run_status.find_jsonl(str(work), "other")
    assert r2 == "other"
    with pytest.raises(SystemExit, match="no checkpoint"):
        run_status.find_jsonl(str(work), "nope")
    with pytest.raises(SystemExit, match="no workdir"):
        run_status.find_jsonl(str(tmp_path / "absent"), None)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no run-\\*.jsonl"):
        run_status.find_jsonl(str(empty), None)


def test_status_main_cli(tmp_path, capsys):
    ids = ["a__a-1", "a__a-2"]
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    man = _manifest(tmp_path, ids)
    assert run_status.main(["--workdir", str(work), "--manifest", man,
                           "--cap", "3"]) == run_status.EXIT_OK
    out = capsys.readouterr().out
    assert "# run full500" in out and "queue: 1 of 2" in out
    assert "ETA: 1 more window(s)" in out


def test_status_main_json_is_machine_readable(tmp_path, capsys):
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    assert run_status.main(["--jsonl", str(work / "run-full500.jsonl"),
                           "--manifest", "", "--cap", "5", "--json"]) \
        == run_status.EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    for key in ("run_id", "by_status", "queue", "eta", "windows",
               "disk"):
        assert key in doc, key
    assert doc["manifest_total"] is None  # --manifest '' skips the view


def test_status_main_input_errors(tmp_path):
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    assert run_status.main(["--jsonl", str(tmp_path / "nope.jsonl")]) \
        == run_status.EXIT_INPUT
    assert run_status.main(["--workdir", str(work), "--manifest",
                          str(tmp_path / "nope.json")]) \
        == run_status.EXIT_INPUT


def test_status_default_cap_when_the_budget_file_is_unreadable(tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """No authorized budget → the WP6a default cap, and the output says
    which cap was used (an ETA is never silently computed from a guess)."""
    def boom(_path):
        raise SystemExit("no budget file")
    monkeypatch.setattr(lp, "load_budget_file", boom)
    work = _mk_run(tmp_path, [_rec(f"a__a-{i}", "resolved", T0 + i * 60)
                             for i in range(3)])
    assert run_status.main(["--workdir", str(work), "--manifest", ""]) \
        == run_status.EXIT_OK
    assert "cap 10/window" in capsys.readouterr().out


def test_status_is_read_only(tmp_path):
    """The operator runs this WHILE a window is in flight: it must not
    touch the run."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)],
                  state={"final_state": "done"})
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in sorted(work.iterdir())}
    assert run_status.main(["--workdir", str(work), "--manifest", ""]) \
        == run_status.EXIT_OK
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(work.iterdir())}
    assert before == after


# ---------------------------------------------------------------------------
# the resume drill (issue #44 E5) — mock only, zero quota
# ---------------------------------------------------------------------------

AUTHORIZED_BUDGET = {
    "max_sessions_per_day": 2, "max_wall_per_session": 2700,
    "calibrated_at": "2026-11-13",
    "basis": "drill fixture (the real file is issue #44 B2)",
    "authorized_by": "Operator Name <op@example.com>",
    "completion_deadline": "2027-06-30",
    # issue #44 B7/B2: the gate enforces this field too, so an
    # "authorized" fixture has to state it
    "abort_rule": "two consecutive RUN FAILED windows stop the run "
                  "(drill fixture)",
}


def _budget(tmp: Path) -> str:
    f = tmp / "budget.json"
    f.write_text(json.dumps(AUTHORIZED_BUDGET))
    return str(f)


def _drill_args(inst: str, wd: Path, cap: int, budget: str,
               stop_at_cap: bool = True) -> list:
    """One window of the drill. ``--stop-at-window-cap`` is the E5
    per-window model (exit 42 at the cap instead of sleeping in-process
    to the next UTC window)."""
    args = ["--pilot", "c", "--mode", "sandbox", "--instances", inst,
            "--workdir", str(wd), "--run-id", "drill", "--devin-bin",
            MOCK_PATH, "--budget-file", budget,
            "--max-sessions-per-day", str(cap),
            "--largest-bytes", "1048576"]
    if stop_at_cap:
        args.append("--stop-at-window-cap")
    return args


def _stub_eval(monkeypatch, statuses=None):
    """The grading leg without pytest/docker: the drill tests the queue,
    the checkpoint and the resume semantics, not the eval verdict."""
    table = statuses or {}

    def fake(self, patch):
        iid = self._spec.instance["instance_id"]
        self.meta["graded_by"] = "drill-stub"
        return table.get(iid, "resolved")
    monkeypatch.setattr(lp.PilotSession, "_host_check", fake)


def test_resume_drill_capped_windows_then_completion(tmp_path, live,
                                                    monkeypatch):
    """Window 1 stops at the daily cap; each later window resumes and
    never re-runs a decided instance; the last window completes the run."""
    inst = _instances(tmp_path, 4)
    wd = tmp_path / "wd"
    jsonl = wd / "run-drill.jsonl"
    budget = _budget(tmp_path)
    _stub_eval(monkeypatch)

    # window 1: the cap is 2 of 4
    assert lp.main(_drill_args(inst, wd, 2, budget)) == lp.EXIT_INTERRUPT
    assert "window cap reached" in (wd / "wp6c-full-run.md").read_text()
    assert len(_lines(jsonl)) == 2
    st = _state(wd / "pilot-drill.state.json")
    assert st["window_cap"] is True and st["interrupted"] is True
    assert st["final_state"] == "interrupted"
    s = run_status.status(str(jsonl),
                         [f"test__repo{i}-1" for i in range(4)], 2)
    assert s["queue_count"] == 2 and s["windows_used"] == 1
    assert s["eta"]["windows"] == 1

    # window 2: resume — the 2 decided instances are SKIPPED (no session)
    assert lp.main(_drill_args(inst, wd, 2, budget) + ["--resume"]) == lp.EXIT_OK
    lines = _lines(jsonl)
    assert len(lines) == 4, "a decided outcome must never be re-run"
    assert len({l["instance_id"] for l in lines}) == 4
    finals, _ = rep.dedupe(lines)
    assert all(r["status"] == "resolved" for r in finals.values())
    s2 = run_status.status(str(jsonl),
                          [f"test__repo{i}-1" for i in range(4)], 2)
    assert s2["queue_count"] == 0 and s2["eta"]["windows"] is None

    # the resume is idempotent: a further window runs nothing and stays 0
    assert lp.main(_drill_args(inst, wd, 2, budget) + ["--resume"]) == lp.EXIT_OK
    assert len(_lines(jsonl)) == 4


def test_resume_drill_interrupted_run_retries_only_the_undecided(
        tmp_path, live, monkeypatch):
    """The kill-mid-instance class: the host dies after instance 1's line
    (the ``--interrupt-after`` seam). The decided outcome survives; the
    instance that had no line runs in the next window; nothing is
    double-run — the pass@1 semantics (PLAN §2.5) across the crash."""
    inst = _instances(tmp_path, 3)
    wd = tmp_path / "wd"
    jsonl = wd / "run-drill.jsonl"
    _stub_eval(monkeypatch)

    # window 1 dies right after the first checkpoint line
    assert lp.main(_drill_args(inst, wd, 10, _budget(tmp_path))
                    + ["--interrupt-after", "1"]) == lp.EXIT_INTERRUPT
    lines = _lines(jsonl)
    assert len(lines) == 1
    st = _state(wd / "pilot-drill.state.json")
    assert st["interrupted"] is True and st["window_cap"] is False

    # the record must not claim a verdict for a run that was interrupted
    record = (wd / "wp6c-full-run.md").read_text()
    assert "n/a (interrupted" in record

    # window 2: resume
    assert lp.main(_drill_args(inst, wd, 10, _budget(tmp_path)) + ["--resume"]) == lp.EXIT_OK
    lines = _lines(jsonl)
    by_id: dict = {}
    for l in lines:
        by_id.setdefault(l["instance_id"], []).append(l["status"])
    assert len(by_id) == 3
    # the decided one was NOT re-run (exactly one line), the other two
    # ran once each — so no instance has two attempts
    assert all(len(v) == 1 for v in by_id.values()), by_id
    finals, _ = rep.dedupe(lines)
    assert {r["status"] for r in finals.values()} == {"resolved"}
    assert run_status.status(str(jsonl),
                            [f"test__repo{i}-1" for i in range(3)],
                            10)["queue_count"] == 0


def test_resume_drill_retry_class_is_always_re_run(tmp_path, live,
                                                  monkeypatch):
    """The other half of §2.5: a retry-class final (``eval_error``) is
    UNDECIDED, so the next window re-runs it — and the retry class is
    excluded from the pass@1 denominator, so a mid-run number never looks
    better or worse than it is."""
    inst = _instances(tmp_path, 2)
    wd = tmp_path / "wd"
    jsonl = wd / "run-drill.jsonl"
    ids = ["test__repo0-1", "test__repo1-1"]
    _stub_eval(monkeypatch, {ids[0]: "eval_error"})

    lp.main(_drill_args(inst, wd, 10, _budget(tmp_path)))
    lines = _lines(jsonl)
    finals, _ = rep.dedupe(lines)
    assert finals[ids[0]]["status"] == "eval_error"
    s = run_status.status(str(jsonl), ids, 10)
    assert s["pending_retry"] == [ids[0]]
    assert s["queue_count"] == 1  # undecided → still queued
    # the retry class is out of the denominator
    assert s["terminal"] == 1 and s["resolved"] == 1

    # the next window: the retry class runs again (the pass@1 retry),
    # the decided one is skipped
    _stub_eval(monkeypatch)
    assert lp.main(_drill_args(inst, wd, 10, _budget(tmp_path)) + ["--resume"]) == lp.EXIT_OK
    lines = _lines(jsonl)
    attempts = [l for l in lines if l["instance_id"] == ids[0]]
    assert len(attempts) == 2, "the retry class must be re-run"
    finals, _ = rep.dedupe(lines)
    assert finals[ids[0]]["status"] == "resolved"
    assert run_status.status(str(jsonl), ids, 10)["queue_count"] == 0
    # and the first attempt is retained: the run's history is auditable
    assert any(l["status"] == "eval_error" for l in lines)


def test_resume_drill_survives_a_torn_last_line(tmp_path, live,
                                                monkeypatch):
    """The real SIGKILL class: a partial line at the end of the
    checkpoint. The loader drops it (the WP5 contract), the resume treats
    that instance as never attempted, and the completed run holds exactly
    one outcome per instance."""
    inst = _instances(tmp_path, 2)
    wd = tmp_path / "wd"
    jsonl = wd / "run-drill.jsonl"
    ids = ["test__repo0-1", "test__repo1-1"]
    _stub_eval(monkeypatch)
    assert lp.main(_drill_args(inst, wd, 10, _budget(tmp_path))) == lp.EXIT_OK
    assert len(_lines(jsonl)) == 2

    # a kill mid-write of a THIRD attempt on instance 1 (the file now has
    # a torn line; the state still says both are decided)
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write('{"instance_id": "test__repo1-1", "stat')
    s = run_status.status(str(jsonl), ids, 10)
    assert s["dropped_line"] is True
    assert s["queue_count"] == 0  # the state file still decides

    assert lp.main(_drill_args(inst, wd, 10, _budget(tmp_path)) + ["--resume"]) == lp.EXIT_OK
    lines = _lines(jsonl)
    assert len(lines) == 2
    assert len({l["instance_id"] for l in lines}) == 2
    # the torn line did not survive the resume either way: the checkpoint
    # the operator archives is parseable to the last byte
    assert all(l.strip() for l in
              jsonl.read_text().splitlines())


def test_resume_drill_window_buckets_match_the_pilot_accounting(tmp_path,
                                                               live,
                                                               monkeypatch):
    """The dashboard and the gating record must agree: the same windows,
    the same session counts, the same wall (item 23's batch discipline
    reads the pilot's buckets; the operator reads run_status)."""
    inst = _instances(tmp_path, 4)
    wd = tmp_path / "wd"
    jsonl = wd / "run-drill.jsonl"
    budget = _budget(tmp_path)
    _stub_eval(monkeypatch)
    assert lp.main(_drill_args(inst, wd, 2, budget)) == lp.EXIT_INTERRUPT
    assert lp.main(_drill_args(inst, wd, 2, budget) + ["--resume"]) == lp.EXIT_OK

    records, _ = rep.load_jsonl(str(jsonl))
    buckets = lp._window_buckets(records)
    s = run_status.status(str(jsonl),
                         [f"test__repo{i}-1" for i in range(4)], 2)
    assert len(buckets) == s["windows_used"]
    assert sum(w["sessions"] for w in buckets.values()) == \
        s["sessions_total"] == 4
    assert round(sum(w["wall_s"] for w in buckets.values()), 3) == \
        round(sum(w["wall_s"] for w in s["windows"].values()), 3)
    # NB the drill's two windows run inside ONE real UTC day, so the day
    # bucket legitimately holds 4 sessions here; what the drill asserts
    # is that the operator's view and the gating view are the same view
    # (each invocation itself stopped at its own cap — see the 42/0 pair)
    assert sum(w["sessions"] for w in buckets.values()) == 4


def test_resume_drill_record_verdict_after_completion(tmp_path, live,
                                                     monkeypatch):
    """The completing window's record carries the full-run items — and a
    complete queue means item 18 can pass (the drill's own proof that the
    checklist measures the run, not the fixture)."""
    inst = _instances(tmp_path, 3)
    wd = tmp_path / "wd"
    budget = _budget(tmp_path)
    _stub_eval(monkeypatch)
    # the partial window decides nothing …
    assert lp.main(_drill_args(inst, wd, 2, budget)) == lp.EXIT_INTERRUPT
    partial = (wd / "wp6c-full-run.md").read_text()
    assert "**Verdict: n/a (window cap reached — resume in the next " \
           "window)**" in partial, partial
    # … and the completing window does. Its cap is the DAY's total (3):
    # the drill's two windows run inside one real UTC day, so item 12 /
    # item 23's per-window gate — which measures sessions per UTC day —
    # would otherwise correctly fail the day for 3 sessions on a cap of
    # 2. In a real run the windows are different days.
    assert lp.main(_drill_args(inst, wd, 3, budget)
                  + ["--resume"]) == lp.EXIT_OK
    record = (wd / "wp6c-full-run.md").read_text()
    assert "**Verdict: GO**" in record, record
    for n in (18, 19, 20, 21, 22, 23):
        assert f"| {n} " in record, n
    assert "3/3 covered" in record


def test_run_window_drives_the_real_harness_one_window(tmp_path, live,
                                                      monkeypatch):
    """The last link: ``run_window.sh`` around the real harness, with the
    mock binary — the window cap reaches the operator as 42 with the log
    written, and the next window resumes to completion (the whole tooling
    chain, zero quota)."""
    inst = _instances(tmp_path, 2)
    wd = tmp_path / "wd"
    _stub_eval(monkeypatch)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    # a harness stub that ALSO exercises the tee/PIPESTATUS path with
    # output on both streams before exiting 42
    e = dict(os.environ)
    e.update({"PYTHON": sys.executable,
             "LIVE_PILOT": str(_BENCH / "live_pilot.py"),
             "BENCH_LIVE": "1"})
    p = subprocess.run(
        ["bash", str(_SCRIPTS / "run_window.sh"),
         "--pilot", "c", "--mode", "sandbox", "--instances", inst,
         "--workdir", str(wd), "--run-id", "win", "--devin-bin",
         MOCK_PATH, "--budget-file", _budget(tmp_path),
         "--stop-at-window-cap",
         "--max-sessions-per-day", "1", "--largest-bytes",
         "1048576"],
        capture_output=True, text=True, env=e, timeout=600,
        cwd=str(_BENCH))
    assert p.returncode == lp.EXIT_INTERRUPT, p.stdout[-3000:]
    assert "WINDOW CAP" in p.stdout
    logs = list((wd / "logs").glob("window-*.log"))
    assert len(logs) == 1
    assert "[pilot]" in logs[0].read_text()
    assert len(_lines(wd / "run-win.jsonl")) == 1
    # the next window resumes to completion through the same script
    p2 = subprocess.run(
        ["bash", str(_SCRIPTS / "run_window.sh"),
         "--pilot", "c", "--mode", "sandbox", "--instances", inst,
         "--workdir", str(wd), "--run-id", "win", "--devin-bin",
         MOCK_PATH, "--budget-file", _budget(tmp_path),
         "--max-sessions-per-day", "5", "--largest-bytes",
         "1048576", "--resume"],
        capture_output=True, text=True, env=e, timeout=600,
        cwd=str(_BENCH))
    assert p2.returncode == lp.EXIT_OK, p2.stdout[-3000:]
    assert "COMPLETION" in p2.stdout
    lines = _lines(wd / "run-win.jsonl")
    # the eval leg is skipped (--no-host-check), so window 1's final is
    # the retry class and the resume re-runs it: attempts ≥ instances, one
    # final per instance (§2.5)
    assert len(lines) >= 2
    assert {l["instance_id"] for l in lines} == {"test__repo0-1",
                                                "test__repo1-1"}
    finals, _ = rep.dedupe(lines)
    assert len(finals) == 2
    assert len(list((wd / "logs").glob("window-*.log"))) == 2


# --------------------------------------------------------------------------
# B4 (issue #44): per-window durability — archive_run.py
# --------------------------------------------------------------------------

ar = _load("archive_run", "archive_run_under_test")


def _mk_workdir(tmp_path, n=3):
    wd = tmp_path / "full-run"
    (wd / "report" / "trajs").mkdir(parents=True)
    (wd / "runs" / "full500" / "test__repo0-1").mkdir(parents=True)
    lines = [json.dumps(_rec(f"test__repo{i}-1", "resolved", T0 + i))
            for i in range(n)]
    (wd / "run-full500.jsonl").write_text("\n".join(lines) + "\n")
    (wd / "pilot-full500.state.json").write_text('{"interrupted": false}\n')
    (wd / "report" / "report.md").write_text("# report\n")
    (wd / "report" / "all_preds.jsonl").write_text('{"instance_id": "x"}\n')
    (wd / "report" / "trajs" / "test__repo0-1.jsonl").write_text("t\n")
    (wd / "report" / "run_meta.json").write_text('{"label": "L"}\n')
    (wd / "runs" / "full500" / "test__repo0-1" / "patch.diff").write_text(
        "diff --git a/x b/x\n")
    (wd / "wp6c-full-run.md").write_text("# record\n")
    # the things that are NOT the run's evidence
    (wd / "pilot.lock").write_text("")
    (wd / "report" / "note.tmp").write_text("half-written\n")
    (wd / "swebench-task-logs").mkdir()
    (wd / "swebench-task-logs" / "junk.log").write_text("noise\n")
    return wd


def test_archive_snapshots_the_run_and_verifies_it(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    assert ar.main(["--workdir", str(wd), "--dest", str(dest),
                    "--stamp", "20261107T090000Z"]) == ar.EXIT_OK
    bundle = dest / "full500" / "windows" / "20261107T090000Z"
    got = sorted(os.path.relpath(os.path.join(dp, f), str(bundle))
                for dp, _d, fs in os.walk(bundle) for f in fs)
    assert got == sorted([
        "MANIFEST.tsv", "pilot-full500.state.json",
        "report/all_preds.jsonl", "report/report.md", "report/run_meta.json",
        "report/trajs/test__repo0-1.jsonl",
        "runs/full500/test__repo0-1/patch.diff", "run-full500.jsonl",
        "wp6c-full-run.md"]), got
    # the copies are byte-identical, and the bundle verifies
    assert (bundle / "run-full500.jsonl").read_bytes() == \
        (wd / "run-full500.jsonl").read_bytes()
    assert ar.verify(str(bundle)) == []
    assert (dest / "full500" / "LATEST.tsv").read_text().strip() == \
        str(bundle / "MANIFEST.tsv")


def test_archive_excludes_what_the_run_did_not_write(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest)])
    listed = " ".join(l for l in
                      (dest / "full500" / "windows" / os.listdir(
                          dest / "full500" / "windows")[0] /
                       "MANIFEST.tsv").read_text().splitlines())
    for junk in ("pilot.lock", "note.tmp", "swebench-task-logs"):
        assert junk not in listed, junk


def test_verify_catches_a_mutated_bundle(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest),
             "--stamp", "W1"])
    bundle = dest / "full500" / "windows" / "W1"
    (bundle / "run-full500.jsonl").write_text("[]\n")
    problems = ar.verify(str(bundle))
    assert any("run-full500.jsonl" in p for p in problems), problems
    assert ar.main(["--verify", "--bundle", str(bundle)]) == ar.EXIT_VERIFY


def test_verify_catches_a_file_that_is_not_in_the_manifest(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest), "--stamp", "W1"])
    bundle = dest / "full500" / "windows" / "W1"
    (bundle / "surprise.jsonl").write_text("unaccounted\n")
    problems = ar.verify(str(bundle))
    assert any("surprise.jsonl" in p and "not the manifest" in p
               for p in problems), problems


def test_verify_catches_a_missing_file(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest), "--stamp", "W1"])
    bundle = dest / "full500" / "windows" / "W1"
    os.remove(bundle / "report" / "all_preds.jsonl")
    assert any("all_preds.jsonl" in p and "absent" in p
               for p in ar.verify(str(bundle)))


def test_a_truncated_copy_fails_the_archive(tmp_path, monkeypatch):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    def truncating(r, w, *a, **kw):
        w.write(r.read(1))  # a torn copy, the way a full disk looks

    monkeypatch.setattr(ar.shutil, "copyfileobj", truncating)
    assert ar.main(["--workdir", str(wd), "--dest", str(dest),
                    "--stamp", "W1"]) == ar.EXIT_VERIFY


def test_two_windows_never_overwrite(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest), "--stamp", "W1"])
    ar.main(["--workdir", str(wd), "--dest", str(dest), "--stamp", "W1"])
    names = sorted(os.listdir(dest / "full500" / "windows"))
    assert names == ["W1", "W1.1"], names
    assert ar.verify(str(dest / "full500" / "windows" / "W1")) == []


def test_dry_run_copies_nothing(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    assert ar.main(["--workdir", str(wd), "--dest", str(dest),
                    "--dry-run"]) == ar.EXIT_OK
    assert not dest.exists()


def test_run_id_is_derived_or_asked(tmp_path):
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    assert ar.main(["--workdir", str(wd), "--dest", str(dest)]) == ar.EXIT_OK
    assert (dest / "full500").is_dir()
    (wd / "run-other.jsonl").write_text("")
    assert ar.main(["--workdir", str(wd), "--dest", str(dest)]) == ar.EXIT_INPUT


def test_run_window_archives_the_window_when_asked(tmp_path):
    """WP6C_ARCHIVE_DEST turns every window into an archived, verified
    bundle — including a window that only hit the cap: the checkpoint of a
    stopped run is exactly the evidence that must survive."""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "run-full500.jsonl").write_text(
        json.dumps(_rec("test__repo0-1", "resolved", T0)) + "\n")
    (wd / "report").mkdir()
    (wd / "report" / "report.md").write_text("# r\n")
    dest = tmp_path / "offhost"
    p, _ = _window(tmp_path, rc=42,
                   args=["--workdir", str(wd), "--run-id", "full500"],
                   env={"WP6C_ARCHIVE_DEST": str(dest)})
    assert p.returncode == 42, p.stdout + p.stderr
    windows = list((dest / "full500" / "windows").iterdir())
    assert len(windows) == 1, windows
    assert (windows[0] / "MANIFEST.tsv").is_file()
    assert "(verified)" in p.stdout, p.stdout


def test_run_window_refuses_to_call_a_window_durable_when_archive_fails(
        tmp_path):
    """A destination that cannot hold the copy exits 5, not 42/0: the next
    window must not start on evidence that is not durable."""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "run-full500.jsonl").write_text(
        json.dumps(_rec("test__repo0-1", "resolved", T0)) + "\n")
    blocker = tmp_path / "offhost"
    blocker.write_text("not a directory\n")
    p, _ = _window(tmp_path, rc=0,
                   args=["--workdir", str(wd), "--run-id", "full500"],
                   env={"WP6C_ARCHIVE_DEST": str(blocker)})
    assert p.returncode == 5, p.stdout + p.stderr
    assert "NOT durable" in p.stderr, p.stderr


# ---------------------------------------------------------------------------
# issue #44 review round 1: the wrapper's window contract, the ETA's
# statistic, and the read-only tool's documented exit code
# ---------------------------------------------------------------------------

def test_run_window_supplies_the_window_cap_flag(tmp_path):
    """A wrapper that documents the per-window model while leaving
    ``--stop-at-window-cap`` to the operator sleeps for days inside the
    process — the resume line never prints and the archive step below it
    never runs (issue #44 E5)."""
    args = ["--pilot", "c", "--mode", "container", "--run-id", "full500",
            "--workdir", str(tmp_path / "wd"), "--resume"]
    p, calls = _window(tmp_path, rc=42, args=args)
    assert p.returncode == 42, p.stdout + p.stderr
    argv = json.loads(calls.read_text())["argv"]
    assert argv == args + ["--stop-at-window-cap"], argv
    assert list((tmp_path / "wd" / "logs").glob("window-*.log"))
    # the printed resume command is the one that ends the window again
    hint = [l for l in p.stdout.splitlines() if "run_window.sh" in l
            and "--workdir" in l]
    assert hint and "--stop-at-window-cap" in hint[-1], p.stdout


def test_run_window_never_doubles_the_flag(tmp_path):
    p, calls = _window(tmp_path, rc=42, args=["--workdir",
                                              str(tmp_path / "wd"),
                                              "--stop-at-window-cap"])
    argv = json.loads(calls.read_text())["argv"]
    assert argv.count("--stop-at-window-cap") == 1, argv


def test_run_window_keep_running_opts_back_into_the_process(tmp_path):
    """WP6b's contract (one long-lived process that sleeps between
    windows) stays available: the wrapper states its model, it does not
    impose it."""
    p, calls = _window(tmp_path, rc=0, env={"WP6C_KEEP_RUNNING": "1"})
    assert p.returncode == 0, p.stdout + p.stderr
    argv = json.loads(calls.read_text())["argv"]
    assert "--stop-at-window-cap" not in argv, argv
    assert "WP6C_KEEP_RUNNING" in p.stdout


def test_run_window_accepts_the_equals_form(tmp_path):
    """``--workdir=path`` is argparse-legal. The wrapper parses the args
    only to place the window log and name the archive, and a parser that
    missed the glued form would write the log somewhere else while the
    harness ran the right run (issue #44 E5)."""
    wd = tmp_path / "wd"
    p, calls = _window(tmp_path, rc=0, args=[f"--workdir={wd}",
                                            "--run-id=full500"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert list((wd / "logs").glob("window-*.log")), p.stdout
    assert not (tmp_path / "logs").exists()
    argv = json.loads(calls.read_text())["argv"]
    # …and what the harness receives is what the operator typed: argparse
    # reads both spellings, and rewriting the glued form makes
    # ``--workdir=--x`` (the value ``--x``) into an option error
    # (issue #44 review round 4)
    assert argv == [f"--workdir={wd}", "--run-id=full500",
                    "--stop-at-window-cap"], argv


def test_run_window_no_args_is_the_documented_config_error(tmp_path):
    """exit 2 + the message — not bash's unbound-variable exit 1, which
    the runbook's exit-code table has no row for."""
    p, _calls = _window(tmp_path, args=[])
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "--workdir" in p.stdout + p.stderr
    assert "unbound variable" not in p.stderr


def test_eta_uses_the_median_when_a_session_wedged(tmp_path):
    """One wedged session at the wall cap is exactly the case the ETA
    exists to survive. The mean projects the run out by days on it; the
    median (also what checklist item 10 calibrates on) does not
    (issue #44 E5)."""
    walls = [1200.0] * 50 + [36000.0, 36000.0]   # two 10h wedges
    ids = [f"a__a-{i}" for i in range(len(walls) + 20)]  # 20 still queued
    lines = [_rec(iid, "failed_test", T0 + i * 60, wall=w)
             for i, (iid, w) in enumerate(zip(ids, walls))]
    work = _mk_run(tmp_path, lines)
    s = run_status.status(str(work / "run-full500.jsonl"), ids, 10)
    assert s["measure"]["median_s"] == 1200.0
    assert s["measure"]["mean_s"] > 2000
    assert s["eta"]["basis"].startswith("median measured session wall")
    assert "mean" in s["eta"]["basis"]          # both, so it's auditable
    assert s["eta"]["windows"] == 2             # 20 queued / cap 10


def test_status_a_malformed_manifest_is_an_input_error(tmp_path, capsys):
    """The manifest is what makes '472/500' mean something; a file that
    is not a manifest must exit 2, not render a dashboard of dashes."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    bad = tmp_path / "bad.json"
    for content in ('{"instances": [{"id": "x"}]}', "[]", "{not json"):
        bad.write_text(content)
        rc = run_status.main(["--workdir", str(work), "--manifest",
                             str(bad)])
        err = capsys.readouterr().err
        assert rc == run_status.EXIT_INPUT, (content, rc, err[-400:])
        assert err.startswith("INPUT:") and "Traceback" not in err, err


def test_status_a_non_numeric_cap_falls_back_loudly(tmp_path, monkeypatch,
                                                   capsys):
    """A budget file whose caps still say PENDING says 'not authorized
    yet' — the read-only dashboard must still answer, and must say which
    cap it invented (issue #44 B2/B7)."""
    monkeypatch.setattr(lp, "load_budget_file",
                        lambda _p: {"max_sessions_per_day": "PENDING",
                                    "max_wall_per_session": "PENDING"})
    work = _mk_run(tmp_path, [_rec(f"a__a-{i}", "resolved", T0 + i * 60)
                             for i in range(3)])
    assert run_status.main(["--workdir", str(work), "--manifest", ""]) \
        == run_status.EXIT_OK
    out = capsys.readouterr()
    assert "NOTE" in out.err and "PENDING" in out.err, out.err
    assert "cap 10/window" in out.out, out.out


def test_archive_latest_is_a_pointer_and_a_corrupt_manifest_is_clean(
        tmp_path, capsys):
    """LATEST.tsv names the newest window's manifest (so a hand-checked
    backup has one file to open), and a manifest with a corrupt byte
    count must exit 2 with a sentence — not a ValueError in the middle of
    the durability check the operator is relying on (issue #44 B4)."""
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "run-full500.jsonl").write_text(
        _rec("test__repo0-1", "resolved", T0) + "\n")
    (wd / "report").mkdir()
    (wd / "report" / "report.md").write_text("# report\n")
    dest = tmp_path / "offhost"
    rc = ar.main(["--workdir", str(wd), "--dest", str(dest),
                 "--stamp", "20260101T000000Z"])
    assert rc == ar.EXIT_OK
    latest = (dest / "full500" / "LATEST.tsv").read_text().strip()
    target = Path(latest)
    assert target.is_file() and target.name == "MANIFEST.tsv"
    assert target.parent.name.startswith("20260101T000000Z")
    # (a) the bundle itself is corrupt → 3: the backup is wrong
    bundle = target.parent
    (bundle / "report" / "report.md").write_text("# tampered\n")
    assert ar.main(["--verify", "--bundle", str(bundle)]) == ar.EXIT_VERIFY
    (bundle / "report" / "report.md").write_text("# report\n")
    assert ar.main(["--verify", "--bundle", str(bundle)]) == ar.EXIT_OK
    # (b) the manifest METADATA is unparseable → 2: the invocation/file
    # is wrong, which is a different statement about the backup
    rows = target.read_text().splitlines()
    assert rows[0].startswith("# sha256")     # the header is a comment
    f = rows[1].split("\t")
    assert len(f) == 4, rows[1]
    rows[1] = "\t".join([f[0], "not-a-number", f[2], f[3]])
    target.write_text("\n".join(rows) + "\n")
    rc = ar.main(["--verify", "--bundle", str(bundle)])
    err = capsys.readouterr().err
    assert rc == ar.EXIT_INPUT, (rc, err[-400:])
    assert "malformed byte count" in err and "Traceback" not in err, err


# --------------------------------------------------------------------------
# issue #44 review round 2 (Devin on #47): the exit code that regressed,
# the banner that described the wrong run, the cap that fell back silently
# --------------------------------------------------------------------------

def test_verify_a_bundle_without_a_manifest_is_exit_3(tmp_path, capsys):
    """`--verify` on a directory that is not a bundle (no MANIFEST.tsv,
    or a mistyped --bundle) is a DURABILITY failure — the same statement
    as a byte mismatch — and never a traceback. Moving read_manifest
    above the problems check made this crash with exit 1 (reviewed)."""
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    assert ar.main(["--verify", "--bundle", str(empty)]) == ar.EXIT_VERIFY
    out = capsys.readouterr()
    assert "no MANIFEST.tsv" in out.out and "Traceback" not in out.err
    assert ar.main(["--verify", "--bundle",
                    str(tmp_path / "does-not-exist")]) == ar.EXIT_VERIFY


def test_status_an_impossible_cap_falls_back_loudly_too(tmp_path, capsys):
    """0 and -5 are as unusable as PENDING: 'which cap did the ETA use'
    must never be the one question the dashboard answers silently."""
    work = _mk_run(tmp_path, [_rec(f"a__a-{i}", "resolved", T0 + i * 60)
                             for i in range(3)])
    for bad in ("0", "-5"):
        assert run_status.main(["--workdir", str(work), "--manifest", "",
                              "--cap", bad]) == run_status.EXIT_OK
        out = capsys.readouterr()
        assert "NOTE" in out.err and "max_sessions_per_day" in out.err, \
            (bad, out.err[-300:])
        assert "cap 10/window" in out.out, (bad, out.out[-300:])


def test_run_window_args_beat_keep_running_in_the_banner(tmp_path):
    """WP6C_KEEP_RUNNING plus an explicit cap flag: the flag is in the
    argv, so the harness exits 42 and the archive runs — a banner that
    still said 'in-process sleep, no exit 42' described a run that was
    not happening (reviewed)."""
    p, calls = _window(tmp_path, rc=42,
                       args=["--workdir", str(tmp_path / "wd"),
                             "--stop-at-window-cap"],
                       env={"WP6C_KEEP_RUNNING": "1"})
    assert p.returncode == 42, p.stdout + p.stderr
    argv = json.loads(calls.read_text())["argv"]
    assert "--stop-at-window-cap" in argv
    assert "stop at the cap" in p.stdout, p.stdout
    assert "in-process sleep" not in p.stdout, p.stdout
    assert "the arguments win" in p.stdout, p.stdout


def test_run_window_prints_a_command_that_runs_that_command(tmp_path):
    """A workdir with a space is legal; the resume hint and the status
    hint are copy-paste artefacts, so they must survive it (the argv
    itself always did — the printed line was the broken half)."""
    wd = tmp_path / "my run dir"
    p, calls = _window(tmp_path, rc=42, args=["--workdir", str(wd)])
    assert p.returncode == 42, p.stdout + p.stderr
    argv = json.loads(calls.read_text())["argv"]
    assert argv[:2] == ["--workdir", str(wd)], argv     # passed intact
    hint = [l for l in p.stdout.splitlines()
            if "run_window.sh" in l and "--workdir" in l]
    assert hint and "'%s'" % wd in hint[-1], p.stdout
    status = [l for l in p.stdout.splitlines()
              if "run_status.py" in l or "--workdir" in l]
    assert any("'%s'" % wd in l for l in status), p.stdout


# --------------------------------------------------------------------------
# issue #44 review round 2 (Devin on #47): the cap's remaining shapes, and
# the hints that have to survive a paste
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_status_a_non_finite_cap_is_reported_not_crashed(tmp_path, capsys,
                                                        monkeypatch, bad):
    """json.loads accepts NaN/Infinity, and int() on them raises — so the
    read-only dashboard would have answered a broken config with exit 1
    and a traceback, the very contract this PR is polishing."""
    monkeypatch.setattr(lp, "load_budget_file",
                        lambda _p: {"max_sessions_per_day": bad})
    work = _mk_run(tmp_path, [_rec(f"a__a-{i}", "resolved", T0 + i * 60)
                             for i in range(3)])
    assert run_status.main(["--workdir", str(work), "--manifest", ""]) \
        == run_status.EXIT_OK
    out = capsys.readouterr()
    assert "NOTE" in out.err and "not a finite number" in out.err, out.err
    assert "cap 10/window" in out.out and "Traceback" not in out.err


def test_status_an_unreadable_budget_file_says_so(tmp_path, capsys,
                                                 monkeypatch):
    """A file the operator DID configure, and broke, is the same class of
    answer as a PENDING cap: the ETA uses a number that is not theirs. It
    names the file to fix (issue #44 review round 2)."""
    def boom(_path):
        raise SystemExit("budget file must be a JSON object")
    monkeypatch.setattr(lp, "load_budget_file", boom)
    work = _mk_run(tmp_path, [_rec(f"a__a-{i}", "resolved", T0 + i * 60)
                             for i in range(3)])
    assert run_status.main(["--workdir", str(work), "--manifest", ""]) \
        == run_status.EXIT_OK
    err = capsys.readouterr().err
    assert "NOTE" in err and "cannot be read" in err, err[-300:]
    assert "live_pilot_budget.json" in err, err[-300:]


def test_status_the_note_blames_the_flag_when_the_flag_is_wrong(
        tmp_path, capsys):
    """--cap 0 is not the budget file's fault; telling the operator to
    edit the file would send them to the wrong place."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    assert run_status.main(["--workdir", str(work), "--manifest", "",
                          "--cap", "0"]) == run_status.EXIT_OK
    err = capsys.readouterr().err
    assert "fix --cap" in err and "live_pilot_budget.json" not in err, \
        err[-300:]


def test_run_window_quotes_what_the_shell_would_act_on(tmp_path):
    """The resume/status hints exist to be pasted. An argument with no
    whitespace can still stop being itself (`&`, `"`, `$`, a newline in a
    path), and an empty one vanishes entirely — so printable() quotes
    anything outside the shell's safe set."""
    for tricky in ["foo&bar", 'we"ird', "$HOME", "a;b", ""]:
        wd = tmp_path / f"wd-{abs(hash(tricky))}"
        p, calls = _window(tmp_path, rc=42, args=["--workdir", str(wd),
                                                 "--instances", tricky])
        argv = json.loads(calls.read_text())["argv"]
        assert argv[:4] == ["--workdir", str(wd), "--instances",
                            tricky], (tricky, argv)
        hint = [l for l in p.stdout.splitlines()
                if "run_window.sh" in l and "--workdir" in l]
        assert hint, (tricky, p.stdout)
        line = hint[-1]
        if tricky:
            assert repr(tricky) in line or f"'{tricky}'" in line, \
                (tricky, line)
        else:
            assert "--instances ''" in line, (tricky, line)


@pytest.mark.parametrize("value,added", [("0", True), ("false", True),
                                         ("no", True), ("1", False),
                                         ("true", False)])
def test_run_window_keep_running_is_a_flag_not_a_string(tmp_path, value,
                                                       added):
    """WP6C_KEEP_RUNNING=0 must mean "do not keep running" — the same
    reading every other flag in the repo gets (issue #44 review r2)."""
    p, calls = _window(tmp_path, rc=42,
                       args=["--workdir", str(tmp_path / "wd")],
                       env={"WP6C_KEEP_RUNNING": value})
    argv = json.loads(calls.read_text())["argv"]
    assert ("--stop-at-window-cap" in argv) is added, (value, argv)
    model = "stop at the cap" if added else "in-process sleep"
    assert model in p.stdout, (value, p.stdout)


# --------------------------------------------------------------------------
# issue #44 review round 3 (Devin on #47): the paste target, the unreadable
# bundle, and the cap that is a number but not a count
# --------------------------------------------------------------------------

def test_status_hint_is_one_pasteable_command(tmp_path):
    """A hint wrapped after the program name is not a command: pasting it
    runs run_status.py bare (argparse exit 2) and then tries to execute
    '--workdir'. Round 2's quoting fix introduced exactly this, and the
    round-2 test only asked that the quoted workdir appear SOMEWHERE."""
    import shlex
    wd = "/tmp/we ird"
    p, _ = _window(tmp_path, rc=42, args=["--workdir", wd])
    lines = p.stdout.splitlines()
    at = [n for n, l in enumerate(lines) if "Status any time" in l]
    assert at, p.stdout
    cmd = lines[at[0] + 1].strip()
    assert "run_status.py" in cmd, cmd
    assert shlex.split(cmd)[-2:] == ["--workdir", wd], cmd
    # and the line parses as a shell command, on its own, in one piece
    chk = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True)
    assert chk.returncode == 0, (cmd, chk.stderr)


@pytest.mark.parametrize("value,follows", [("FALSE", True), ("Off", True),
                                           ("NO", True), ("yes", False),
                                           ("1", False)])
def test_run_window_keep_running_reads_the_word_not_the_case(tmp_path,
                                                            value, follows):
    """WP6C_KEEP_RUNNING=FALSE is a person saying no in the caps their
    shell typed (issue #44 review round 3)."""
    p, calls = _window(tmp_path, rc=42,
                       args=["--workdir", str(tmp_path / "wd")],
                       env={"WP6C_KEEP_RUNNING": value})
    argv = json.loads(calls.read_text())["argv"]
    assert ("--stop-at-window-cap" in argv) is follows, (value, argv)


def test_verify_an_unreadable_manifest_is_input_not_a_traceback(tmp_path,
                                                               capsys):
    """A MANIFEST.tsv that exists but cannot be opened (mode 000, a dead
    mount) is a statement about the host, not about the bundle's
    durability — exit 2, no traceback (issue #44 review round 3)."""
    wd = _mk_workdir(tmp_path)
    dest = tmp_path / "offhost"
    ar.main(["--workdir", str(wd), "--dest", str(dest), "--stamp", "W1"])
    bundle = dest / "full500" / "windows" / "W1"
    man = bundle / "MANIFEST.tsv"
    os.chmod(man, 0)
    try:
        rc = ar.main(["--verify", "--bundle", str(bundle)])
    finally:
        os.chmod(man, 0o644)
    err = capsys.readouterr().err
    assert rc == ar.EXIT_INPUT, (rc, err)
    assert "INPUT:" in err and "Traceback" not in err, err


def test_usable_cap_says_so_for_a_fractional_cap():
    """3.7 is a cap the harness itself would refuse; truncating it to 3 in
    silence would make the ETA's denominator a private decision
    (issue #44 review round 3)."""
    cap, why = run_status.usable_cap(3.7)
    assert cap == 3
    assert "not a whole number" in why and "3/window" in why, why
    assert run_status.usable_cap(4) == (4, "")
    assert run_status.usable_cap(None) == (run_status.DEFAULT_CAP, "")


def test_status_a_manifest_with_a_hollow_entry_is_input_too(tmp_path,
                                                           capsys):
    """Every queue number on the dashboard comes from the manifest, and a
    null id used to pass the container check (issue #44 review r3)."""
    work = _mk_run(tmp_path, [_rec("a__a-1", "resolved", T0)])
    for doc in ('{"instances": [{"instance_id": null}]}',
                '{"instances": [5]}',
                '{"instances": [{"other": 1}]}'):
        man = tmp_path / "m.json"
        man.write_text(doc)
        rc = run_status.main(["--workdir", str(work), "--manifest",
                              str(man)])
        err = capsys.readouterr().err
        assert rc == run_status.EXIT_INPUT, (doc, rc, err)
        assert "instance_id" in err and "Traceback" not in err, (doc, err)


# --------------------------------------------------------------------------
# issue #44 review round 4 (Devin on #47): the tokens of the hint that were
# still bare, and two numbers that were not the numbers used
# --------------------------------------------------------------------------

def _run_from(tmp_path, spaced=True):
    """Run the wrapper from a checkout whose path contains a space — the
    condition every printed hint has to survive."""
    where = tmp_path / ("a copy" if spaced else "plain")
    where.mkdir(parents=True, exist_ok=True)
    target = where / "run_window.sh"
    target.write_text((_SCRIPTS / "run_window.sh").read_text())
    stub = tmp_path / "stub_harness.py"
    stub.write_text(STUB)
    env = dict(os.environ)
    env.update({"PYTHON": sys.executable, "LIVE_PILOT": str(stub),
                "STUB_RC": "42", "STUB_CALLS": str(tmp_path / "c.json")})
    p = subprocess.run(["bash", str(target), "--workdir",
                        str(tmp_path / "wd")], capture_output=True,
                       text=True, env=env, timeout=120, cwd=str(tmp_path))
    return p, target


def test_status_hint_survives_a_checkout_with_a_space(tmp_path):
    """Round 3 quoted $0 and the workdir but left ``$PYTHON`` and
    ``$SELF_DIR/run_status.py`` bare, so under '/tmp/spaced dir/' the hint
    printed a command that runs '/tmp/spaced'. Both hints are one shlex
    round-trip from being the real argv now."""
    import shlex
    p, target = _run_from(tmp_path)
    lines = p.stdout.splitlines()
    wd = str(tmp_path / "wd")

    def hint(marker):
        at = [n for n, l in enumerate(lines) if marker in l]
        assert at, (marker, p.stdout)
        cmd = lines[at[0] + 1].strip()
        chk = subprocess.run(["bash", "-n", "-c", cmd],
                             capture_output=True)
        assert chk.returncode == 0, (cmd, chk.stderr)
        return shlex.split(cmd)

    resume = hint("Next window")
    assert resume[0] == str(target), resume        # $0, quoted
    # …followed by the EFFECTIVE argv, which is the operator's own argv
    # plus the --stop-at-window-cap this script supplies
    assert resume[1:3] == ["--workdir", wd], resume
    assert resume[-1] == "--stop-at-window-cap", resume
    status = hint("Status any time")
    assert status[0] == sys.executable, status     # $PYTHON
    assert status[1] == str(target.parent / "run_status.py"), status
    assert status[-2:] == ["--workdir", wd], status


@pytest.mark.parametrize("arg", ["--workdir={wd}", "--workdir {wd}"])
def test_the_operator_argv_reaches_the_harness_exactly_as_typed(tmp_path,
                                                               arg):
    """The wrapper used to rewrite ``--workdir=X`` into ``--workdir X``,
    which is a DIFFERENT command to argparse: the glued form carries a
    value beginning with ``-`` (``--workdir=--x`` is the value ``--x``),
    the split form hands argparse an option where the value was. So argv
    goes through untouched, and the wrapper reads both spellings itself
    for the one thing it needs — where the log and the archive go
    (issue #44 review round 4)."""
    wd = str(tmp_path / "wd")
    p, calls = _window(tmp_path, rc=42, args=arg.format(wd=wd).split())
    argv = json.loads(calls.read_text())["argv"]
    assert " ".join(argv).startswith(arg.format(wd=wd)), argv
    assert "--stop-at-window-cap" in argv, argv
    assert list((tmp_path / "wd" / "logs").glob("window-*.log")), \
        p.stdout


def test_status_a_missing_workdir_is_exit_2(tmp_path, capsys):
    """"no workdir"/"two checkpoints, pass --run-id" is unreadable input,
    which this script documents as exit 2 — it left with exit 1 and a
    bare message (pre-existing, in the contract this PR polishes)."""
    for args in ([str(tmp_path / "absent"), None],):
        rc = run_status.main(["--workdir", args[0], "--manifest", ""])
        err = capsys.readouterr().err
        assert rc == run_status.EXIT_INPUT, (rc, err)
        assert err.startswith("INPUT:"), err
    # an ambiguous workdir says the same thing
    work = tmp_path / "two"
    work.mkdir()
    for name in ("run-a.jsonl", "run-b.jsonl"):
        (work / name).write_text("")
    assert run_status.main(["--workdir", str(work), "--manifest",
                          ""]) == run_status.EXIT_INPUT


def test_usable_cap_names_the_cap_the_eta_uses():
    """status() computes with max(1, cap), so a 0.5 announced as
    '0/window' would name a number the ETA never used (r4)."""
    for value, expected in [(0.5, 1), (3.7, 3), (1.2, 1)]:
        cap, why = run_status.usable_cap(value)
        assert cap == expected, (value, cap)
        assert f"uses {expected}/window" in why, (value, why)
