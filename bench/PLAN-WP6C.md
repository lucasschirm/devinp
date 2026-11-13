# WP6c Implementation Plan — Full 500-instance run + submission-ready artifacts

Implements issue #23 (part of the #9 epic). Plan of record: `bench/PLAN.md`
§5 WP6.3 + §10. Depends on: #22 (WP6b, GO — see the evidence caveat in §0.3),
#16 (WP3c pruning), #18 (WP5 report). Estimate: XL (8 pts, real quota,
several **weeks** of calendar time — see the schedule arithmetic in §13).

**Branch base: `dev`, not `main`.** The WP4/WP5/WP6a/WP6b code
(`evaluate.py`, `report.py`, `live_pilot.py`, `pilot_b_instances.json`) lives
on `dev` (PRs #31–#38 all targeted `dev`); `main` stops at WP3c. This branch
(`lucasschirm-app/23`) was reset to `origin/dev` @ `5b90bb9` before any work,
and the PR for #23 must target `dev`.

---

## 0. Where things stand (audited 2026-11-13)

### 0.1 What already works (nothing here needs rebuilding)

| Piece | State |
|---|---|
| `runner.py` (WP3a/b/c) | Sequential queue + overlap guard + `pilot.lock`, budget state machine (`running → budget_exhausted → sleeping(next UTC midnight) → resume`), pass@1-correct `resume_queue`, fail-closed cost-tier gate, pre-flight, post-eval image/container pruning. 2 093 lines, merged. |
| `live_pilot.py` (WP6a/b) | 4 491 lines: pre-flight → model gate → real `devin acp` session (WP1 `AcpSession` + `SwebenchPolicy`) → leak guard → patch extraction → checkpoint → prune → WP5 report → auto-generated go/no-go checklist (items 1–14; +15–17 for `--pilot b`). Exit codes 0/2/3/4/42. `--interrupt-after` + `--resume` + `--import-run` are the multi-window primitives. |
| `evaluate.py` (WP4) | Delegates to `python -m swebench.harness.run_evaluation` (`swebench==5.0.2`) on a standard predictions file in a fresh container. Unit-tested; **its container integration tests skip without a Docker daemon** → never executed for real. |
| `report.py` (WP5) | pass@1 overall + per-repo table + taxonomy + methodology block + `all_preds.jsonl` + `trajs/` + `--check` (R1 referenced-files-exist, R2 patch byte-equality). CLI exit 0/2/3/4. |
| Pilots | Pilot A **GO** (3 instances, `notes/wp6a-go-no-go.md` — complete record). Pilot B: harness + 25-instance manifest + record file merged; the **live 25-instance record is incomplete** (§0.3). |
| CI | `.github/workflows/ci.yml` has the Node job, the pack job (bench/ never ships) and the Python job (`pip install -r bench/requirements.lock`, `pytest bench/`); `publish.yml` has `paths-ignore: ['bench/**']`. Baseline at PR #38: 464 passed, 3 skipped, zero quota. |

### 0.2 What issue #23 actually needs that does not exist yet

1. A pinned **500-instance manifest** (only 3- and 25-instance files exist).
2. A **full-run mode** in `live_pilot.py` — `--pilot` accepts only `a|b`;
   `RECORD_NAMES` has no `c`; the checklist has no full-run items
   (completeness, artifact integrity, methodology completeness, the
   pass@1-vs-per-repo cross-check, the submission label).
3. A standalone **artifact-integrity / submission-verification script**
   (the issue's "Testable by" line) — `report.py --check` covers R1+R2 only.
4. A **submission bundle layout**: the AC words `runs/<run_id>/<instance_id>/…`;
   the harness writes `runs/<instance_id>/…` (a tested contract that the
   WP6b import + artifact self-heal depend on). Resolve by packaging (§3 D4),
   not by re-laying-out the harness.
5. The run-level **methodology meta is built in memory and passed to
   `report.generate` but never persisted** — for a submission the
   `run_meta.json` must exist next to the report.
6. **Operations**: a 20–50-day sequential run needs a per-window driver,
   a resume drill, disk/clone-cache governance and a per-window log —
   none of it is written down or scripted.
7. The submission **label** ("Devin (agent) on SWE-bench Verified"): today
   `all_preds.jsonl`'s `model_name_or_path` falls back to `model` =
   `swe-2-high`.

### 0.3 Two findings that must be stated before any quota is spent

* **Pilot B's live record was merged incomplete.** `notes/wp6b-pilot-b.md`
  carries 3 of the 8 window-1 instances; its own headings
  ("final run output, verbatim", "measured numbers", "2-run overlap") are
  still `_recorded when the final window completes_` placeholders, and the
  Pilot-B workdir is gone (the wp6a/wp6b Orca workspaces are deleted —
  `find` finds no `run-pilotB.jsonl` anywhere on this host). Consequences:
  the pass@1 **estimate** is 3+3 instances deep, not 25; the per-repo
  env/parser validation the pilot was supposed to buy did **not** happen;
  and there is no Pilot-B checkpoint to `--import-run` into the full run.
  ⇒ Decision D3 (§3): re-run a small stratified **Pilot B′** (12 instances,
  one per repo, container mode) as the first paid step of #23, or explicitly
  waive it and accept a blind 500-instance launch.
* **The official eval path has never executed.** WP4's positive/negative/
  isolation controls skip without Docker; Pilots A and B ran **sandbox mode**
  (a host "host-check": fresh clone + venv + `pip install` + `pytest -rA`),
  which is *our* grading leg, not `swebench.harness.run_evaluation` with the
  repo log parsers. A leaderboard/Cognition submission requires the official
  harness. ⇒ Decision D1 (§3) is the single biggest scope fork of this issue.

---

## 1. Acceptance-criteria map

| # | Issue #23 acceptance criterion | Delivered by | Evidence |
|---|---|---|---|
| AC1 | 500 instances, strictly sequential batches across quota windows, within the calibrated budget | §4 manifest, §5 `--pilot c`, §7 window driver, §9 calibration, §10 runbook | `run-full500.jsonl` with 500 unique finals; checklist item 12 per-UTC-window gate (sessions ≤ cap, wall ≤ cap × 45 min); the window log table in `notes/wp6c-full-run.md` |
| AC2 | Artifacts complete: `run-<id>.jsonl`, `all_preds.jsonl` (full patch strings), `trajs/<iid>.jsonl`, per-instance `runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,eval.log}` | §5 (harness artifacts), §6 `scripts/package_submission.py` (the `runs/<run_id>/<iid>/` layout), §6 `scripts/verify_submission.py` (integrity) | `verify_submission.py` exit 0 + its CI fixtures |
| AC3 | Markdown report with the full methodology block (CLI version, model + cost_tier, prompt hash, permission policy, caps, harness version, images, date range, sessions used) | §5 persisted `run_meta.json` + WP5 `report.md`; §6 completeness gate (no `unknown`) | `report.md` Methodology section + `verify_submission.py` "methodology complete" check |
| AC4 | Labeled "Devin (agent) on SWE-bench Verified"; ready for submission | §3 D5 + §5 (label in meta → `model_name_or_path`) + §11 bundle | `verify_submission.py` label check on every `all_preds.jsonl` line + the bundle `README.md` |
| Testable-by | Artifact-integrity script (every JSONL line's files exist; patch content matches); methodology-block completeness check; pass@1 cross-check against per-repo table | §6 `scripts/verify_submission.py` (wraps `report.check` R1+R2 **and** adds the meta-completeness + pass@1 ⟷ per-repo reconciliation + completeness + label checks) | `bench/tests/test_verify_submission.py` (zero quota, fixture run dirs; deliberate-break tests) |

---

## 2. Verified environment facts (re-verified 2026-11-13, this workspace)

1. `devin` **3000.12.43** (`517d03d73926`) — **drifted** from the 3000.11.92
   the WP0/WP6a recon recorded. `devin auth status` works from this session's
   bash (the WP0 "bash-blocked, delegate" fact no longer holds here) and
   reports **Tier: Devin Pro / Plan: Pro**, team `devin-team$account-6e0f…`.
   No quota/remaining figures are exposed by the CLI (WP0 §4 still true) —
   quota remaining is only visible in the web app (Settings ▸ Usage & limits).
2. **No container runtime** (`docker`/`podman`/`nerdctl` all absent) and
   **107 GB free** on `/` (83 % used). ⇒ Container mode (the publishable eval
   path) **cannot run in this workspace** (§3 D1, §8).
3. Network + HF datasets-server reachable. Fetched the full
   `SWE-bench/SWE-bench_Verified` `test` split: **500 rows, 500 unique
   `instance_id`s, 12 repos, `eval_type` = `pass_and_fail` for all 500, an
   `image` for all 500**. Per-repo counts match WP0 §5 exactly:
   django 231 · sympy 75 · sphinx 44 · matplotlib 34 · scikit-learn 32 ·
   astropy 22 · xarray 22 · pytest 19 · pylint 10 · requests 8 · seaborn 2 ·
   flask 1 (= 500). Raw payload ≈ 9.4 MB; the `KEEP`-field projection that
   `pin_pilot_b_instances.py` uses ≈ 4 MB. Note the API shape: fields are
   under `row["row"]`, with `row_idx`/`truncated_cells` siblings.
4. Measured session walls (the only real calibration we have): Pilot A
   **251.1 s / 411.5 s / 105 s**; Pilot B window 1 **277.0 s / 201.7 s /
   282.1 s** → mean ≈ 255 s, max ≈ 412 s (cap 2 700 s). Planning figure used
   below: **300 s/session** incl. setup+eval slack.
5. Python 3.12.3 here; the bench deps (`swebench==5.0.2`, pytest,
   pytest-timeout) are **not installed** — Phase 0 creates a venv from
   `bench/requirements.lock`.
6. `bench/live_pilot.py` constants that Phase 2 touches:
   `DEFAULT_MODEL="swe-2-high"`, `DEFAULT_MAX_WALL_S=2700`,
   `DEFAULT_DAILY_PAUSE_S=12 h`, `BUDGET_FILE=bench/live_pilot_budget.json`
   (optional; "budget is config, not code"), `RECORD_NAMES={"a","b"}`,
   `DEFAULT_INSTANCES_FILE` / `DEFAULT_B_INSTANCES_FILE`,
   per-instance artifacts under `<workdir>/runs/<instance_id>/`.

---

## 3. Decisions to close before any quota is spent (the G0 gate)

Each has a recommendation and a default. **D1 and D2 are blocking**; D3–D5
have safe defaults and may be accepted as written.

**D1 — Which eval leg produces the published score.** (blocking)

| Option | What it means | Cost / requirement | Verdict |
|---|---|---|---|
| **A. Container mode, official harness** (recommended) | Sessions *and* grading inside the published `sweb.eval.x86_64.<iid>` images; `evaluate.py` → `run_evaluation` in a fresh container, repo log parsers, FAIL_TO_PASS/PASS_TO_PASS | A docker-capable host with ≥ 300 GB free (WP0 §7: 500 images ≈ 1–2 TB unpinned; per-instance pruning keeps the peak far lower — but the union of shared layers for 12 repos must be **measured**, §8.1). Not this workspace. | The only option that is unambiguously **submission-ready** per PLAN §1/"the official harness path". |
| **B. Sandbox mode, disclosed deviation** | Reuse the Pilot A/B path verbatim (host clone + venv + `pytest -rA`) | Runs here, cheap disk | Honest, reproducible, but *our* grading leg → label as an internal estimate; **not** leaderboard-submittable. Also empirically fragile on the big repos (Pilot B's django lines all `eval_error`'d on the install/apply legs). |
| **C. Hybrid** | Agent sessions in sandbox mode; the final `all_preds.jsonl` is then graded by the **official** harness in container mode (zero extra devin sessions) | Docker host needed, but only for the eval pass (~500 image pulls, no `devin`) | Official *grading*, non-official *agent environment* (the agent ran on host python, not the conda `testbed` env). Must be disclosed in the methodology block. Use if A is blocked on infrastructure but an official number is required. |

Default if the operator does not answer: **A, with C as the fallback if the
docker host only materialises for the eval pass.** No session is spent until
D1 is answered, because D1 changes *where* the run runs.

**D2 — Quota authorization + the real per-window cap.** (blocking)

500 sequential sessions is the whole issue. At the WP6a-calibrated
`max_sessions_per_day = 10` the run is **50 UTC windows ≈ 50 days** (§13).
The account is Pro (daily **and** weekly quota; the numbers are only in the
web app). Before the first batch the operator must:
1. read the current daily/weekly allowance in app.devin.ai ▸ Settings ▸
   Usage & limits and state how many sessions/day the 500-run may consume
   and by when it must finish;
2. confirm the model stays inside the free gate (`cost_tier == "Free"` —
   `swe-2-high`; the gate is fail-closed and unchanged — a paid model would
   additionally need `--allow-paid` + a token budget, PLAN §2.4);
3. sign off the abort rule (§10.4).
The answer lands in `bench/live_pilot_budget.json`
(`{"max_sessions_per_day": N, "max_wall_per_session": 2700, "calibrated_at": …,
"basis": "WP6c §9 burst + Pro allowance <date>"}`) — config, not code.
The wall leg is not binding: 45 min × N ≫ the measured ~270 s/session
(the item-12 wall check will run at ~10 % utilisation).

**D3 — Pilot B's evidence gap.** §0.3. Default: run **Pilot B′** — 12
instances, one per repo, in the D1-chosen mode, ~1 window — as step 1 of the
run (§9.2), and treat its per-repo eval-leg success rate as the gate: any
repo whose *infra* leg fails (`env`-class `eval_error`) is fixed before the
full run rather than discovered 200 times. This costs one window and buys
what the original Pilot B was supposed to buy.

**D4 — The `runs/<run_id>/<iid>/` layout.** Default: keep the harness layout
(`runs/<iid>/`, load-bearing for the WP6b import + artifact self-heal tests)
and let `scripts/package_submission.py` **materialise** the AC's layout in the
submission bundle, with `verify_submission.py` asserting the bundle layout and
byte-equality against the checkpoint. Rationale: zero churn in a 5 500-line
tested contract; the AC is about the *submitted artifact*, not the scratch dir.

**D5 — The submission label.** Default: the run meta carries
`model_name_or_path = "Devin (agent) on SWE-bench Verified"` (which
`report._model_name_or_path` propagates into every `all_preds.jsonl` line),
plus a `label` line in the report header and in the bundle README, plus
`model: swe-2-high` kept as the *model* field so the methodology block stays
honest about which model ran.

---

## 4. Phase 1 — Pin the 500-instance manifest (zero quota, ~1 h)

* `bench/scripts/pin_verified_instances.py` (new) — reuse
  `pin_pilot_b_instances.py`'s fetch (the retry, the error-shaped-200 guard,
  the `row_idx` schema guard, the no-progress guard, the de-dupe) but with
  **no selection**: keep all 500 rows in `row_idx` order. Assert and record:
  `len == 500`, `len(set(instance_id)) == 500`, 12 repos, the per-repo counts
  of §2.3, `image` present on every row, `FAIL_TO_PASS`/`PASS_TO_PASS` lists
  on every row, and that the 25 Pilot-B + 3 Pilot-A ids are a subset (the
  overlap statement for the record). `--check` mode: re-fetch and fail if the
  committed file is not byte-reproducible (the WP6b convention).
* `bench/verified_instances.json` (new) — `_meta` (source, fetched_via,
  fetched_at, rule = "all 500 rows of the `test` split in row_idx order",
  the per-repo counts, the sha256 of the instance list) + `instances`.
  ≈ 4 MB, committed (provenance beats size here; `pilot_b_instances.json`
  set the precedent).
* Test: `tests/test_pin_verified.py` — a fixture-driven test of the
  assert/validation layer (no network in CI), plus a `--check`-mode test
  against a cached payload.

## 5. Phase 2 — Full-run mode in `live_pilot.py` (zero quota, ~1–1.5 d)

* `PilotConfig`: `pilot` ∈ `("a","b","c")`; `DEFAULT_C_INSTANCES_FILE =
  bench/verified_instances.json`; workdir default `./full-run`;
  `RECORD_NAMES["c"] = "wp6c-full-run.md"`; `_pilot_title` for `c`.
* `--import-run` under `--pilot c`: **allowed but mode-gated** — refuse
  (config error, exit 2, pre-session) an import whose source run's recorded
  mode ≠ this run's mode. Rationale: importing a sandbox-mode `resolved`
  into a container-mode pass@1 would silently mix two grading legs. (Today
  `--import-run` is refused for anything but `--pilot b`; the pilot-B
  requirement itself must not apply to `c`.)
* Persist the run meta: write `<workdir>/report/run_meta.json` (the dict
  already built at the report step) **and** pass it to `rep.generate`, so the
  methodology inputs are part of the artifact set and `report.py --meta-file`
  can re-verify later. Add `label` + `model_name_or_path` (D5) and
  `mode` (`container`/`sandbox`) + `devin_cli_version` (now 3000.12.43) to it.
* New **full-run checklist items** (rendered for `--pilot c` only, so the
  WP6a 14-item and WP6b 17-item contracts and their tests are untouched):
  * **18 Completeness** (gating): 500 distinct instances have a *final* line;
    every manifest id is covered; zero retry-class finals pending
    (`quota_limited`/`eval_error`/`preflight_failed` all resolved by resume).
  * **19 Artifact integrity** (gating): `report.check(jsonl, out, meta)`
    returns `[]` (R1 referenced files exist + the `resolved`/`failed_test`
    ⇒ existing `patch.diff` rule; R2 `all_preds.jsonl` ≡ `patch.diff`
    byte-for-byte) **and** `trajs/` has one file per instance that produced a
    trajectory.
  * **20 Methodology completeness** (gating): no field of the 11 renders
    `unknown` (run_id, CLI version, model, cost_tier, prompt hash, permission
    policy, caps, harness version, images, date range, sessions used) **and**
    `sessions_used` reconciles with the checkpoint attempt count.
  * **21 pass@1 cross-check** (gating): overall `resolved`/`terminal` ==
    Σ over the per-repo table (and the recomputed pct matches to the rendered
    precision); the denominator equals
    `TERMINAL − RETRY` per PLAN §2.5.
  * **22 Submission label** (gating): every `all_preds.jsonl` line's
    `model_name_or_path` == the D5 label; the report header carries it.
  * **23 Batch discipline** (gating): the run spanned ≥ 1 UTC window; every
    window ≤ `max_sessions_per_day` sessions and ≤ `max_wall_per_session ×
    max_sessions_per_day` wall (the item-12 per-window math, restated for the
    batch plan); at most one session ever live (`pilot.lock` + the WP3
    overlap guard evidence).
* Verdict for `c`: GO ⟺ items 1–14 + 18–23 (15–17 render `n/a`), item 13
  stays deviation-tolerant/non-gating but its `graded_by` **is printed in the
  submission bundle** (it is the D1 honesty record).

## 6. Phase 3 — The verification + packaging scripts (zero quota, ~1 d)

* `bench/scripts/verify_submission.py` — the "Testable by" script, stdlib
  only, exit 0 = verified / 2 = bad input / 3 = integrity violation:
  1. `report.check` (R1 + R2) over the run JSONL and the report dir;
  2. methodology completeness (no `unknown`; `run_meta.json` present and
     consistent with the report block);
  3. completeness vs the manifest (500 finals, no pending retry class);
  4. pass@1 ⟷ per-repo reconciliation (re-derived from the JSONL, not from
     the rendered markdown, plus a markdown parse to catch render drift);
  5. label check on every `all_preds.jsonl` line;
  6. bundle layout check: `runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,
     eval.log}` present where the checkpoint references them.
  Prints a one-line-per-check PASS/FAIL table; a single violation ⇒ exit 3.
* `bench/scripts/package_submission.py` — builds
  `submission/<run_id>/` with `all_preds.jsonl`, `trajs/`,
  `runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,eval.log}`, `report.md`,
  `run_meta.json`, `run-<id>.jsonl` and a `README.md` (the label, the
  methodology block, the **deviations section** mandated by D1/D3, the
  sha256 of every file). Deterministic (sorted, no timestamps of its own) so
  a re-package is diff-clean.
* Tests: `bench/tests/test_verify_submission.py`,
  `test_package_submission.py` — fixture workdirs (happy + every deliberate
  break: missing `patch.diff`, mutated patch, `unknown` meta field, a repo
  row that no longer sums, a wrong label, a missing traj). Zero quota, zero
  network, zero Docker.

## 7. Phase 4 — Multi-window operation (the part that actually breaks) (~1 d)

A 500-session run spans 10–50 UTC windows; a single 50-day process is not a
realistic operator assumption, and PLAN §2.5 exists precisely so that
cross-invocation resume is safe. So: **one invocation per window**, driven by
a supervisor.

* `bench/scripts/run_window.sh` (new) — `BENCH_LIVE=1 python live_pilot.py
  --pilot c --mode <D1> --instances verified_instances.json --run-id
  <run_id> --workdir <dir> --resume --interrupt-after <cap>`; captures
  stdout+stderr to `<dir>/windows/<UTC-date>-<attempt>.log`, records
  `date`/exit-code, and treats 0 (queue empty ⇒ done), 42 (window cap
  reached ⇒ re-invoke next window) and 4 (abort ⇒ **stop, human triage**)
  distinctly. Never retries a 4 in a loop; never runs while `pilot.lock` is
  held (the overlap guard is the harness', the script must not defeat it).
* `bench/scripts/run_status.py` (new) — read-only progress: windows used,
  sessions attempted, finals by status, the pending queue head, ETA at the
  current cap, disk free, clone-cache size. Prints the row that gets pasted
  into the window log. Zero quota, safe to run any time.
* The **resume drill** (a hard gate before the paid run): on a mock-driven
  `--pilot c` run (zero quota, `mock_devin`) kill mid-instance (SIGKILL),
  resume, assert: never-rerun statuses skipped, retry-class re-run, no
  duplicate lines, `report.check` still clean. Already covered for pilots in
  `test_live_pilot.py`; extend for the 500-item manifest + the new items.
* Disk governance: sandbox mode ≈ one fresh clone + one venv per instance
  (pruned per instance by the WP3c/WP6b pruning; 12 bare repos retained in
  `clone-cache/`) — measure it in §9 and set a `run_status.py` threshold;
  container mode uses the WP3c image/container pruning with the pre-flight
  check sized at 2 × the largest image (measured in §8.1).

## 8. Phase 5 — Eval-infrastructure bring-up (D1-dependent; 1–2 d)

Only for option A/C. Steps, each with a recorded number:

1. **Provision a docker host** (any Orca workspace with a runtime, or a VM).
   `docker version` + `docker info` recorded in `notes/wp6c-infra.md`.
2. **Measure the real image cost** before promising disk: pull 3 images from
   different repos (a django, a sympy, a flask), record
   `docker system df -v` before/after each, and extrapolate the shared-layer
   union for 12 repos. Update the PLAN §7 estimate with the measurement.
3. **In-image devin smoke** (the WP0 §9 open item that never closed): inside
   an eval image, with the real binary + `_versions` tree mounted (never the
   wrapper) and an ephemeral XDG dir: `devin --version`, an HTTPS call to the
   API, `python -V` under `PATH=/opt/miniconda3/envs/testbed/bin`. Closes the
   glibc/CA risk that WP0 left open — a failure here invalidates container
   mode for the whole 500-run and must be known **before** sessions are spent.
4. **WP4 real controls**: run the Docker-gated tests that have never executed
   (`tests/test_evaluate.py`: positive control on a known-resolved patch,
   negative control, corruption guard, isolation) and record them.
5. **Container-mode 3-instance smoke** (≤ 3 sessions, reuses the Pilot-A
   instances): full adapter path + `evaluate()` + pruning, then
   `verify_submission.py` over the 3-instance checkpoint.

## 9. Phase 6 — Calibration + the stratified Pilot B′ (live, 1–2 windows)

1. **§9.1 Budget burst**: run one window at the cap agreed in D2; record
   sessions attempted, `quota_limited`/429 occurrences, mean/p95 session
   wall, tokens, and the web-app usage delta before/after. Write
   `bench/live_pilot_budget.json` + the numbers into
   `notes/wp6c-full-run.md` §Calibration.
2. **§9.2 Pilot B′** (D3 default): 12 instances, one per repo, D1 mode.
   Gate: every repo produces a **graded** outcome (any status except an
   `env`/`parser`-class `eval_error`). Fix the env/parser per repo before the
   500-run; a repo that cannot be made to grade is a documented exclusion
   (and then AC1's "500" is disclosed as 488 + 12 excluded, never silently).
3. **CLI drift check** (§2.1): the `BENCH_LIVE`-gated handshake smoke against
   3000.12.43, confirming the WP0 wire surface (NDJSON, `session/new`,
   `session/prompt`, `session/cancel`, `session/request_permission`,
   `session/close`, the ignorable vendor notifications) is unchanged. Record
   the version in the meta; a wire change is a stop-ship, not a pilot detail.

## 10. Phase 7 — The 500-instance run (the runbook)

1. **Start**: `run_window.sh` with `--run-id full500 --workdir ./full-run`,
   window 1 also creating `notes/wp6c-full-run.md` skeleton.
2. **Per window** (operator, ~5 min of attention): confirm the previous
   window's exit was 0/42; run `run_status.py`; invoke the next window; paste
   the status row into the window log; check disk; check quota in the web app
   every 5 windows.
3. **Triage rules** (fixed in advance so no window invents policy):
   `eval_error(env)` ⇒ resume the instance (retry class), root-cause it, log
   it; `eval_error(parser)` ⇒ same, plus a per-repo parser note (WP6b item
   17's classification is already recorded on the line); `timeout`/`no_patch`
   /`agent_error` ⇒ **terminal, never re-run** (PLAN §2.5 — re-running them
   turns pass@1 into a filtered pass@k); 2 consecutive `agent_error` ⇒ the
   WP3 abort (environment drift) — fix the environment, `--resume`.
4. **Abort rule** (D2.3, sign-off required): stop the run if (a) quota
   exhaustion would push completion past the agreed deadline, (b) any
   evidence of upstream leakage (the leak-guard verify fails on any instance
   — item 14), (c) ≥ 3 instances in a window classified `patch`-class
   `eval_error` (a systematic patch-extraction fault), or (d) any session
   observed running with a non-Free `cost_tier` model.
5. **Finish**: a window exits 0 with 500 unique finals ⇒ item 18 gate; the
   harness writes the report + record; then Phase 8.
6. **Mid-run publishes**: the report is regenerable from the checkpoint at
   any time (`report.py --jsonl … --meta-file run_meta.json`), so an interim
   pass@1 is always available; the checkpoint is the source of truth, not
   memory.

## 11. Phase 8 — Report, bundle, submission

1. `python report.py --jsonl full-run/run-full500.jsonl --out full-run/report
   --meta-file full-run/report/run_meta.json` then `… --check` (must be clean).
2. `python scripts/verify_submission.py …` ⇒ exit 0.
3. `python scripts/package_submission.py …` ⇒ `submission/full500/…` (D4
   layout, checksums, README with the label + methodology + **deviations**).
4. `notes/wp6c-full-run.md` (the operator record): the calibration, the
   window table, the failure taxonomy with root causes, the resume/interrupt
   evidence, the per-repo table, the verdict, the deviations, and the exact
   commands run (the WP6a record's format — that record is the model).
5. Submission steps (external, documented in the bundle README, executed by
   the operator): the SWE-bench leaderboard submission form + the Cognition
   publication handoff, with the results labeled **"Devin (agent) on
   SWE-bench Verified"** and the harness/prompt hashes for reproducibility.

## 12. Phase 9 — Docs, PR, close-out

* `bench/README.md`: the WP6c section (the commands, the two scripts, the
  window model, the D1/D4 decisions), the file table rows for
  `verified_instances.json`, `scripts/*`, `PLAN-WP6C.md`.
* `bench/PLAN.md`: §5 WP6.3 annotated with the realized batch model
  (per-window invocation rather than one 50-day process) and the §7 disk
  estimate replaced by the §8.2 measurement.
* PR → **`dev`** (`bench: WP6c — … (issue #23)`); CI green (Node + pack +
  Python, zero quota); the PR body carries the AC→evidence table of §1.
* Then: close #23, update the epic #9 (its §10 definition of done #4 is the
  last open item — this issue is what closes the epic).

## 13. Budget & schedule arithmetic (the honest headline)

| Cap (sessions/UTC window) | Windows for 500 fresh instances | Session wall (≈300 s avg) | Calendar |
|---|---|---|---|
| 10 (WP6a calibration, conservative) | 50 | ≈ 41 h of sessions | **≈ 50 days** |
| 20 | 25 | ≈ 41 h | ≈ 25 days |
| 25 | 20 | ≈ 41 h | ≈ 20 days |
| 36 | 14 | ≈ 41 h | ≈ 14 days |
| 50 | 10 | ≈ 41 h | ≈ 10 days |

* The issue's "≈ several days" estimate is not reachable at the calibrated
  10/window; it needs D2 to authorise a higher cap **and** the Pro daily/
  weekly allowance to survive it. This must be settled before the run, not
  discovered in window 3.
* Session-cap utilization is ~270 s of a 2 700 s per-session cap; the daily
  **wall** budget (cap × 45 min) is never the binding constraint.
* Add for infra: ~40 h of official-harness eval time (container mode,
  parallel-allowed — evaluation is not quota-bound, only the sessions are),
  ~1–2 windows for Pilot B′ + the calibration burst, and the engineering
  phases (§4–§7, ~4–5 days).
* Reuse note: there is **no** Pilot-B checkpoint to import (§0.3), so the
  25 pilot instances are re-spent in the 500 unless Pilot B′ is run in the
  same mode as the full run (then `--import-run` legitimately saves 12).

## 14. Test matrix (all of #23's engineering is zero-quota; only §9–§10 spend)

| Layer | Runs where | Devin? | What |
|---|---|---|---|
| Unit | CI every commit | mock only | pinning validation, `--pilot c` config + validation (incl. the mode-gated import refusal), checklist items 18–23, `verify_submission` every-break case, packaging determinism |
| Integration (mock) | CI every commit | `mock_devin` | the full `--pilot c` pipeline over a 6-instance manifest incl. a synthetic multi-window checkpoint, the SIGKILL+resume drill, report + verify + package end to end |
| Integration (docker) | CI daily / the §8 host | no | `test_evaluate.py`'s positive/negative/corruption/isolation controls (never executed to date) |
| Live (gated) | operator, `BENCH_LIVE=1` | yes | the §8.5 3-instance container smoke, the §9 calibration burst + Pilot B′, the §10 500-run |

CI must still never consume quota (PLAN §6): every new live path stays behind
`BENCH_LIVE=1` and the existing static guard that fails the suite on an
unmarked real-Devin test.

## 15. Risks specific to WP6c

| Risk | Mitigation |
|---|---|
| D1 unresolved ⇒ sessions spent in a mode whose score can't be submitted | G0 gate: no session before D1; the harness records `mode` in the meta and the report, and `verify_submission` prints it in the deviations block |
| Quota ceiling hit mid-run (weekly cap) | D2 pre-check in the web app; per-window accounting (item 12/23); `quota_limited` is the retry class — instances resume, they are never lost; abort rule (a) |
| 50-day run outlives the operator's attention | per-window driver + read-only `run_status.py` + the checkpoint as the single source of truth; a dead window is a resume, not a loss; §10's 5-min routine |
| Sandbox host-check cannot grade the big repos (measured in Pilot B: django `eval_error`s) | D1 option A/C; Pilot B′ as the per-repo gate before the 500 |
| CLI drift (3000.11.92 → 3000.12.43) changed the ACP surface | §9.3 handshake smoke before window 1; the wire surface is asserted per session by items 4/5/7 anyway |
| Disk: 107 GB here, images 1–2 TB unpinned | container mode off this host; §8.2 measurement; WP3c pruning + the 2 × largest-image pre-flight |
| Pass@1 inflated by accidental re-runs | §2.5 statuses are enforced by `resume_queue` (tested); item 18 requires exactly one *final* per instance; the retry-class exclusion is WP5-tested |
| 4 MB manifest silently drifting from the published split | `pin_verified_instances.py --check` re-fetches and diffs; the `_meta` sha256 of the id list |
| Reviewers relitigate the layout (`runs/<run_id>/…`) | D4 documented in this plan + the bundle layout asserted by `verify_submission` (the AC's wording is satisfied in the submitted artifact) |
| A partial run gets quoted as a result | `verify_submission` exits 3 unless completeness holds; interim reports are labelled interim in the record |

## 16. Out of scope (unchanged from PLAN/epic #9)

Agentless / raw-model runs (no completions endpoint for SWE models); paid
`cost_tier` models (they need an explicit override + a token budget, and this
run is a free-tier-model run); concurrency > 1 (the overlap guard and
`--i-know-about-quota` stay); changes to the npm package surfaces.

## 17. Definition of done (issue #23 ⇄ this plan)

1. `verified_instances.json` + its `--check` script committed; 500/12-repo
   assertions green in CI. (§4)
2. `live_pilot.py --pilot c` with the persisted `run_meta.json` and the
   full-run items 18–23, all covered by zero-quota mock tests; CI green
   (Node + pack + Python). (§5, §14)
3. `verify_submission.py` + `package_submission.py` with the break-detection
   fixtures green. (§6)
4. A recorded resume drill (kill mid-instance → resume → correct retry
   classes) and a clean per-window driver log format. (§7)
5. The official eval leg actually executed: `test_evaluate.py`'s Docker
   controls + the in-image devin smoke + a 3-instance container-mode run —
   or an operator-waived D1 deviation recorded verbatim in the submission
   bundle. (§8)
6. `run-full500.jsonl` with 500 unique finals across ≥ 1 UTC window, all
   within the calibrated cap, no duplicate sessions, `pilot.lock`-guarded.
   (§9–§10)
7. `report.md` with a complete methodology block (no `unknown`),
   `all_preds.jsonl` with full patch strings labeled "Devin (agent) on
   SWE-bench Verified", `trajs/`, the `runs/<run_id>/<iid>/` bundle, and
   `verify_submission.py` exit 0. (§11)
8. `notes/wp6c-full-run.md` complete (calibration, windows, taxonomy with
   root causes, deviations, verdict) and `bench/README.md` + `bench/PLAN.md`
   updated; PR merged into `dev`; #23 closed and epic #9's definition of
   done #4 satisfied. (§12)

## 18. Open questions for the operator (with defaults, so silence is safe)

1. **D1** container/sandbox/hybrid? *(default: A; C if the docker host is only
   available for the eval pass)*
2. **D2** sessions/window and the deadline? *(default: 10/window ⇒ ~50 days;
   no session starts until answered)*
3. **D3** Pilot B′ (12 instances, 1 window) or waive? *(default: run it)*
4. **D4** package-time layout vs. harness re-layout? *(default: package-time)*
5. **D5** exact label string? *(default: "Devin (agent) on SWE-bench
   Verified")*
6. Where do the run's artifacts live durably for 50 days (this workspace is
   disposable — the wp6a/wp6b workspaces are already gone)? *(default: an
   operator-owned host directory, plus a checksummed bundle pushed to the
   repo/releases at the end — **this is the §0.3 failure mode repeating**)*
