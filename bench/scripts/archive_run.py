#!/usr/bin/env python3
"""WP6c pre-flight B4 (issue #44) — make a window's artifacts durable.

Pilot B's evidence gap (issue #22, closed by an operator waiver) happened
because the artifacts lived only inside the workspace that produced them
and were deleted after the merge. The 500-instance run cannot afford that:
its artifacts are the submission, and it produces them over 10–50 days in a
directory an agent session can easily clean.

This script snapshots a workdir **off the run path**, content-addressed:

    <dest>/<run_id>/windows/<UTC>/MANIFEST.tsv    sha256 · bytes · path
    <dest>/<run_id>/windows/<UTC>/…               the artifacts themselves
    <dest>/<run_id>/LATEST.tsv                    the PATH of the newest
                                                  window's manifest

and then **verifies the copy** (a backup nobody re-reads is a rumor — the
same rule WP5's R1/R2 checks apply to the patch artifacts). A window's
bundle is never overwritten: the name carries the UTC timestamp, so the
history is append-only and a later bad copy cannot destroy an earlier good
one. ``--verify`` re-reads an existing bundle against its own manifest
before the next window starts, and at any time afterwards.

What it copies (everything the run wrote, nothing it did not):

    run-<id>.jsonl               the checkpoint (the source of truth)
    pilot-<id>.state.json        the resume/interrupt bookkeeping
    report/**                    report.md · all_preds.jsonl · trajs/ ·
                                 run_meta.json (the submission artifacts)
    runs/**                      the per-instance patch.diff · trajectory
                                 .jsonl · eval.log (the WP5 references)
    wp6c-full-run.md / wp6b-pilot-b.md / wp6a-go-no-go.md   the records

Usage:
  python archive_run.py --workdir full-run --run-id full500 \
      --dest /mnt/offhost/devinp            # snapshot + verify
  python archive_run.py --bundle /mnt/offhost/devinp/full500/windows/<UTC> \
      --verify                             # re-verify an existing bundle

Exit codes: 0 = archived and verified (or verified); 2 = bad input;
3 = the verification failed (the bundle is NOT a durable copy).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_VERIFY = 3

MANIFEST_NAME = "MANIFEST.tsv"
LATEST_NAME = "LATEST.tsv"
#: the run's own files, by name pattern — anything else in the workdir (a
#: stale clone, an editor swap file) is not the run's evidence and is not
#: copied
INCLUDE_DIRS = ("report", "runs")
RECORD_NAMES = ("wp6c-full-run.md", "wp6b-pilot-b.md", "wp6a-go-no-go.md")
SKIP_NAMES = (".lock", "pilot.lock")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(workdir: str, run_id: str) -> "list[str]":
    """The run's own files, relative to ``workdir``, sorted (a manifest is
    only comparable across windows if the walk is deterministic)."""
    if not os.path.isdir(workdir):
        raise SystemExit(f"no workdir at {workdir!r}")
    rels = [f"run-{run_id}.jsonl", f"pilot-{run_id}.state.json",
            *RECORD_NAMES]
    out = []
    for rel in rels:
        p = os.path.join(workdir, rel)
        if os.path.isfile(p):
            out.append(rel)
    for d in INCLUDE_DIRS:
        root = os.path.join(workdir, d)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, names in os.walk(root):
            for n in sorted(names):
                if n in SKIP_NAMES or n.endswith(".tmp"):
                    continue
                full = os.path.join(dirpath, n)
                if not os.path.isfile(full):
                    continue  # a socket / a fifo is not evidence
                out.append(os.path.relpath(full, workdir))
    return sorted(set(out))


def entries(workdir: str, rels: "list[str]") -> "list[tuple[str, int, str, float]]":
    """``(sha256, bytes, relpath, mtime)`` per archived file — the tuple
    the manifest is rendered from and the copy is checked against."""
    out = []
    for rel in rels:
        full = os.path.join(workdir, rel)
        st = os.stat(full)
        out.append((_sha256_file(full), st.st_size, rel, st.st_mtime))
    return out


def manifest_lines(ents: "list[tuple[str, int, str, float]]") -> "list[str]":
    lines = ["# sha256\tbytes\tpath\tmtime_utc"]
    for sha, size, rel, mtime in ents:
        lines.append(
            f"{sha}\t{size}\t{rel}\t"
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(mtime))}")
    return lines


def read_manifest(path: str) -> "list[tuple[str, int, str]]":
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                raise SystemExit(f"{path!r}: malformed manifest line")
            try:
                size = int(parts[1])
            except ValueError as exc:
                # a corrupt manifest is an input problem with a name,
                # not a traceback in the middle of a --verify
                raise SystemExit(
                    f"{path!r}: malformed byte count {parts[1]!r} for "
                    f"{parts[2]!r}") from exc
            out.append((parts[0], size, parts[2]))
    return out


def archive(workdir: str, run_id: str, dest: str,
           stamp: "str | None" = None) -> dict:
    workdir = os.path.abspath(workdir)
    dest = os.path.abspath(dest)
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bundle = os.path.join(dest, run_id, "windows", stamp)
    if os.path.exists(bundle):
        # an append-only history: a same-second re-run gets its own suffix
        # rather than writing over a bundle somebody may already verify
        n = 1
        while os.path.exists(f"{bundle}.{n}"):
            n += 1
        bundle = f"{bundle}.{n}"
    rels = collect(workdir, run_id)
    if not rels:
        raise SystemExit(f"{workdir!r} holds no run-{run_id}.* artifacts")
    ents = entries(workdir, rels)
    os.makedirs(bundle, exist_ok=True)
    total = 0
    for sha, size, rel, _mtime in ents:
        dst = os.path.join(bundle, rel)
        os.makedirs(os.path.dirname(dst) or bundle, exist_ok=True)
        # copy-then-rename: a window killed mid-archive leaves a .part, and
        # verify() never mistakes a partial file for an archived one
        tmp = dst + ".part"
        with open(tmp, "wb") as w, open(os.path.join(workdir, rel),
                                        "rb") as r:
            shutil.copyfileobj(r, w)
        actual = os.path.getsize(tmp)
        if actual != size:
            # keep the torn file: it is the evidence of where the write
            # stopped (the shape a full destination takes)
            os.replace(tmp, dst + ".TORN")
            raise SystemExit(f"copy truncated: {rel} ({actual} of "
                            f"{size} bytes written)")
        os.replace(tmp, dst)
        total += size
    lines = manifest_lines(ents)
    manifest = os.path.join(bundle, MANIFEST_NAME)
    with open(manifest, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    latest = os.path.join(dest, run_id, LATEST_NAME)
    with open(latest, "w", encoding="utf-8", newline="") as f:
        f.write(manifest + "\n")
    return {"bundle": bundle, "manifest": manifest, "files": len(ents),
           "bytes": total}


def verify(bundle: str) -> "list[str]":
    """Re-hash every manifest entry against the bundle. Returns the
    problems (empty = the bundle is byte-identical to what was archived).
    A file present in the bundle but absent from the manifest is a problem
    too: an unaccounted artifact cannot be trusted as evidence."""
    mpath = os.path.join(bundle, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        return [f"no {MANIFEST_NAME} in {bundle!r}"]
    problems: "list[str]" = []
    listed = set()
    for sha, size, rel in read_manifest(mpath):
        listed.add(rel)
        full = os.path.join(bundle, rel)
        if not os.path.isfile(full):
            problems.append(f"{rel}: absent from the bundle")
            continue
        if os.path.getsize(full) != size:
            problems.append(f"{rel}: size {os.path.getsize(full)} != "
                           f"manifest {size}")
            continue
        if _sha256_file(full) != sha:
            problems.append(f"{rel}: sha256 differs from the manifest")
    for dirpath, _dirs, names in os.walk(bundle):
        for n in names:
            rel = os.path.relpath(os.path.join(dirpath, n), bundle)
            if rel == MANIFEST_NAME or n.endswith(".part"):
                continue
            if rel not in listed:
                problems.append(f"{rel}: in the bundle but not the "
                               "manifest")
    return problems


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="archive_run",
        description="snapshot + verify a WP6c window's artifacts off the "
                    "run path (issue #44 B4)")
    p.add_argument("--workdir", help="the run workdir")
    p.add_argument("--run-id", default=None,
                   help="which run (default: the single run-*.jsonl)")
    p.add_argument("--dest", help="the durable destination directory")
    p.add_argument("--stamp", default=None,
                   help="the window label (default: now, UTC)")
    p.add_argument("--bundle", default=None,
                   help="with --verify: an existing bundle to re-verify")
    p.add_argument("--verify", action="store_true",
                   help="verify --bundle instead of archiving")
    p.add_argument("--dry-run", action="store_true",
                   help="print the file list + the total, copy nothing")
    args = p.parse_args(argv)

    if args.verify:
        if not args.bundle:
            print("INPUT: --verify needs --bundle", file=sys.stderr)
            return EXIT_INPUT
        try:
            problems = verify(args.bundle)
            ents = read_manifest(os.path.join(args.bundle, MANIFEST_NAME))
        except SystemExit as exc:
            # a MANIFEST.tsv that cannot be parsed is BAD INPUT (exit 2),
            # which is a different statement from "this bundle is not
            # byte-identical" (exit 3): one means fix the invocation or
            # the file, the other means the backup is wrong
            print(f"INPUT: {exc}", file=sys.stderr)
            return EXIT_INPUT
        if problems:
            for x in problems[:20]:
                print(f"[FAIL] {x}")
            print(f"NOT DURABLE: {len(problems)} problem(s) in "
                  f"{args.bundle}", file=sys.stderr)
            return EXIT_VERIFY
        print(f"VERIFIED {args.bundle}: {len(ents)} file(s), "
              f"{sum(e[1] for e in ents)} bytes, byte-identical to the "
              "manifest")
        return EXIT_OK

    if not args.workdir or not args.dest:
        print("INPUT: --workdir and --dest are required (or use "
              "--verify --bundle)", file=sys.stderr)
        return EXIT_INPUT
    run_id = args.run_id
    if not run_id:
        found = sorted(f for f in os.listdir(args.workdir)
                      if f.startswith("run-") and f.endswith(".jsonl")
                      ) if os.path.isdir(args.workdir) else []
        if len(found) != 1:
            print(f"INPUT: cannot derive the run id from "
                  f"{args.workdir!r} ({len(found)} checkpoints) — pass "
                  "--run-id", file=sys.stderr)
            return EXIT_INPUT
        run_id = found[0][4:-len(".jsonl")]
    try:
        rels = collect(args.workdir, run_id)
    except SystemExit as exc:
        print(f"INPUT: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.dry_run:
        total = sum(os.path.getsize(os.path.join(args.workdir, r))
                   for r in rels)
        for r in rels:
            print(r)
        print(f"{len(rels)} file(s), {total} bytes → "
              f"{os.path.abspath(args.dest)}/{run_id}/windows/<utc>")
        return EXIT_OK
    try:
        res = archive(args.workdir, run_id, args.dest, stamp=args.stamp)
    except SystemExit as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_VERIFY
    except OSError as exc:
        # a destination that cannot be written (not a directory, read-only,
        # out of space) is the failure B4 exists to make loud
        print(f"FAILED: cannot write {args.dest!r}: {exc}", file=sys.stderr)
        return EXIT_VERIFY
    problems = verify(res["bundle"])
    if problems:
        for x in problems[:20]:
            print(f"[FAIL] {x}")
        print(f"NOT DURABLE: the fresh bundle failed its own verification "
              f"({len(problems)} problem(s))", file=sys.stderr)
        return EXIT_VERIFY
    print(f"archived {res['files']} file(s), {res['bytes']} bytes → "
          f"{res['bundle']} (verified)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
