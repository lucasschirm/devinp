#!/usr/bin/env python3
"""WP6c pre-flight E1 (issue #44) — pin the 500-instance Verified manifest.

Fetches the SWE-bench Verified ``test`` split verbatim from the HF
datasets-server rows API and pins **all 500 rows** (no selection) into
``bench/verified_instances.json`` — the queue #23 runs.

The fetch itself is **not** re-implemented: it is imported from
``pin_pilot_b_instances.py`` (the WP6b module), which carries the
hardened page loop — the retry, the error-shaped-200 guard, the
``row_idx`` schema guard, the no-progress guard against a cycling API
and the ``row_idx`` sort + de-dupe. Re-implementing it here would fork
those guards; the pinned #23 run must fetch through exactly one
implementation.

What this module adds is the **assertion layer** (:func:`validate`) that
a 500-instance queue needs and a 25-instance pilot did not:

1. ``len(rows) == 500`` and 500 **unique** ``instance_id``s;
2. exactly 12 repos, with the per-repo counts of WP0 §5 / PLAN-WP6C §2.3
   (django 231 · sympy 75 · sphinx 44 · matplotlib 34 · scikit-learn 32 ·
   astropy 22 · xarray 22 · pytest 19 · pylint 10 · requests 8 ·
   seaborn 2 · flask 1);
3. every row carries every field the harness consumes, a non-empty
   ``image`` and ``eval_type == "pass_and_fail"``;
4. ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` are non-empty lists of strings
   (the official harness' grading contract);
5. the 3 Pilot-A and the 25 Pilot-B ids are a **subset** of the 500
   (the overlap statement for the record — those sessions are the
   reuse candidates for ``--import-run``, never a silent re-spend);
6. the id list is hashed (``_meta.instance_ids_sha256``) so ``--check``
   and a reader can verify the queue content without re-diffing 4 MB.

Modes:
  (default)  fetch → validate → write the manifest.
  ``--check`` fetch → validate → build the document and compare it with
             the committed file; exit 1 on any drift. ``_meta.fetched_at``
             is the one field excluded from the comparison (it is the
             fetch *date*, not queue content).

Usage:
  python pin_verified_instances.py                  # write the manifest
  python pin_verified_instances.py --check          # CI reproducibility
  python pin_verified_instances.py --offline FILE   # validate a cached payload
                                                    # (zero network; the shape
                                                    #  ``fetch_all_rows`` returns)

Exit codes: 0 = written / reproducible; 1 = drift or a failed assertion;
2 = a usage / input error.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
DEFAULT_OUT = str(_BENCH / "verified_instances.json")

#: WP0 §5 / PLAN-WP6C §2.3 — the published per-repo row counts of the
#: Verified ``test`` split. A drift here means the upstream split moved:
#: the pinned queue must be re-pinned deliberately, never silently.
EXPECTED_PER_REPO: dict = {
    "django/django": 231,
    "sympy/sympy": 75,
    "sphinx-doc/sphinx": 44,
    "matplotlib/matplotlib": 34,
    "scikit-learn/scikit-learn": 32,
    "astropy/astropy": 22,
    "pydata/xarray": 22,
    "pytest-dev/pytest": 19,
    "pylint-dev/pylint": 10,
    "psf/requests": 8,
    "mwaskom/seaborn": 2,
    "pallets/flask": 1,
}
EXPECTED_TOTAL = 500

#: The already-spent sessions (WP6a Pilot A, WP6b Pilot B). Their ids
#: must be inside the 500 — the overlap statement of PLAN-WP6C §13
#: ("the 25 pilot instances are re-spent in the 500 unless Pilot B′ is
#: run in the same mode and imported").
PILOT_A_IDS = ("pallets__flask-5014", "mwaskom__seaborn-3069",
               "psf__requests-1142")
PILOT_B_FILE = str(_BENCH / "pilot_b_instances.json")

RULE = ("all rows of the SWE-bench/SWE-bench_Verified `test` split, in "
        "the published dataset (row_idx) order — no selection, no "
        "filtering")


class ManifestError(Exception):
    """A failed manifest assertion (exit 1) — the pinned queue would not
    be what #23 promises to run."""


