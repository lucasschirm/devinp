"""WP6c pre-flight E2 + E3 (issue #44) — the ``--pilot c`` full-run mode.

Zero quota: the checklist evidence is driven from synthetic checkpoints
and the injected WP2 ``mock_devin`` seam. Covers

* the config: ``pilot="c"`` accepted, the record/workdir naming, the
  gating set (1–12 + 14 + 18–23; 13 non-gating; 15–17 render ``n/a``);
* the **B2 authorization gate**: ``--pilot c`` refuses an unauthorised
  budget (exit 2, before any session) — a ``PENDING`` placeholder is not
  an authorization — while ``--pilot a``/``--pilot b`` are untouched and
  a ``--dry-run`` only prints the refusal it would have made;
* the **mode-gated import**: a source whose recorded grading mode differs
  is refused for every pilot, and ``--pilot c`` also refuses a source
  whose mode is unknown (the two grading legs must never share a
  pass@1);
* items **18–23**: absent evidence is never a pass; each item's own
  break fails exactly that item; the render-drift and label cases;
* the persisted ``run_meta.json`` + the submission label reaching
  ``all_preds.jsonl`` and the report header (E3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import live_pilot as lp
import report as rep

MOCK_PATH = str(Path(__file__).resolve().parent.parent / "mock_devin.py")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    """The BENCH_LIVE flag (the harness refuses a non-dry run without it
    — CI never consumes quota; PLAN §6)."""
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    return monkeypatch


def _cfg(pilot="c", **kw) -> lp.PilotConfig:
    base = dict(mode="sandbox", devin_bin=MOCK_PATH, pilot=pilot,
                instances_file=str(Path(__file__)), workdir="/tmp/x",
                largest_bytes=1048576)
    base.update(kw)
    return lp.PilotConfig(**base)


def _good_meta(**over) -> dict:
    m = {
        "run_id": "full500",
        "devin_cli_version": "devin 3000.12.43 (517d03d73926)",
        "model": "swe-2-high",
        "cost_tier": "Free",
        "prompt_template_hash": "b" * 64,
        "permission_policy": "SwebenchPolicy",
        "caps": {"max_sessions_per_day": 10,
                 "max_wall_per_session": 2700},
        "harness_version": "swebench==5.0.2",
        "images": "swebench/sweb.eval.x86_64.*",
        "label": lp.SUBMISSION_LABEL,
        "model_name_or_path": lp.SUBMISSION_LABEL,
        "mode": "sandbox",
        "grading_leg": "host check",
    }
    m.update(over)
    return m


def _ev(**over) -> dict:
    """A complete, passing evidence dict (see
    :func:`live_pilot.gather_full_run_evidence`) — one key break per
    test flips exactly one item."""
    per = {"a/repo": {"resolved": 1, "terminal": 2, "rate": 0.5,
                     "pct": "50.0%"}}
    ev = {
        "manifest_total": 3, "covered": 3, "missing_ids": [],
        "unknown_ids": [],
        "retry_pending": [],
        "report_violations": [], "trajs_expected": 3, "trajs_missing": [],
        "meth_unknown": [], "meth_sessions": 3, "attempt_sessions": 3,
        "pass1": {"overall": {"resolved": 1, "terminal": 2,
                             "rate": 0.5, "pct": "50.0%"},
                 "per_repo": per},
        "outcome_finals": 2,
        "md_parsed": ({"resolved": 1, "terminal": 2, "pct": "50.0%"},
                     {"a/repo": (1, 2, "50.0%")}),
        "preds_lines": 3, "preds_bad_label": [], "header_has_label": True,
        "windows": {0: {"sessions": 3, "wall_s": 600.0}},
        "max_window_sessions": 3, "max_window_wall_s": 600.0,
        "single_session_ok": True,
    }
    ev.update(over)
    return ev


def _items(ev=None, pilot="c") -> "list[dict]":
    return lp.build_full_run_items(ev if ev is not None else _ev(),
                                  _cfg(pilot))


def _by_num(items, num) -> dict:
    return next(i for i in items if i["item"].startswith(f"{num} "))


def _authorized_budget(**over) -> dict:
    b = {"max_sessions_per_day": 10, "max_wall_per_session": 2700,
        "calibrated_at": "2026-11-13",
        "basis": "WP6c §9: the Pro daily allowance read in app.devin.ai "
                "on 2026-11-13 + the WP6a/WP6b measured session walls",
        "authorized_by": "Operator Name <op@example.com>",
        "completion_deadline": "2027-06-30",
        "abort_rule": "two consecutive RUN FAILED windows, or the "
                      "deadline at 60 % queue remaining, stop the run"}
    b.update(over)
    return b


# ---------------------------------------------------------------------------
# the config + the naming + the gating set
# ---------------------------------------------------------------------------

def test_pilot_c_is_a_valid_kind():
    cfg = _cfg("c")
    assert cfg.pilot == "c"
    assert lp.RECORD_NAMES["c"] == "wp6c-full-run.md"
    assert lp.WORKDIR_NAMES["c"] == "full-run"
    assert "500" in lp._pilot_title(cfg)
    assert Path(lp.DEFAULT_C_INSTANCES_FILE).name == \
        "verified_instances.json"


def test_the_wp6a_and_wp6b_contracts_are_unchanged():
    assert lp.RECORD_NAMES["a"] == "wp6a-go-no-go.md"
    assert lp.RECORD_NAMES["b"] == "wp6b-pilot-b.md"
    assert lp.WORKDIR_NAMES["a"] == "pilot-a"
    assert lp.WORKDIR_NAMES["b"] == "pilot-b"
    assert lp._pilot_title(_cfg("a")) == "WP6a Pilot A (issue #21)"
    assert lp._pilot_title(_cfg("b")) == "WP6b Pilot B (issue #22)"


def test_gating_set_for_c():
    g = lp._pilot_gating(_cfg("c"))
    assert {"18", "19", "20", "21", "22", "23"} <= g
    # item 13 stays deviation-tolerant / non-gating; the pilot-B AC
    # items are not part of the full-run verdict
    assert "13" not in g
    assert not ({"15", "16", "17"} & g)
    assert lp._pilot_gating(_cfg("b")) == frozenset(
        {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
         "14", "15", "16", "17"})
    assert "18" not in lp._pilot_gating(_cfg("b"))
    assert "18" not in lp._pilot_gating(_cfg("a"))


def test_import_run_is_allowed_for_c_and_still_refused_for_a():
    # the 500-run reuses the already-spent pilot sessions (item 18
    # verifies the completeness, so the import is not unverified)
    assert _cfg("c", import_run="pilotB").import_run == "pilotB"
    assert _cfg("b", import_run="pilotA").import_run == "pilotA"
    with pytest.raises(lp.PilotConfigError, match="pilot-B/pilot-C"):
        _cfg("a", import_run="pilotA")


# ---------------------------------------------------------------------------
# the B2 authorization gate
# ---------------------------------------------------------------------------

def test_full_run_requires_an_authorized_budget():
    with pytest.raises(lp.PilotConfigError, match="missing"):
        lp.require_full_run_authorization({}, "/x/live_pilot_budget.json")


def test_a_pending_placeholder_is_not_an_authorization():
    """The committed template carries PENDING values — the 500-session
    invocation must refuse it, not read the numbers around them."""
    b = _authorized_budget(authorized_by="PENDING (issue #44 B2)")
    with pytest.raises(lp.PilotConfigError, match="authorized_by"):
        lp.require_full_run_authorization(b)
    b2 = _authorized_budget(completion_deadline="")
    with pytest.raises(lp.PilotConfigError, match="completion_deadline"):
        lp.require_full_run_authorization(b2)
    b3 = _authorized_budget(basis="   ")
    with pytest.raises(lp.PilotConfigError, match="basis"):
        lp.require_full_run_authorization(b3)


def test_full_run_rejects_a_non_positive_or_bool_cap():
    for bad in (0, -5, True):
        with pytest.raises(lp.PilotConfigError):
            lp.require_full_run_authorization(
                _authorized_budget(max_sessions_per_day=bad))


def test_full_run_rejects_a_paid_cost_tier():
    with pytest.raises(lp.PilotConfigError, match="free-tier"):
        lp.require_full_run_authorization(_authorized_budget(
            cost_tier="Standard"))


def test_authorized_budget_passes():
    lp.require_full_run_authorization(_authorized_budget())


def test_pilot_c_live_refuses_an_unauthorized_budget(tmp_path, monkeypatch,
                                                    capsys):
    monkeypatch.setenv(lp.LIVE_ENV, "1")
    monkeypatch.setattr(lp, "BUDGET_FILE",
                       str(tmp_path / "absent-budget.json"))
    rc = lp.main(["--pilot", "c", "--instances",
                 str(tmp_path / "no-such-manifest.json"),
                 "--workdir", str(tmp_path / "wd"), "--devin-bin",
                 MOCK_PATH])
    assert rc == lp.EXIT_CONFIG
    err = capsys.readouterr().out
    assert "unauthorised budget" in err or "authorized budget" in err
    # the record invariant: no verdict left behind
    assert not list((tmp_path / "wd").glob("wp6c-full-run.md"))


def test_pilot_dry_run_is_not_gated_but_says_so(tmp_path, monkeypatch,
                                                capsys):
    """A --dry-run spends nothing, so it is not gated — but it prints the
    exact refusal a live invocation would make (no surprise on the live
    window)."""
    monkeypatch.setattr(lp, "BUDGET_FILE",
                       str(tmp_path / "absent-budget.json"))
    inst = tmp_path / "m.json"
    inst.write_text(json.dumps({"instances": [{
        "instance_id": "a__a-1", "repo": "a/a", "base_commit": "x",
        "problem_statement": "p", "FAIL_TO_PASS": ["t.py::test_a"],
        "PASS_TO_PASS": []}]}))
    wd = tmp_path / "wd"
    rc = lp.main(["--pilot", "c", "--instances", str(inst), "--workdir",
                 str(wd), "--dry-run", "--devin-bin", MOCK_PATH])
    assert rc == lp.EXIT_OK
    out = capsys.readouterr().out
    assert "NOTE (dry run only)" in out
    assert "500-session" in out


def test_pilot_a_is_not_b2_gated(tmp_path, monkeypatch):
    """The gate is on the 500-session invocation, not on a 3-instance
    smoke: a pilot A with no budget file at all behaves exactly as
    before."""
    monkeypatch.setattr(lp, "BUDGET_FILE",
                       str(tmp_path / "absent-budget.json"))
    inst = tmp_path / "m.json"
    inst.write_text(json.dumps({"instances": [{
        "instance_id": "a__a-1", "repo": "a/a", "base_commit": "x",
        "problem_statement": "p", "FAIL_TO_PASS": ["t.py::test_a"],
        "PASS_TO_PASS": []}]}))
    rc = lp.main(["--pilot", "a", "--instances", str(inst), "--workdir",
                 str(tmp_path / "wd"), "--dry-run", "--devin-bin",
                 MOCK_PATH])
    assert rc == lp.EXIT_OK


def test_pilot_c_dry_run_renders_the_full_run_skeleton(tmp_path,
                                                      monkeypatch):
    monkeypatch.setattr(lp, "BUDGET_FILE", str(tmp_path / "b.json"))
    Path(tmp_path / "b.json").write_text(json.dumps(
        _authorized_budget()))
    inst = tmp_path / "m.json"
    inst.write_text(json.dumps({"instances": [{
        "instance_id": "a__a-1", "repo": "a/a", "base_commit": "x",
        "problem_statement": "p", "FAIL_TO_PASS": ["t.py::test_a"],
        "PASS_TO_PASS": []}]}))
    wd = tmp_path / "wd"
    rc = lp.main(["--pilot", "c", "--instances", str(inst), "--workdir",
                 str(wd), "--dry-run", "--devin-bin", MOCK_PATH])
    assert rc == lp.EXIT_OK
    md = (wd / "wp6c-full-run.md").read_text()
    for n in list(range(1, 15)) + [18, 19, 20, 21, 22, 23]:
        assert f"| {n} |" in md, n
    # the pilot-B items are not part of the full-run skeleton
    assert "| 15 |" not in md and "| 17 |" not in md
    # the other kinds' records are removed from the same workdir
    assert not (wd / "wp6a-go-no-go.md").exists()
    assert not (wd / "wp6b-pilot-b.md").exists()


# ---------------------------------------------------------------------------
# the mode-gated import
# ---------------------------------------------------------------------------

_src_n = [0]


def _import_fixture(tmp_path, *, mode=None, state_mode=None,
                    model=None, state_model=None):
    """A source workdir with a checkpoint, and the methodology evidence
    asked for (meta / state / neither). The state file carries the same
    ``mode`` + ``model`` keys the real ``_write_state`` writes."""
    _src_n[0] += 1
    src = tmp_path / f"srcwd{_src_n[0]}"
    src.mkdir()
    (src / "run-srcA.jsonl").write_text(json.dumps(
        {"instance_id": "a__a-1", "status": "resolved",
         "ts": 1_700_000_000.0, "meta": {}}) + "\n")
    if mode is not None or model is not None:
        (src / "report").mkdir()
        doc = {}
        if mode is not None:
            doc["mode"] = mode
        if model is not None:
            doc["model"] = model
        (src / "report" / "run_meta.json").write_text(json.dumps(doc))
    if state_mode is not None or state_model is not None:
        doc = {}
        if state_mode is not None:
            doc["mode"] = state_mode
        if state_model is not None:
            doc["model"] = state_model
        (src / "pilot-srcA.state.json").write_text(json.dumps(doc))
    return src


def _import_cfg(tmp_path, src, mode):
    return lp.PilotConfig(mode=mode, pilot="c", devin_bin=MOCK_PATH,
                         instances_file=str(Path(__file__)),
                         workdir=str(tmp_path / "wd"),
                         import_run="srcA", import_workdir=str(src),
                         largest_bytes=1048576)


def _insts():
    return [{"instance_id": "a__a-1"}]


def test_provenance_prefers_the_meta_then_the_state(tmp_path):
    src = _import_fixture(tmp_path, mode="container", model="swe-2-max",
                         state_mode="sandbox", state_model="other")
    assert lp.source_provenance(str(src), "srcA") == {
        "mode": "container", "model": "swe-2-max"}
    # a field the meta does not carry is still read from the state file
    src2 = _import_fixture(tmp_path, mode="container",
                          state_mode="sandbox", state_model="swe-2-high")
    assert lp.source_provenance(str(src2), "srcA") == {
        "mode": "container", "model": "swe-2-high"}
    src3 = _import_fixture(tmp_path, state_mode="sandbox")
    assert lp.source_provenance(str(src3), "srcA") == {"mode": "sandbox"}
    assert lp.source_provenance(str(_import_fixture(tmp_path)),
                               "srcA") == {}


def test_provenance_ignores_garbage(tmp_path):
    """Unreadable / unparseable evidence is the ABSENCE of evidence —
    never a guess (the refusal that follows from an absence is the
    caller's decision, and --pilot b tolerates it)."""
    src = _import_fixture(tmp_path)
    (src / "report").mkdir()
    (src / "report" / "run_meta.json").write_text("{not json")
    assert lp.source_provenance(str(src), "srcA") == {}
    (src / "report" / "run_meta.json").write_text("[1, 2]")
    assert lp.source_provenance(str(src), "srcA") == {}
    (src / "report" / "run_meta.json").write_text(json.dumps(
        {"mode": 42, "model": "   "}))
    assert lp.source_provenance(str(src), "srcA") == {}


def test_a_source_claiming_an_unknown_mode_is_refused_not_ignored(
        tmp_path):
    """A recorded-but-impossible mode is evidence of a methodology this
    run cannot mix with, so it is a mismatch — the strict reading is the
    safe one for a submission."""
    src = _import_fixture(tmp_path, mode="kubernetes",
                         model="swe-2-high")
    cfg = _import_cfg(tmp_path, src, "sandbox")
    with pytest.raises(lp.PilotConfigError, match="kubernetes"):
        lp.load_imported(cfg, str(tmp_path / "wd"), _insts())


def test_import_of_a_different_model_is_refused(tmp_path):
    """One pass@1 cannot be two models: the imported lines were produced
    by the source's model and are never re-run (issue #44 E2)."""
    src = _import_fixture(tmp_path, mode="sandbox",
                         model="some-paid-model")
    cfg = _import_cfg(tmp_path, src, "sandbox")
    with pytest.raises(lp.PilotConfigError, match="model"):
        lp.load_imported(cfg, str(tmp_path / "wd"), _insts())


def test_import_of_a_different_grading_leg_is_refused(tmp_path):
    src = _import_fixture(tmp_path, mode="sandbox")
    cfg = _import_cfg(tmp_path, src, "container")
    with pytest.raises(lp.PilotConfigError, match="grading legs"):
        lp.load_imported(cfg, str(tmp_path / "wd"), _insts())


def test_pilot_c_refuses_a_source_with_an_unrecorded_methodology(
        tmp_path):
    src = _import_fixture(tmp_path)  # nothing recorded anywhere
    cfg = _import_cfg(tmp_path, src, "sandbox")
    with pytest.raises(lp.PilotConfigError,
                       match="recorded no mode, model"):
        lp.load_imported(cfg, str(tmp_path / "wd"), _insts())


def test_pilot_c_refuses_a_half_recorded_source(tmp_path):
    """Mode without model is still an unattributable methodology for the
    submission-bound run; --pilot b keeps tolerating it (the WP6b pair
    predates the meta)."""
    src = _import_fixture(tmp_path, state_mode="sandbox")
    cfg = _import_cfg(tmp_path, src, "sandbox")
    with pytest.raises(lp.PilotConfigError, match="model"):
        lp.load_imported(cfg, str(tmp_path / "wd"), _insts())


def test_pilot_b_still_tolerates_a_pre_meta_source(tmp_path):
    """The historical pilot-B pair predates the persisted meta — the
    refusal is on a KNOWN mismatch for it, not on the absence of the
    WP6c evidence (the WP6b contract stays)."""
    src = _import_fixture(tmp_path)
    cfg = lp.PilotConfig(mode="sandbox", pilot="b", devin_bin=MOCK_PATH,
                        instances_file=str(Path(__file__)),
                        workdir=str(tmp_path / "wd"), import_run="srcA",
                        import_workdir=str(src), largest_bytes=1048576)
    imported, _retry = lp.load_imported(cfg, str(tmp_path / "wd"),
                                       _insts())
    assert "a__a-1" in imported


def test_import_mode_matches_is_allowed(tmp_path):
    src = _import_fixture(tmp_path, state_mode="container",
                         state_model=lp.DEFAULT_MODEL)
    cfg = _import_cfg(tmp_path, src, "container")
    imported, _ = lp.load_imported(cfg, str(tmp_path / "wd"), _insts())
    assert "a__a-1" in imported


# ---------------------------------------------------------------------------
# items 18–23: the renderer
# ---------------------------------------------------------------------------

def test_absent_evidence_is_never_a_pass():
    """A checklist may only GO on evidence that was gathered: an empty
    evidence dict renders every item n/a (pass=None), never PASS."""
    items = lp.build_full_run_items({}, _cfg("c"))
    assert len(items) == 6
    for i in items:
        assert i["pass"] is None, i
        assert "evidence not gathered" in i["observed"], i


def test_all_items_pass_on_good_evidence():
    items = _items()
    assert [i["pass"] for i in items] == [True] * 6


def test_item18_coverage_break():
    ev = _ev(covered=499)
    assert _by_num(_items(ev), 18)["pass"] is False
    ev = _ev(retry_pending=["a__a-1"])
    assert "pending" in _by_num(_items(ev), 18)["observed"]
    assert _by_num(_items(ev), 18)["pass"] is False
    ev = _ev(unknown_ids=["ghost__ghost-1"])
    assert _by_num(_items(ev), 18)["pass"] is False


def test_item19_integrity_breaks():
    ev = _ev(report_violations=["all_preds.jsonl: x differs"])
    assert _by_num(_items(ev), 19)["pass"] is False
    ev = _ev(trajs_missing=["a__a-1"])
    assert _by_num(_items(ev), 19)["pass"] is False


def test_item20_methodology_breaks():
    ev = _ev(meth_unknown=["cost_tier"])
    assert _by_num(_items(ev), 20)["pass"] is False
    ev = _ev(attempt_sessions=4)   # the report counted a different file
    assert _by_num(_items(ev), 20)["pass"] is False


def test_item21_arithmetic_and_render_breaks():
    broken = _ev(pass1={"overall": {"resolved": 9, "terminal": 9,
                                  "rate": 1.0, "pct": "100.0%"},
                       "per_repo": {"a/repo": {"resolved": 1,
                                              "terminal": 2,
                                              "rate": 0.5,
                                              "pct": "50.0%"}}})
    assert _by_num(_items(broken), 21)["pass"] is False
    # the denominator must be TERMINAL − retry-class
    ev = _ev(outcome_finals=3)
    assert _by_num(_items(ev), 21)["pass"] is False
    # render drift: the markdown says something else than the checkpoint
    ev = _ev(md_parsed=({"resolved": 2, "terminal": 2, "pct": "100.0%"},
                       {"a/repo": (2, 2, "100.0%")}))
    assert "DISAGREES" in _by_num(_items(ev), 21)["observed"]
    assert _by_num(_items(ev), 21)["pass"] is False
    # a repo row that exists only in the markdown
    ov, rows = _ev()["md_parsed"]
    rows = dict(rows)
    rows["ghost/repo"] = (0, 0, "n/a")
    ev = _ev(md_parsed=(ov, rows))
    assert _by_num(_items(ev), 21)["pass"] is False


def test_item22_label_breaks():
    ev = _ev(preds_bad_label=["a__a-1"])
    assert _by_num(_items(ev), 22)["pass"] is False
    ev = _ev(header_has_label=False)
    assert _by_num(_items(ev), 22)["pass"] is False
    ev = _ev(preds_lines=0)
    assert _by_num(_items(ev), 22)["pass"] is False


def test_item23_batch_discipline_breaks():
    ev = _ev(windows={}, max_window_sessions=0, max_window_wall_s=0.0)
    assert _by_num(_items(ev), 23)["pass"] is False
    ev = _ev(max_window_sessions=11)
    assert _by_num(_items(ev), 23)["pass"] is False
    ev = _ev(max_window_wall_s=10 * 2700 + 1)
    assert _by_num(_items(ev), 23)["pass"] is False
    ev = _ev(single_session_ok=False)
    assert _by_num(_items(ev), 23)["pass"] is False


def test_verdict_uses_the_full_run_gating_set():
    cfg = _cfg("c")
    base = [{"item": f"{n} x", "expected": "", "observed": "",
            "pass": True} for n in
           ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
            "12", "14")]
    base.append({"item": "13 eval leg", "expected": "", "observed": "",
                "pass": None})
    base.append({"item": "15 stratification", "expected": "",
                "observed": "n/a", "pass": None})
    items = base + _items()
    verdict, failed = lp._verdict(items, gating=lp._pilot_gating(cfg))
    assert verdict == "GO", failed
    # a failing full-run item flips the verdict
    items = base + _items(_ev(retry_pending=["a__a-1"]))
    verdict, failed = lp._verdict(items, gating=lp._pilot_gating(cfg))
    assert verdict == "NO-GO" and any("18" in f for f in failed)
    # and an interrupted run decides nothing (the WP6a contract holds)
    verdict, _ = lp._verdict(items, interrupted=True,
                           gating=lp._pilot_gating(cfg))
    assert verdict.startswith("n/a (interrupted")


