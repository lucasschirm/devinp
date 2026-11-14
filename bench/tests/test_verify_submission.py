"""WP6c pre-flight E4 (issue #44) — verify_submission.py /
package_submission.py.

Zero quota, zero network, zero docker: every test builds a small run dir
(a checkpoint + the per-instance artifacts + a real WP5 report written by
``report.generate``) and drives the scripts' ``main`` — the happy path
plus **every deliberate break** the AC's "artifact-integrity script"
exists to catch.
"""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parent.parent
_SCRIPTS = _BENCH / "scripts"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import report as rep  # noqa: E402


def _load(name, alias):
    """Load a bench/scripts/*.py as a module (they are scripts, not an
    importable package — the same convention as the other script tests)."""
    spec = importlib.util.spec_from_file_location(alias,
                                                 str(_SCRIPTS /
                                                     f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


vs = _load("verify_submission", "verify_submission_under_test")
ps = _load("package_submission", "package_submission_under_test")

IIDS = ["pallets__flask-5014", "mwaskom__seaborn-3069",
        "psf__requests-1142"]
STATUSES = ["resolved", "failed_test", "resolved"]
PATCHES = ["--- a/f.py\n+++ b/f.py\n@@\n-x\n+y\n",
           "--- a/g.py\n+++ b/g.py\n@@\n-1\n+2\n",
           "--- a/h.py\n+++ b/h.py\n@@\n-a\n+b\n"]


def _meta(**over) -> dict:
    m = {
        "run_id": "r1",
        "devin_cli_version": "devin 3000.12.43 (abc)",
        "model": "swe-2-high",
        "cost_tier": "Free",
        "prompt_template_hash": "a" * 64,
        "permission_policy": "SwebenchPolicy",
        "caps": {"max_sessions_per_day": 10,
                 "max_wall_per_session": 2700},
        "harness_version": "swebench==5.0.2",
        "images": "swebench/sweb.eval.x86_64.*",
        "label": vs.DEFAULT_LABEL,
        "model_name_or_path": vs.DEFAULT_LABEL,
        "mode": "container",
        "grading_leg": ("official swebench.harness.run_evaluation (WP4) "
                        "in the published sweb.eval image"),
    }
    m.update(over)
    return m


def build(tmp_path, *, statuses=None, meta=None, manifest=True,
         patches=None, write_meta=True, with_eval=True):
    """A complete run dir: the checkpoint, the per-instance artifacts and
    a report written by the real WP5 generator. Returns the paths."""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    jsonl = work / "run-r1.jsonl"
    st = statuses or STATUSES
    pt = patches or PATCHES
    with open(jsonl, "w", encoding="utf-8", newline="") as f:
        for i, (iid, status) in enumerate(zip(IIDS, st)):
            ad = f"runs/{iid}"
            inst_dir = work / ad
            inst_dir.mkdir(parents=True, exist_ok=True)
            m = {"artifacts_dir": ad}
            if status in ("resolved", "failed_test"):
                (inst_dir / "patch.diff").write_text(pt[i])
                m["patch_diff"] = "patch.diff"
                m["patch_bytes"] = len(pt[i])
            (inst_dir / "trajectory.jsonl").write_text(
                json.dumps({"role": "user", "text": f"t{i}"}) + "\n")
            m["trajectory"] = "trajectory.jsonl"
            if with_eval:
                (inst_dir / "eval.log").write_text(f"eval {iid}\n")
                m["eval_log"] = "eval.log"
            m["wall"] = {"session_s": 100.0 + i}
            m["usage"] = {"totalTokens": 1000 + i}
            f.write(json.dumps({"instance_id": iid, "status": status,
                               "ts": 1_700_000_000.0 + i * 60,
                               "meta": m}) + "\n")
    report_dir = work / "report"
    m = meta if meta is not None else _meta()
    rep.generate(str(jsonl), str(report_dir), m)
    # what live_pilot.run_pilot persists next to the report (issue #44
    # E3) — the fixture writes it the same way
    with open(report_dir / "run_meta.json", "w", encoding="utf-8") as f:
        json.dump(m, f)
    if not write_meta:
        (report_dir / "run_meta.json").unlink()
    man = None
    if manifest:
        man = tmp_path / "manifest.json"
        man.write_text(json.dumps(
            {"instances": [{"instance_id": i} for i in IIDS]}))
    return {"work": work, "jsonl": str(jsonl),
           "report": str(report_dir), "manifest": str(man) if man
           else None}


def vmain(*args) -> int:
    return vs.main(list(args))


def pmain(*args) -> int:
    return ps.main(list(args))


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_verify_clean(tmp_path, capsys):
    f = build(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                 ) == vs.EXIT_OK
    out = capsys.readouterr().out
    assert "NOT" not in out
    for name in ("META", "R1+R2", "METHODOLOGY", "GRADING LEG",
                 "PASS@1", "LABEL", "QUEUE"):
        assert f"[PASS] {name}" in out, out


def test_verify_sandbox_mode_still_verifies_but_says_estimate(tmp_path,
                                                              capsys):
    """D1 honesty: a sandbox-mode run passes the integrity checks and the
    verifier says out loud that it is not a submission score."""
    f = build(tmp_path, meta=_meta(mode="sandbox",
                                  grading_leg="host check"))
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                 ) == vs.EXIT_OK
    out = capsys.readouterr().out
    assert "internal estimate" in out
    assert "not submit" in out.lower()


def test_verify_report_dir_default(tmp_path):
    f = build(tmp_path)
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert vmain("--jsonl", f["jsonl"], "--manifest",
                    f["manifest"]) == vs.EXIT_OK
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# every deliberate break
# ---------------------------------------------------------------------------

def test_break_missing_run_meta(tmp_path, capsys):
    f = build(tmp_path, write_meta=False)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "[FAIL] META" in capsys.readouterr().out


def test_break_meta_not_object(tmp_path):
    f = build(tmp_path)
    Path(f["report"], "run_meta.json").write_text("[1,2]")
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY


def test_break_unknown_methodology_field(tmp_path, capsys):
    """A methodology field that renders ``unknown`` means the run did not
    record it — item 20 / the AC's 'methodology-block completeness'."""
    m = _meta()
    del m["devin_cli_version"]
    f = build(tmp_path, meta=m)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "unknown" in capsys.readouterr().out


def test_break_grading_leg_unrecorded(tmp_path, capsys):
    m = _meta()
    del m["mode"]
    f = build(tmp_path, meta=m)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "GRADING LEG" in capsys.readouterr().out


def test_break_sandbox_without_grading_leg_text(tmp_path):
    f = build(tmp_path, meta=_meta(mode="sandbox", grading_leg=""))
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY


def test_break_missing_patch_file(tmp_path, capsys):
    """R1: a referenced patch.diff that is gone."""
    f = build(tmp_path)
    (Path(f["work"]) / "runs" / IIDS[0] / "patch.diff").unlink()
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "[FAIL] R1+R2" in capsys.readouterr().out


def test_break_mutated_patch_vs_preds(tmp_path, capsys):
    """R2: the checkpoint's patch and all_preds.jsonl disagree."""
    f = build(tmp_path)
    (Path(f["work"]) / "runs" / IIDS[0] / "patch.diff").write_text(
        "--- a/f.py\n+++ b/f.py\n@@\n-x\n+Z\n")
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "[FAIL] R1+R2" in capsys.readouterr().out


def test_break_missing_manifest_instance(tmp_path, capsys):
    f = build(tmp_path, statuses=["resolved", "resolved", "resolved"])
    # drop the last checkpoint line: the queue is incomplete
    lines = Path(f["jsonl"]).read_text().splitlines()[:-1]
    Path(f["jsonl"]).write_text("\n".join(lines) + "\n")
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "[FAIL] QUEUE" in capsys.readouterr().out


def test_break_pending_retry_final(tmp_path, capsys):
    """A ``quota_limited`` final is undecided: the run is not finished,
    so the submission is not submittable (issue #44 E4 / item 18)."""
    f = build(tmp_path, statuses=["resolved", "quota_limited",
                                 "resolved"])
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    out = capsys.readouterr().out
    assert "[FAIL] QUEUE" in out and "pending retry-class 1" in out


def test_break_final_outside_manifest(tmp_path, capsys):
    f = build(tmp_path, manifest=False)
    man = tmp_path / "m2.json"
    man.write_text(json.dumps({"instances": [
        {"instance_id": i} for i in IIDS[:2]]}))
    assert vmain("--jsonl", f["jsonl"], "--manifest", str(man)
                ) == vs.EXIT_INTEGRITY
    assert "outside-manifest 1" in capsys.readouterr().out


def test_break_render_drift_in_the_report(tmp_path, capsys):
    """The published markdown must equal the recomputation — a hand-edit
    (or a render bug) that inflates the number is caught."""
    f = build(tmp_path)
    md = Path(f["report"], "report.md")
    orig = md.read_text()
    text = orig.replace("| pallets__flask | 1 | 1 | 100.0% |",
                       "| pallets__flask | 2 | 2 | 100.0% |")
    assert text != orig  # the fixture row really is there to inflate
    md.write_text(text)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    assert "DISAGREES" in capsys.readouterr().out


def test_break_wrong_label(tmp_path, capsys):
    f = build(tmp_path)
    preds = Path(f["report"], "all_preds.jsonl")
    lines = [json.loads(l) for l in preds.read_text().splitlines()]
    lines[1]["model_name_or_path"] = "swe-2-high"
    preds.write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY
    out = capsys.readouterr().out
    assert "[FAIL] LABEL" in out


def test_break_unparseable_preds_line(tmp_path):
    f = build(tmp_path)
    preds = Path(f["report"], "all_preds.jsonl")
    preds.write_text(preds.read_text() + "{not json\n")
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY


def test_break_missing_report_md(tmp_path):
    f = build(tmp_path)
    (Path(f["report"]) / "report.md").unlink()
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY


def test_break_missing_eval_log_reference(tmp_path, capsys):
    """A line referencing an eval.log that was never written fails R1."""
    f = build(tmp_path)
    (Path(f["work"]) / "runs" / IIDS[0] / "eval.log").unlink()
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == vs.EXIT_INTEGRITY


# ---------------------------------------------------------------------------
# the input errors (exit 2, not an integrity violation)
# ---------------------------------------------------------------------------

def test_input_missing_jsonl(tmp_path):
    assert vmain("--jsonl", str(tmp_path / "nope.jsonl")) == vs.EXIT_INPUT


def test_input_missing_report_dir(tmp_path):
    f = build(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--report",
                str(tmp_path / "nope")) == vs.EXIT_INPUT


def test_input_missing_manifest(tmp_path):
    f = build(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--manifest",
                str(tmp_path / "nope.json")) == vs.EXIT_INPUT


def test_manifest_empty_string_skips_the_queue_check(tmp_path, capsys):
    f = build(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--manifest", "") == vs.EXIT_OK
    assert "QUEUE" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# package_submission
# ---------------------------------------------------------------------------

def _pkg(tmp_path, *extra, manifest=None):
    f = build(tmp_path)
    out = str(tmp_path / "submission" / "r1")
    man = f["manifest"] if manifest is None else manifest
    rc = pmain("--jsonl", f["jsonl"], "--out", out,
              *(["--manifest", man] if man else ["--manifest", ""]),
              *extra)
    return f, out, rc


def test_package_layout(tmp_path):
    f, out, rc = _pkg(tmp_path)
    assert rc == ps.EXIT_OK
    root = Path(out)
    for name in ("README.md", "all_preds.jsonl", "report.md",
                "run_meta.json", "run-r1.jsonl", "SHA256SUMS"):
        assert (root / name).is_file(), name
    # the AC's runs/<run_id>/<iid>/… layout (D4)
    for iid, status in zip(IIDS, STATUSES):
        d = root / "runs" / "r1" / iid
        assert (d / "trajectory.jsonl").is_file(), d
        assert (d / "eval.log").is_file(), d
        if status in ("resolved", "failed_test"):
            assert (d / "patch.diff").is_file(), d
        else:
            assert not (d / "patch.diff").exists()
    assert (root / "trajs" / f"{IIDS[0]}.jsonl").is_file()


def test_package_readme_carries_label_methodology_and_deviation(tmp_path):
    f, out, rc = _pkg(tmp_path)
    md = (Path(out) / "README.md").read_text()
    assert vs.DEFAULT_LABEL in md
    for label in ("Devin CLI version", "Cost tier", "Prompt template hash",
                 "Permission policy", "Caps", "Harness version",
                 "Eval images", "Date range (UTC)",
                 "Sessions used (attempts)"):
        assert label in md, label
    assert "## Deviations" in md
    assert "unknown" not in md.split("## Deviations")[0].replace(
        "unknown test", "")  # no methodology field rendered unknown


def test_package_sandbox_deviation_is_stated(tmp_path):
    f = build(tmp_path, meta=_meta(mode="sandbox",
                                  grading_leg="host check"))
    out = str(tmp_path / "sub")
    rc = pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
              f["manifest"])
    assert rc == ps.EXIT_OK
    md = (Path(out) / "README.md").read_text()
    assert "**not** leaderboard-submittable" in md
    assert "host check" in md


def test_package_checksums_match(tmp_path):
    f, out, rc = _pkg(tmp_path)
    sums = (Path(out) / "SHA256SUMS").read_text().splitlines()
    assert sums
    for line in sums:
        sha, rel = line.split("  ")
        target = Path(out) / rel
        assert target.is_file(), rel
        assert hashlib.sha256(target.read_bytes()).hexdigest() == sha, rel


def test_package_is_deterministic(tmp_path):
    f, out, rc = _pkg(tmp_path)
    first = sorted((p.relative_to(out), p.read_bytes())
                 for p in Path(out).rglob("*") if p.is_file())
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"], "--force") == ps.EXIT_OK
    second = sorted((p.relative_to(out), p.read_bytes())
                  for p in Path(out).rglob("*") if p.is_file())
    assert first == second


def test_package_refuses_non_empty_out_without_force(tmp_path):
    f, out, rc = _pkg(tmp_path)
    assert rc == ps.EXIT_OK
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"]) == ps.EXIT_INPUT


def test_package_refuses_incomplete_queue(tmp_path, capsys):
    f = build(tmp_path, statuses=["resolved", "quota_limited",
                                 "resolved"])
    out = str(tmp_path / "sub")
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"]) == ps.EXIT_INTEGRITY
    assert "QUEUE" in capsys.readouterr().err


def test_package_interim_bundle_is_stamped(tmp_path):
    f = build(tmp_path, statuses=["resolved", "quota_limited",
                                 "resolved"])
    out = str(tmp_path / "sub")
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"], "--allow-incomplete") == ps.EXIT_OK
    md = (Path(out) / "README.md").read_text()
    assert "INTERIM" in md
    assert "still pending the" in md


def test_package_allow_incomplete_does_not_cover_other_breaks(tmp_path,
                                                             capsys):
    """``--allow-incomplete`` is for the queue only — an unlabelled or
    integrity-broken run never produces a bundle."""
    f = build(tmp_path)
    preds = Path(f["report"], "all_preds.jsonl")
    lines = [json.loads(l) for l in preds.read_text().splitlines()]
    lines[0]["model_name_or_path"] = "swe-2-high"
    preds.write_text("".join(json.dumps(l) + "\n" for l in lines))
    out = str(tmp_path / "sub")
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"], "--allow-incomplete") == ps.EXIT_INTEGRITY
    assert "covers an incomplete QUEUE only" in capsys.readouterr().err


def test_package_refuses_a_missing_run_meta(tmp_path):
    f = build(tmp_path, write_meta=False)
    out = str(tmp_path / "sub")
    assert pmain("--jsonl", f["jsonl"], "--out", out, "--manifest",
                f["manifest"]) == ps.EXIT_INPUT


def test_package_input_errors(tmp_path):
    f = build(tmp_path)
    assert pmain("--jsonl", str(tmp_path / "nope.jsonl"),
                "--out", str(tmp_path / "s")) == ps.EXIT_INPUT
    assert pmain("--jsonl", f["jsonl"], "--report",
                str(tmp_path / "nope"), "--out",
                str(tmp_path / "s")) == ps.EXIT_INPUT
    assert pmain("--jsonl", f["jsonl"], "--manifest",
                str(tmp_path / "nope.json"), "--out",
                str(tmp_path / "s")) == ps.EXIT_INPUT


def test_package_default_out_dir(tmp_path):
    """--out omitted: submission/<run_id> next to the workdir."""
    f = build(tmp_path)
    assert pmain("--jsonl", f["jsonl"], "--manifest", f["manifest"]
                ) == ps.EXIT_OK
    assert (tmp_path / "submission" / "r1" / "README.md").is_file()


# ---------------------------------------------------------------------------
# the bundle check (verify --bundle), and the round trip
# ---------------------------------------------------------------------------

def test_verify_bundle_round_trip(tmp_path, capsys):
    f, out, rc = _pkg(tmp_path)
    assert rc == ps.EXIT_OK
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", out) == vs.EXIT_OK
    assert "[PASS] BUNDLE" in capsys.readouterr().out


def test_verify_bundle_missing_file(tmp_path, capsys):
    f, out, rc = _pkg(tmp_path)
    (Path(out) / "runs" / "r1" / IIDS[0] / "patch.diff").unlink()
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", out) == vs.EXIT_INTEGRITY
    assert "absent from the bundle" in capsys.readouterr().out


def test_verify_bundle_mutated_file(tmp_path, capsys):
    f, out, rc = _pkg(tmp_path)
    p = Path(out) / "runs" / "r1" / IIDS[0] / "patch.diff"
    p.write_text(p.read_text().replace("+y", "+EVIL"))
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", out) == vs.EXIT_INTEGRITY
    assert "differs" in capsys.readouterr().out


def test_verify_bundle_missing_runs_dir(tmp_path, capsys):
    f, out, rc = _pkg(tmp_path)
    import shutil as _sh
    _sh.rmtree(Path(out) / "runs" / "r1")
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", out) == vs.EXIT_INTEGRITY


def test_verify_bundle_not_a_directory(tmp_path):
    f, out, rc = _pkg(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", str(tmp_path / "absent")) == vs.EXIT_INTEGRITY


def test_verify_bundle_source_artifact_missing(tmp_path, capsys):
    """The bundle is checked against the checkpoint's own artifacts: a
    source that vanished is reported (the bundle alone cannot vouch for a
    run whose artifacts are gone — the Pilot-B failure mode)."""
    f, out, rc = _pkg(tmp_path)
    (Path(f["work"]) / "runs" / IIDS[0] / "patch.diff").unlink()
    assert vmain("--jsonl", f["jsonl"], "--manifest", f["manifest"],
                "--bundle", out) == vs.EXIT_INTEGRITY
    assert "the source" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the shared constants must not drift
# ---------------------------------------------------------------------------

def test_label_constant_matches_the_harness():
    import live_pilot as lp
    assert vs.DEFAULT_LABEL == lp.SUBMISSION_LABEL


def test_run_id_derivation(tmp_path):
    assert vs.bundle_run_id("/x/work/run-full500.jsonl") == "full500"
    # an input problem, not a crash: it maps to the documented exit 2
    with pytest.raises(vs.InputError):
        vs.bundle_run_id("/x/work/checkpoint.json")


def test_report_md_parser_round_trips_the_generator(tmp_path):
    f = build(tmp_path)
    md = Path(f["report"], "report.md").read_text()
    overall, rows = rep.parse_report_pass1(md)
    finals, _ = rep.dedupe(rep.load_jsonl(f["jsonl"])[0])
    p1 = rep.pass1(finals)
    assert overall == {"resolved": p1["overall"]["resolved"],
                      "terminal": p1["overall"]["terminal"],
                      "pct": p1["overall"]["pct"]}
    assert rows == {r: (c["resolved"], c["terminal"], c["pct"])
                   for r, c in p1["per_repo"].items()}


def test_report_md_parser_absent_sections():
    overall, rows = rep.parse_report_pass1("nothing here")
    assert overall is None and rows == {}


def test_report_md_parser_malformed_row_is_not_silently_dropped():
    md = ("## Per-repo pass@1\n\n| Repo | Resolved | Terminal | pass@1 |\n"
         "|---|---|---|---|\n| a/b | x | 1 | n/a |\n")
    _ov, rows = rep.parse_report_pass1(md)
    assert rows == {"a/b": (None, None, "n/a")}


# --------------------------------------------------------------------------
# issue #44 review round 1: the documented exit codes must be the ones the
# runbook sees — and a dropped checkpoint line is never silently accepted
# --------------------------------------------------------------------------

def test_clean_checkpoint_is_itself_a_check(tmp_path, capsys):
    f = build(tmp_path)
    assert vmain("--jsonl", f["jsonl"], "--manifest",
                f["manifest"]) == vs.EXIT_OK
    assert "[PASS] CHECKPOINT" in capsys.readouterr().out


def test_a_dropped_line_fails_the_verification(tmp_path, capsys):
    """The loader's torn-last-line rule makes that instance's outcome
    UNKNOWN, and WP5 excludes it from the pass@1 denominator — so a
    verifier that ignored `dropped` would certify a submission whose
    submitted number silently shrank (issue #44 E4)."""
    f = build(tmp_path)
    with open(f["jsonl"], "a", encoding="utf-8") as fh:
        fh.write('{"instance_id": "psf__requests-1142", "stat')  # torn
    assert vmain("--jsonl", f["jsonl"], "--manifest",
                f["manifest"]) == vs.EXIT_INTEGRITY
    out = capsys.readouterr().out
    assert "[FAIL] CHECKPOINT" in out, out
    assert "DROPPED" in out


def test_a_corrupt_checkpoint_is_an_input_error_not_a_traceback(
        tmp_path, capsys):
    """Mid-file corruption is fail-loud in the WP5 loader; the verifier's
    contract is the documented exit 2 (bad input), not exit 1 with a
    stack trace the runbook cannot interpret."""
    f = build(tmp_path)
    lines = Path(f["jsonl"]).read_text().splitlines()
    lines.insert(1, "{ this is not json")
    Path(f["jsonl"]).write_text("\n".join(lines) + "\n")
    assert vmain("--jsonl", f["jsonl"], "--manifest",
                f["manifest"]) == vs.EXIT_INPUT
    err = capsys.readouterr().err
    assert err.startswith("INPUT:") and "Traceback" not in err, err


def test_a_malformed_manifest_is_an_input_error(tmp_path, capsys):
    """Completeness against the pinned manifest is the check that gives
    the submission its 'all 500' claim — a file that is not a manifest
    must stop the run, never degrade into 'verified'."""
    f = build(tmp_path)
    bad = tmp_path / "bad-manifest.json"
    for content in ('{"instances": [{"id": "x"}]}',   # KeyError shape
                   "[]",                              # empty
                   "{not json",                       # not JSON
                   '{"instances": "nope"}'):          # wrong type
        bad.write_text(content)
        assert vmain("--jsonl", f["jsonl"], "--manifest",
                    str(bad)) == vs.EXIT_INPUT, content
        err = capsys.readouterr().err
        assert err.startswith("INPUT:") and "Traceback" not in err, err


def test_the_label_must_be_the_header(tmp_path, capsys):
    """A substring test would PASS a report that mentions the label in
    prose while its header renders without it — the reviewer reads the
    header (issue #44 E3)."""
    f = build(tmp_path)
    md_path = Path(f["report"]) / "report.md"
    md = md_path.read_text()
    want = f"**{vs.DEFAULT_LABEL}**"
    assert want in md
    drift = "\n".join(l for l in md.splitlines() if l.strip() != want)
    drift += f"\n- methodology note: this is {vs.DEFAULT_LABEL}\n"
    md_path.write_text(drift)
    assert vs.DEFAULT_LABEL in md_path.read_text()   # substring: present
    assert vmain("--jsonl", f["jsonl"], "--manifest",
                f["manifest"]) == vs.EXIT_INTEGRITY
    out = capsys.readouterr().out
    assert "[FAIL] LABEL" in out, out
