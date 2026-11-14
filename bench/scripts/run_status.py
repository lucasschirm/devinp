#!/usr/bin/env python3
"""WP6c pre-flight E5 (issue #44) — the read-only view of a run in flight.

A 10–50-day sequential run is 500 sessions across dozens of daily windows,
so the operator needs one command that answers "where are we, what is
still queued, and when do we finish?" **without touching the run**. This
script opens the checkpoint, the state file and the filesystem, and writes
a report. It never writes to the workdir, never starts a session and never
mutates a checkpoint line: two operators can run it while a window is in
flight (the single-live-session guard is the harness' job, not this
script's).

    windows   how many UTC windows have sessions, and how full each is
    statuses  the finals by status (the WP3 status set)
    queue     the ids with no final line yet — never attempted, or pending
              the retry class (``--resume`` is what resolves those)
    progress  the pass@1 SO FAR (terminal basis, retry class excluded —
              PLAN §2.5; the number is provisional, the label says so)
    ETA       from THIS run's measured session walls (no assumed average):
              remaining sessions / the per-window cap → windows → the
              projected finish date
    disk      the workdir size and the free space at the workdir's
              filesystem (the #23 abort rule's signal)

Exit codes: 0 = the status was printed; 2 = bad input (no checkpoint to
read). The exit code is never a verdict about the run — ``verify_submission
.py`` and the pilot record are the verdicts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

_BENCH = Path(__file__).resolve().parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

import report as rep  # noqa: E402  (WP5 — the dedupe/pass@1 contract)
import runner as rr  # noqa: E402  (WP3 — the status sets)

EXIT_OK = 0
EXIT_INPUT = 2


def find_jsonl(workdir: str, run_id: "str | None") -> "tuple[str, str]":
    """Locate the run's checkpoint. With ``--run-id`` it is exact; without
    it, a workdir holding exactly ONE ``run-*.jsonl`` is unambiguous and
    several are not (guessing would report the wrong run's progress —
    ``--resume`` refuses the same ambiguity, and for the same reason)."""
    if run_id:
        path = os.path.join(workdir, f"run-{run_id}.jsonl")
        if not os.path.isfile(path):
            raise SystemExit(f"no checkpoint at {path!r}")
        return path, run_id
    if not os.path.isdir(workdir):
        raise SystemExit(f"no workdir at {workdir!r}")
    found = sorted(f for f in os.listdir(workdir)
                  if f.startswith("run-") and f.endswith(".jsonl"))
    if not found:
        raise SystemExit(f"no run-*.jsonl in {workdir!r}")
    if len(found) > 1:
        raise SystemExit(f"{workdir!r} holds {len(found)} checkpoints "
                        f"({', '.join(found[:5])}) — pass --run-id")
    base = found[0]
    return os.path.join(workdir, base), base[4:-len(".jsonl")]


def window_stats(records: list) -> dict:
    """Sessions + wall per UTC window (the quota reset boundary). The
    ``preflight_failed`` batch markers count no session but do count wall;
    imported lines are excluded (they burned the SOURCE run's budget) —
    the same accounting ``live_pilot._window_buckets`` and checklist item
    12/23 use, so the operator's dashboard and the gating record cannot
    disagree."""
    out: dict = {}
    for r in records:
        if not r.get("instance_id"):
            continue  # a batch-level marker line
        meta = r.get("meta") or {}
        if not isinstance(meta, dict) or meta.get("imported_from"):
            continue
        ts = r.get("ts") or 0.0
        day = int(ts // 86400)
        w = out.setdefault(day, {"sessions": 0, "wall_s": 0.0})
        if r.get("status") != "preflight_failed":
            w["sessions"] += 1
        wall = meta.get("wall_s", 0.0)
        if isinstance(wall, (int, float)) and not isinstance(wall, bool):
            w["wall_s"] += float(wall)
    return out


def measure(records: list) -> dict:
    """This run's measured session walls — the ETA's only input (an
    assumed per-session cost would silently mis-date a 500-session
    finish)."""
    walls: "list[float]" = []
    for r in records:
        meta = r.get("meta") or {}
        if not isinstance(meta, dict) or meta.get("imported_from"):
            continue
        if r.get("status") == "preflight_failed":
            continue  # no session
        for key in ("wall_s",):
            v = meta.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) \
                    and v > 0:
                walls.append(float(v))
                break
    ordered = sorted(walls)
    med = None
    if ordered:
        mid = len(ordered) // 2
        med = (ordered[mid] if len(ordered) % 2
              else (ordered[mid - 1] + ordered[mid]) / 2.0)
    return {"n": len(walls), "total_s": sum(walls),
            "mean_s": (sum(walls) / len(walls)) if walls else None,
            # the ETA rests on the MEDIAN: one wedged session that hit
            # the wall cap would otherwise drag the projection out by
            # days, and item 10's calibration prints the median too
            "median_s": med}


#: the WP6a default per-window session cap — used only when nothing
#: usable is configured, and always said out loud (an ETA is a plan, and
#: a plan built on a guessed number has to say which number it guessed)
DEFAULT_CAP = 10


def usable_cap(value) -> "tuple[int, str]":
    """The ETA's per-window cap, plus the reason a configured value was
    refused (empty when it was fine).

    A ``PENDING`` placeholder, a stray string, a ``0`` and a negative
    number all mean the same thing to the operator: the number the ETA
    used is not their authorization, so all four earn the NOTE — while a
    value that was simply never configured (the normal state before B2)
    does not, or every dashboard would open with a warning nobody can
    act on (issue #44 E5/B2, reviewed).
    """
    if value is None:
        return DEFAULT_CAP, ""
    fallback = (f"the ETA below uses the WP6a default of "
                f"{DEFAULT_CAP}/window instead")
    if value is None:
        return DEFAULT_CAP, ""      # nothing configured: a quiet fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_CAP, (f"max_sessions_per_day {value!r} is not a "
                             f"number — {fallback}")
    if not math.isfinite(value):
        # json.loads accepts NaN and Infinity, and int() on them raises —
        # the read-only dashboard must not answer a bad config with exit 1
        # (issue #44 review round 2)
        return DEFAULT_CAP, (f"max_sessions_per_day {value!r} is not a "
                             f"finite number — {fallback}")
    if value <= 0:
        return DEFAULT_CAP, (f"max_sessions_per_day {value!r} is not a "
                             f"positive number — {fallback}")
    if int(value) != value:
        # 3.7 is a cap the harness itself would refuse; saying so beats
        # truncating it to 3 in silence (review r3)
        return int(value), (f"max_sessions_per_day {value!r} is not a whole "
                            f"number — the ETA below uses {int(value)}"
                            f"/window")
    return int(value), ""


def status(jsonl: str, manifest_ids: "list[str] | None",
          cap: int, now: "float | None" = None) -> dict:
    """The status dict (the human report renders exactly this, and
    ``--json`` prints it)."""
    now = time.time() if now is None else now
    records, dropped = rep.load_jsonl(jsonl)
    finals, markers = rep.dedupe(records)
    p1 = rep.pass1(finals)
    windows = window_stats(records)
    measured = measure(records)

    # the queue is the harness' OWN resume decision (WP3
    # ``resume_queue``): an id with a never-rerun final line is skipped,
    # an id with a retry-class final line or no line at all is re-run.
    # Re-deriving it here would be a second implementation of the pass@1
    # rules — the dashboard would drift from the run it describes.
    pending_retry = sorted(iid for iid, r in finals.items()
                          if r.get("status") in rr.RETRY_STATUSES)
    workdir = os.path.dirname(os.path.abspath(jsonl))
    run_id = Path(jsonl).name[4:-len(".jsonl")]
    # the state file: the previous window's flags (it carries no queue —
    # the checkpoint does), so it is reported, never trusted for the math
    state: dict = {}
    state_error = ""
    state_path = os.path.join(workdir, f"pilot-{run_id}.state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        state = loaded if isinstance(loaded, dict) else {}
    except OSError:
        pass  # no state file yet is normal (before the first window)
    except (json.JSONDecodeError, AttributeError) as exc:
        state_error = f"{type(exc).__name__}: {exc}"
    flags = {k: state[k] for k in ("final_state", "interrupted",
                                  "window_cap", "aborted")
            if k in state}

    queue: "list[str]" = []
    skipped: "dict[str, str]" = {}
    unknown: "list[str]" = []
    queue_error = ""
    if manifest_ids is not None:
        try:
            to_run, skipped_map = rr.resume_queue(list(manifest_ids),
                                                 records)
            queue = list(to_run)
            skipped = {k: (v if isinstance(v, str) else "?")
                       for k, v in (skipped_map or {}).items()}
        except rr.RunnerError as exc:
            queue_error = f"{type(exc).__name__}: {exc}"
        unknown = sorted(set(finals) - set(manifest_ids))

    by_status: "dict[str, int]" = {}
    for r in finals.values():
        by_status[r.get("status", "?")] = by_status.get(
            r.get("status", "?"), 0) + 1

    remaining = len(queue) if manifest_ids is not None else 0
    eta: dict = {"remaining": remaining, "windows": None,
                "finish_date": None, "basis": "no measurement yet"}
    if manifest_ids is not None and remaining:
        eta["windows"] = -(-remaining // max(1, cap))
        if measured["median_s"]:
            eta["session_hours"] = round(
                remaining * measured["median_s"] / 3600.0, 1)
            eta["basis"] = (f"median measured session wall "
                           f"{measured['median_s']:.0f}s over "
                           f"{measured['n']} session(s)"
                           + (f" (mean {measured['mean_s']:.0f}s — the "
                              "median ignores a wedged session's "
                              "wall-cap outlier)"
                              if measured["n"] >= 3 else ""))
        # one window per UTC day is the quota ceiling, so the date
        # projection is windows, not hours
        eta["finish_date"] = time.strftime(
            "%Y-%m-%d", time.gmtime(now + eta["windows"] * 86400))
    used = sum(w["sessions"] for w in windows.values())
    return {
        "run_id": run_id,
        "jsonl": jsonl,
        "workdir": workdir,
        "lines": len(records),
        # the WP5 loader reports a torn/dropped line as a flag, not a
        # count — the operator only needs "the file is not clean"
        "dropped_line": bool(dropped),
        "markers": len(markers),
        "distinct": len(finals),
        "skipped": len(skipped),
        "skipped_by_status": _tally(skipped.values()),
        "state_error": state_error,
        "queue_error": queue_error,
        "state_flags": flags,
        "by_status": dict(sorted(by_status.items(),
                                key=lambda kv: (-kv[1], kv[0]))),
        "terminal": p1["overall"]["terminal"],
        "resolved": p1["overall"]["resolved"],
        "pass1_pct": p1["overall"]["pct"],
        "pending_retry": pending_retry,
        "windows": {_day_str(d): v for d, v in sorted(windows.items())},
        "windows_used": len(windows),
        "sessions_total": used,
        "cap_per_window": cap,
        "queue": queue,
        "queue_count": len(queue),
        "outside_manifest": unknown,
        "manifest_total": (len(manifest_ids)
                          if manifest_ids is not None else None),
        "measure": measured,
        "eta": eta,
        "last_line": _last(records),
    }


def _tally(values) -> "dict[str, int]":
    out: "dict[str, int]" = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _day_str(day: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day * 86400))


def _last(records: list) -> "dict | None":
    for r in reversed(records):
        if r.get("instance_id"):
            meta = r.get("meta") or {}
            return {"instance_id": r["instance_id"],
                   "status": r.get("status"),
                   "ts": r.get("ts"),
                   "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(r.get("ts") or 0)),
                   "wall_s": (meta.get("wall_s")
                             if isinstance(meta, dict) else None)}
    return None


def disk_view(workdir: str) -> dict:
    """The size of the artifact directory + the free space of the
    filesystem holding it (the #23 abort rule watches both)."""
    total = 0
    files = 0
    for root, _dirs, names in os.walk(workdir):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
                files += 1
            except OSError:
                pass
    try:
        free = shutil.disk_usage(workdir).free
    except OSError:
        free = None
    return {"workdir_bytes": total, "files": files, "free_bytes": free}


def render(s: dict, disk: "dict | None") -> str:
    out = [f"# run {s['run_id']} — {s['jsonl']}", ""]
    if s["dropped_line"]:
        out.append("WARNING: a malformed line was dropped reading the "
                  "checkpoint (the torn-last-line class of a SIGKILL — "
                  "that instance's outcome is unknown and --resume "
                  "re-runs it)")
    out += [
        f"- lines {s['lines']} · distinct {s['distinct']} · batch "
        f"markers {s['markers']}",
        f"- skip set (what --resume would skip): {s['skipped']} "
        f"({_fmt_tally(s['skipped_by_status'])})",
    ]
    if s.get("state_error"):
        out.append(f"WARNING: the state file is unreadable "
                  f"({s['state_error']}) — the queue below is derived "
                  "from the checkpoint alone")
    if s.get("queue_error"):
        out.append(f"WARNING: the queue could not be derived "
                  f"({s['queue_error']}) — the checkpoint holds a status "
                  "outside the WP3 status set")
    fl = s["state_flags"]
    if fl:
        out.append(f"- last window: {fl.get('final_state', 'unknown')}"
                  + (" (interrupted)" if fl.get("interrupted") else "")
                  + (" (window cap)" if fl.get("window_cap") else "")
                  + (" (ABORTED)" if fl.get("aborted") else ""))
    out += [
        f"- finals by status: {_fmt_tally(s['by_status'])}",
        f"- pass@1 SO FAR (terminal basis, retry class excluded — "
        f"PLAN §2.5, provisional until the queue is empty): "
        f"{s['resolved']}/{s['terminal']} ({s['pass1_pct']})",
        f"- pending retry class (--resume re-runs these): "
        f"{len(s['pending_retry'])}"
        + (f" ({', '.join(s['pending_retry'][:5])}"
           f"{'…' if len(s['pending_retry']) > 5 else ''})"
           if s["pending_retry"] else ""),
        f"- windows used: {s['windows_used']} (cap "
        f"{s['cap_per_window']}/window; "
        f"{s['sessions_total']} session(s) total)",
    ]
    for day, w in s["windows"].items():
        out.append(f"    {day}: {w['sessions']} session(s), "
                  f"{w['wall_s'] / 3600:.1f} h wall")
    if s["manifest_total"] is None:
        out.append("- queue: no manifest given (pass --manifest for the "
                  "completeness view)")
    else:
        out.append(f"- queue: {s['queue_count']} of {s['manifest_total']} "
                  "manifest instance(s) still need a terminal outcome")
    if s["outside_manifest"]:
        out.append(f"WARNING: {len(s['outside_manifest'])} final id(s) "
                  "are outside the manifest: "
                  f"{', '.join(s['outside_manifest'][:5])}")
    eta = s["eta"]
    if eta["windows"] is None:
        out.append(f"- ETA: {eta['basis']}")
    else:
        out.append(f"- ETA: {eta['windows']} more window(s) → "
                  f"{eta['finish_date']} ({eta['basis']}"
                  + (f"; ≈{eta['session_hours']} h of sessions"
                     if eta.get("session_hours") else "") + ")")
    if s["last_line"]:
        l = s["last_line"]
        out.append(f"- last line: {l['instance_id']} → {l['status']} at "
                  f"{l['utc']}"
                  + (f" ({l['wall_s']:.0f}s)"
                     if isinstance(l["wall_s"], (int, float)) else ""))
    else:
        out.append("- last line: (none — the run has not started)")
    if disk:
        out.append(f"- disk: {_mb(disk['workdir_bytes'])} in "
                  f"{s['workdir']} ({disk['files']} files)"
                  + (f"; {_mb(disk['free_bytes'])} free on that filesystem"
                     if disk["free_bytes"] is not None else ""))
    return "\n".join(out)


def _mb(n: "float | None") -> str:
    if n is None:
        return "unknown"
    return f"{n / 1e6:.0f} MB"


def _fmt_tally(tally: dict) -> str:
    if not tally:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in sorted(
        tally.items(), key=lambda kv: (-kv[1], str(kv[0]))))


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(
        prog="run_status",
        description="read-only status of a WP6c run in flight "
                    "(issue #44 E5)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--workdir", help="the run workdir (the checkpoint is "
                                    "run-<id>.jsonl in it)")
    g.add_argument("--jsonl", help="the checkpoint directly")
    p.add_argument("--run-id", default=None,
                   help="which checkpoint of the workdir (required when "
                        "it holds more than one)")
    p.add_argument("--manifest", default=str(_BENCH /
                                            "verified_instances.json"),
                   help="the pinned manifest for the queue view ('' to "
                        "skip)")
    p.add_argument("--cap", type=int, default=None,
                   help="sessions per window for the ETA (default: the "
                        "authorized budget file)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    args = p.parse_args(argv)

    if args.jsonl:
        jsonl = args.jsonl
        run_id = Path(jsonl).name[4:-len(".jsonl")]
    else:
        jsonl, run_id = find_jsonl(args.workdir, args.run_id)
    if not os.path.isfile(jsonl):
        print(f"INPUT: no checkpoint at {jsonl!r}", file=sys.stderr)
        return EXIT_INPUT
    cap = args.cap
    manifest_ids = None
    if args.manifest:
        if not os.path.isfile(args.manifest):
            print(f"INPUT: no manifest at {args.manifest!r} (pass "
                  "--manifest '' to skip the queue view)",
                  file=sys.stderr)
            return EXIT_INPUT
        try:
            with open(args.manifest, "r", encoding="utf-8") as f:
                doc = json.load(f)
            manifest_ids = rep.manifest_ids(doc)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # every queue number on the dashboard comes from this file;
            # a wrong shape must stop the report, not corrupt it
            print(f"INPUT: {args.manifest!r} is not an instance manifest "
                  f"(an object with an \"instances\" list of objects "
                  f"carrying instance_id): {exc}", file=sys.stderr)
            return EXIT_INPUT
    source = "--cap" if cap is not None else "live_pilot_budget.json"
    why_read = ""
    if cap is None:
        # the operator's authorized per-window cap, not the pilot default
        # — a 500-run ETA computed from a 10-session pilot cap would be
        # wrong by the ratio of the two
        try:
            import live_pilot as lp
            cap = lp.load_budget_file(lp.BUDGET_FILE).get(
                "max_sessions_per_day")
        except (Exception, SystemExit) as exc:
            # a budget loader that refuses loudly must still leave the
            # operator a dashboard — but a file they DID configure and
            # that cannot be read belongs with PENDING/0/-5: the ETA is
            # about to use a number that is not theirs, so it says so,
            # and says which file to fix (issue #44 review round 2)
            cap = None
            why_read = ("the budget file's max_sessions_per_day cannot be "
                        f"read ({type(exc).__name__}: {exc})")
    cap, why = usable_cap(cap)
    why = why or why_read
    if why:
        print(f"NOTE: {why} (fix {source}, issue #44 B2)",
              file=sys.stderr)
    try:
        s = status(jsonl, manifest_ids, cap)
    except rep.ReportError as exc:
        # a mid-file corrupt line is fail-loud in the WP5 loader: say so
        # rather than printing a partial dashboard
        print(f"INPUT: {jsonl} is not readable as a checkpoint: {exc}",
              file=sys.stderr)
        return EXIT_INPUT
    if args.json:
        print(json.dumps({**s, "disk": disk_view(os.path.dirname(
            os.path.abspath(jsonl)))}, indent=2))
        return EXIT_OK
    print(render(s, disk_view(os.path.dirname(os.path.abspath(jsonl)))))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
