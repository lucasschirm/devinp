#!/usr/bin/env python3
"""WP6c pre-flight E4 (issue #44) — verify a run is submission-shaped.

The script #23's "Testable by" line asks for: *every JSONL line's files
exist; patch content matches; the methodology block is complete; the
pass@1 cross-checks against the per-repo table; the queue is complete;
the label is on every line*. ``report.py --check`` covers the first two
(R1 referenced-files-exist + R2 patch byte-equality) and nothing else, so
this composes it with the checks the full run adds. Stdlib + the pinned
``report`` module only: no network, no devin, no docker — it must be
runnable on the artifact bundle alone.

Checks (one line each on stdout; a single violation ⇒ exit 3):

=====  ===================================================================
R1+R2  ``report.check`` over the run JSONL and the report dir (referenced
       files exist, the ``resolved``/``failed_test`` ⇒ patch rule, and
       ``all_preds.jsonl`` ≡ ``patch.diff`` byte-for-byte)
META   ``run_meta.json`` exists in the report dir, parses, and no
       methodology field renders ``unknown`` (issue #44 E3 persisted it;
       without it the report's own block is unverifiable)
MODE   the meta records the grading leg (``container``/``sandbox``) — the
       D1 honesty record: a sandbox-mode pass@1 is an internal estimate,
       and the verifier prints which one it verified
QUEUE  completeness against the manifest: every manifest id has a final
       line, no final id is outside the manifest, and no retry-class
       final (``quota_limited`` / ``eval_error`` / ``preflight_failed``)
       is left pending
PASS1  the overall pass@1 == Σ over the per-repo table == the rendered
       ``report.md``, and the denominator is TERMINAL − retry-class
LABEL  every ``all_preds.jsonl`` line carries the submission label, and
       ``report.md`` renders it
BUNDLE (only with ``--bundle``) the AC layout
       ``runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,eval.log}``
       exists for every instance whose checkpoint line references it,
       byte-identical to the source artifact, and the bundle carries the
       report / meta / preds / checkpoint files

Usage:
  python verify_submission.py --jsonl full-run/run-full500.jsonl \
      [--report full-run/report] [--manifest bench/verified_instances.json] \
      [--bundle submission/full500] [--label "Devin (agent) on …"]

Exit codes: 0 = verified; 2 = bad input (a missing file the operator must
pass); 3 = an integrity violation (the run is not submission-shaped).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import report as rep  # noqa: E402  (WP5 — the R1/R2 + pass@1 contract)

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_INTEGRITY = 3

#: Must equal ``live_pilot.SUBMISSION_LABEL`` (the CI consistency test
#: asserts it — asserted here without importing the 4 500-line pilot
#: module, because a verifier must run on the artifacts alone). A label
#: drift would make this script PASS a bundle the harness never labels.
DEFAULT_LABEL = "Devin (agent) on SWE-bench Verified"

#: The per-instance bundle layout #23's AC words (PLAN-WP6C §3 D4): the
#: harness writes ``runs/<iid>/…`` (a tested contract); the bundle
#: materialises ``runs/<run_id>/<iid>/…``.
BUNDLE_ARTIFACTS = (("patch_diff", "patch.diff"),
                   ("trajectory", "trajectory.jsonl"),
                   ("eval_log", "eval.log"))

#: The meta keys the bundle README must state as the deviations record.
GRADING_LEGS = ("container", "sandbox")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class Results:
    """The PASS/FAIL table under construction."""

    def __init__(self) -> None:
        self.rows: "list[tuple[str, bool, str]]" = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.rows)

    def render(self) -> str:
        w = max([len(r[0]) for r in self.rows] or [0])
        out = []
        for name, ok, detail in self.rows:
            out.append(f"[{'PASS' if ok else 'FAIL'}] {name:<{w}}"
                       + (f"  {detail}" if detail else ""))
        return "\n".join(out)


def load_meta(report_dir: str, res: Results) -> "dict | None":
    """``<report dir>/run_meta.json`` (the WP6c E3 artifact)."""
    path = os.path.join(report_dir, "run_meta.json")
    if not os.path.isfile(path):
        res.add("META", False, f"no run_meta.json in {report_dir!r} — the "
               "methodology block has no verifiable input (issue #44 E3)")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        res.add("META", False, f"run_meta.json unreadable: {exc}")
        return None
    if not isinstance(meta, dict):
        res.add("META", False, "run_meta.json is not a JSON object")
        return None
    res.add("META", True, path)
    return meta


def check_r1_r2(jsonl: str, report_dir: str, meta: "dict | None",
               res: Results) -> None:
    """R1 + R2 through the WP5 checker itself (no re-implementation)."""
    try:
        violations = list(rep.check(jsonl, report_dir, meta))
    except rep.ReportError as exc:
        res.add("R1+R2", False, f"report.check raised "
               f"{type(exc).__name__}: {exc}")
        return
    res.add("R1+R2", not violations,
            "referenced files exist + all_preds ≡ patch.diff"
            if not violations
            else f"{len(violations)} violation(s): "
                 + "; ".join(violations[:5]))


def check_methodology(meta: dict, records: "list[dict]",
                     res: Results) -> None:
    meth = rep.methodology(meta, records)
    unknown = sorted(k for k, v in meth.items() if v == "unknown")
    res.add("METHODOLOGY", not unknown,
            f"{len(meth)} fields complete"
            if not unknown else f"unknown: {', '.join(unknown)}")


def check_grading_leg(meta: dict, res: Results) -> None:
    """The D1 honesty record: which leg produced this number."""
    mode = meta.get("mode")
    if mode not in GRADING_LEGS:
        res.add("GRADING LEG", False,
                f"meta.mode is {mode!r} — expected one of "
                f"{list(GRADING_LEGS)} (the run must record which eval "
                "leg produced the score)")
        return
    leg = meta.get("grading_leg") or ""
    if mode == "container":
        res.add("GRADING LEG", True,
                "container — the official run_evaluation leg "
                "(submission-ready)")
    else:
        res.add("GRADING LEG", mode == "sandbox" and bool(leg),
                "SANDBOX — our host check, NOT the official harness: an "
                "internal estimate; disclose it and do not submit it as "
                "an official score (PLAN-WP6C §3 D1 option B)"
                + ("" if leg else " — and meta.grading_leg is empty"))


def check_queue(finals: "dict[str, dict]", manifest_ids: "list[str]",
               res: Results) -> None:
    ids = set(manifest_ids)
    missing = sorted(ids - set(finals))
    unknown = sorted(set(finals) - ids)
    pending = sorted(iid for iid, r in finals.items()
                    if r.get("status") in rep.RETRY_STATUSES)
    ok = not missing and not unknown and not pending
    res.add("QUEUE", ok,
            f"{len(finals)}/{len(ids)} manifest ids have a final line"
            + ("" if ok else
               f"; missing {len(missing)}"
               f" ({', '.join(missing[:3])}{'…' if len(missing) > 3 else ''})"
               f"; outside-manifest {len(unknown)}"
               f" ({', '.join(unknown[:3])})"
               f"; pending retry-class {len(pending)}"
               f" ({', '.join(pending[:3])})"))


def check_pass1(finals: "dict[str, dict]", report_md: str,
               res: Results) -> None:
    """Re-derived from the JSONL, then compared with the rendered
    markdown (render drift would publish a number the checkpoint does
    not hold)."""
    p1 = rep.pass1(finals)
    ov = p1["overall"]
    per = p1["per_repo"]
    sum_res = sum(c["resolved"] for c in per.values())
    sum_ter = sum(c["terminal"] for c in per.values())
    arith = sum_res == ov["resolved"] and sum_ter == ov["terminal"]
    outcomes = sum(1 for r in finals.values()
                  if r.get("status") in rep.PASS1_OUTCOMES)
    denom = ov["terminal"] == outcomes
    md_ov, md_rows = rep.parse_report_pass1(report_md)
    render = md_ov is not None and (
        md_ov["resolved"], md_ov["terminal"], md_ov["pct"]) == (
        ov["resolved"], ov["terminal"], ov["pct"])
    for repo, c in per.items():
        row = md_rows.get(repo)
        render = (render and row is not None
                 and row == (c["resolved"], c["terminal"], c["pct"]))
    for repo in md_rows:
        render = render and repo in per
    ok = arith and denom and render and ov["terminal"] > 0
    detail = (f"pass@1 {ov['resolved']}/{ov['terminal']} ({ov['pct']})"
              f" · Σ per-repo {sum_res}/{sum_ter}"
              f" · legitimate outcomes {outcomes}"
              f" · rendered {'agrees' if render else 'DISAGREES'}")
    if ov["terminal"] == 0:
        detail += " — nothing graded: no pass@1 to verify"
    res.add("PASS@1", ok, detail)


def check_label(report_dir: str, label: str, res: Results) -> None:
    preds = os.path.join(report_dir, "all_preds.jsonl")
    if not os.path.isfile(preds):
        res.add("LABEL", False, f"no all_preds.jsonl in {report_dir!r}")
        return
    bad: "list[str]" = []
    n = 0
    with open(preds, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad.append("<unparseable line>")
                continue
            if rec.get("model_name_or_path") != label:
                bad.append(str(rec.get("instance_id")))
    md = ""
    md_path = os.path.join(report_dir, "report.md")
    if os.path.isfile(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    header = label in md
    ok = n > 0 and not bad and header
    res.add("LABEL", ok,
            f"{n} preds line(s) labelled"
            if ok else
            f"{n} line(s), {len(bad)} without the label"
            + (f" ({', '.join(bad[:3])})" if bad else "")
            + f"; report.md header {'has' if header else 'MISSING'} the "
              "label")


def check_bundle(bundle: str, run_id: str, finals: "dict[str, dict]",
                base_dir: str, res: Results) -> None:
    """The AC layout (D4): ``runs/<run_id>/<iid>/…``, byte-identical to
    the artifacts the checkpoint references."""
    if not os.path.isdir(bundle):
        res.add("BUNDLE", False, f"{bundle!r} is not a directory")
        return
    for name in ("all_preds.jsonl", "report.md", "run_meta.json"):
        if not os.path.isfile(os.path.join(bundle, name)):
            res.add("BUNDLE", False, f"bundle is missing {name}")
            return
    problems: "list[str]" = []
    copied = 0
    for iid in sorted(finals):
        # the SAME reference-resolution contract as R1 (report._ref_path:
        # a relative meta.artifacts_dir resolves against the run JSONL's
        # directory) — the bundle must be checked against the paths the
        # harness itself means, never a second interpretation of them
        try:
            refs = rep._ref_path(finals[iid], base_dir)
        except rep.ReportError as exc:
            problems.append(f"{iid}: unresolvable references "
                           f"({exc})")
            continue
        dst_dir = os.path.join(bundle, "runs", run_id, iid)
        for key, fname in BUNDLE_ARTIFACTS:
            src = refs.get(key)
            if not src:
                continue
            dst = os.path.join(dst_dir, fname)
            if not os.path.isfile(src):
                problems.append(f"{iid}: checkpoint references "
                               f"{fname} but the source {src!r} is "
                               "missing")
                continue
            if not os.path.isfile(dst):
                problems.append(f"{iid}: runs/{run_id}/{iid}/{fname} "
                               "absent from the bundle")
                continue
            if _sha256_file(src) != _sha256_file(dst):
                problems.append(f"{iid}: {fname} differs from the "
                               "checkpoint artifact")
                continue
            copied += 1
    ok_dirs = [d for d in sorted(os.listdir(os.path.join(bundle, "runs")))
              if d == run_id] if os.path.isdir(
                  os.path.join(bundle, "runs")) else []
    if not ok_dirs:
        problems.append(f"no runs/{run_id}/ directory in the bundle")
    res.add("BUNDLE", not problems,
            f"runs/{run_id}/<iid> complete for {copied} referenced "
            "artifact(s)"
            if not problems else
            f"{len(problems)} problem(s): " + "; ".join(problems[:5]))


def verify(jsonl: str, report_dir: str,
           manifest_path: "str | None",
           bundle: "str | None", label: str) -> Results:
    res = Results()
    records, _dropped = rep.load_jsonl(jsonl)
    finals, _markers = rep.dedupe(records)
    meta = load_meta(report_dir, res)
    check_r1_r2(jsonl, report_dir, meta, res)
    if meta is not None:
        check_methodology(meta, records, res)
        check_grading_leg(meta, res)
    md_path = os.path.join(report_dir, "report.md")
    md = ""
    if os.path.isfile(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
    else:
        res.add("REPORT.md", False, f"missing {md_path!r}")
    check_pass1(finals, md, res)
    check_label(report_dir, label, res)
    if manifest_path:
        with open(manifest_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        ids = [i["instance_id"] for i in doc["instances"]]
        check_queue(finals, ids, res)
    if bundle:
        check_bundle(bundle, bundle_run_id(jsonl), finals,
                     os.path.dirname(os.path.abspath(jsonl)), res)
    return res


def bundle_run_id(jsonl: str) -> str:
    """The run id: the checkpoint's own name (``run-<id>.jsonl``) — the
    single source, so a bundle built for one run can never be verified
    against another run's layout."""
    base = os.path.basename(jsonl)
    if base.startswith("run-") and base.endswith(".jsonl"):
        return base[4:-len(".jsonl")]
    raise SystemExit(f"cannot derive the run id from {jsonl!r}")


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="verify_submission",
        description="verify a WP6c run is submission-shaped "
                    "(issue #44 E4)")
    p.add_argument("--jsonl", required=True,
                   help="the run checkpoint (run-<id>.jsonl)")
    p.add_argument("--report", default=None,
                   help="the report dir (default: <jsonl dir>/report)")
    p.add_argument("--manifest", default=str(_BENCH /
                                            "verified_instances.json"),
                   help="the pinned manifest to check completeness "
                        "against ('' to skip)")
    p.add_argument("--bundle", default=None,
                   help="the package_submission.py bundle to check the "
                        "runs/<run_id>/<iid>/ layout of (optional)")
    p.add_argument("--label", default=DEFAULT_LABEL,
                   help="the required submission label")
    args = p.parse_args(argv)

    if not os.path.isfile(args.jsonl):
        print(f"INPUT: no run JSONL at {args.jsonl!r}", file=sys.stderr)
        return EXIT_INPUT
    report_dir = args.report or os.path.join(
        os.path.dirname(os.path.abspath(args.jsonl)), "report")
    if not os.path.isdir(report_dir):
        print(f"INPUT: no report dir at {report_dir!r}", file=sys.stderr)
        return EXIT_INPUT
    manifest = args.manifest or None
    if manifest and not os.path.isfile(manifest):
        print(f"INPUT: no manifest at {manifest!r} (pass --manifest '' "
              "to skip the completeness check)", file=sys.stderr)
        return EXIT_INPUT

    res = verify(args.jsonl, report_dir, manifest, args.bundle,
                args.label)
    print(res.render())
    print()
    if res.ok:
        print(f"VERIFIED: {args.jsonl}")
        return EXIT_OK
    failed = [r[0] for r in res.rows if not r[1]]
    print(f"NOT SUBMISSION-SHAPED: {', '.join(failed)}", file=sys.stderr)
    return EXIT_INTEGRITY


if __name__ == "__main__":
    sys.exit(main())
