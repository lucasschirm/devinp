#!/usr/bin/env python3
"""WP6b (issue #22) — pin the 25 stratified Pilot-B instances.

Fetches the SWE-bench Verified ``test`` split (500 rows, 12 repos)
verbatim from the HF datasets-server rows API (the same source
``pilot_instances.json`` was pinned from, PLAN-WP6A.md) and selects the
25 Pilot-B instances with the documented, deterministic stratification
rule:

1. at least one instance from **every one of the 12** Verified repos;
2. the remaining 13 seats are dealt round-robin in descending repo
   size (ties broken by repo name), **capped at each repo's row
   count in the split** (flask holds one seat — its single split
   row), so the largest repos carry the extra seats;
3. within a repo the selected instances are the **first N rows in
   dataset order** (the canonical HF row order of the published
   split).

The selected rows are pinned verbatim (the fields the harness consumes)
into ``bench/pilot_b_instances.json`` with the ``_meta`` provenance
(source, fetch date, the rule, the per-repo counts, the 3 Pilot-A
instances flagged). The 3 Pilot-A instances (flask-5014,
seaborn-3069, requests-1142) are covered by the rule: flask is the
split's only flask instance, and the rule's first-N selection reaches
the Pilot-A seaborn / requests rows (asserted below).
"""

import json
import sys
import time
import urllib.request

API = ("https://datasets-server.huggingface.co/rows"
       "?dataset=SWE-bench%2FSWE-bench_Verified&config=default"
       "&split=test&offset={offset}&limit=100")

UA = {"User-Agent": "devinp-bench (WP6b instance pinning; issue #22)"}

KEEP = ("instance_id", "repo", "base_commit",
        "environment_setup_commit", "image", "eval_type",
        "log_parser", "version", "FAIL_TO_PASS", "PASS_TO_PASS",
        "problem_statement", "test_patch")

EXPECTED_REPOS = 12
SEATS = 25
PILOT_A = ("pallets__flask-5014", "mwaskom__seaborn-3069",
           "psf__requests-1142")


def _get(url: str) -> dict:
    """The page fetch with retry (the HF API 502s intermittently)."""
    last = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA),
                    timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry all failures
            last = exc
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"HF API fetch failed after retries: {last}")


def fetch_all_rows() -> list:
    rows = []
    offset = 0
    seen_first = set()
    while True:
        data = _get(API.format(offset=offset))
        # the round-20 Minor fix (issue #22): a 200 error-shaped
        # body ({"error": ...} — no "rows") would raise an
        # uncaught KeyError; _get retries fetch EXCEPTIONS but not
        # a parsed error body — fail closed before the access
        if (not isinstance(data, dict)
                or not isinstance(data.get("rows"), list)):
            raise SystemExit(
                "the HF API returned an error-shaped 200 body "
                f"(no 'rows' list): {str(data)[:200]}")
        page = data["rows"]
        # the round-24 Minor fix (issue #22): a row missing
        # row_idx (schema drift) would raise an uncaught KeyError
        # in the sort / de-dupe below (_get retries fetch
        # exceptions, not a per-row key) — fail closed
        for r in page:
            if (not isinstance(r, dict)
                    or not isinstance(r.get("row_idx"), int)
                    or isinstance(r.get("row_idx"), bool)):
                raise SystemExit(
                    "an API row is missing its row_idx "
                    f"(schema drift): {json.dumps(r)[:200]}")
        rows.extend(page)
        if not page:
            break
        # a persistently partial=True response (a misbehaving API)
        # would make the offset loop unbounded — the no-progress
        # guard: ANY page first-row repeat (immediate OR after other
        # pages — a cycling API with two or more distinct pages is
        # caught, which an immediate-only check left unbounded)
        first = page[0].get("row_idx")
        if first in seen_first:
            break
        seen_first.add(first)
        if not data.get("partial", False) and len(page) < 100:
            break
        offset += 100
    rows.sort(key=lambda r: r["row_idx"])
    seen = set()
    out = []
    for r in rows:  # a partial page can overlap the next — de-dupe
        if r["row_idx"] in seen:
            continue
        seen.add(r["row_idx"])
        out.append(r)
    return out