def load_pinning_module(path: "str | None" = None):
    """Load the WP6b pinning module by path (the flat ``bench/`` layout
    has no importable package; the same convention as
    ``tests/test_live_pilot.py``)."""
    src = Path(path) if path else _HERE / "pin_pilot_b_instances.py"
    spec = importlib.util.spec_from_file_location(
        "pin_pilot_b_instances", str(src))
    if spec is None or spec.loader is None:
        raise ManifestError(f"cannot load the pinning module: {src}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256_ids(ids: "list[str]") -> str:
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def validate(rows: list, *, keep: "tuple[str, ...]",
             pilot_b_ids: "list[str]",
             expected_total: int = EXPECTED_TOTAL,
             expected_per_repo: "dict[str, int]" = EXPECTED_PER_REPO,
             ) -> "list[dict]":
    """The assertion layer. *rows* is what
    ``pin_pilot_b_instances.fetch_all_rows()`` returns (API rows with
    ``row_idx`` + ``row``); returns the pinned instance dicts (the
    ``KEEP`` projection + ``repo_url``) in dataset order.

    Raises :class:`ManifestError` on the first broken assertion — this
    gates a 500-session run, so every check fails closed."""
    if not isinstance(rows, list) or not rows:
        raise ManifestError(
            f"the fetch returned no rows (got {type(rows).__name__})")
    if len(rows) != expected_total:
        raise ManifestError(
            f"expected {expected_total} rows in the published split, "
            f"got {len(rows)} — the split moved, the queue must be "
            "re-pinned deliberately")
    out: "list[dict]" = []
    seen_ids: "set[str]" = set()
    per_repo: "dict[str, int]" = {}
    for pos, api_row in enumerate(rows):
        row = api_row.get("row") if isinstance(api_row, dict) else None
        if not isinstance(row, dict):
            raise ManifestError(
                f"row {pos} has no row object (schema drift): "
                f"{json.dumps(api_row)[:200]}")
        missing = [k for k in keep if k not in row]
        if missing:
            raise ManifestError(
                f"row {pos} ({row.get('instance_id')!r}) is missing "
                f"pinned field(s): {', '.join(missing)}")
        iid = row["instance_id"]
        if not isinstance(iid, str) or not iid:
            raise ManifestError(f"row {pos} has no instance_id")
        if iid in seen_ids:
            raise ManifestError(
                f"duplicate instance_id {iid!r} at row {pos} — the "
                f"queue would run it twice")
        seen_ids.add(iid)
        for k in ("repo", "base_commit", "image", "eval_type",
                  "problem_statement"):
            v = row[k]
            if not isinstance(v, str) or not v:
                raise ManifestError(
                    f"{iid}: field {k!r} must be a non-empty string "
                    f"(got {type(v).__name__})")
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            v = row[k]
            if (not isinstance(v, list)
                    or not all(isinstance(t, str) and t for t in v)):
                raise ManifestError(
                    f"{iid}: {k} must be a list of non-empty strings "
                    f"(got {str(v)[:80]!r}) — the official harness "
                    "grades against these test lists")
        # ``PASS_TO_PASS`` may legitimately be EMPTY (measured on the
        # live split: e.g. django__django-10973 ships "[]") — a repo
        # with no regression list to carry. ``FAIL_TO_PASS`` may not:
        # ``eval_type == pass_and_fail`` promises the instance has
        # failing tests to fix, and an empty fail list would grade a
        # do-nothing patch as resolved.
        if not row["FAIL_TO_PASS"]:
            raise ManifestError(
                f"{iid}: FAIL_TO_PASS is empty while eval_type is "
                "'pass_and_fail' — nothing to fix, the instance cannot "
                "be graded")
        # the container-mode leg is the publishable grading path
        # (PLAN-WP6C §3 D1): a row without an image cannot be graded
        # there, so the manifest must not carry one silently
        if row["eval_type"] != "pass_and_fail":
            raise ManifestError(
                f"{iid}: eval_type {row['eval_type']!r} is not "
                "'pass_and_fail' — the split's grading contract changed")
        repo = row["repo"]
        per_repo[repo] = per_repo.get(repo, 0) + 1
        entry = {k: row[k] for k in keep}
        entry["repo_url"] = "https://github.com/" + repo + ".git"
        out.append(entry)
    if sorted(per_repo) != sorted(expected_per_repo):
        raise ManifestError(
            "the repo set drifted from the published Verified split: "
            f"got {sorted(per_repo)}")
    bad = {r: n for r, n in per_repo.items()
           if n != expected_per_repo[r]}
    if bad:
        raise ManifestError(
            "the per-repo counts drifted from WP0 §5 / PLAN-WP6C §2.3: "
            + ", ".join(f"{r} {per_repo[r]} (expected "
                        f"{expected_per_repo[r]})"
                        for r in sorted(bad)))
    missing_a = [i for i in PILOT_A_IDS if i not in seen_ids]
    if missing_a:
        raise ManifestError(
            f"the Pilot-A instances {missing_a} are not in the split — "
            "the overlap statement of PLAN-WP6C §13 no longer holds")
    missing_b = [i for i in pilot_b_ids if i not in seen_ids]
    if missing_b:
        raise ManifestError(
            f"{len(missing_b)} of the {len(pilot_b_ids)} Pilot-B "
            f"instances are not in the split (first few: "
            f"{missing_b[:5]}) — the pinned pilot manifest and the "
            "published split disagree")
    return out


def pilot_b_instance_ids(path: str = PILOT_B_FILE) -> "list[str]":
    """The ids of the committed Pilot-B manifest (the overlap check).
    A missing / unreadable file is an empty set: the Pilot-B manifest is
    a repo artifact, and its absence must not make the *split*
    validation unreadable — ``--check`` reports it separately."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    insts = data.get("instances") if isinstance(data, dict) else data
    if not isinstance(insts, list):
        return []
    return [i["instance_id"] for i in insts
            if isinstance(i, dict) and isinstance(i.get("instance_id"),
                                                 str)]


def build_doc(pinned: "list[dict]", *, fetched_at: str,
              keep: "tuple[str, ...]") -> dict:
    per_repo: "dict[str, int]" = {}
    for inst in pinned:
        per_repo[inst["repo"]] = per_repo.get(inst["repo"], 0) + 1
    ids = [i["instance_id"] for i in pinned]
    idset = set(ids)
    pilot_b = pilot_b_instance_ids()
    return {
        "_meta": {
            "source": ("SWE-bench/SWE-bench_Verified (test split) — the "
                       "full 500-instance queue for the WP6c run "
                       "(issue #44 E1 / issue #23)"),
            "fetched_via": ("HF datasets-server rows API (https://"
                           "datasets-server.huggingface.co/rows"
                           "?dataset=SWE-bench%2FSWE-bench_Verified"
                           "&config=default&split=test)"),
            "fetched_at": fetched_at,
            "rule": RULE,
            "total_instances": len(pinned),
            "repo_count": len(per_repo),
            "per_repo": dict(sorted(per_repo.items(),
                                    key=lambda kv: (-kv[1], kv[0]))),
            "instance_ids_sha256": _sha256_ids(ids),
            "eval_type": "pass_and_fail (asserted on every row)",
            "pinned_fields": list(keep),
            "overlap_pilot_a": [i for i in PILOT_A_IDS if i in idset],
            "overlap_pilot_b": sorted(i for i in pilot_b if i in idset),
            "note": ("pinned verbatim for the WP6c full run. "
                     "`image` is the container-mode sweb.eval image "
                     "(the publishable grading leg, PLAN-WP6C §3 D1); "
                     "`test_patch` is consumed by the sandbox-mode host "
                     "check. The overlap lists name the instances whose "
                     "sessions were already spent in WP6a/WP6b — they "
                     "are `--import-run` candidates, never a silent "
                     "re-spend. Reproducibility: "
                     "`scripts/pin_verified_instances.py --check` "
                     "re-fetches and fails on any drift except "
                     "`_meta.fetched_at`."),
        },
        "instances": pinned,
    }


def _comparable(doc: dict) -> dict:
    """The document minus the fetch *date* — the reproducibility
    comparison of ``--check`` (queue content, not the day it was read)."""
    meta = {k: v for k, v in doc.get("_meta", {}).items()
            if k != "fetched_at"}
    return {"_meta": meta, "instances": doc.get("instances")}


def check(out_path: str, doc: dict) -> "list[str]":
    """The drift report for ``--check`` (empty = reproducible)."""
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            committed = json.load(f)
    except OSError as exc:
        return [f"the committed manifest is unreadable: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"the committed manifest is not JSON: {exc}"]
    got, want = _comparable(committed), _comparable(doc)
    if got == want:
        return []
    diffs: "list[str]" = []
    gi, wi = got.get("instances") or [], want.get("instances") or []
    if [i.get("instance_id") for i in gi] != [i["instance_id"]
                                             for i in wi]:
        diffs.append(
            f"the instance list differs ({len(gi)} committed vs "
            f"{len(wi)} fetched)")
    gm, wm = got.get("_meta") or {}, want.get("_meta") or {}
    for key in sorted(set(gm) | set(wm)):
        if gm.get(key) != wm.get(key):
            diffs.append(f"_meta.{key} differs")
    if not diffs:
        diffs.append("the instance CONTENT differs (same ids) — a "
                     "pinned field changed upstream")
    return diffs


def _offline_rows(path: str) -> list:
    """A cached payload in the ``fetch_all_rows()`` shape
    (``[{"row_idx": …, "row": {…}}, …]``) for the zero-network mode."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise SystemExit(f"cached payload unreadable: {exc}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"cached payload is not JSON: {exc}")
    if not isinstance(data, list):
        raise SystemExit(
            "cached payload must be a list of API rows "
            f"(got {type(data).__name__})")
    return data


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="pin_verified_instances",
        description="pin the 500-instance SWE-bench Verified manifest "
                    "for the WP6c run (issue #44 E1)")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help="the manifest path (default: "
                        f"{DEFAULT_OUT})")
    p.add_argument("--check", action="store_true",
                   help="re-fetch and fail if the committed manifest is "
                        "not reproducible (queue content + meta, "
                        "_meta.fetched_at excluded)")
    p.add_argument("--offline", default=None,
                   help="validate a cached payload instead of fetching "
                        "(zero network — the CI/air-gapped path)")
    p.add_argument("--fetched-at", default=None,
                   help="the _meta.fetched_at value (default: today; "
                        "--check does not compare it)")
    args = p.parse_args(argv)

    pin = load_pinning_module()
    if args.offline:
        rows = _offline_rows(args.offline)
    else:
        rows = pin.fetch_all_rows()
    try:
        pinned = validate(rows, keep=pin.KEEP,
                         pilot_b_ids=pilot_b_instance_ids())
    except ManifestError as exc:
        print(f"manifest assertion FAILED: {exc}", file=sys.stderr)
        return 1
    doc = build_doc(pinned,
                   fetched_at=args.fetched_at
                   or time.strftime("%Y-%m-%d"),
                   keep=pin.KEEP)

    if args.check:
        drift = check(args.out, doc)
        if drift:
            print("DRIFT — the committed manifest is not reproducible:",
                  file=sys.stderr)
            for d in drift:
                print(f"  - {d}", file=sys.stderr)
            return 1
        print(f"reproducible: {doc['_meta']['total_instances']} "
              f"instances / {doc['_meta']['repo_count']} repos · "
              f"sha256 {doc['_meta']['instance_ids_sha256'][:16]}…")
        return 0

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"pinned {doc['_meta']['total_instances']} instances across "
          f"{doc['_meta']['repo_count']} repos -> {args.out}")
    for repo, n in doc["_meta"]["per_repo"].items():
        print(f"  {repo:28s} {n:3d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
