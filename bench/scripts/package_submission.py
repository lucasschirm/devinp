#!/usr/bin/env python3
"""WP6c pre-flight E4 (issue #44) — build the submission bundle.

Materialises the layout #23's acceptance criteria word —

    submission/<run_id>/
        README.md                  the label, the methodology block, the
                                   deviations, the sha256 of every file
        all_preds.jsonl            the harness predictions file (verbatim
                                   copy of the report artifact)
        report.md                  the WP5 report
        run_meta.json              the persisted methodology meta (E3)
        run-<run_id>.jsonl         the checkpoint (the source of truth)
        trajs/<instance_id>.jsonl  the trajectories
        runs/<run_id>/<instance_id>/{patch.diff,trajectory.jsonl,eval.log}
        SHA256SUMS                 one line per file (relative path)

Why the layout is built here and not in the harness (PLAN-WP6C §3 D4):
the harness writes ``runs/<instance_id>/…``, and that path is a *tested*
contract — the WP6b checkpoint import and the artifact self-heal resolve
references against it. The AC's ``runs/<run_id>/<iid>/`` is a statement
about the **submitted artifact**, so the bundle adds the run-id level and
the harness stays untouched.

Packaging implies verifying: :func:`verify_submission.verify` runs first
and a violation refuses the bundle (exit 3). ``--allow-incomplete``
packages anyway and stamps the README **INTERIM** — an interim bundle is
for the operator's own reading mid-run, and the label check still has to
pass (an unlabelled bundle must never be built at all).

Deterministic by construction: byte-for-byte copies, sorted iteration, no
timestamp of its own (the only dates are the checkpoint's, via the
methodology block), so re-packaging the same checkpoint is diff-clean.

Exit codes: 0 = bundle written; 2 = bad input; 3 = a verification
violation (or a re-pack that would silently overwrite a different
bundle).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import report as rep  # noqa: E402
import verify_submission as vs  # noqa: E402

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_INTEGRITY = 3


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def _readme(meta: dict, ctx: dict, deviations: "list[str]",
           interim: bool, checksums: "dict[str, str]") -> str:
    meth = rep.methodology(meta, ctx["records"])
    ov = ctx["p1"]["overall"]
    lines = ["# SWE-bench Verified submission bundle", ""]
    lines += [f"**{meta.get('label') or vs.DEFAULT_LABEL}**", ""]
    if interim:
        lines += ["> **INTERIM** — packaged before the queue was "
                 "complete (`--allow-incomplete`). Not a submission: "
                 "the completeness gate (issue #44 E4 / item 18) has "
                 "not passed.", ""]
    lines += [
        f"- run id: `{ctx['run_id']}`",
        f"- pass@1: **{ov['resolved']}/{ov['terminal']} ({ov['pct']})** "
        f"over {len(ctx['finals'])} instance(s)",
        f"- grading leg: **{meta.get('mode')}** — "
        f"{meta.get('grading_leg', 'not recorded')}",
        "",
        "## Methodology",
        "",
    ]
    for key, label in rep._METHODOLOGY_FIELDS:
        lines.append(f"- **{label}:** {meth[key]}")
    lines += [f"- **Date range (UTC):** {meth['date_range']}",
              f"- **Sessions used (attempts):** {meth['sessions_used']}",
              "", "## Deviations", ""]
    if deviations:
        lines += [f"- {d}" for d in deviations]
    else:
        lines.append("- none.")
    lines += ["",
              "## How this bundle was produced",
              "",
              "```",
              "python bench/scripts/package_submission.py \\",
              f"    --jsonl <workdir>/run-{ctx['run_id']}.jsonl \\",
              f"    --out submission/{ctx['run_id']}",
              "```",
              "",
              "`verify_submission.py` runs first; re-running it on the",
              "bundle with `--bundle` re-checks the per-instance layout",
              "byte-for-byte:",
              "",
              "```",
              "python bench/scripts/verify_submission.py \\",
              f"    --jsonl <workdir>/run-{ctx['run_id']}.jsonl \\",
              f"    --bundle submission/{ctx['run_id']}",
              "```",
              "",
              "## File checksums (sha256)",
              "",
              "| file | sha256 |",
              "|---|---|"]
    for rel in sorted(checksums):
        if rel == "README.md":
            continue  # the README cannot contain its own hash
        lines.append(f"| `{rel}` | `{checksums[rel]}` |")
    lines.append("")
    return "\n".join(lines)


def _deviations(meta: dict, pending: int) -> "list[str]":
    """The mandated deviations block (D1/D3 honesty record) — derived,
    so it cannot drift from what the run actually was."""
    out: "list[str]" = []
    if meta.get("mode") == "sandbox":
        out.append(
            "**The agent environment is the host check, not the "
            "official harness.** Sessions ran on this host (git clone "
            "+ venv + `pytest -rA`), so this is our grading leg, not "
            "`swebench.harness.run_evaluation` with the repo log "
            "parsers. It is an internal estimate and is **not** "
            "leaderboard-submittable (PLAN-WP6C §3 D1 option B).")
    if pending:
        out.append(f"**{pending} instance(s) are still pending the "
                   "retry class** (undecided — excluded from the pass@1 "
                   "denominator, PLAN §2.5).")
    if meta.get("deviations"):
        extra = meta["deviations"]
        if isinstance(extra, str):
            out.append(extra)
        elif isinstance(extra, list):
            out += [str(e) for e in extra]
    return out


def package(jsonl: str, report_dir: str, out: str, *,
           manifest: "str | None" = None,
           allow_incomplete: bool = False,
           force: bool = False,
           label: str = vs.DEFAULT_LABEL) -> dict:
    """Build the bundle. Returns the summary dict."""
    records, _dropped = rep.load_jsonl(jsonl)
    finals, _markers = rep.dedupe(records)
    run_id = vs.bundle_run_id(jsonl)
    meta_path = os.path.join(report_dir, "run_meta.json")
    if not os.path.isfile(meta_path):
        raise SystemExit(f"no run_meta.json in {report_dir!r}")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (ValueError, OSError) as exc:
        # a corrupt run_meta.json used to escape as a JSONDecodeError
        # traceback (exit 1) because this read happens before vs.verify()
        # gets to report the same file as a failed META check — the whole
        # point of this PR's error-handling pass (issue #44 review r4)
        raise vs.InputError(f"{meta_path!r} is not readable JSON: {exc}"
                            ) from exc

    # a stale bundle must never be published beside a changed checkpoint:
    # re-packaging is allowed only when the destination is empty (or the
    # run id matches and --force was passed)
    if os.path.isdir(out) and os.listdir(out):
        if not force:
            raise SystemExit(
                f"{out!r} is not empty — pass --force to rebuild it in "
                "place (a bundle must never mix artifacts of two "
                "checkpoints)")

    # packaging IMPLIES verifying (PLAN-WP6C §11: verify, then package)
    res = vs.verify(jsonl, report_dir, manifest, None, label)
    failed = [r[0] for r in res.rows if not r[1]]
    pending = sum(1 for r in finals.values()
                 if r.get("status") in rep.RETRY_STATUSES)
    if failed:
        only_queue = failed == ["QUEUE"]
        if not (allow_incomplete and only_queue):
            print("verification FAILED — refusing to package:",
                  file=sys.stderr)
            for name in failed:
                detail = next(r[2] for r in res.rows
                             if r[0] == name and not r[1])
                print(f"  - {name}: {detail}", file=sys.stderr)
            if allow_incomplete and not only_queue:
                print("  (--allow-incomplete covers an incomplete QUEUE "
                      "only — every other check must pass)",
                      file=sys.stderr)
            raise SystemExit(EXIT_INTEGRITY)

    base_dir = os.path.dirname(os.path.abspath(jsonl))
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    written: "list[str]" = []
    # the report artifacts (verbatim copies — never re-serialised)
    for name in ("all_preds.jsonl", "report.md", "run_meta.json"):
        _copy(os.path.join(report_dir, name), os.path.join(out, name))
        written.append(name)
    _copy(jsonl, os.path.join(out, f"run-{run_id}.jsonl"))
    written.append(f"run-{run_id}.jsonl")
    trajs_src = os.path.join(report_dir, "trajs")
    if os.path.isdir(trajs_src):
        for name in sorted(os.listdir(trajs_src)):
            src = os.path.join(trajs_src, name)
            if os.path.isfile(src):
                _copy(src, os.path.join(out, "trajs", name))
                written.append(f"trajs/{name}")
    # the AC layout: runs/<run_id>/<iid>/… (D4)
    for iid in sorted(finals):
        refs = rep._ref_path(finals[iid], base_dir)
        for key, fname in vs.BUNDLE_ARTIFACTS:
            src = refs.get(key)
            if not src or not os.path.isfile(src):
                continue  # nothing referenced / nothing written for it
            dst = os.path.join(out, "runs", run_id, iid, fname)
            _copy(src, dst)
            written.append(os.path.relpath(dst, out))

    checksums = {rel: _sha256_file(os.path.join(out, rel))
                for rel in sorted(written)}
    sums = "".join(f"{checksums[rel]}  {rel}\n"
                  for rel in sorted(checksums))
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8",
             newline="") as f:
        f.write(sums)

    # the README is rendered last: its table hashes everything else
    readme = _readme(meta,
                    {"records": records, "finals": finals, "p1":
                     rep.pass1(finals), "run_id": run_id},
                    _deviations(meta, pending),
                    bool(allow_incomplete), checksums)
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8",
             newline="") as f:
        f.write(readme)
    return {"out": out, "run_id": run_id, "files": len(checksums) + 2,
           "instances": len(finals), "interim": bool(allow_incomplete),
           "bundle": out}


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="package_submission",
        description="build the submission bundle for a WP6c run "
                    "(issue #44 E4)")
    p.add_argument("--jsonl", required=True,
                   help="the run checkpoint (run-<id>.jsonl)")
    p.add_argument("--report", default=None,
                   help="the report dir (default: <jsonl dir>/report)")
    p.add_argument("--out", default=None,
                   help="the bundle dir (default: "
                        "submission/<run_id> next to the workdir)")
    p.add_argument("--manifest",
                   default=str(_BENCH / "verified_instances.json"),
                   help="the manifest for the completeness check ('' to "
                        "skip)")
    p.add_argument("--allow-incomplete", action="store_true",
                   help="package an incomplete queue and stamp the "
                        "README INTERIM (covers the QUEUE check only)")
    p.add_argument("--force", action="store_true",
                   help="rebuild a non-empty bundle dir in place")
    p.add_argument("--label", default=vs.DEFAULT_LABEL,
                   help="the required submission label")
    args = p.parse_args(argv)

    jsonl = args.jsonl
    if not os.path.isfile(jsonl):
        print(f"INPUT: no run JSONL at {jsonl!r}", file=sys.stderr)
        return EXIT_INPUT
    report_dir = args.report or os.path.join(
        os.path.dirname(os.path.abspath(jsonl)), "report")
    if not os.path.isdir(report_dir):
        print(f"INPUT: no report dir at {report_dir!r}", file=sys.stderr)
        return EXIT_INPUT
    manifest = args.manifest or None
    if manifest and not os.path.isfile(manifest):
        print(f"INPUT: no manifest at {manifest!r}", file=sys.stderr)
        return EXIT_INPUT
    try:
        # bundle_run_id() raises vs.InputError — and it is called HERE too,
        # before package(). It used to raise SystemExit, which the handler
        # below turned into "INPUT:" + the documented exit 2; swapping the
        # exception type silently turned that path into a traceback and
        # exit 1 (issue #44 review round 3, confirmed at runtime).
        out = args.out or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(jsonl))),
            "submission", vs.bundle_run_id(jsonl))
        summary = package(jsonl, report_dir, out, manifest=manifest,
                         allow_incomplete=args.allow_incomplete,
                         force=args.force, label=args.label)
    except vs.InputError as exc:
        print(f"INPUT: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except SystemExit as exc:  # the refuse paths inside package()
        code = exc.code
        if isinstance(code, int) and code in (EXIT_INTEGRITY,):
            return code
        msg = exc.args[0] if exc.args and isinstance(exc.args[0], str) \
            else str(exc)
        print(f"INPUT: {msg}", file=sys.stderr)
        return EXIT_INPUT
    except rep.ReportError as exc:
        print(f"INPUT: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INPUT
    print(f"packaged: {summary['out']} "
          f"({summary['files']} files, {summary['instances']} instance(s)"
          + (", INTERIM" if summary["interim"] else "") + ")")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