def test_build_checklist_appends_the_items_only_for_c():
    kwargs = dict(config=_cfg("c"), dry_run=False, gate={"ok": True},
                 preflight_ok=True, preflight_failures=[], records=[],
                 skipped_on_resume={}, interrupted_this_run=None,
                 resume_after_interrupt=False, devin_ok=True,
                 sessions_used=0, wall_budget_s=27000.0, wall_used_s=0.0)
    a = lp.build_checklist(**{**kwargs, "config": _cfg("a")})
    assert [i["item"].split(" ")[0] for i in a] == \
        [str(n) for n in range(1, 15)]
    b = lp.build_checklist(**{**kwargs, "config": _cfg("b"),
                            "pilot_b": True})
    assert [i["item"].split(" ")[0] for i in b][-3:] == ["15", "16", "17"]
    assert not [i for i in b if i["item"].startswith("18")]
    c = lp.build_checklist(**kwargs, pilot_c=True, full_run=_ev())
    nums = [i["item"].split(" ")[0] for i in c]
    assert nums == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
                   "11", "12", "13", "14", "15", "16", "17", "18", "19",
                   "20", "21", "22", "23"]
    # 15–17 render an explicit n/a (non-gating), 18–23 gate
    for i in c:
        if i["item"].split(" ")[0] in ("15", "16", "17"):
            assert i["pass"] is None
    assert all(i["pass"] for i in c if i["item"].split(" ")[0]
               in ("18", "19", "20", "21", "22", "23"))


