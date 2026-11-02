# WP6b Implementation Plan — Pilot B: 25 stratified instances across all 12 repos

Implements issue #22 (part of #9). Plan of record: `bench/PLAN.md` §5
WP6.2. Depends on: #21 (WP6a Pilot A — GO, merged as PR #37).
Estimate: L (5 pts, real quota).

**Acceptance (issue #22):**
1. 25 instances **stratified across all 12** SWE-bench Verified repos.
2. The 3 Pilot A instances are **reused inside** the 25 for the 2-run
   overlap/variance check (no extra sessions spent).
3. Full failure taxonomy observed in the wild; each `eval_error`
   root-caused (env vs. parser vs. patch).
4. A real mid-batch interrupt demonstrates §2.5 resume semantics with
   correct pass@1 retry classes.

Testable by: a scheduled sequential run under the calibrated budget;
variance report (2-run overlap on the 3 shared instances).

**Environment facts (carried from WP6a — PLAN-WP6A.md §Environment facts,
re-verified 2026-11-02):** no container runtime in the operator
workspace ⇒ the live run is **sandbox mode** (the documented
no-container fallback; the deviation is recorded in the checklist +
meta), in a delegated Devin session (the operator session is
bash-blocked from `devin`; the WP0 Appendix A / WP6a pattern). Pilot
A's artifacts live at
`/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/`
(`run-pilotA.jsonl` — the import source).

## The 25 instances (AC1)

`bench/pilot_b_instances.json` (new) — 25 rows pinned **verbatim** from
the `SWE-bench/SWE-bench_Verified` `test` split (fetched via the HF
datasets-server rows API, the same source as `pilot_instances.json`;
`source` + `fetched_at` + the selection rule in `_meta`). Pinned by
`bench/scripts/pin_pilot_b_instances.py` (committed — the selection is
reproducible: the script re-fetches, re-selects, and must produce the
same 25).

**Stratification rule (deterministic, recorded in `_meta`):**
1. one instance from **every one of the 12** Verified repos
   (django 231 · sympy 75 · sphinx 44 · matplotlib 34 · scikit-learn
   32 · astropy 22 · pydata/xarray 22 · pytest 19 · pylint 10 ·
   requests 8 · seaborn 2 · flask 1 — the WP0 §5 counts, re-verified
   against the live fetch);
2. the remaining 13 seats dealt round-robin in descending repo size
   (ties by name), **capped at each repo's row count in the split**
   (flask holds one seat — its single split row);
3. within a repo, the **first N rows in dataset order** (the canonical
   HF row order of the published split).

Result: django 3 · sympy 3 · sphinx 2 · matplotlib 2 · scikit-learn 2 ·
astropy 2 · xarray 2 · pytest 2 · pylint 2 · requests 2 · seaborn 2 ·
flask 1 (= 25; all 12 repos covered). The 3 Pilot A instances are in
the 25 by construction: flask-5014 is the split's only flask row, and
the first-N rule reaches the Pilot A seaborn / requests rows
(asserted by the pinning script).

## AC2 — the 2-run overlap/variance check (no extra sessions)

**Semantics.** Pilot B's 25 = 22 new + 3 shared. The shared 3 are
**reused** through the new checkpoint **import** mechanism:

* `--import-run <run-id> [--import-workdir <path>]` — the harness reads
  `<workdir>/run-<run-id>.jsonl` (Pilot A's checkpoint), dedupes
  (last line per instance, the WP3 contract), and for each instance in
  the current instance file that the source carries a line for:
  * a **never-rerun** status (`resolved` / `failed_test` / `timeout` /
    `no_patch` / `agent_error` — the WP3
    `NEVER_RERUN_STATUSES`) → the line is **imported**: copied into
    this run's checkpoint **with its original `ts`** (the source run's
    window — it must not count toward this run's daily budget) and
    `meta.imported_from = <run-id>`; the instance is **skipped** by
    `resume_queue` — never re-run, no session spent.
  * a **retry-class** status (`quota_limited` / `eval_error` /
    `preflight_failed` — the WP3 `RETRY_STATUSES`) → **not imported**;
    the instance **runs** in this run (the only pass@1 retry class,
    PLAN §2.5).
  * a source line whose `instance_id` is not in the current file →
    ignored (stale).
* The import is **fail-closed and pre-session**: a missing / corrupt
  source file is a config error (exit 2) before the pre-flight — no
  session is ever spent against an import that did not validate.
* Seeding is **idempotent**: an instance that already has a line in
  this run's checkpoint is never re-seeded (a repeat `--resume` does
  not duplicate the import lines).
* For the real pair: flask (`failed_test`) + seaborn (`resolved`) are
  imported (2 sessions **not** spent); `psf__requests-1142`
  (`eval_error` in Pilot A — a pip-install infra failure, PLAN-WP6A
  record) is the retry class and **re-runs** in Pilot B. The 2-run
  overlap is the 2 imported instances; the 3rd shared instance's
  re-run is the pass@1 retry class itself (the Pilot A session for it
  was consumed by the failed eval — the re-run is the only retry
  path, no session is "wasted").

**The check (checklist item 16, gating for a pilot-B run with an
import).** For each imported instance: the source line and this
run's final line for the same instance must agree on **status,
session wall, total tokens, and patch bytes** — the import is
faithful **and** the reused instance was not re-run (a re-run would
append a divergent final line and fail the item). Plus the session
arithmetic: (imported) + (sessions this run) == (unique instances with
a final line) — i.e. exactly 25 unique sessions across the two runs,
zero double-counting.

## AC3 — the failure taxonomy + the root-caused `eval_error`

`meta.eval_error_root_cause` ∈ {`env`, `parser`, `patch`} (+ a
`eval_error_root_cause_detail`) is recorded on **every** `eval_error`
line:

| failure point (sandbox host check) | class |
|---|---|
| `git clone --bare` / `git clone` / `git checkout base_commit` | `env` (host git infra / network) |
| `git apply model_patch` fails | `patch` (the agent's patch does not apply to the fresh checkout) |
| `git apply test_patch` fails | `env` (the harness test fixture does not apply to the pinned base — dataset/checkout class, not the agent's patch) |
| `venv` / `pip install` / `pip install pytest` | `env` |
| a spawned-tool failure (`FileNotFoundError` / `TimeoutExpired`) | `env` |
| pytest ran but no parseable `-rA` results | `parser` (the log-parser leg) |
| the documented `no_container_runtime` skip (host check off) | `env` |
| the PLAN §4 oversize-patch cap (`PilotPatchTooLarge`) | `patch` |
| container mode: a WP4 `EvaluationError` whose text marks the patch-apply leg | `patch` |
| container mode: any other WP4 `EvaluationError` | `env` (the harness container / the harness itself) |

The record renders the **full taxonomy** (every WP5 terminal status:
count + instance ids) and the **root-cause table** (every `eval_error`
line: `instance_id: class (detail)`). Checklist item 17 (gating for a
pilot-B run): PASS ⟺ every `eval_error` line carries a class.

## AC4 — the real mid-batch interrupt

The existing `--interrupt-after N` (the WP6a AC3 path) is the
mechanism; Pilot B exercises it **mid-batch** (`--interrupt-after 8`
on the 23-to-run queue — 2 imports + 22 new, of which the `requests`
retry is queued first by the resume order… the interrupt counts
**completed instances**, not sessions, so N is relative to the
invocation's completions). The follow-up `--resume` goes through
`resume_queue`: finished instances (the never-rerun class, imported or
not) are skipped, the rest run. Checklist item 11 (gating, the
existing WP6a item) covers the skip / no-duplicates / session
arithmetic.

**The budget windows.** The WP3 runner enforces the local daily cap
per **process** and **sleeps to the next UTC-midnight window** when
it is exhausted (the PLAN §2.2 calendar-reset leg; a cross-invocation
resume restarts the counter — the account-level 429 backstop, PLAN
§2.3, is the hard stop, and a `quota_limited` line is the retry class
that resumes in the next window). Checklist item 12 therefore gates
**per window** (checkpoint lines bucketed by their `ts`'s UTC day —
imported lines keep the source run's `ts`, so they land in the source
window, never in this run's): every window ≤ `max_sessions_per_day`
sessions and ≤ `max_wall_per_session × max_sessions_per_day` wall. A
single-window run renders exactly the current text (the WP6a contract
is unchanged).

The operational plan (the calibrated budget: 10 sessions/day, the WP6a
conservative number; 23 to-run ⇒ 3 windows): window 1: invocation 1
`--interrupt-after 8` (the mid-batch interrupt — 8 sessions) +
invocation 2 `--resume` (2 more, the window-1 cap); window 2:
invocation 3 `--resume` (10); window 3: invocation 4 `--resume` (3).
Each invocation is delegated (the operator session is bash-blocked
from `devin` — the WP6a pattern).

## Harness changes (`bench/live_pilot.py`)

* `PilotConfig`: + `import_run` / `import_workdir` / `pilot`
  (`"a" | "b"`); validation (an `import_run` without a readable
  source is a `PilotConfigError` — fail-closed).
* `load_instances_meta(path)` (new helper; `load_instances`' signature
  is unchanged — the WP6a tests hold).
* `run_pilot`: the import load + fail-closed validation + the
  idempotent checkpoint seeding (the lines are raw-written with the
  source `ts` before the `JsonlCheckpoint` opens — the write-side
  repair passes well-formed lines through untouched); the per-window
  item-12 accounting; the pilot-B record sections; the state file
  gains `imported_run` / `imported` / `import_retry_class`.
* `PilotSession.run` / `_host_check` / `_eval_container`: the
  `eval_error_root_cause` recording (the table above).
* `build_checklist`: items **15** (stratification — PASS ⟺ every repo
  in the instance file's `stratified_repos` meta appears among the
  final lines), **16** (the 2-run overlap/variance), **17** (the
  root-caused `eval_error`) — appended **only for `--pilot b`** (the
  14-item WP6a contract + the `test_checklist_verdict_gating`
  invariants hold); each is `n/a` (non-gating) when its data is
  absent. Item 12: the per-window gate.
* `_verdict`: a `gating` parameter (default = the WP6a 14-item set;
  pilot-B passes the 17-item set — 13 stays non-gating).
* `render_checklist_md`: the pilot title / record name
  (`wp6a-go-no-go.md` vs `wp6b-pilot-b.md`) + the pilot-B sections
  (the per-repo stratification table, the overlap/variance table, the
  taxonomy + root-cause table, the per-window budget table).
* `_remove_stale_record`: removes **both** record names (the early-
  exit invariant holds across pilot kinds).
* CLI: `--import-run` / `--import-workdir` / `--pilot {a,b}`
  (default `a` ⇒ the WP6a behavior, verbatim: default workdir
  `pilot-a`, record `wp6a-go-no-go.md`, 14 items).

## Tests (`bench/tests/test_live_pilot.py` — zero quota, mock-driven)

* the import: a `--import-run` pair (a fixture pilot-A JSONL written by
  a mock-driven run 1 into a second workdir; a pilot-B run 2 with the
  import): the never-rerun lines are imported (copied, `ts` preserved,
  `imported_from` set, the instance **skipped** — the mock factory is
  never called for them), the retry-class line re-runs (a fresh line),
  the source file is untouched, a repeat `--resume` is idempotent (no
  duplicate import lines).
* the fail-closed import (missing source → exit 2 before the
  pre-flight — the mock is never spawned; corrupt source → exit 2).
* item 16: a faithful import → PASS (+ GO on the pilot-B gating set);
  a divergent final line (a re-run of an imported instance, simulated
  through `build_checklist`) → FAIL → NO-GO.
* item 15: a 12-repo `stratified_repos` meta with the finals covering
  all 12 → PASS; one repo missing from the finals → FAIL → NO-GO.
* item 17: every `eval_error` line root-caused → PASS; an unclassed
  `eval_error` → FAIL; zero `eval_error` lines → PASS (vacuous).
* the root-cause classification through the mock + the stubbed
  host-check legs (the `pip install` failure → `env`; the `git apply
  model_patch` failure → `patch`; the `test_patch` apply failure →
  `env`; the unparseable pytest → `parser`; the oversize-patch cap →
  `patch`; the container-mode `EvaluationError` sniff → `patch` /
  `env`).
* item 12 per-window: two UTC-day buckets (one over the cap → FAIL;
  both within → PASS); a single-window run's text is the current one
  (the WP6a `test_budget_counts_superseded_attempts` holds).
* the pilot-B dry-run: the 17-item skeleton + the `wp6b-pilot-b.md`
  record name.
* the pilot-B full mock run: 12-repo fixture + a fixture import → the
  record carries items 15/16/17 + the four pilot-B sections.

The `test_live_gate.py` static guard is untouched (every new test is
mock-driven; no real-binary name is named outside a
`@pytest.mark.bench_live` test).

## Docs

* `bench/PLAN-WP6B.md` — this file.
* `bench/README.md` — the WP6b layout rows (`pilot_b_instances.json`,
  `scripts/pin_pilot_b_instances.py`, `PLAN-WP6B.md`,
  `notes/wp6b-pilot-b.md`) + the WP6b summary + the import flag.
* `bench/live_pilot.py` module docstring — the WP6b section.
* `bench/notes/wp6b-pilot-b.md` — the live measurement record (filled
  from the live run, the `wp6a-go-no-go.md` analogue).
* No `publish.yml` / `package.json` / CI changes (the `bench/**`
  `paths-ignore` + the `files` array + the Python CI job already hold —
  PLAN §8).

## Live-run execution (the real quota leg)

The operator session cannot run `devin` (bash-blocked; the WP6a
pattern): the live run is delegated, in this workspace (the sandbox
under the trusted workspace root — WP0 §8), sequential, `swe-2-high`
(Free tier, WP0 §7), the 45-min cap, the calibrated budget (10/day).
The invocation sequence per the AC4 section above; the `--import-run
pilotA --import-workdir <the wp6a workspace>/pilot-a` reuse; the
interrupt after 8 completions (mid-batch); the windowed resumes.

## Contingencies

1. **Quota hit mid-run** (`quota_limited`) → the WP3b state machine
   stops the batch, checkpoints, the 429 backstop parks it; the
   follow-up `--resume` continues in the next window (the retry
   class — the run's budget-window accounting item 12 holds because
   the `quota_limited` line is excluded from the session count).
2. **An `eval_error` whose host-check phase is ambiguous** → the
   class is whatever phase failed (the table is exhaustive over the
   host-check's failure points); the `detail` carries the raw
   evidence (the eval.log) — the record's root-cause table is
   complete by construction (item 17 cannot fail on a classified
   line).
3. **The delegated window invocation dies mid-window** (a host crash)
   → the WP3b host-crash class: the partial line is torn-dropped on
   the next `load_checkpoint`, the instance re-runs (the pass@1
   contract — a killed session never had a verdict line), the state
   file records `run_failed`.
4. **Pilot A's `run-pilotA.jsonl` is gone by the live run** → the
   import is optional: without it the 3 shared instances simply run
   fresh (3 extra sessions — recorded as a deviation in
   `notes/wp6b-pilot-b.md`; item 16 renders `n/a`).
5. **The HF rows API drift** (the pinning script's shape) → the
   committed `pilot_b_instances.json` is the source of truth (the
   script is the provenance, re-runnable while the API holds).

## AC map

* **AC1 (25 stratified across all 12 repos)** →
  `pilot_b_instances.json` (the pinning script + the `_meta`
  provenance) + checklist item 15 (gating).
* **AC2 (the 3 Pilot A instances reused; the 2-run overlap/variance;
  no extra sessions)** → the `--import-run` mechanism (the
  never-rerun import / the retry-class re-run) + item 16 (gating:
  the faithful, non-re-run overlap + the session arithmetic).
* **AC3 (the taxonomy in the wild; each `eval_error` root-caused)** →
  the `eval_error_root_cause` recording (the mapping table) + the
  record's taxonomy + root-cause sections + item 17 (gating).
* **AC4 (the real mid-batch interrupt; the §2.5 resume semantics; the
  correct pass@1 retry classes)** → `--interrupt-after 8` + the
  windowed `--resume` pair; item 11 (gating, the WP6a path) + the
  state file's interrupt/resume bookkeeping.