def stratify(rows: list) -> "tuple[list, dict]":
    """The documented rule (see the module docstring). Returns
    (selected_rows, per_repo_total_counts)."""
    by_repo: dict = {}
    for r in rows:  # dataset order (row_idx ascending)
        by_repo.setdefault(r["row"]["repo"], []).append(r["row"])
    if len(by_repo) != EXPECTED_REPOS:
        raise SystemExit(
            f"expected {EXPECTED_REPOS} repos, got {len(by_repo)}: "
            f"{sorted(by_repo)}")
    # rule 2's order: descending repo size, ties by name
    order = sorted(by_repo, key=lambda r: (-len(by_repo[r]), r))
    seats = {r: 1 for r in order}  # rule 1: one per repo
    left = SEATS - len(order)
    i = 0
    streak = 0
    while left > 0:
        # rule 2: round-robin by size — a repo that is exhausted by
        # its split size (flask: 1 instance in the whole split) takes
        # no extra seat; the seat lands on the next repo in order
        repo = order[i % len(order)]
        i += 1
        if seats[repo] < len(by_repo[repo]):
            seats[repo] += 1
            left -= 1
            streak = 0
        else:
            streak += 1
            if streak == len(order):
                # a full lap with no progress: the split holds fewer
                # distinct rows than SEATS (impossible: 500 ≥ 25) —
                # fail loud instead of looping
                raise SystemExit(
                    "cannot deal the remaining "
                    f"{left} seats: every repo is exhausted by "
                    "its split size")
    selected = []
    for repo in order:
        for r in by_repo[repo][:seats[repo]]:  # rule 3: first N
            selected.append(r)
    return selected, {repo: len(by_repo[repo]) for repo in order}


def main(out_path: str) -> None:
    rows = fetch_all_rows()
    if len(rows) != 500:
        raise SystemExit(f"expected 500 rows, got {len(rows)}")
    selected, per_repo = stratify(rows)
    if len(selected) != SEATS:
        raise SystemExit(f"selected {len(selected)} != {SEATS}")
    # the Pilot-A reuse precondition (issue #22 AC2): all 3 must be in
    # the 25 (flask: the split's only flask row; seaborn / requests:
    # reached by the first-N rule)
    sel_ids = {r["instance_id"] for r in selected}
    missing = [i for i in PILOT_A if i not in sel_ids]
    if missing:
        raise SystemExit(f"Pilot-A instances missing from the 25: {missing}")
    pinned = []
    for r in selected:
        v = r
        entry = {k: v[k] for k in KEEP}
        entry["repo_url"] = "https://github.com/" + v["repo"] + ".git"
        pinned.append(entry)
    sel_count: dict = {}
    for r in selected:
        sel_count[r["repo"]] = sel_count.get(r["repo"], 0) + 1
    doc = {
        "_meta": {
            "source": ("SWE-bench/SWE-bench_Verified (test split) — "
                      "25 instances stratified across all 12 repos "
                      "(issue #22 AC1)"),
            "fetched_via": ("HF datasets-server rows API (https://"
                           "datasets-server.huggingface.co/rows"
                           "?dataset=SWE-bench%2FSWE-bench_Verified"
                           "&config=default&split=test)"),
            "fetched_at": time.strftime("%Y-%m-%d"),
            "stratification_rule": (
                "1 instance per each of the 12 Verified repos; the "
                "remaining 13 seats dealt round-robin in descending "
                "repo size (ties by name), capped at each repo's row "
                "count in the split (flask: 1 seat — its single "
                "split row); within a repo the first N rows in "
                "dataset (row_idx) order"),
            "stratified_repos": sorted({r["repo"] for r in selected}),
            "per_repo_selected": dict(
                sorted(sel_count.items(), key=lambda kv: (-kv[1], kv[0]))),
            "per_repo_total_in_split": per_repo,
            "pilot_a_instances_reused": [
                {"instance_id": i, "status_in_pilotA": s}
                for i, s in (
                    ("pallets__flask-5014", "failed_test"),
                    ("mwaskom__seaborn-3069", "resolved"),
                    ("psf__requests-1142", "eval_error"))],
            "note": ("pinned verbatim for the WP6b pilot (issue #22); "
                    "`test_patch` is consumed by the sandbox-mode host "
                    "check; `image` is the container-mode sweb.eval "
                    "image; the 3 Pilot-A instances are reused inside "
                    "the 25 for the 2-run overlap/variance check "
                    "(the import never re-runs a never-rerun status — "
                    "the psf__requests-1142 eval_error is the "
                    "pass@1 retry class, PLAN §2.5)"),
        },
        "instances": pinned,
    }
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    print(f"pinned {len(pinned)} instances across "
          f"{len(sel_count)} repos -> {out_path}")
    for repo, n in sorted(sel_count.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {repo:30s} {n:2d} / {per_repo[repo]:3d}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else "bench/pilot_b_instances.json")
