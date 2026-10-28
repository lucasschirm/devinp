# WP5 Implementation Plan — `report.py`: aggregation, failure taxonomy, methodology block, leaderboard artifacts

Implements issue #18 (part of #9). Plan of record: `bench/PLAN.md` §5 WP5.
Develops against the WP3 JSONL schema (issue #14/#15); fixture tests do not
block on WP3 code (the schema is pinned by `runner.py`'s
`JsonlCheckpoint`/`load_checkpoint`, which report.py *reuses by import*).
Estimate: M (3 pts).

**Acceptance (issue #18):**
1. pass@1 overall + per-repo table from the run JSONL.
2. Failure taxonomy: `timeout`, `no_patch`, `quota_limited`, `agent_error`,
   `resolved`, `failed_test`, `preflight_failed`, `eval_error`.
3. Methodology block: devin CLI version, model + `cost_tier`, prompt
   template hash, permission policy, caps, harness version, images, date
   range, sessions used.
4. Emits **leaderboard artifacts**: `all_preds.jsonl` (full `model_patch`
   strings per instance) and `trajs/<instance_id>.jsonl` (per-instance
   action trajectories).
5. Artifact cross-check: every JSONL line's referenced files exist;
   `all_preds.jsonl` patch content matches `patch.diff`.
6. Testable by (unit, CI): fixture JSONL → exact expected numbers
   (resume-dedup, terminal-status exclusion, artifact cross-checks).

## Schema facts (verified in this repo, not re-derived)

1. **The run JSONL line** (WP3b `JsonlCheckpoint.record_instance`,
   `runner.py`): `{"instance_id": str | None, "status": str, "meta":
   dict, "ts": float-epoch}` — one fsync'd line per finished instance.
   `instance_id = None` is the **batch-level marker** (a pre-flight
   failure before the first instance; consumes no queue slot).
2. **Status split** (WP3b, `runner.py`): `TERMINAL_STATUSES` = all 8
   taxonomy values; `RETRY_STATUSES` = `{quota_limited, eval_error,
   preflight_failed}` — the *only* retry class (PLAN §2.5: interrupted
   before a legitimate agent outcome, "nothing was burned"). The
   **pass@1 outcome set** = `TERMINAL_STATUSES − RETRY_STATUSES` =
   `{resolved, failed_test, timeout, no_patch, agent_error}`: `timeout`
   and `no_patch` are *legitimate agent outcomes* (PLAN §2.5 — re-running
   them "would turn pass@1 into a filtered pass@k"), `agent_error` is
   "a terminal agent outcome" (README WP3b) — all five count in the
   denominator. The three retry-class statuses are **excluded** from the
   pass@1 denominator (the outcome is undecided; the instance is pending
   retry) but are counted in the failure taxonomy.
3. **Resume-dedup rule** (WP3b `resume_queue`, README: "a duplicated line
   resolves to the last line, so no instance runs twice"): per instance,
   the **last line wins** (file order, not timestamp — a resumed run
   appends its retry line after the superseded one). report.py applies
   the identical rule to the *report's* view of the run: an instance's
   final status is its last line's status.
4. **Reading semantics**: report.py reads the run JSONL via
   `runner.load_checkpoint` (import, not re-implementation — one source of
   truth for the WP3 schema): a torn **last** line is *dropped, not
   fatal* (a kill mid-write); a corrupted **mid-file** line raises
   `RunnerProtocolError` (fail-loud). The dropped-tail fact is surfaced
   in the report (a note line).
5. **The standard predictions-file fields** (WP4, `evaluate.py`
   `_write_predictions_file`; the harness's `get_dataset_from_preds`):
   exactly `instance_id`, `model_name_or_path`, `model_patch` —
   `all_preds.jsonl` uses those exact field names (a leaderboard
   artifact, per PLAN §5 WP5, is a predictions file the official harness
   consumes). **Empty-patch semantics** (WP4, verified against the pinned
   `swebench==5.0.2`): an empty/`None` `model_patch` instance is skipped
   by the harness and counted in `empty_patch_ids` (never in
   `resolved_ids`) — so instances with no patch carry `model_patch: ""`
   in `all_preds.jsonl` and can never be misread as resolved.
6. **The per-instance artifact layout** (PLAN §10.4 "definition of
   done"): `runs/<run_id>/<instance_id>/{patch.diff, trajectory.jsonl,
   eval.log}`. The JSONL `meta` carries **artifact paths** (WP3b doc:
   "meta (the artifact paths / usage the adapter passes in)"). The
   adapter WP is not in this repo yet, so **WP5 defines the meta
   contract it consumes** (the adapter conforms to it):

   ```
   meta = {
     "artifacts_dir": str,          # optional; base dir for relative paths
                                   #   (default: the run JSONL's directory)
     "patch_diff":  str | null,    # path to the instance's patch.diff
     "trajectory":  str | null,    # path to the instance's trajectory.jsonl
     "eval_log":    str | null,   # path to the instance's eval.log
     "usage":       dict,         # opaque; carried into the report, not
                                   #   interpreted (WP0 §2.6 proxies or
                                   #   ACP usage.{inputTokens,…})
   }
   ```

   A `null`/absent reference = "no such artifact" (a `no_patch` instance
   has no `patch_diff`). A **non-null reference must resolve to an
   existing file** (the cross-check, AC5; fail-loud).
7. **The instance-id → repo mapping.** SWE-bench Verified instance ids
   are `<owner>__<repo>-<number>` (WP0 notes §5: e.g.
   `astropy__astropy-12907`; the 12 Verified repos). `repo_of(iid)` =
   the prefix before the **last** `-` (the number is always the last
   dash segment — correct for dash-bearing repo names like
   `scikit__scikit-learn-99` → `scikit__scikit-learn`); an id with no
   `-` is its own repo key (synthetic fixtures). The mapping is pure and
   needs no dataset download (zero-quota CI).

## Design decisions (pinned by the tests)

1. **pass@1 = resolved / terminal**, where *terminal* = the AC2 outcome
   set above (fact 2). Per-repo: the same formula per `repo_of` group,
   over the **deduped** final status (fact 3). A repo with zero
   terminal instances renders `n/a` (not 0%, not a division by zero).
   The overall figure is the sum of resolved over the sum of terminal
   across *all* instances (not a mean of per-repo rates — it is the
   global pass@1).
2. **Resume-dedup**: `dedupe(records)` → `{instance_id: last-record}`
   (file order; `instance_id = None` lines are *not* instance records —
   they are counted separately as **batch markers** in the taxonomy
   section; they can never shadow or be shadowed by an instance line).
3. **Terminal-status exclusion** (the issue's explicit test case):
   instances whose *final* status is in `RETRY_STATUSES` are **absent
   from the pass@1 numerator and denominator** (undecided), present in
   the failure taxonomy, and present in `all_preds.jsonl` — with
   `model_patch: ""` when no patch is referenced / exists (fact 5 — the
   harness's own empty-patch semantics keeps them out of
   `resolved_ids`); when the line references an *existing* `patch.diff`,
   that content is emitted regardless of status (the agent's work is
   preserved — on a resume re-evaluation, that is exactly the patch
   that will be graded).
4. **`all_preds.jsonl`** (AC4): one line **per deduped instance** (all
   8 final statuses, incl. pending retry-class), **sorted by
   `instance_id`** (deterministic; byte-stable across regenerations),
   exactly the harness field set (fact 5). `model_patch` = the full
   contents (UTF-8) of the referenced `patch.diff` when the reference is
   present and the file exists (AC5 guarantees existence on the
   generate path), else ``""`` (no reference / no file — the harness's
   own empty-patch semantics keeps it out of ``resolved_ids``); a
   referenced, existing patch is emitted for *any* status (the agent's
   work is preserved for a resume re-evaluation). `model_name_or_path`
   = meta-file
   `model_name_or_path` → meta-file `model` → `"devin"` (the WP4
   `DEFAULT_MODEL_NAME`).
5. **`trajs/<instance_id>.jsonl`** (AC4): for each deduped instance with
   a present, existing `trajectory` reference, a **byte-for-byte copy**
   of that file into `<out>/trajs/<instance_id>.jsonl` (no
   re-serialization — the trajectory is the adapter's, verbatim). No
   reference / no file → no `trajs/` entry (absence = no trajectory
   captured; not an error).
6. **Cross-check** (AC5) — run on **generation** (fail-loud before the
   report is declared complete) and standalone (`--check`), with two
   rules, checked over **every complete line** (not just the deduped
   view — a superseded line's artifacts are part of the run record too,
   WP2 artifact atomicity):
   * **R1 — referenced files exist**: every non-null `patch_diff` /
     `trajectory` / `eval_log` reference (resolved against
     `meta.artifacts_dir`, else the JSONL's directory) points at an
     existing file. Plus the semantic rule: a **`resolved` or
     `failed_test` final status must reference an existing
     `patch_diff`** (a graded verdict requires a patch — the harness
     graded `patch.diff`; its absence is a pipeline bug, fail-loud).
   * **R2 — content match**: after `all_preds.jsonl` is written (or in
     `--check` against the existing one), re-read it and, for every
     line with a non-empty `model_patch`, byte-compare against the
     corresponding instance's referenced `patch.diff`.
   Any violation → `ArtifactCheckError` (typed, carries the full
   violation list; CLI exit 3 — the report is *not* trusted).
7. **Methodology block** (AC3): the **run-level** meta is a separate
   operator file (`--meta-file`, optional JSON — the WP3
   `JsonlCheckpoint` writes only instance lines; run-level facts are the
   operator's record):

   ```
   {"run_id", "devin_cli_version", "model", "cost_tier",
    "prompt_template_hash", "permission_policy",
    "caps": {"max_sessions_per_day", "max_wall_per_session_s",
             "daily_pause_h"},
    "harness_version", "images", "model_name_or_path"?}
   ```

   * **computed from the JSONL** (always present, exact): **date range**
     = min/max `ts` over all lines (ISO-8601 UTC, second precision);
     **sessions used** = the count of lines with `instance_id ≠ null`
     and `status ≠ "preflight_failed"` (a pre-flight failure stops
     *before* a session starts — PLAN §2.7 "stop before burning a
     session"; a `quota_limited` line is a started-then-refused attempt
     and counts; a re-run on resume is a second attempt and counts a
     second time — the figure is *session attempts*, documented as
     such).
   * **from the meta file**: the remaining fields. No meta file / missing
     field → the block still contains the field, value `unknown`
     (explicit — a submission-grade report never silently omits a
     methodology field).
8. **Markdown report** (`report.md` in the out dir — PLAN §10.4 "the
   markdown report with the methodology block"): a **Summary** (pass@1
   overall `r/t (pct)`, instance counts incl. pending, date range,
   sessions used, the dropped-tail note when `load_checkpoint` dropped
   one), a **Per-repo pass@1** table (sorted by repo key; `n/a` for
   zero-terminal repos), the **Failure taxonomy** table (all 8 statuses,
   zeros included, sorted in the issue's listed order — `timeout`,
   `no_patch`, `quota_limited`, `agent_error`, `resolved`,
   `failed_test`, `preflight_failed`, `eval_error` (issue #18 AC2;
   PLAN.md §5 WP5 agrees) + the batch-marker
   count, the **Methodology** block (every AC3 field, one per line), and
   an **Artifacts** section (all_preds line count, trajs file count,
   cross-check status `PASS` + the per-check detail). Deterministic
   (byte-stable for stable inputs: sorted iteration everywhere, no
   wall-clock reads into the output — `ts` values only).
9. **Typed errors + CLI** (the repo's fail-loud contract, mirroring
   `evaluate.py`'s exit-code discipline):
   `ReportError` (base) ← `ReportProtocolError` (a run-JSONL schema
   violation — wraps `RunnerProtocolError` from `load_checkpoint`;
   exit 4), `InvalidInputError` (bad flag / unreadable / non-JSON meta
   file; exit 2), `ArtifactCheckError` (an AC5 violation; exit 3).
   CLI: `python3 report.py --jsonl <run-<id>.jsonl> --out <dir>
   [--meta-file <meta.json>] [--check]` — default: generate
   `report.md` + `all_preds.jsonl` + `trajs/` **and** run the
   cross-check (fail-loud, exit 3 on violation); `--check`: verify an
   *existing* out dir against the JSONL (all AC5 checks, no writes).

## Files

* `bench/report.py` — new; pure stdlib (+ `import runner` for the WP3
  read semantics — both are `py-modules`); structure:
  `load_jsonl` (→ `runner.load_checkpoint`) · `dedupe` · `repo_of` ·
  `pass1` · `taxonomy` · `crosscheck` (R1+R2, pure: takes records + a
  path resolver) · `methodology` · `write_artifacts` (all_preds + trajs,
  sorted/deterministic) · `render_report` (markdown, pure over a
  computed dict) · `generate` · `check` · `main` (argparse).
* `bench/tests/test_report.py` — new; unit, CI, zero quota (tmp_path
  fixtures only; no devin, no Docker, no network).
* `bench/pyproject.toml` — `py-modules` += `report`.
* `bench/README.md` — WP5 section + layout-table row (the WP3b/WP4
  pattern: AC map, the meta contract, the cross-check rules, CLI + exit
  codes).

## Test plan (fixture JSONL → exact expected numbers, AC6)

The fixture is built in-test on `tmp_path` (10 lines + 1 batch marker +
torn-tail variant), with `patch.diff` / `trajectory.jsonl` files written
by the test — every expected number is asserted **exactly**:

* **F1 pass@1 + per-repo (AC1)** — fixture: `pallets__flask-5014`
  resolved; `pallets__flask-5015` `quota_limited` → re-run →
  `failed_test` (the dedup pair); `django__django-11001` timeout;
  `django__django-11002` no_patch; `astropy__astropy-12907` eval_error;
  `sympy__sympy-123` preflight_failed; `scikit__scikit-learn-99`
  eval_error; `mwaskom__seaborn-2650` resolved; `psf__requests-1234`
  agent_error; `matplotlib__matplotlib-2870` quota_limited (the run
  stopped at the quota — a *pending* final); a `None`-iid
  preflight_failed batch marker. 12 lines (11 per-instance + 1 marker),
  all 8 statuses non-zero in the final tally. Expected: 10 distinct
  instances; final taxonomy `resolved 2, failed_test 1, timeout 1,
  no_patch 1, agent_error 1, quota_limited 1, eval_error 2,
  preflight_failed 1`; terminal set = 6; **pass@1 overall = 2/6 =
  33.3%**; per-repo (exactly the eight repo rows, sorted):
  `pallets__flask 1/2 (50.0%)`, `psf__requests 0/1 (0.0%)`,
  `django__django 0/2 (0.0%)`, `mwaskom__seaborn 1/1 (100.0%)`,
  `astropy__astropy n/a`, `scikit__scikit-learn n/a`,
  `sympy__sympy n/a`, `matplotlib__matplotlib n/a`.
* **F2 resume-dedup** — the `flask-5015` pair (retry-class line followed
  by the terminal line → final = `failed_test`, *last line wins* —
  asserted including a line whose later `ts` is *older* than the
  superseded line's, pinning file-order authority); batch-marker lines
  never appear as instance
  records (and cannot shadow an `i0`-style instance).
* **F3 terminal-status exclusion (AC1/AC6)** — the four
  retry-class finals (`astropy-12907`, `sympy-123`,
  `scikit-learn-99`, `matplotlib-2870`) are in the taxonomy but
  **absent from the pass@1 denominator** (the 6, not 10); they are in
  `all_preds.jsonl` with `model_patch: ""` (no patch referenced in the
  fixture — fact 5; a referenced, existing patch is emitted regardless
  of status — `test_all_preds_preserves_referenced_patch_for_retry_
  class_final`).
* **F4 artifact cross-check (AC5)** — positive: all referenced files
  exist → `PASS`, the generate path completes; negative (each a typed
  `ArtifactCheckError`, exit 3): a referenced file missing; a
  `resolved` final with no `patch_diff` reference; an `all_preds.jsonl`
  line whose `model_patch` ≠ the `patch.diff` contents (force via
  `--check` after mutating the patch file post-generation). R1 is
  checked over *every* line (a superseded line's missing reference
  fails — asserted).
* **F5 trajs (AC4)** — the two instances with trajectory references get
  byte-identical `<out>/trajs/<iid>.jsonl` copies (exact bytes
  asserted, incl. a multi-line JSONL with a `\n`-embedded string); the
  rest get no file; `trajs/` contains exactly the 2 files.
* **F6 methodology (AC3)** — with a meta file: every AC3 field lands in
  `report.md` with the exact value; computed: date range =
  min/max `ts` (exact ISO strings); sessions used = 10 (the 11
  per-instance lines − the `sympy` preflight line; the `None`-iid line
  never counts — asserted exactly); without a meta file: every AC3
  field present with `unknown`; the computed fields are still exact.
* **F7 determinism + torn tail** — two generates on the same input are
  byte-identical (`report.md`, `all_preds.jsonl`, `trajs/`); a fixture
  with a torn final line → `load_checkpoint` drops it, the report
  proceeds on the complete records and *notes the drop* (exact note
  text asserted); a corrupted **mid-file** line → typed
  `ReportProtocolError` (exit 4).
* **F8 CLI** — the flag contract (`--jsonl`/`--out` required; `--check`
  with a healthy out dir exits 0, with a tampered one exits 3; a bad
  meta file exits 2; a bad status mid-file exits 4) via `subprocess`
  on `report.py`.

## Contingencies / out-of-scope

* The **adapter WP** is not in this repo — the meta contract (§facts.6)
  is WP5's definition; the adapter conforms to it (documented in the
  README so the adapter WP can find it).
* No dataset download, no Docker, no devin: `repo_of` parses the id
  (fact 7); the eval *verdict* is already the WP3 status (report.py
  never re-grades — the JSONL status is the record, per the WP3/WP4
  split).
* The per-repo table needs the Verified repo list only for *labeling*;
  it groups by the id prefix (no external input) — a pilot run over a
  subset of the 500 renders exactly the repos present.
* WP3c's post-eval pruning (issue #16) removes docker *images/
  containers*, never the per-instance *files* — R1's "referenced files
  exist" stays true for a pruned run.
