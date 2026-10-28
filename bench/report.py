"""``report.py`` — WP5 (issue #18): aggregation, failure taxonomy,
methodology block, and the leaderboard artifacts.

Plan of record: ``bench/PLAN.md`` §5 WP5 · implementation plan:
``bench/PLAN-WP5.md``. Consumes the WP3 checkpoint file
(``run-<id>.jsonl`` — the WP3b schema, read **by import** from
``runner.load_checkpoint`` so the read semantics — a torn *last* line
drops not fatal, a corrupted *mid-file* line fails loud — have one
source of truth) and produces:

* the **markdown report** (``report.md``) — pass@1 overall + per-repo
  table, the failure taxonomy, the methodology block, the artifact
  inventory (PLAN §10.4 "the markdown report with the methodology
  block");
* the **leaderboard artifacts** — ``all_preds.jsonl`` (one line per
  deduped instance, exactly the harness's standard predictions fields
  ``instance_id`` / ``model_name_or_path`` / ``model_patch`` — WP4
  verified those against the pinned ``swebench==5.0.2``; a
  no-patch instance carries ``model_patch: ""`` under the harness's own
  empty-patch semantics, so it can never be misread as resolved) and
  ``trajs/<instance_id>.jsonl`` (byte-for-byte copies of the
  per-instance action trajectories, PLAN §10.4);
* the **artifact cross-check** (AC5): every JSONL line's referenced
  artifact files exist (plus the semantic rule — a ``resolved`` /
  ``failed_test`` final status must reference an existing
  ``patch.diff``), and ``all_preds.jsonl``'s patch content matches
  ``patch.diff`` byte-for-byte. Any violation is a typed
  :class:`ArtifactCheckError` (the report is *not* trusted; CLI exit 3).

**pass@1 semantics (pinned by the tests, PLAN §2.5).** Dedup is the
WP3b resume rule — a duplicated line resolves to the **last** line
(file order, not timestamp). The denominator is the set of *legitimate
agent outcomes* = ``TERMINAL_STATUSES − RETRY_STATUSES`` =
``{resolved, failed_test, timeout, no_patch, agent_error}`` (``timeout``
/ ``no_patch`` are legitimate agent outcomes — re-running them "would
turn pass@1 into a filtered pass@k"; ``agent_error`` is a terminal
agent outcome, README WP3b). The retry class
(``quota_limited`` / ``eval_error`` / ``preflight_failed``) is
*excluded* from the pass@1 denominator (the outcome is undecided — the
instance is pending retry) but counted in the failure taxonomy. A
``instance_id: null`` line is a **batch marker** (a pre-flight failure
before the first instance, WP3b) — it is counted in the taxonomy
section, never as an instance record.

**The per-instance ``meta`` contract (defined here; the adapter WP
conforms to it).**

    {
      "artifacts_dir": str,       # optional; base dir for relative
                                  #   references (default: the run
                                  #   JSONL's directory)
      "patch_diff":  str | null,  # path to the instance's patch.diff
      "trajectory":  str | null,  # path to its trajectory.jsonl
      "eval_log":    str | null,  # path to its eval.log
      "usage":       dict,        # opaque; carried into the report, not
                                  #   interpreted (WP0 §2.6 proxies or
                                  #   ACP usage.{inputTokens,…})
    }

A ``null``/absent reference = "no such artifact". A non-null reference
**must** resolve to an existing file (the cross-check, fail-loud).

**The run-level methodology meta** is a separate operator file
(``--meta-file`` — the WP3 checkpoint writes instance lines only;
run-level facts are the operator's record):

    {"run_id", "devin_cli_version", "model", "cost_tier",
     "prompt_template_hash", "permission_policy",
     "caps": {"max_sessions_per_day", "max_wall_per_session_s",
              "daily_pause_h"},
     "harness_version", "images", "model_name_or_path"?}

Two methodology fields are **computed from the JSONL** (exact): the
**date range** (min/max ``ts`` over all lines, ISO-8601 UTC) and
**sessions used** = the count of lines with ``instance_id ≠ null`` and
``status ≠ "preflight_failed"`` (a pre-flight failure stops *before* a
session starts — PLAN §2.7 "stop before burning a session"; a
``quota_limited`` line is a started-then-refused attempt and counts; a
resume re-run counts its second attempt — the figure is *session
attempts*). A missing meta file / field renders ``unknown`` — the
methodology block never silently omits a field.

**Typed errors + exit codes** (the repo's fail-loud contract, mirroring
``evaluate.py``): :class:`InvalidInputError` (bad flag / unreadable /
non-JSON meta file; exit 2), :class:`ReportProtocolError` (a run-JSONL
schema violation — wraps the runner's ``RunnerProtocolError``; exit 4),
:class:`ArtifactCheckError` (an AC5 violation; exit 3).

    python3 report.py --jsonl run-001.jsonl --out out/ [--meta-file m.json]
    python3 report.py --jsonl run-001.jsonl --out out/ --check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Final, Sequence

from runner import (
    RETRY_STATUSES,
    TERMINAL_STATUSES,
    RunnerProtocolError,
    load_checkpoint,
)

__all__ = [
    "TAXONOMY_ORDER",
    "PASS1_OUTCOMES",
    "InvalidInputError",
    "ReportError",
    "ReportProtocolError",
    "ArtifactCheckError",
    "repo_of",
    "dedupe",
    "pass1",
    "taxonomy",
    "sessions_used",
    "date_range",
    "crosscheck",
    "check_artifacts",
    "write_artifacts",
    "methodology",
    "render_report",
    "generate",
    "check",
    "main",
]

# The failure taxonomy in the issue's listed order (AC2) — zeros
# included, so the table is complete for a partial (pilot) run.
TAXONOMY_ORDER: Final[tuple] = (
    "resolved",
    "failed_test",
    "timeout",
    "no_patch",
    "agent_error",
    "quota_limited",
    "eval_error",
    "preflight_failed",
)

#: The pass@1 *outcome* set — the legitimate agent outcomes (PLAN §2.5):
#: every terminal status minus the only retry class. A ``timeout``
#: (45-min cap) and a ``no_patch`` (the agent finished without edits)
#: are legitimate agent outcomes — re-running them would turn pass@1
#: into a filtered pass@k. The retry class is *excluded* from the
#: denominator (the outcome is undecided — pending retry).
PASS1_OUTCOMES: Final[frozenset] = frozenset(
    TERMINAL_STATUSES - RETRY_STATUSES
)

#: The meta keys whose non-null values are file references (AC5 R1).
_REFERENCE_KEYS: Final[tuple] = ("patch_diff", "trajectory", "eval_log")


# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class ReportError(Exception):
    """Base class for every typed failure the report can produce."""


class InvalidInputError(ReportError):
    """A bad CLI flag, an unreadable input, or a non-JSON / malformed
    methodology meta file (CLI exit 2)."""


class ReportProtocolError(ReportError):
    """A run-JSONL schema violation (CLI exit 4) — wraps the runner's
    ``RunnerProtocolError``: a corrupted *mid-file* line is not a kill
    artifact and fails loud (the WP3b reader contract)."""


class ArtifactCheckError(ReportError):
    """An AC5 cross-check violation (CLI exit 3): a referenced artifact
    file is missing, a graded verdict (``resolved`` / ``failed_test``)
    has no existing ``patch.diff``, or ``all_preds.jsonl``'s patch
    content does not match ``patch.diff``. The report is *not*
    trusted."""


def _raise_on_missing(path: str, what: str) -> None:
    if not os.path.exists(path):
        raise InvalidInputError(f"{what} does not exist: {path!r}")


def _raise_on_bad_status(status: object) -> None:
    if status not in TERMINAL_STATUSES:
        raise ReportProtocolError(
            f"unknown status {status!r} (valid: {sorted(TERMINAL_STATUSES)})"
        )


# ---------------------------------------------------------------------------
# the WP3 JSONL → the report's view of the run
# ---------------------------------------------------------------------------


def load_jsonl(path: str) -> "tuple[list, bool]":
    """Read the run JSONL ``run-<id>.jsonl`` → ``(records, dropped)``
    via the WP3b reader (imported — one source of truth for the schema
    and its torn-EOF rule): a torn **last** line is dropped, not fatal;
    a corrupted **mid-file** line raises (→ :class:
    `ReportProtocolError`, fail-loud); a missing file is a fresh run (no
    records)."""
    try:
        return load_checkpoint(path)
    except RunnerProtocolError as e:
        raise ReportProtocolError(str(e)) from e


def dedupe(records: "Sequence[dict]") -> "tuple[dict, list]":
    """The WP3b resume rule, applied to the report's view: a
    duplicated ``instance_id`` resolves to the **last** line (file
    order, *not* timestamp — a resumed run appends its retry line
    after the superseded one). Returns ``(finals, markers)``:
    ``finals`` maps ``instance_id → record``; ``markers`` is the list of
    ``instance_id: null`` batch-level lines (they are counted in the
    taxonomy section — never as instance records, and they can never
    shadow or be shadowed by an instance line)."""
    finals: "dict[str, dict]" = {}
    markers: "list[dict]" = []
    for rec in records:
        _raise_on_bad_status(rec.get("status"))
        iid = rec.get("instance_id")
        if iid is None:
            markers.append(rec)
        elif isinstance(iid, str) and iid:
            finals[iid] = rec  # the last line wins
        else:
            raise ReportProtocolError(
                f"instance_id must be a non-empty string or null "
                f"(got {iid!r})"
            )
    return finals, markers


def repo_of(instance_id: str) -> str:
    """The repo key of a SWE-bench Verified instance id (fact: the id
    is ``<owner>__<repo>-<number>`` — e.g. ``astropy__astropy-12907``;
    the number is always the last dash segment, so the prefix before
    the **last** ``-`` is the repo even for dash-bearing repo names —
    ``scikit__scikit-learn-99`` → ``scikit__scikit-learn``). An id
    with no ``-`` is its own repo key (synthetic fixtures)."""
    if not isinstance(instance_id, str) or not instance_id:
        raise ReportProtocolError(
            f"instance_id must be a non-empty string (got {instance_id!r})"
        )
    idx = instance_id.rfind("-")
    if idx <= 0:
        return instance_id
    return instance_id[:idx]


def _parse_ts(value: object) -> float:
    """The ``ts`` field is a float epoch (WP3b ``JsonlCheckpoint``) —
    a non-numeric ``ts`` is a schema violation (fail-loud)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportProtocolError(f"ts must be a number (got {value!r})")
    return float(value)