# ---------------------------------------------------------------------------
# the gatherer (the filesystem side of the evidence)
# ---------------------------------------------------------------------------

IIDS = ["pallets__flask-5014", "mwaskom__seaborn-3069",
       "psf__requests-1142"]


def _run_dir(tmp_path, statuses=("resolved", "failed_test", "resolved"),
            meta=None):
    work = tmp_path / "work"
    work.mkdir()
    jsonl = work / "run-full500.jsonl"
    patch = "--- a/f.py\n+++ b/f.py\n@@\n-x\n+y\n"
    with open(jsonl, "w", encoding="utf-8", newline="") as f:
        for i, (iid, st) in enumerate(zip(IIDS, statuses)):
            d = work / "runs" / iid
            d.mkdir(parents=True)
            m = {"artifacts_dir": f"runs/{iid}",
                 "trajectory": "trajectory.jsonl",
                 "eval_log": "eval.log", "wall_s": 200.0}
            (d / "trajectory.jsonl").write_text(
                json.dumps({"role": "user"}) + "\n")
            (d / "eval.log").write_text("ok\n")
            if st in ("resolved", "failed_test"):
                (d / "patch.diff").write_text(patch)
                m["patch_diff"] = "patch.diff"
            f.write(json.dumps({"instance_id": iid, "status": st,
                               "ts": 1_700_000_000.0 + i * 60,
                               "meta": m}) + "\n")
    report_dir = work / "report"
    meta = meta if meta is not None else _good_meta()
    rep.generate(str(jsonl), str(report_dir), meta)
    with open(report_dir / lp.RUN_META_NAME, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return str(jsonl), str(report_dir)


def _gather(tmp_path, jsonl, report_dir, manifest=None, aborted=False):
    records, _ = rep.load_jsonl(jsonl)
    finals, _ = rep.dedupe(records)
    with open(Path(report_dir) / lp.RUN_META_NAME,
              encoding="utf-8") as f:
        meta = json.load(f)
    return lp.gather_full_run_evidence(
        config=_cfg("c"), jsonl=jsonl, report_dir=report_dir, meta=meta,
        finals=finals, records=records,
        manifest_ids=manifest if manifest is not None else list(IIDS),
        buckets=lp._window_buckets(records), aborted=aborted)


def test_gather_evidence_on_a_real_report(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    ev = _gather(tmp_path, jsonl, report_dir)
    assert ev["covered"] == 3 and ev["unknown_ids"] == []
    assert ev["retry_pending"] == []
    assert ev["report_violations"] == []
    assert ev["trajs_expected"] == 3 and ev["trajs_missing"] == []
    assert ev["meth_unknown"] == []
    assert ev["meth_sessions"] == ev["attempt_sessions"] == 3
    assert ev["preds_lines"] == 3 and ev["preds_bad_label"] == []
    assert ev["header_has_label"] is True
    items = _items(ev)
    assert [i["pass"] for i in items] == [True] * 6, items


def test_gather_detects_a_pending_retry_class(tmp_path):
    jsonl, report_dir = _run_dir(
        tmp_path, statuses=("resolved", "quota_limited", "resolved"))
    ev = _gather(tmp_path, jsonl, report_dir)
    assert ev["retry_pending"] == ["mwaskom__seaborn-3069"]
    assert _by_num(_items(ev), 18)["pass"] is False


def test_gather_detects_a_manifest_gap(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    ev = _gather(tmp_path, jsonl, report_dir,
                manifest=list(IIDS) + ["django__django-1"])
    assert _by_num(_items(ev), 18)["pass"] is False
    assert "django__django-1" in _by_num(_items(ev), 18)["observed"]


def test_gather_detects_a_missing_artifact(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    (Path(report_dir).parent / "runs" / IIDS[0] / "patch.diff").unlink()
    ev = _gather(tmp_path, jsonl, report_dir)
    assert ev["report_violations"]
    assert _by_num(_items(ev), 19)["pass"] is False


def test_gather_detects_a_missing_traj(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    (Path(report_dir) / "trajs" / f"{IIDS[1]}.jsonl").unlink()
    ev = _gather(tmp_path, jsonl, report_dir)
    assert ev["trajs_missing"] == [IIDS[1]]
    assert _by_num(_items(ev), 19)["pass"] is False


def test_gather_detects_an_unknown_methodology_field(tmp_path):
    m = _good_meta()
    del m["harness_version"]
    jsonl, report_dir = _run_dir(tmp_path, meta=m)
    ev = _gather(tmp_path, jsonl, report_dir)
    assert "harness_version" in ev["meth_unknown"]
    assert _by_num(_items(ev), 20)["pass"] is False


def test_gather_detects_a_missing_report_md(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    (Path(report_dir) / "report.md").unlink()
    ev = _gather(tmp_path, jsonl, report_dir)
    assert ev["header_has_label"] is None
    # item 21 sees the absent rendered view and fails (it can never pass
    # on a report it cannot read)
    assert _by_num(_items(ev), 21)["pass"] is False


def test_gather_detects_the_wrong_label(tmp_path):
    m = _good_meta(model_name_or_path="swe-2-high")
    jsonl, report_dir = _run_dir(tmp_path, meta=m)
    ev = _gather(tmp_path, jsonl, report_dir)
    assert len(ev["preds_bad_label"]) == 3
    assert _by_num(_items(ev), 22)["pass"] is False


def test_gather_aborted_run_fails_the_batch_item(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    ev = _gather(tmp_path, jsonl, report_dir, aborted=True)
    assert ev["single_session_ok"] is False
    assert _by_num(_items(ev), 23)["pass"] is False


def test_gather_windows_are_the_utc_day_buckets(tmp_path):
    jsonl, report_dir = _run_dir(tmp_path)
    ev = _gather(tmp_path, jsonl, report_dir)
    # all three lines are within one UTC day
    assert len(ev["windows"]) == 1
    assert ev["max_window_sessions"] == 3
    assert ev["max_window_wall_s"] == 200.0 * 3


# ---------------------------------------------------------------------------
# E3 — the persisted meta + the label, end to end through the pipeline
# ---------------------------------------------------------------------------

def test_run_meta_is_persisted_and_carries_the_label(tmp_path, live,
                                                    monkeypatch):
    """The methodology inputs are part of the artifact set: the run
    writes <workdir>/report/run_meta.json, the report header renders the
    label, and every all_preds line carries it (issue #44 E3 / D5)."""
    import subprocess
    monkeypatch.setattr(lp, "BUDGET_FILE", str(tmp_path / "b.json"))
    Path(tmp_path / "b.json").write_text(json.dumps(_authorized_budget()))
    # one real git repo as the "instance" (the sandbox checkout leg)
    repo = tmp_path / "srcrepo"
    repo.mkdir()
    (repo / "base.py").write_text("print('base')\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@l.local"],
               ["config", "user.name", "T"], ["add", "."],
               ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True).stdout.decode().strip()
    inst = tmp_path / "inst.json"
    inst.write_text(json.dumps({"instances": [{
        "instance_id": "test__repo-1", "repo": "test/repo",
        "repo_url": str(repo), "base_commit": head,
        "problem_statement": "Fix it.",
        "FAIL_TO_PASS": ["t.py::test_a"], "PASS_TO_PASS": []}]}))
    wd = tmp_path / "wd"
    rc = lp.main(["--pilot", "c", "--mode", "sandbox", "--instances",
                 str(inst), "--workdir", str(wd), "--run-id", "fullrun",
                 "--devin-bin", MOCK_PATH, "--no-host-check",
                 "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_OK
    meta_path = wd / "report" / "run_meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["model_name_or_path"] == lp.SUBMISSION_LABEL
    assert meta["label"] == lp.SUBMISSION_LABEL
    assert meta["mode"] == "sandbox"
    assert meta["pilot"] == "c"
    assert meta["devin_cli_version"]
    assert meta["manifest_instances"] == 1
    # the report header + every preds line
    md = (wd / "report" / "report.md").read_text()
    assert lp.SUBMISSION_LABEL in md
    preds = [json.loads(l) for l in
            (wd / "report" / "all_preds.jsonl").read_text().splitlines()
            if l.strip()]
    assert preds and all(
        p["model_name_or_path"] == lp.SUBMISSION_LABEL for p in preds)
    # the full-run record exists and carries the items
    record = (wd / "wp6c-full-run.md").read_text()
    for n in (18, 19, 20, 21, 22, 23):
        assert f"| {n} |" in record, n
    # the eval leg was skipped (no container runtime, --no-host-check):
    # the finals are the retry class, so item 18 must NOT pass — and the
    # verdict must be NO-GO, never a GO on an incomplete queue
    assert "| 18 |" in record and "1 pending retry-class" in record
    assert "**Verdict: NO-GO**" in record


def test_pilot_b_record_still_has_no_full_run_items(tmp_path, live,
                                                   monkeypatch):
    """The WP6b 17-item contract is untouched by the new items."""
    import subprocess
    repo = tmp_path / "srcrepo"
    repo.mkdir()
    (repo / "base.py").write_text("print('base')\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@l.local"],
               ["config", "user.name", "T"], ["add", "."],
               ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True).stdout.decode().strip()
    inst = tmp_path / "inst.json"
    inst.write_text(json.dumps({"_meta": {"stratified_repos": [
        "test/repo"]}, "instances": [{
        "instance_id": "test__repo-1", "repo": "test/repo",
        "repo_url": str(repo), "base_commit": head,
        "problem_statement": "Fix it.",
        "FAIL_TO_PASS": ["t.py::test_a"], "PASS_TO_PASS": []}]}))
    # the import source (the pilot-B requirement) — same mode, so the
    # mode gate is satisfied
    src = tmp_path / "srcwd"
    src.mkdir()
    (src / "run-srcA.jsonl").write_text(json.dumps(
        {"instance_id": "test__repo-1", "status": "no_patch",
         "ts": 1_600_000_000.0, "meta": {}}) + "\n")
    wd = tmp_path / "wdb"
    rc = lp.main(["--pilot", "b", "--mode", "sandbox", "--instances",
                 str(inst), "--workdir", str(wd), "--run-id", "pb",
                 "--devin-bin", MOCK_PATH, "--no-host-check",
                 "--import-run", "srcA", "--import-workdir", str(src),
                 "--resume", "--largest-bytes", "1048576"])
    assert rc == lp.EXIT_OK
    record = (wd / "wp6b-pilot-b.md").read_text()
    for n in (15, 16, 17):
        assert f"| {n} |" in record, n
    for n in (18, 19, 20, 21, 22, 23):
        assert f"| {n} |" not in record, n
    # E3 holds for the pilots too (the meta is the report's input in
    # every kind) — and the label header too
    assert (wd / "report" / "run_meta.json").is_file()
    assert lp.SUBMISSION_LABEL in (wd / "report" / "report.md").read_text()


# --------------------------------------------------------------------------
# M1 (issue #44 review round 1): the abort rule is an enforced B2 field
# --------------------------------------------------------------------------

def test_b2_gate_requires_the_abort_rule(tmp_path):
    """Every other field filled and the abort rule still PENDING is NOT
    an authorization: B7's discipline ("a failed window stops") has no
    content until the operator writes the sentence, and the 500-session
    invocation must not start without it."""
    for bad in ("PENDING (issue #44 B7)", "", None, 0):
        b = _authorized_budget()
        if bad is None:
            del b["abort_rule"]
        else:
            b["abort_rule"] = bad
        with pytest.raises(lp.PilotConfigError, match="abort_rule"):
            lp.require_full_run_authorization(b, "budget.json")
    # and the committed template still refuses (it says PENDING)
    with open(lp.BUDGET_FILE, encoding="utf-8") as f:
        committed = json.load(f)
    assert committed["abort_rule"].startswith("PENDING")
    with pytest.raises(lp.PilotConfigError, match="abort_rule"):
        lp.require_full_run_authorization(committed, lp.BUDGET_FILE)
    # the docstring/README/notes list the same field set the code enforces
    assert set(lp.FULL_RUN_BUDGET_FIELDS) == {
        "max_sessions_per_day", "max_wall_per_session", "calibrated_at",
        "basis", "authorized_by", "completion_deadline", "abort_rule"}


def test_b2_gate_precedes_the_live_gate(tmp_path, monkeypatch):
    """Authorization is a configuration refusal (exit 2), so it is the
    message an operator gets whether or not BENCH_LIVE is set — the env
    var must never look like the thing standing between them and an
    unauthorized 500-session run."""
    monkeypatch.delenv("BENCH_LIVE", raising=False)
    inst = tmp_path / "inst.json"
    inst.write_text(json.dumps({"instances": [
        {"instance_id": "a__a-1", "repo": "a/b", "repo_url": "https://x/y",
         "base_commit": "0" * 40, "problem_statement": "p"}]}))
    budget = tmp_path / "budget.json"
    b = _authorized_budget()
    del b["abort_rule"]
    budget.write_text(json.dumps(b))
    rc = lp.main(["--pilot", "c", "--mode", "sandbox", "--instances",
                 str(inst), "--workdir", str(tmp_path / "wd"),
                 "--budget-file", str(budget)])
    assert rc == lp.EXIT_CONFIG, rc


def test_item22_needs_the_header_not_a_prose_mention(tmp_path):
    """A report that mentions the label in its methodology text but does
    not stand under it is not labelled (issue #44 E3, reviewed)."""
    jsonl, report_dir = _run_dir(tmp_path)
    md_path = Path(report_dir) / "report.md"
    md = md_path.read_text()
    assert f"**{lp.SUBMISSION_LABEL}**" in md
    body_only = "\n".join(
        l for l in md.splitlines()
        if l.strip() != f"**{lp.SUBMISSION_LABEL}**")
    body_only += (f"\nSome prose that happens to mention "
                 f"{lp.SUBMISSION_LABEL} further down.\n")
    md_path.write_text(body_only)
    ev = _gather(tmp_path, jsonl, report_dir)
    assert lp.SUBMISSION_LABEL in (md_path.read_text())  # substring: not
    assert ev["header_has_label"] is False              # enough
    assert _by_num(_items(ev), 22)["pass"] is False
