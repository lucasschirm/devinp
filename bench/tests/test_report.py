"""WP5 (issue #18) — ``report.py``: aggregation, failure taxonomy,
methodology block, and the leaderboard artifacts.

All tests are unit-level against fixture JSONL on ``tmp_path`` — zero
quota, no ``devin``, no Docker, no network, CI safe. The fixture is
built with **exact expected numbers** (issue #18 "Testable by"):

* F1 — pass@1 overall + per-repo table (the 12-line fixture; all 8
  statuses non-zero in the final tally);
* F2 — resume-dedup (a duplicated line resolves to the **last** line —
  file order, not timestamp; a batch marker can never shadow an
  instance);
* F3 — terminal-status exclusion (the retry-class finals are absent
  from the pass@1 denominator, present in the taxonomy, and in
  ``all_preds.jsonl`` under the harness's empty-patch semantics);
* F4 — the artifact cross-check (R1: every line's referenced files
  exist — incl. a *superseded* line's references; a ``resolved`` /
  ``failed_test`` final requires an existing ``patch.diff``; R2:
  ``all_preds.jsonl`` content matches ``patch.diff`` byte-for-byte —
  detected in ``--check`` after a post-generation tamper);
* F5 — ``trajs/<instance_id>.jsonl`` byte-for-byte copies;
* F6 — the methodology block (meta file → exact values; the computed
  date range + sessions used exact; no meta file → ``unknown``);
* F7 — determinism (byte-stable) + the torn-EOF drop (noted, not
  fatal) + the mid-file corruption (fail-loud);
* F8 — the CLI exit-code contract (0 / 2 / 3 / 4) via subprocess.

Acceptance mapping (issue #18): AC1 → ``test_pass1_overall_and_per_
repo_exactly``, ``test_repo_of_mapping``, ``test_terminal_status_
exclusion_from_pass1``; AC2 → ``test_taxonomy_exact``; AC3 → ``test_
methodology_from_meta_file_exactly``, ``test_methodology_unknown_
without_meta_file``; AC4 → ``test_all_preds_jsonl_exactly``, ``test_
model_name_precedence``, ``test_trajs_copies_exact_bytes``; AC5 →
``test_crosscheck_positive_passes``, the ``test_crosscheck_*``
negatives, ``test_check_mode_detects_tampered_patch``; AC6 → all of
the above (fixture → exact numbers).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import report
from report import (
    ArtifactCheckError,
    InvalidInputError,
    ReportProtocolError,
)

_BENCH = Path(__file__).resolve().parent.parent
_REPORT = _BENCH / "report.py"

# ---------------------------------------------------------------------------
# the fixture (PLAN-WP5.md F1) — 12 lines: 11 per-instance + 1 batch
# marker; all 8 statuses non-zero in the final tally
# ---------------------------------------------------------------------------

P1 = (
    "diff --git a/flask/app.py b/flask/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/flask/app.py\n"
    "+++ b/flask/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    # fix 5014\n"
)
P1B = (
    "diff --git a/flask/app.py b/flask/app.py\n"
    "index 2222222..3333333 100644\n"
    "--- a/flask/app.py\n"
    "+++ b/flask/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def f():\n"
    "-    return 2\n"
    "+    return 3\n"
    "+    # fix 5015\n"
)
P2 = (
    "diff --git a/seaborn/axes.py b/seaborn/axes.py\n"
    "index 4444444..5555555 100644\n"
    "--- a/seaborn/axes.py\n"
    "+++ b/seaborn/axes.py\n"
    "@@ -10,2 +10,3 @@\n"
    "+    # fix 2650\n"
)
# A multi-line trajectory JSONL with a NEWLINE embedded in a JSON string
# (the byte-for-byte copy must preserve it exactly).
T1 = (
    '{"ts": 1, "action": "read", "path": "flask/app.py"}\n'
    '{"ts": 2, "action": "edit", "note": "line one\\nline two"}\n'
    '{"ts": 3, "action": "exec", "cmd": "python -m pytest flask/app.py"}\n'
)
T2 = '{"ts": 1, "action": "read", "path": "seaborn/axes.py"}\n'
E1 = "eval log line 1\n"

# (instance_id | None, status, meta) — in file order. The flask-5015 pair
# is the dedup case: the quota_limited line (ts 1001) is superseded by
# the failed_test line (ts 1008); the later line's ts is deliberately
# *older* than a sibling line's (ts 1009) to pin file-order authority.
FIXTURE_LINES = [
    ("pallets__flask-5014", "resolved",
     {"patch_diff": "p1.diff", "trajectory": "t1.jsonl"}),
    ("pallets__flask-5015", "quota_limited", {"eval_log": "e1.log"}),
    ("django__django-11001", "timeout", {}),
    ("django__django-11002", "no_patch", {}),
    ("astropy__astropy-12907", "eval_error", {}),
    ("sympy__sympy-123", "preflight_failed", {}),
    ("scikit__scikit-learn-99", "eval_error", {}),
    ("mwaskom__seaborn-2650", "resolved",
     {"patch_diff": "p2.diff", "trajectory": "t2.jsonl"}),
    ("pallets__flask-5015", "failed_test", {"patch_diff": "p1b.diff"}),
    ("psf__requests-1234", "agent_error", {}),
    ("matplotlib__matplotlib-2870", "quota_limited", {}),
    (None, "preflight_failed", {}),
]

# Exact expected values (PLAN-WP5.md F1).
EXPECTED_DISTINCT = 10
EXPECTED_TAXONOMY = {
    "resolved": 2,
    "failed_test": 1,
    "timeout": 1,
    "no_patch": 1,
    "agent_error": 1,
    "quota_limited": 1,
    "eval_error": 2,
    "preflight_failed": 1,
}
EXPECTED_TERMINAL = 6
EXPECTED_PASS1_PCT = "33.3%"
EXPECTED_PENDING = 4
EXPECTED_SESSIONS_USED = 10
EXPECTED_DATE_RANGE = ("1970-01-01T00:16:40Z", "1970-01-01T00:16:51Z")
EXPECTED_PER_REPO = {
    "astropy__astropy": {"resolved": 0, "terminal": 0, "rate": None,
                       "pct": "n/a"},
    "django__django": {"resolved": 0, "terminal": 2, "rate": 0.0,
                     "pct": "0.0%"},
    "matplotlib__matplotlib": {"resolved": 0, "terminal": 0, "rate": None,
                             "pct": "n/a"},
    "mwaskom__seaborn": {"resolved": 1, "terminal": 1, "rate": 1.0,
                       "pct": "100.0%"},
    "pallets__flask": {"resolved": 1, "terminal": 2, "rate": 0.5,
                      "pct": "50.0%"},
    "psf__requests": {"resolved": 0, "terminal": 1, "rate": 0.0,
                    "pct": "0.0%"},
    "scikit__scikit-learn": {"resolved": 0, "terminal": 0, "rate": None,
                            "pct": "n/a"},
    "sympy__sympy": {"resolved": 0, "terminal": 0, "rate": None,
                    "pct": "n/a"},
}
EXPECTED_PRED_IDS = [
    "astropy__astropy-12907",
    "django__django-11001",
    "django__django-11002",
    "matplotlib__matplotlib-2870",
    "mwaskom__seaborn-2650",
    "pallets__flask-5014",
    "pallets__flask-5015",
    "psf__requests-1234",
    "scikit__scikit-learn-99",
    "sympy__sympy-123",
]


def _build_fixture(tmp_path: Path,
                  lines=None,
                  artifacts: "dict[str, str] | None" = None,
                  ) -> "tuple[Path, Path]":
    """Write the fixture run JSONL + the referenced artifact files (by
    default the *complete* set — p1/p1b/p2/t1/t2/e1 all exist). Returns
    ``(jsonl_path, out_dir)``. ``lines`` overrides the fixture lines;
    ``artifacts`` (``name → content``) overrides the default set (a
    name absent from the dict is *not written* — for the missing-file
    negative tests)."""
    lines = FIXTURE_LINES if lines is None else lines
    if artifacts is None:
        artifacts = {
            "p1.diff": P1,
            "p1b.diff": P1B,
            "p2.diff": P2,
            "t1.jsonl": T1,
            "t2.jsonl": T2,
            "e1.log": E1,
        }
    for name, content in artifacts.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    jsonl = tmp_path / "run-001.jsonl"
    with open(jsonl, "w", encoding="utf-8", newline="") as f:
        for i, (iid, status, meta) in enumerate(lines):
            f.write(json.dumps(
                {"instance_id": iid, "status": status, "meta": meta,
                 "ts": 1000.0 + i}
            ) + "\n")
    out = tmp_path / "out"
    return jsonl, out


def _read_jsonl(path: Path) -> "list[dict]":
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines()
                if line != ""]


def _cli(tmp_path: Path, *args: str) -> "subprocess.CompletedProcess":
    return subprocess.run(
        [sys.executable, str(_REPORT), *args],
        capture_output=True,
        text=True,
        cwd=str(_BENCH),
        timeout=120,
    )


# ---------------------------------------------------------------------------
# F2 — resume-dedup (the last line wins; file order, not ts)
# ---------------------------------------------------------------------------


def test_resume_dedup_last_line_wins_file_order(tmp_path):
    finals, markers = report.dedupe(_records(tmp_path))
    # the flask-5015 pair: quota_limited (ts 1001) → failed_test (ts 1008)
    assert finals["pallets__flask-5015"]["status"] == "failed_test"
    # the superseded line's artifact reference (e1.log) is still checked
    # by the cross-check (R1 over every line) — the dedup view must not
    # have lost it: the record kept is the last line's, but *every*
    # line is cross-checked (asserted in
    # test_crosscheck_superseded_line_reference_is_checked).
    assert len(finals) == EXPECTED_DISTINCT
    assert len(markers) == 1
    assert markers[0]["instance_id"] is None
    # a batch marker can never shadow / be shadowed by an instance
    assert None not in finals


def test_resume_dedup_later_line_with_older_ts_still_wins(tmp_path):
    """File order, *not* timestamp: a retry line written after the
    superseded one wins even when its ``ts`` is older (a wall-clock
    step-back — NTP / VM snapshot — must not reorder the record)."""
    jsonl, _ = _build_fixture(tmp_path, lines=[
        ("a__b-1", "quota_limited", {}),          # ts 1000
        ("a__b-1", "resolved", {}),              # ts 1001
    ])
    # now append a *third* line with ts older than the second's
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps(
            {"instance_id": "a__b-1", "status": "no_patch",
             "meta": {}, "ts": 999.0}
        ) + "\n")
    records, _dropped = report.load_jsonl(str(jsonl))
    finals, _ = report.dedupe(records)
    assert finals["a__b-1"]["status"] == "no_patch"


def _records(tmp_path: Path) -> "list[dict]":
    jsonl, _ = _build_fixture(tmp_path)
    records, _dropped = report.load_jsonl(str(jsonl))
    assert _dropped is False
    return records


# ---------------------------------------------------------------------------
# F1 — pass@1 overall + per-repo (AC1) — the exact numbers
# ---------------------------------------------------------------------------


def test_pass1_overall_and_per_repo_exactly(tmp_path):
    records = _records(tmp_path)
    finals, markers = report.dedupe(records)
    p1 = report.pass1(finals)
    ov = p1["overall"]
    assert len(finals) == EXPECTED_DISTINCT
    assert len(markers) == 1
    assert ov["resolved"] == 2
    assert ov["terminal"] == EXPECTED_TERMINAL
    assert ov["pct"] == EXPECTED_PASS1_PCT
    assert ov["rate"] == pytest.approx(2 / 6)
    assert p1["per_repo"] == EXPECTED_PER_REPO


def test_repo_of_mapping():
    # the 12 Verified repos (WP0 notes §5) — incl. the dash-bearing
    # scikit-learn (the prefix before the *last* dash)
    assert report.repo_of("astropy__astropy-12907") == "astropy__astropy"
    assert report.repo_of("django__django-11001") == "django__django"
    assert report.repo_of("matplotlib__matplotlib-2870") == "matplotlib__matplotlib"
    assert report.repo_of("mwaskom__seaborn-2650") == "mwaskom__seaborn"
    assert report.repo_of("pallets__flask-5014") == "pallets__flask"
    assert report.repo_of("psf__requests-1234") == "psf__requests"
    assert report.repo_of("scikit__scikit-learn-99") == "scikit__scikit-learn"
    assert report.repo_of("sphinx-doc__sphinx-11510") == "sphinx-doc__sphinx"
    assert report.repo_of("sympy__sympy-123") == "sympy__sympy"
    assert report.repo_of("pytest-dev__pytest-11148") == "pytest-dev__pytest"
    assert report.repo_of("pydata__xarray-5845") == "pydata__xarray"
    assert report.repo_of("pvlib__pvlib-python-1394") == "pvlib__pvlib-python"
    # synthetic ids without a dash are their own repo key
    assert report.repo_of("i0") == "i0"
    # a dash at position 0 is not a split point
    assert report.repo_of("-weird-1") == "-weird"
    with pytest.raises(ReportProtocolError):
        report.repo_of("")


def test_pass1_empty_run_is_n_a(tmp_path):
    jsonl, out = _build_fixture(tmp_path, lines=[])
    res = report.generate(str(jsonl), str(out))
    assert res["pass1"]["terminal"] == 0
    assert res["pass1"]["pct"] == "n/a"
    assert res["pass1"]["rate"] is None


# ---------------------------------------------------------------------------
# F3 — terminal-status exclusion (the retry class is out of the
# denominator, in the taxonomy, and in all_preds under the harness's
# empty-patch semantics)
# ---------------------------------------------------------------------------


def test_terminal_status_exclusion_from_pass1(tmp_path):
    records = _records(tmp_path)
    finals, _ = report.dedupe(records)
    # the four retry-class finals
    assert finals["astropy__astropy-12907"]["status"] == "eval_error"
    assert finals["sympy__sympy-123"]["status"] == "preflight_failed"
    assert finals["scikit__scikit-learn-99"]["status"] == "eval_error"
    assert finals["matplotlib__matplotlib-2870"]["status"] == "quota_limited"
    p1 = report.pass1(finals)
    # they are *absent* from the denominator: 6, not 10
    assert p1["overall"]["terminal"] == EXPECTED_TERMINAL
    # and in the taxonomy (test_taxonomy_exact) + in all_preds with ""
    # (test_all_preds_jsonl_exactly)
    for repo in ("astropy__astropy", "sympy__sympy",
                 "scikit__scikit-learn", "matplotlib__matplotlib"):
        assert p1["per_repo"][repo]["terminal"] == 0
        assert p1["per_repo"][repo]["pct"] == "n/a"


def test_taxonomy_exact(tmp_path):
    records = _records(tmp_path)
    finals, markers = report.dedupe(records)
    tax = report.taxonomy(finals, markers)
    assert tax["by_status"] == EXPECTED_TAXONOMY
    assert tax["markers"] == 1
    # zeros included, in the issue's listed order
    assert list(tax["by_status"]) == list(report.TAXONOMY_ORDER)


# ---------------------------------------------------------------------------
# F6 — sessions used + date range (the computed methodology fields)
# ---------------------------------------------------------------------------


def test_sessions_used_and_date_range_exact(tmp_path):
    records = _records(tmp_path)
    # 11 per-instance lines − the sympy preflight line (a pre-flight
    # failure stops *before* a session starts) = 10 session attempts;
    # the batch marker never counts.
    assert report.sessions_used(records) == EXPECTED_SESSIONS_USED
    assert report.date_range(records) == EXPECTED_DATE_RANGE


def test_sessions_used_empty_run(tmp_path):
    jsonl, _ = _build_fixture(tmp_path, lines=[])
    records, _ = report.load_jsonl(str(jsonl))
    assert report.sessions_used(records) == 0
    assert report.date_range(records) is None


# ---------------------------------------------------------------------------
# F5/F4 — all_preds.jsonl (AC4) + the cross-check (AC5)
# ---------------------------------------------------------------------------


def test_all_preds_jsonl_exactly(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    res = report.generate(str(jsonl), str(out))
    assert res["artifacts"]["preds_lines"] == EXPECTED_DISTINCT
    lines = _read_jsonl(Path(out) / "all_preds.jsonl")
    assert len(lines) == EXPECTED_DISTINCT
    assert [l["instance_id"] for l in lines] == EXPECTED_PRED_IDS
    # exactly the harness's standard predictions field set
    for l in lines:
        assert set(l) == {"instance_id", "model_name_or_path",
                         "model_patch"}
        assert l["model_name_or_path"] == "devin"  # the WP4 default
    by_id = {l["instance_id"]: l for l in lines}
    # the full model_patch strings (the exact artifact contents)
    assert by_id["pallets__flask-5014"]["model_patch"] == P1
    assert by_id["pallets__flask-5015"]["model_patch"] == P1B  # the dedup: the last line's patch
    assert by_id["mwaskom__seaborn-2650"]["model_patch"] == P2
    # every no-patch instance (incl. the four pending retry-class
    # finals) is "" — the harness's own empty-patch semantics keeps it
    # out of resolved_ids
    for iid in ("django__django-11001", "django__django-11002",
                "astropy__astropy-12907", "sympy__sympy-123",
                "scikit__scikit-learn-99", "psf__requests-1234",
                "matplotlib__matplotlib-2870"):
        assert by_id[iid]["model_patch"] == ""


def test_model_name_precedence(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    meta = tmp_path / "meta.json"
    # model_name_or_path beats model
    meta.write_text(json.dumps(
        {"model": "swe-2-high", "model_name_or_path": "devin_swe2high"}
    ), encoding="utf-8")
    report.generate(str(jsonl), str(out),
                   _meta(meta))
    lines = _read_jsonl(Path(out) / "all_preds.jsonl")
    assert all(l["model_name_or_path"] == "devin_swe2high" for l in lines)
    # model beats the default
    meta.write_text(json.dumps({"model": "swe-2-high"}), encoding="utf-8")
    out2 = tmp_path / "out2"
    report.generate(str(jsonl), str(out2), _meta(meta))
    lines = _read_jsonl(Path(out2) / "all_preds.jsonl")
    assert all(l["model_name_or_path"] == "swe-2-high" for l in lines)


def _meta(path: Path) -> "dict":
    return json.loads(path.read_text(encoding="utf-8"))


def test_crosscheck_positive_passes(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    res = report.generate(str(jsonl), str(out))
    assert res["artifacts"]["crosscheck"] == "PASS"
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    assert "cross-check: PASS" in md


def test_crosscheck_missing_referenced_file_fails(tmp_path):
    # t2.jsonl is *not written* → the seaborn line's reference is missing
    jsonl, out = _build_fixture(tmp_path, artifacts={
        "p1.diff": P1,
        "p1b.diff": P1B,
        "p2.diff": P2,
        "t1.jsonl": T1,
        "e1.log": E1,
    })
    with pytest.raises(ArtifactCheckError) as e:
        report.generate(str(jsonl), str(out))
    assert "t2.jsonl" in str(e.value)


def test_crosscheck_superseded_line_reference_is_checked(tmp_path):
    """R1 is over *every* line: the *superseded* flask-5015 line (the
    quota_limited one) references e1.log; a missing e1.log fails the
    check even though the surviving line does not reference it."""
    jsonl, out = _build_fixture(tmp_path, artifacts={
        "p1.diff": P1,
        "p1b.diff": P1B,
        "p2.diff": P2,
        "t1.jsonl": T1,
        "t2.jsonl": T2,
        # e1.log is *not written*
    })
    with pytest.raises(ArtifactCheckError) as e:
        report.generate(str(jsonl), str(out))
    assert "e1.log" in str(e.value)


def test_crosscheck_resolved_without_patch_ref_fails(tmp_path):
    """A graded verdict (``resolved``) with no ``patch_diff`` reference
    is a pipeline bug (the harness graded a patch that is not there) —
    fail-loud."""
    lines = [
        ("a__b-1", "resolved", {}),  # no patch_diff reference
    ]
    jsonl, out = _build_fixture(tmp_path, lines=lines)
    with pytest.raises(ArtifactCheckError) as e:
        report.generate(str(jsonl), str(out))
    assert "patch_diff" in str(e.value)


def test_crosscheck_failed_test_without_existing_patch_fails(tmp_path):
    """A ``failed_test`` final whose referenced ``patch.diff`` is missing
    (the reference is present but the file is not) — fail-loud."""
    lines = [
        ("a__b-1", "failed_test", {"patch_diff": "missing.diff"}),
    ]
    jsonl, out = _build_fixture(tmp_path, lines=lines)
    with pytest.raises(ArtifactCheckError) as e:
        report.generate(str(jsonl), str(out))
    assert "missing.diff" in str(e.value)


def test_check_mode_detects_tampered_patch(tmp_path):
    """R2 in ``--check``: after a clean generation, tamper
    ``patch.diff``; the standalone check flags the content mismatch
    (no writes)."""
    jsonl, out = _build_fixture(tmp_path)
    report.generate(str(jsonl), str(out))
    assert report.check(str(jsonl), str(out)) == []
    (tmp_path / "p1.diff").write_text(
        P1 + "TAMPERED\n", encoding="utf-8"
    )
    violations = report.check(str(jsonl), str(out))
    assert any("pallets__flask-5014" in v and "model_patch does not match" in v
               for v in violations)


def test_check_mode_detects_tampered_preds_file(tmp_path):
    """R2: a tampered ``all_preds.jsonl`` (a missing line / an unsorted
    / a bad field set) is flagged by the standalone check."""
    jsonl, out = _build_fixture(tmp_path)
    report.generate(str(jsonl), str(out))
    preds = Path(out) / "all_preds.jsonl"
    lines = preds.read_text(encoding="utf-8").splitlines()
    # remove the first line (the set/count check fires)
    preds.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    violations = report.check(str(jsonl), str(out))
    assert any("instance lines" in v for v in violations)
    # restore; unsort (the order check fires)
    preds.write_text("\n".join([lines[1], lines[0]] + lines[2:]) + "\n",
                    encoding="utf-8")
    violations = report.check(str(jsonl), str(out))
    assert any("not sorted" in v for v in violations)


# ---------------------------------------------------------------------------
# F5 — trajs/ (AC4)
# ---------------------------------------------------------------------------


def test_trajs_copies_exact_bytes(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    report.generate(str(jsonl), str(out))
    trajs = Path(out) / "trajs"
    # exactly the two instances with trajectory references
    names = sorted(p.name for p in trajs.iterdir())
    assert names == ["mwaskom__seaborn-2650.jsonl", "pallets__flask-5014.jsonl"]
    # byte-for-byte (the embedded-newline trajectory must survive exactly)
    assert (trajs / "pallets__flask-5014.jsonl").read_bytes() == T1.encode()
    assert (trajs / "mwaskom__seaborn-2650.jsonl").read_bytes() == T2.encode()


def test_trajs_absent_when_no_trajectory_reference(tmp_path):
    # a fixture variant whose lines reference no trajectories at all
    lines = [
        ("pallets__flask-5014", "resolved", {"patch_diff": "p1.diff"}),
        ("mwaskom__seaborn-2650", "resolved", {"patch_diff": "p2.diff"}),
    ]
    jsonl, out = _build_fixture(tmp_path, lines=lines, artifacts={
        "p1.diff": P1,
        "p2.diff": P2,
    })
    report.generate(str(jsonl), str(out))
    assert not (Path(out) / "trajs").exists()
    assert (Path(out) / "report.md").read_text(
        encoding="utf-8").count("trajs/: 0") == 1


# ---------------------------------------------------------------------------
# F6 — the methodology block (AC3)
# ---------------------------------------------------------------------------

META = {
    "run_id": "run-2026-10-28-001",
    "devin_cli_version": "26.1028.0",
    "model": "swe-2-high",
    "cost_tier": "Free",
    "prompt_template_hash": "sha256:abc123",
    "permission_policy": "allow read / edit / exec in /testbed + /tmp; "
                        "deny web tools; default deny",
    "caps": {"max_sessions_per_day": 10,
             "max_wall_per_session_s": 2700,
             "daily_pause_h": 12},
    "harness_version": "swebench==5.0.2",
    "images": "swebench/sweb.eval.x86_64.*:latest (12 Verified bases)",
}


def test_methodology_from_meta_file_exactly(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(META), encoding="utf-8")
    report.generate(str(jsonl), str(out), _meta(meta))
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    for value in META.values():
        if isinstance(value, str) and value:
            assert value in md
    assert "caps: " not in md  # rendered as the key=value map
    assert "max_sessions_per_day=10" in md
    assert "max_wall_per_session_s=2700" in md
    assert "daily_pause_h=12" in md
    # the computed fields — exact
    assert f"Date range (UTC): {EXPECTED_DATE_RANGE[0]} – " \
        f"{EXPECTED_DATE_RANGE[1]}" in md
    assert f"Sessions used (attempts): {EXPECTED_SESSIONS_USED}" in md
    # the pass@1 + the taxonomy, exactly
    assert "**pass@1 (overall): 2/6 (33.3%)**" in md
    for status, n in EXPECTED_TAXONOMY.items():
        assert f"| {status} | {n} |" in md


def test_methodology_unknown_without_meta_file(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    report.generate(str(jsonl), str(out))
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    # every AC3 field present, value unknown (the block never silently
    # omits a field) — but the computed fields are still exact
    for label in ("Devin CLI version:", "Model:", "Cost tier:",
                  "Prompt template hash:", "Permission policy:",
                  "Caps:", "Harness version:", "Eval images:"):
        assert f"**{label}** unknown" in md
    assert f"Date range (UTC): {EXPECTED_DATE_RANGE[0]} – " \
        f"{EXPECTED_DATE_RANGE[1]}" in md
    assert f"Sessions used (attempts): {EXPECTED_SESSIONS_USED}" in md


def test_methodology_partial_meta_file(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"model": "swe-2-high"}), encoding="utf-8")
    report.generate(str(jsonl), str(out), _meta(meta))
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    assert "**Model:** swe-2-high" in md
    assert "**Cost tier:** unknown" in md


# ---------------------------------------------------------------------------
# F7 — determinism + torn tail + mid-file corruption
# ---------------------------------------------------------------------------


def test_generate_is_byte_stable(tmp_path):
    jsonl, out1 = _build_fixture(tmp_path)
    report.generate(str(jsonl), str(out1))
    out2 = tmp_path / "out2"
    # a fresh fixture with identical contents (same ts values)
    jsonl2, _ = _build_fixture(tmp_path / "second", lines=FIXTURE_LINES,
                              artifacts={
                                  "p1.diff": P1,
                                  "p1b.diff": P1B,
                                  "p2.diff": P2,
                                  "t1.jsonl": T1,
                                  "t2.jsonl": T2,
                                  "e1.log": E1,
                              })
    report.generate(str(jsonl2), str(out2))
    a = Path(out1) / "report.md"
    b = Path(out2) / "report.md"
    assert a.read_bytes() == b.read_bytes()
    assert (Path(out1) / "all_preds.jsonl").read_bytes() == \
        (Path(out2) / "all_preds.jsonl").read_bytes()
    t1f = Path(out1) / "trajs" / "pallets__flask-5014.jsonl"
    t2f = Path(out2) / "trajs" / "pallets__flask-5014.jsonl"
    assert t1f.read_bytes() == t2f.read_bytes()


def test_torn_tail_is_dropped_and_noted(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    # kill mid-write: a torn fragment at EOF (truncated JSON)
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write('{"instance_id": "psf__requests-9999", "stat')
    records, dropped = report.load_jsonl(str(jsonl))
    assert dropped is True
    # the torn line is not a record
    assert all(r["instance_id"] != "psf__requests-9999" for r in records)
    res = report.generate(str(jsonl), str(out))
    assert res["artifacts"]["preds_lines"] == EXPECTED_DISTINCT
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    assert "a torn final line at EOF was dropped" in md


def test_torn_tail_with_valid_json_but_bad_shape_is_dropped(tmp_path):
    """A complete JSON with the wrong shape (a kill can leave a
    non-record JSON at EOF) is a droppable tail too (the WP3b reader
    contract — the report inherits it by import)."""
    jsonl, out = _build_fixture(tmp_path)
    with open(jsonl, "a", encoding="utf-8") as f:
        f.write('{"instance_id": "a__b-1"}\n')  # no "status"
    records, dropped = report.load_jsonl(str(jsonl))
    assert dropped is True
    report.generate(str(jsonl), str(out))


def test_midfile_corruption_fails_loud(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    lines = (jsonl).read_text(encoding="utf-8").splitlines()
    # corrupt a *mid-file* line (not the last)
    lines[2] = '{"broken": true'
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ReportProtocolError):
        report.generate(str(jsonl), str(out))


def test_unknown_status_fails_loud(tmp_path):
    # the unknown-status line must be *mid-file* — a single (last)
    # line with a bad shape is the droppable torn-EOF zone (the WP3b
    # reader contract, inherited by import)
    jsonl, out = _build_fixture(tmp_path, lines=[
        ("a__b-1", "mystery", {}),
        ("a__b-2", "resolved", {}),
    ])
    with pytest.raises(ReportProtocolError):
        report.generate(str(jsonl), str(out))


def test_missing_jsonl_is_invalid_input(tmp_path):
    with pytest.raises(InvalidInputError):
        report.generate(str(tmp_path / "nope.jsonl"), str(tmp_path / "out"))


# ---------------------------------------------------------------------------
# F8 — the CLI exit-code contract
# ---------------------------------------------------------------------------


def test_cli_generate_ok(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out))
    assert proc.returncode == 0, proc.stderr


def test_cli_check_ok(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    assert _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out)
                ).returncode == 0
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out),
                "--check")
    assert proc.returncode == 0, proc.stderr
    assert "PASS" in proc.stdout


def test_cli_check_tampered_exits_3(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out))
    (tmp_path / "p1.diff").write_text(P1 + "TAMPERED\n", encoding="utf-8")
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out),
                "--check")
    assert proc.returncode == 3
    assert "does not match" in proc.stderr


def test_cli_missing_referenced_file_exits_3(tmp_path):
    jsonl, out = _build_fixture(tmp_path, artifacts={
        "p1.diff": P1,
        "p1b.diff": P1B,
        "p2.diff": P2,
        "t1.jsonl": T1,
        "e1.log": E1,
        # t2.jsonl missing
    })
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out))
    assert proc.returncode == 3
    assert "t2.jsonl" in proc.stderr
    # the report still renders (with the violations) so the operator
    # sees *why*
    md = (Path(out) / "report.md").read_text(encoding="utf-8")
    assert "FAIL" in md
    assert "t2.jsonl" in md


def test_cli_missing_jsonl_exits_2(tmp_path):
    proc = _cli(tmp_path, "--jsonl", str(tmp_path / "nope.jsonl"),
                "--out", str(tmp_path / "out"))
    assert proc.returncode == 2


def test_cli_bad_meta_file_exits_2(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    meta = tmp_path / "meta.json"
    meta.write_text("{not json", encoding="utf-8")
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out),
                "--meta-file", str(meta))
    assert proc.returncode == 2


def test_cli_meta_file_not_an_object_exits_2(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    meta = tmp_path / "meta.json"
    meta.write_text("[1, 2, 3]", encoding="utf-8")
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out),
                "--meta-file", str(meta))
    assert proc.returncode == 2


def test_cli_midfile_corruption_exits_4(tmp_path):
    jsonl, out = _build_fixture(tmp_path)
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"broken": true'
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = _cli(tmp_path, "--jsonl", str(jsonl), "--out", str(out))
    assert proc.returncode == 4