def date_range(records: "Sequence[dict]") -> "tuple[str, str] | None":
    """The methodology date range: min/max ``ts`` over **all** lines
    (batch markers included — they are part of the run), ISO-8601 UTC
    at second precision; ``None`` for an empty run."""
    if not records:
        return None
    stamps = [_parse_ts(r.get("ts")) for r in records]
    lo, hi = min(stamps), max(stamps)
    fmt = lambda s: datetime.fromtimestamp(s, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return fmt(lo), fmt(hi)


def sessions_used(records: "Sequence[dict]") -> int:
    """**Sessions used** = the count of *session attempts*: lines with
    ``instance_id ≠ null`` and ``status ≠ "preflight_failed"``. A
    pre-flight failure stops **before** a session starts (PLAN §2.7 —
    "stop before burning a session" — its re-run is free); a
    ``quota_limited`` line is a started-then-refused attempt and
    counts; a resume re-run is a second attempt and counts a second
    time. (Per-instance lines only — a batch marker never started a
    session.)"""
    n = 0
    for rec in records:
        if rec.get("instance_id") is None:
            continue
        if rec.get("status") == "preflight_failed":
            continue
        _raise_on_bad_status(rec.get("status"))
        n += 1
    return n


# ---------------------------------------------------------------------------
# AC1 — pass@1 overall + per-repo
# ---------------------------------------------------------------------------


def pass1(finals: "dict[str, dict]") -> "dict":
    """pass@1 over the **deduped** final statuses:

    * overall: ``resolved`` / *terminal*, where *terminal* =
      :data:`PASS1_OUTCOMES` (the legitimate agent outcomes); the
      retry-class finals are **excluded** from numerator and
      denominator (undecided — pending retry, PLAN §2.5);
    * per-repo: the same formula per :func:`repo_of` group; a repo
      with zero terminal instances is ``n/a`` (never 0%, never a
      division by zero).

    Returns ``{"overall": {resolved, terminal, rate, pct} (rate None
    when terminal == 0), "per_repo": {repo: {resolved, terminal,
    rate, pct}}}`` — ``pct`` is the formatted ``"40.0%"`` (``"n/a"``
    for a zero-terminal group)."""
    per: "dict[str, dict]" = {}
    for iid in sorted(finals):
        rec = finals[iid]
        _raise_on_bad_status(rec.get("status"))
        st = rec["status"]
        repo = repo_of(iid)
        cell = per.setdefault(repo, {"resolved": 0, "terminal": 0})
        if st in PASS1_OUTCOMES:
            cell["terminal"] += 1
            if st == "resolved":
                cell["resolved"] += 1
    for cell in per.values():
        if cell["terminal"] == 0:
            cell["rate"] = None
            cell["pct"] = "n/a"
        else:
            cell["rate"] = cell["resolved"] / cell["terminal"]
            cell["pct"] = f"{cell['rate'] * 100:.1f}%"
    tot_res = sum(c["resolved"] for c in per.values())
    tot_ter = sum(c["terminal"] for c in per.values())
    overall = {
        "resolved": tot_res,
        "terminal": tot_ter,
        "rate": (tot_res / tot_ter) if tot_ter else None,
        "pct": (f"{tot_res / tot_ter * 100:.1f}%" if tot_ter else "n/a"),
    }
    return {"overall": overall, "per_repo": per}


def taxonomy(finals: "dict[str, dict]", markers: "list[dict]") -> "dict":
    """The failure taxonomy (AC2) over the **deduped** final statuses —
    all 8 statuses, zeros included, in :data:`TAXONOMY_ORDER` — plus
    ``markers``: the count of ``instance_id: null`` batch-level lines."""
    counts = {s: 0 for s in TAXONOMY_ORDER}
    for iid in sorted(finals):
        st = finals[iid].get("status")
        _raise_on_bad_status(st)
        counts[st] += 1
    return {"by_status": counts, "markers": len(markers)}


# ---------------------------------------------------------------------------
# AC5 — the artifact cross-check (R1: references exist; R2: content
# match — R2 is ``check_artifacts``, run after the write)
# ---------------------------------------------------------------------------


def _ref_path(rec: "dict", base_dir: "str | os.PathLike") -> "dict[str, str | None]":
    """The instance's referenced artifact paths, resolved: relative
    references resolve against ``meta.artifacts_dir`` when present,
    else against ``base_dir`` (the run JSONL's directory)."""
    meta = rec.get("meta") or {}
    if not isinstance(meta, dict):
        raise ReportProtocolError(
            f"meta must be an object (got {type(meta).__name__})"
        )
    base = meta.get("artifacts_dir")
    base = base if base else str(base_dir)
    out: "dict[str, str | None]" = {}
    for key in _REFERENCE_KEYS:
        val = meta.get(key)
        if val is None:
            out[key] = None
        elif isinstance(val, str) and val:
            out[key] = val if os.path.isabs(val) else os.path.join(base, val)
        else:
            raise ReportProtocolError(
                f"meta.{key} must be a non-empty string or null "
                f"(got {val!r})"
            )
    return out


def crosscheck(records: "Sequence[dict]", base_dir: "str | os.PathLike") -> "list[str]":
    """AC5 rule R1 — **every JSONL line's referenced files exist** —
    checked over **every complete line** (not just the deduped view: a
    superseded line's artifacts are part of the run record too, WP2
    artifact atomicity), plus the semantic rule: a **``resolved`` /
    ``failed_test`` final status must reference an existing
    ``patch.diff``** (a graded verdict requires a patch). Returns the
    violation list (empty = PASS)."""
    violations: "list[str]" = []
    finals, _ = dedupe(records)
    graded = {
        iid: rec for iid, rec in finals.items()
        if rec["status"] in ("resolved", "failed_test")
    }
    for i, rec in enumerate(records):
        meta = rec.get("meta") or {}
        if not isinstance(meta, dict):
            violations.append(
                f"line {i + 1}: meta must be an object "
                f"(got {type(meta).__name__})"
            )
            continue
        try:
            refs = _ref_path(rec, base_dir)
        except ReportProtocolError as e:
            violations.append(f"line {i + 1}: {e}")
            continue
        iid = rec.get("instance_id")
        label = iid if iid is not None else "<batch>"
        for key in _REFERENCE_KEYS:
            p = refs[key]
            if p is not None and not os.path.isfile(p):
                violations.append(
                    f"line {i + 1} ({label}): referenced {key} does not "
                    f"exist: {p!r}"
                )
    for iid in sorted(graded):
        rec = graded[iid]
        refs = _ref_path(rec, base_dir)
        p = refs["patch_diff"]
        if p is None:
            violations.append(
                f"{iid}: status {rec['status']!r} requires a patch_diff "
                f"reference (a graded verdict requires a patch)"
            )
        elif not os.path.isfile(p):
            violations.append(
                f"{iid}: status {rec['status']!r} requires an existing "
                f"patch.diff: {p!r}"
            )
    return violations


def check_artifacts(
    preds_path: str,
    finals: "dict[str, dict]",
    base_dir: "str | os.PathLike",
) -> "list[str]":
    """AC5 rule R2 — ``all_preds.jsonl``'s **patch content matches
    ``patch.diff``** (and the file itself is well-formed: exactly one
    line per deduped instance, the harness field set, sorted by
    ``instance_id``). Byte-for-byte comparison of every non-empty
    ``model_patch`` against the referenced ``patch.diff``. Returns the
    violation list (empty = PASS)."""
    violations: "list[str]" = []
    if not os.path.isfile(preds_path):
        return [f"all_preds.jsonl does not exist: {preds_path!r}"]
    try:
        with open(preds_path, "r", encoding="utf-8", newline="") as f:
            raw_lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return [f"all_preds.jsonl unreadable: {e!r}"]
    ids = []
    lines: "list[dict]" = []
    for i, line in enumerate(raw_lines):
        if line == "":
            violations.append(f"all_preds.jsonl line {i + 1}: empty line")
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            violations.append(
                f"all_preds.jsonl line {i + 1}: not JSON ({e})"
            )
            continue
        if not isinstance(obj, dict):
            violations.append(
                f"all_preds.jsonl line {i + 1}: not an object"
            )
            continue
        keys = set(obj)
        expected = {"instance_id", "model_name_or_path", "model_patch"}
        if keys != expected:
            violations.append(
                f"all_preds.jsonl line {i + 1}: field set {sorted(keys)} "
                f"!= {sorted(expected)}"
            )
            continue
        if not isinstance(obj["instance_id"], str) or not obj["instance_id"]:
            violations.append(
                f"all_preds.jsonl line {i + 1}: bad instance_id "
                f"{obj['instance_id']!r}"
            )
            continue
        if not isinstance(obj["model_name_or_path"], str):
            violations.append(
                f"all_preds.jsonl line {i + 1}: bad model_name_or_path "
                f"{obj['model_name_or_path']!r}"
            )
            continue
        if not isinstance(obj["model_patch"], str):
            violations.append(
                f"all_preds.jsonl line {i + 1}: bad model_patch "
                f"{type(obj['model_patch']).__name__}"
            )
            continue
        ids.append(obj["instance_id"])
        lines.append(obj)
    # the instance set: exactly the deduped instances, sorted
    want = sorted(finals)
    if len(ids) != len(want):
        violations.append(
            f"all_preds.jsonl has {len(ids)} instance lines; the run "
            f"has {len(want)} distinct instances"
        )
    elif ids != want:
        violations.append(
            "all_preds.jsonl is not sorted by instance_id, or its "
            f"instance set differs (first mismatch: {ids[:3]}… vs "
            f"{want[:3]}…)"
        )
    for obj in lines:
        iid = obj["instance_id"]
        if iid not in finals:
            violations.append(
                f"all_preds.jsonl: instance {iid!r} is not in the run "
                f"JSONL"
            )
            continue
        rec = finals[iid]
        refs = _ref_path(rec, base_dir)
        patch = obj["model_patch"]
        if patch == "":
            # empty-patch semantics (harness's own): the reference, when
            # present, must point at a file with no usable patch — a
            # missing reference is the expected no-patch shape; a present
            # reference with non-empty content would be a mismatch the
            # next rule's inverse (asserted here directly).
            if refs["patch_diff"] is not None:
                p = refs["patch_diff"]
                if os.path.isfile(p):
                    with open(p, "r", encoding="utf-8", newline="") as f:
                        if f.read() != "":
                            violations.append(
                                f"all_preds.jsonl: {iid} has an empty "
                                f"model_patch but the referenced "
                                f"patch.diff {p!r} is non-empty"
                            )
                else:
                    violations.append(
                        f"all_preds.jsonl: {iid} references a missing "
                        f"patch.diff: {p!r}"
                    )
            continue
        if refs["patch_diff"] is None:
            violations.append(
                f"all_preds.jsonl: {iid} has a non-empty model_patch "
                f"but no patch_diff reference"
            )
            continue
        p = refs["patch_diff"]
        if not os.path.isfile(p):
            violations.append(
                f"all_preds.jsonl: {iid} references a missing "
                f"patch.diff: {p!r}"
            )
            continue
        with open(p, "r", encoding="utf-8", newline="") as f:
            content = f.read()
        if content != patch:
            violations.append(
                f"all_preds.jsonl: {iid} model_patch does not match "
                f"patch.diff {p!r} ({len(patch)} chars vs "
                f"{len(content)} chars)"
            )
    return violations


# ---------------------------------------------------------------------------
# AC4 — the leaderboard artifacts
# ---------------------------------------------------------------------------


def _model_name_or_path(meta_file: "dict | None",
                       ) -> str:
    """``model_name_or_path`` for ``all_preds.jsonl``: the meta file's
    ``model_name_or_path`` → its ``model`` → ``"devin"`` (the WP4
    ``DEFAULT_MODEL_NAME``)."""
    if meta_file:
        for key in ("model_name_or_path", "model"):
            v = meta_file.get(key)
            if isinstance(v, str) and v:
                return v
    return "devin"


def write_artifacts(
    finals: "dict[str, dict]",
    meta_file: "dict | None",
    out_dir: str,
    base_dir: "str | os.PathLike",
) -> "dict[str, Any]":
    """Write the leaderboard artifacts (AC4), deterministically:

    * ``<out>/all_preds.jsonl`` — one line **per deduped instance**
      (all 8 final statuses, incl. pending retry-class), **sorted by
      ``instance_id``**, exactly the harness's standard predictions
      fields; ``model_patch`` = the full contents (UTF-8) of the
      referenced ``patch.diff`` (AC5 R1 guarantees existence on the
      generate path), else ``""`` (the harness's own empty-patch
      semantics keeps it out of ``resolved_ids``);
    * ``<out>/trajs/<instance_id>.jsonl`` — a **byte-for-byte copy** of
      each referenced, existing trajectory (no re-serialization — the
      trajectory is the adapter's, verbatim); no reference / no file →
      no entry (absence = no trajectory captured, not an error).

    Returns ``{"all_preds": str, "preds_lines": int, "trajs": [str…]}``.
    ``base_dir`` resolves the meta's relative references (the run
    JSONL's directory — the contract's default)."""
    os.makedirs(out_dir, exist_ok=True)
    name = _model_name_or_path(meta_file)
    preds_path = os.path.join(out_dir, "all_preds.jsonl")
    trajs_dir = os.path.join(out_dir, "trajs")
    trajs_written: "list[str]" = []
    base = str(base_dir)
    with open(preds_path, "w", encoding="utf-8", newline="") as f:
        for iid in sorted(finals):
            rec = finals[iid]
            refs = _ref_path(rec, base)
            patch = ""
            p = refs["patch_diff"]
            if p is not None and os.path.isfile(p):
                with open(p, "r", encoding="utf-8", newline="") as pf:
                    patch = pf.read()
            line = {
                "instance_id": iid,
                "model_name_or_path": name,
                "model_patch": patch,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            t = refs["trajectory"]
            if t is not None and os.path.isfile(t):
                os.makedirs(trajs_dir, exist_ok=True)
                dest = os.path.join(trajs_dir, f"{iid}.jsonl")
                with open(t, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                trajs_written.append(dest)
    return {
        "all_preds": preds_path,
        "preds_lines": len(finals),
        "trajs": trajs_written,
    }


# ---------------------------------------------------------------------------
# AC3 — the methodology block
# ---------------------------------------------------------------------------

#: The methodology fields in the AC3 order (one per line in the report).
_METHODOLOGY_FIELDS: tuple = (
    ("run_id", "Run ID"),
    ("devin_cli_version", "Devin CLI version"),
    ("model", "Model"),
    ("cost_tier", "Cost tier"),
    ("prompt_template_hash", "Prompt template hash"),
    ("permission_policy", "Permission policy"),
    ("caps", "Caps"),
    ("harness_version", "Harness version"),
    ("images", "Eval images"),
)


def methodology(meta_file: "dict | None", records: "Sequence[dict]") -> "dict":
    """The methodology block (AC3): the static fields from the operator
    meta file (``unknown`` for a missing file / field — the block never
    silently omits a field) plus the two computed fields — **date range**
    (min/max ``ts``, exact) and **sessions used** (session attempts,
    exact). ``caps`` renders as its ``{key=value …}`` map (missing caps
    → ``unknown``)."""
    meta_file = meta_file or {}
    if not isinstance(meta_file, dict):
        raise InvalidInputError(
            f"meta file must be a JSON object (got "
            f"{type(meta_file).__name__})"
        )

    def field(key: str) -> str:
        v = meta_file.get(key)
        if v is None or (isinstance(v, str) and v == ""):
            return "unknown"
        if isinstance(v, str):
            return v
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f"{v:g}" if isinstance(v, float) else str(v)
        return json.dumps(v, ensure_ascii=False)

    out = {}
    for key, _label in _METHODOLOGY_FIELDS:
        if key == "caps":
            caps = meta_file.get("caps")
            if not isinstance(caps, dict) or not caps:
                out[key] = "unknown"
            else:
                out[key] = " ".join(
                    f"{k}={caps[k]}"
                    for k in sorted(caps)
                    if caps[k] is not None
                ) or "unknown"
        else:
            out[key] = field(key)
    rng = date_range(records)
    out["date_range"] = (
        f"{rng[0]} – {rng[1]}" if rng is not None else "unknown"
    )
    out["sessions_used"] = str(sessions_used(records))
    return out


# ---------------------------------------------------------------------------
# the markdown report (PLAN §10.4)
# ---------------------------------------------------------------------------


def render_report(ctx: "dict") -> str:
    """Render the markdown report (pure over the computed ``ctx`` dict
    — no filesystem, no wall clock: byte-stable for stable inputs).
    ``ctx`` keys: ``p1`` (:func:`pass1` result), ``tax`` (:func:
    `taxonomy` result), ``meth`` (:func:`methodology` result),
    ``artifacts`` (:func:`write_artifacts` result + ``"crosscheck"``),
    ``dropped`` (bool — the torn-EOF line was dropped), ``pending``
    (count of retry-class finals)."""
    p1 = ctx["p1"]
    tax = ctx["tax"]
    meth = ctx["meth"]
    art = ctx["artifacts"]
    ov = p1["overall"]
    lines: "list[str]" = []
    lines.append("# SWE-bench Verified — Devin ACP run report")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if ov["terminal"] > 0:
        lines.append(
            f"- **pass@1 (overall): {ov['resolved']}/{ov['terminal']} "
            f"({ov['pct']})** — resolved / legitimate agent outcomes"
        )
    else:
        lines.append(
            "- **pass@1 (overall): n/a** — no legitimate agent outcome "
            "yet (all instances pending or infra-class)"
        )
    lines.append(
        f"- Instances: {ctx['distinct']} distinct; "
        f"{ov['terminal']} terminal (agent outcomes); "
        f"{ctx['pending']} pending (retry class)"
    )
    lines.append(f"- Date range (UTC): {meth['date_range']}")
    lines.append(f"- Sessions used (attempts): {meth['sessions_used']}")
    if ctx["dropped"]:
        lines.append(
            "- Note: a torn final line at EOF was dropped (a kill "
            "mid-write — not fatal, WP3b reader contract); the report "
            "covers the complete records only"
        )
    lines.append("")
    lines.append("## Per-repo pass@1")
    lines.append("")
    lines.append("| Repo | Resolved | Terminal | pass@1 |")
    lines.append("|---|---|---|---|")
    for repo in sorted(p1["per_repo"]):
        c = p1["per_repo"][repo]
        lines.append(
            f"| {repo} | {c['resolved']} | {c['terminal']} | {c['pct']} |"
        )
    lines.append("")
    lines.append("## Failure taxonomy")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|---|---|")
    for status in TAXONOMY_ORDER:
        lines.append(f"| {status} | {tax['by_status'][status]} |")
    lines.append("")
    lines.append(
        f"Batch-level markers (``instance_id: null``): "
        f"{tax['markers']}"
    )
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    for key, label in _METHODOLOGY_FIELDS:
        lines.append(f"- **{label}:** {meth[key]}")
    lines.append(f"- **Date range (UTC):** {meth['date_range']}")
    lines.append(f"- **Sessions used (attempts):** {meth['sessions_used']}")
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append(
        f"- all_preds.jsonl: {art['preds_lines']} lines "
        f"(`{os.path.basename(art['all_preds'])}`)"
    )
    lines.append(f"- trajs/: {len(art['trajs'])} per-instance trajectory files")
    lines.append(f"- cross-check: {art['crosscheck']}")
    for v in art.get("crosscheck_violations", []):
        lines.append(f"  - {v}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the operators: generate (default) / check (--check)
# ---------------------------------------------------------------------------


def _load_meta_file(path: "str | None") -> "dict | None":
    if path is None:
        return None
    _raise_on_missing(path, "meta file")
    try:
        with open(path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        raise InvalidInputError(f"meta file is not JSON: {e}") from e
    except OSError as e:
        raise InvalidInputError(f"meta file unreadable: {e!r}") from e
    if not isinstance(meta, dict):
        raise InvalidInputError(
            f"meta file must be a JSON object (got "
            f"{type(meta).__name__})"
        )
    return meta


def _ctx(records: "list", dropped: bool, meta_file: "dict | None") -> "dict":
    finals, markers = dedupe(records)
    p1 = pass1(finals)
    tax = taxonomy(finals, markers)
    meth = methodology(meta_file, records)
    pending = sum(
        1 for rec in finals.values() if rec["status"] in RETRY_STATUSES
    )
    return {
        "records": records,
        "finals": finals,
        "markers": markers,
        "p1": p1,
        "tax": tax,
        "meth": meth,
        "pending": pending,
        "distinct": len(finals),
        "dropped": dropped,
    }


def generate(
    jsonl_path: str,
    out_dir: str,
    meta_file: "dict | None" = None,
) -> "dict[str, Any]":
    """The default operation: read the run JSONL (WP3b reader), compute
    the aggregation, write ``report.md`` + the leaderboard artifacts,
    run the cross-check (R1 + R2) — **fail-loud** (:class:
    `ArtifactCheckError`, exit 3) on any violation *before* the report
    is declared complete (the report.md records the check status)."""
    _raise_on_missing(jsonl_path, "run JSONL")
    records, dropped = load_jsonl(jsonl_path)
    ctx = _ctx(records, dropped, meta_file)
    base = os.path.dirname(os.path.abspath(jsonl_path))
    violations = crosscheck(records, base)
    art = write_artifacts(ctx["finals"], meta_file, out_dir, base)
    violations += check_artifacts(
        art["all_preds"], ctx["finals"], base
    )
    if violations:
        art["crosscheck"] = f"FAIL ({len(violations)} violations)"
        art["crosscheck_violations"] = violations
        # the report still renders (with the violations) so the
        # operator sees *why* — but the error propagates: the report
        # is not trusted.
        report_path = os.path.join(out_dir, "report.md")
        os.makedirs(out_dir, exist_ok=True)
        ctx["artifacts"] = art
        with open(report_path, "w", encoding="utf-8", newline="") as f:
            f.write(render_report(ctx))
        raise ArtifactCheckError(
            "artifact cross-check failed:\n  - "
            + "\n  - ".join(violations)
        )
    art["crosscheck"] = "PASS"
    ctx["artifacts"] = art
    report_path = os.path.join(out_dir, "report.md")
    os.makedirs(out_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        f.write(render_report(ctx))
    return {
        "report": report_path,
        "artifacts": art,
        "pass1": ctx["p1"]["overall"],
        "taxonomy": ctx["tax"]["by_status"],
        "methodology": ctx["meth"],
    }


def check(
    jsonl_path: str,
    out_dir: str,
    meta_file: "dict | None" = None,
) -> "list[str]":
    """``--check``: verify an **existing** out dir against the run
    JSONL — R1 (every line's referenced files exist) + R2 (the
    written ``all_preds.jsonl`` matches ``patch.diff`` byte-for-byte
    and is well-formed) — and return the violation list (empty = PASS;
    the caller raises / exits). No writes."""
    _raise_on_missing(jsonl_path, "run JSONL")
    records, _dropped = load_jsonl(jsonl_path)
    ctx = _ctx(records, False, meta_file)
    base = os.path.dirname(os.path.abspath(jsonl_path))
    violations = crosscheck(records, base)
    violations += check_artifacts(
        os.path.join(out_dir, "all_preds.jsonl"), ctx["finals"], base
    )
    return violations


# ---------------------------------------------------------------------------
# the operator CLI
# ---------------------------------------------------------------------------


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "WP5: aggregate a run JSONL into the pass@1 report, the "
            "failure taxonomy, the methodology block and the "
            "leaderboard artifacts (all_preds.jsonl + trajs/), with the "
            "artifact cross-check. Exit codes: 0 = OK (generated / "
            "checked clean); 2 InvalidInputError; 3 ArtifactCheckError "
            "(the cross-check failed — the report is not trusted); 4 "
            "ReportProtocolError (a run-JSONL schema violation)."
        )
    )
    parser.add_argument("--jsonl", required=True,
                       help="the run checkpoint file (run-<id>.jsonl)")
    parser.add_argument("--out", required=True,
                       help="the output directory (report.md, "
                       "all_preds.jsonl, trajs/)")
    parser.add_argument("--meta-file", default=None,
                       help="the run-level methodology meta (JSON)")
    parser.add_argument("--check", action="store_true",
                       help="verify an existing out dir against the "
                       "JSONL (no writes)")
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    try:
        meta = _load_meta_file(args.meta_file)
        if args.check:
            violations = check(args.jsonl, args.out, meta)
        else:
            generate(args.jsonl, args.out, meta)
            violations = []
    except InvalidInputError as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 2
    except ReportProtocolError as e:
        print(f"protocol violation: {e}", file=sys.stderr)
        return 4
    except ArtifactCheckError as e:
        print(f"cross-check failed: {e}", file=sys.stderr)
        return 3
    if args.check:
        if violations:
            print(
                "cross-check failed:\n  - " + "\n  - ".join(violations),
                file=sys.stderr,
            )
            return 3
        print("cross-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
