# WP4 Implementation Plan — `evaluate.py`: thin delegate to `swebench.harness.run_evaluation`

Implements issue #17 (part of #9). Plan of record: `bench/PLAN.md` §5 WP4.
Depends on: #10 (WP0 — the `swebench==5.0.2` pin; the `run_evaluation` input
format and the Verified split/images verified in the pinned source,
`bench/notes/acp-surface.md` §5).
Estimate: S (1 pt).

**Acceptance (issue #17):**
1. Delegates **entirely** to `python -m swebench.harness.run_evaluation` on a
   standard predictions file (`instance_id`, `model_name_or_path`,
   `model_patch`).
2. Runs in a **fresh** container; never reuses the agent's mutated container
   (working tree / submodules / installed packages are contaminated by the run).
3. Returns `{resolved, logs}`; no bespoke apply/grade logic inside.
4. Testable by (integration, CI, public images, no quota): **positive control**
   (known-resolved patch from a published Verified run → `resolved: true`),
   **negative control** (no-op/empty patch → `false`), **corruption guard**
   (truncated `model_patch` → clean typed error), **isolation** (eval container
   id ≠ agent container id).

## Verified environment facts (from the pinned `swebench==5.0.2` source)

Re-verified directly against the pinned wheel
(`swebench-5.0.2-py3-none-any.whl`, unpacked `swebench/harness/…`):

1. **CLI (WP0-verified, re-confirmed against the pinned `main()` argparse):**
   `python -m swebench.harness.run_evaluation -d <dataset> -s <split> -p
   <predictions.jsonl> [-i <instance_id>] -id <run_id> --max_workers N
   [-t <timeout>] [--report_dir <dir>]`. Defaults: dataset
   `SWE-bench/SWE-bench_Lite`, split `test`, `--max_workers 4`, `-t 1800`,
   `--report_dir .`. `-p` and `-id` are required. This module passes the
   Verified dataset, `--max_workers 1` (one call = one instance — the WP3
   runner is sequential), `-t` the per-instance test timeout, and
   `--report_dir <workdir>`.
2. **Log layout is CWD-relative:** `harness/constants/__init__.py` sets
   `RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")` — *relative to the
   process CWD*. So the harness writes
   `<cwd>/logs/run_evaluation/<run_id>/<model_name_or_path (with `/` → `__`)>
   /<instance_id>/{report.json, run_instance.log, test_output.txt, patch.diff,
   eval.sh}` plus `<cwd>/logs/run_evaluation/<run_id>/run.json` (the
   run-metadata record). ⇒ evaluate.py runs the harness with `cwd = workdir`
   (the per-instance artifact dir the runner owns) and `--report_dir workdir`.
   The **final run report** is written by the pin's `make_run_report` to
   `<report_dir>/<model>.<run_id>.json` (verified in the pinned
   `reporting.py` — `--report_dir` *is* honored in the 5.0.2 pin, default
   `.`). With `cwd = --report_dir = workdir`, every artifact lands under one
   per-instance directory (PLAN §5 WP2 "artifact atomicity").
3. **Per-instance report** (`report.json`): `{instance_id: {resolved,
   patch_is_None, patch_exists, patch_successfully_applied, infra_failure,
   infra_failure_reason?, tests_status?}}` — `infra_failure_reason` is
   conditional (set with the flag) and `FAIL_TO_PASS` / `PASS_TO_PASS`
   (alongside `FAIL_TO_FAIL` / `PASS_TO_FAIL`) live nested under
   `tests_status` (per `get_eval_report`, `grading.py:329-391`). **Final
   report** (`<report_dir>/<model>.<run_id>.json`, `schema_version: 2`)
   carries `resolved_ids` / `unresolved_ids` / `error_ids` /
   `empty_patch_ids` / **`infra_failure_ids`** (with per-instance
   `failure_reasons` — `reporting.py`): on unparseable test output from an
   environment fault, `grading.py:371-374` sets `infra_failure` + reason
   while keeping `resolved` False — the harness's *own* environment-fault
   classification, tallied separately from `unresolved_ids`. evaluate.py maps
   that classification to `HarnessFailedError` (the WP3 `eval_error` retry
   class) — never a terminal "not resolved" verdict.
4. **Empty patches are filtered by the harness itself**
   (`get_dataset_from_preds`): an empty/`None` `model_patch` instance is
   *skipped* — no container is created, no report is written, and the final
   report counts it in `empty_patch_ids` (never in `resolved_ids`). Official
   harness semantics: empty patch ⇒ not resolved. evaluate.py mirrors that
   exactly by short-circuiting (no harness spawn) — the *semantics* are the
   harness's; the short-circuit only saves the HF dataset download and the
   Docker handshake on a guaranteed no-op.
5. **Fresh container — the harness creates it** (`create_container`):
   `containers.create(image=<instance eval image>, name=
   "sweb.eval.<instance_id.lower()>.<run_id>")` — a brand-new container from
   the published eval image, force-removing any pre-existing container with
   that exact name first (ghost cleanup; on a 409 create conflict it retries
   once with a `.{timestamp}` name suffix). `cleanup_container` (finally
   block) stops and removes it. The agent's container (named
   `sweb.eval.<instance_id>` by the adapter, PLAN §1) is never referenced —
   there is *no parameter for it*. With a per-call unique `run_id`, the eval
   container name is structurally distinct from the agent's name. The harness
   logs the created container's id into `run_instance.log`
   (`Container for <iid> created: <id>` / `started: <id>`) — the source of
   truth for the isolation assertion.
6. **The harness swallows per-instance failures** (`run_instance`'s
   `except EvaluationError` / `except Exception` — it logs and *prints*,
   returns None, exit stays 0). A per-instance failure ⇒ no `report.json` for
   that instance ⇒ the final report counts it in `error_ids`. So "the process
   exited 0" is *not* a completed run; the per-instance `report.json` is.
7. **Harness-written failure markers** (the only triage signals — no
   re-grading):
   - `>>>>> Patch Apply Failed` (`APPLY_PATCH_FAIL`) — written to
     `run_instance.log` after the 4-command git-apply chain
     (`git apply` / `--3way` / `--reject` / `patch --fuzz=5`) fails; the
     harness then raises `EvaluationError` (swallowed per 6). The chain is
     the *sole authority* on patch applicability: it accepts patch forms
     with no `---`/`+++` file headers (rename-only and mode-only
     `diff --git` hunks, `GIT binary patch` sections, tab-separated
     `diff -u` headers, context/normal diffs) — a header-based pre-check
     would reject applicable patches before they could score `resolved`, so
     **no structural pre-check is made**; every non-empty patch reaches the
     harness.
   - `Timeout error: <n> seconds exceeded.` — appended to `test_output.txt`
     as the file's *final write* when the in-container test exec hits `-t`
     (hence tail-anchored); the harness also logs its own `Test timed out
     after {timeout} seconds.` `EvaluationError` message into
     `run_instance.log` — the number is *this run's `-t` value*
     (`run_evaluation.py:379`), so the marker is tied to the run's own
     test cap, not any number a patch's diff might contain.
   - `>>>>> Tests Timed Out` (`TESTS_TIMEOUT`) — the dataset eval-script's
     timeout marker, which grading reads *inside the test output*
     (`grading.py:137`) — it is never written to `run_instance.log`.
   - **Echo-masked instance-log markers:** the harness reflects the patch
     text into `run_instance.log` in exactly two places — the
     `Git diff before:` / `Git diff after:` echoes (one logger call each,
     the diff is the multi-line message body → un-prefixed continuation
     lines). The terminal-class instance-log markers (the apply-fail line —
     first line of its own logger call, `run_evaluation.py:332` — and the
     timeout message — the traceback's last line) are *outside* those echo
     regions, so the marker checks run on the log with the echo regions
     masked: a patch containing marker text (a diff touching code or docs
     carrying `>>>>> Patch Apply Failed`, or the exact timeout sentence)
     cannot be triaged as the harness's verdict.
   - Markers are anchored and file-specific because `EvaluationTimeoutError`
     is *terminal* in WP3 while `HarnessFailedError` is the retried
     `eval_error` class — a loose substring in the wrong file (or echoed
     patch text in the instance log) must not convert a retryable infra
     flake into a terminal `timeout`.
8. **Stale-report / stale-log risk of a re-used (workdir, run_id) pair:**
   `get_dataset_from_preds` treats a pre-existing per-run `report.json` as
   "already run" (`exclude_completed` → the instance is silently skipped), and
   `run_instance` itself early-returns a pre-existing report. A re-used pair
   would return the *previous* run's verdict for a patch that was never
   evaluated (with the stale container id in `logs.container`). evaluate.py
   never trusts a pre-existing report: it unlinks one before the spawn (on an
   unlink failure it records the pre-run mtime and requires the post-run
   report to be strictly newer — a missing pre-run mtime or an unreadable
   post-run mtime makes freshness unverifiable → reject). The same re-use
   risk for the *terminal-class* triage markers: a pre-existing
   `run_instance.log` / `test_output.txt` whose mtime this run did not update
   is stale — its markers are not this run's verdict, and triage downgrades
   them to the retryable infra class (the safe direction: a wrong terminal
   verdict is worse than a retried infra class).
9. **Prediction file** (`get_predictions_from_file`): `.jsonl` (one dict per
   line) or `.json`; each prediction must carry `instance_id` (hard-checked).
   This module writes exactly `instance_id` / `model_name_or_path` /
   `model_patch` — nothing else (no extra fields for the harness to ignore,
   no fields of our own for it to choke on).
10. **`run_id` must be non-empty** (`assert len(run_id) > 0`) and flows into
    the container name (Docker-name charset considerations apply — the
    default `eval-<iid>-<8-hex-uuid>` satisfies them).

## Design — `bench/evaluate.py` (pure stdlib; `swebench` only via subprocess)

**The whole design is "delegate, then read what the harness wrote."** The
module never applies a patch, never runs a test, never grades output, and
never imports `swebench` (no bespoke apply/grade logic — AC1/AC3). Its *only*
harness interaction is spawning the official CLI:

```
python -m swebench.harness.run_evaluation
    -d SWE-bench/SWE-bench_Verified -s test
    -p <predictions.jsonl> -i <instance_id> -id <run_id>
    --max_workers 1 -t <timeout> --report_dir <workdir>
```

with `cwd = workdir` (fact 2). After the process exits, `evaluate` reads
*only* the harness's own artifacts to resolve the outcome:

- `exit ≠ 0` → a typed `HarnessFailedError` **always** — even when a
  `report.json` exists: the harness writes the per-instance report before the
  final-report leg (`make_run_report`), so a crash after the report must not
  be read as a completed run.
- `report.json` (present — and fresh, fact 8) → `resolved` is the harness's
  value, and it must be a **bool** — a corrupted-but-parseable verdict is a
  typed failure, never a silent `bool()` coercion (AC3: the harness is the
  sole source of the resolved flag).
- `report.json` with `resolved: false` + `infra_failure: true` → a typed
  `HarnessFailedError` (fact 3 — the harness's own environment-fault
  classification; the WP3 `eval_error` retry class, never a terminal "not
  resolved" verdict).
- no `report.json` (fact 6) → triage on harness-written markers (fact 7), by
  the file the pinned harness writes them to:
  `APPLY_PATCH_FAIL` in `run_instance.log` → `PatchCorruptionError`
  (the definitive corruption guard, AC4; wins over a timeout marker — a
  corrupt patch is terminal and a retry would just re-fail the apply); the
  timeout markers (fact 7) → `EvaluationTimeoutError`; a marker that survived
  from a previous run (the stale-log guard, fact 8) → the retryable
  `HarnessFailedError` class; anything else → `HarnessFailedError` (Docker /
  infra — the WP3 `eval_error` retry class, PLAN §2.5). No marker is
  re-interpreted as a grade.

**Empty patches (the negative control)** — fact 4: short-circuit to
`{"resolved": False, …}` with `empty_patch: True` (no harness spawn, no
tempdir, no dataset download, no Docker; the run-identity log values are
None — the caller created nothing to clean up).

**`workdir`** is the per-instance artifact directory (the harness cwd). With
`None` a fresh `swebench-eval-<iid>-…` tempdir is created and the *caller*
owns its cleanup (the artifacts are the point) — the path is surfaced in
`logs.workdir` (success) and in `error.workdir` (every typed error raised
after the workdir is resolved) so it is never unreachable. A *relative*
workdir is resolved to an absolute path **before** the spawn — the harness
resolves `-p` / `--report_dir` against its own cwd (= workdir), so an
unresolved relative path would double-resolve (`workdir/workdir/…`).

**`overall_timeout`** is the outer watchdog in seconds (the project's hang
policy, `runner.py`: a hung run is a failure, not a wait). The harness's own
`-t` bounds only the *in-container* test exec; the HF dataset download, the
multi-GB image pull and the Docker daemon are not bound by `-t`, so the
watchdog wraps the whole subprocess. It is **bounded by default** (the rest
of the package uses bounded defaults for subprocess calls, `runner.py`
`timeout_s=30/15`): with `None` — the default, including the CLI default —
the watchdog is `timeout + DEFAULT_OVERALL_TIMEOUT_MARGIN_S` (1800 s), so a
wedged dataset download, image pull, or Docker daemon cannot hang the
sequential benchmark forever; an explicit value overrides. When it fires,
the typed error surfaces the possibly-still-running eval container name
(`sweb.eval.<iid>.<run_id>`, possibly `.{timestamp}`-suffixed — fact 5) +
the partial output.

**Typed, never a traceback escape.** Every failure mode is one of the typed
errors (a WP3 caller classifies them into retry decisions; the CLI maps them
to exit codes):

| class | meaning | CLI exit |
|---|---|---|
| `InvalidInputError` | bad input (charset / NUL / type / finiteness checks) — raised *before any side effect*, zero-cost | 2 |
| `PatchCorruptionError` | the harness's git-apply chain wrote `APPLY_PATCH_FAIL` — corrupt/truncated patch (AC4) | 3 |
| `EvaluationTimeoutError` | the harness timeout marker — terminal in WP3 | 4 |
| `HarnessFailedError` | spawn failure, watchdog fire, non-zero exit (always), pre-spawn FS failure (ENOSPC/EACCES — PLAN §7), unreadable / non-object / non-bool report, stale report, the infra-fault classification, or no report + no known marker | 5 |
| (CLI catch-all) | an unexpected exception is exit 5 — the documented contract, not a traceback with exit 1 | 5 |

Errors carry `instance_id`, the harness log paths, the workdir (fact: every
typed error raised after the workdir is resolved carries it) and a combined
tail of the harness output (stdout + stderr — when one is non-empty the
other is not discarded entirely; bytes are decoded with replacement, never a
`b'…'` repr).

**Input validation (typed, before any side effect).** `instance_id` /
`run_id`: Docker container-name charset `[A-Za-z0-9][A-Za-z0-9._-]*`
(starting alphanumeric, no `..` — real SWE-bench ids `owner__repo-N` satisfy
it; the run id seeds the container name, the tempdir prefix and a log-path
component). NUL bytes are rejected for every string that lands in an
argv/shell context (dataset, split, workdir, python, model name — a NUL in
an argv element would otherwise surface as a raw `ValueError` at `Popen`).
`timeout` must be a positive int (bools excluded; a huge int is refused —
it would overflow the float conversion at spawn); `overall_timeout` a
positive *finite* number (nan/inf/huge-int would hang or immediately fire
the watchdog — `math.isnan`/`isinf` on a huge int raises a raw
`OverflowError` the validation must not let out); `model_name_or_path` is
escaped `/` → `__` like the harness, but the escaped name must not be a
log-path traversal (`.` / `..`).
`workdir` (str or path-like — an *empty* path-like resolves to the caller's
CWD and is refused; a broken `__fspath__` raising — including a `RuntimeError`
symlink-loop — is a typed refusal) and a
non-callable `harness_runner` are refused the same way.

**Return contract (AC3, typed `TypedDict` — the repo's first; the sibling
structured results are `@dataclass` and this JSON-shaped contract is the
fit; the key set is pinned by tests for *both* the full and the
empty-patch paths so key-drift is test-enforced):**

```
{
  "resolved": bool,
  "logs": {
    "instance":     str | None,   # harness run_instance.log (path)
    "test_output":  str | None,   # harness test_output.txt (path)
    "report":       dict | None,  # harness report.json, verbatim
    "final_report": str | None,   # harness final run report (path)
    "container":    str | None,   # id of the container the harness created
    "workdir":      str | None,   # the artifact dir (the harness cwd)
    "run_id":       str | None,   # the -id passed to the harness
    "predictions":  str | None,   # the standard predictions file written
    "empty_patch":  bool,         # short-circuit marker (fact 4)
  },
}
```

Paths, not contents: the WP5 report / WP3 checkpoint consume artifact paths;
embedding multi-MB test output in memory is not the contract.

**The `harness_runner` test seam.** `evaluate(…, harness_runner=fn)` with
`fn(argv, cwd, overall_timeout) → CompletedProcess-like` replaces the real
subprocess — the unit-test injection point (fabricated harness trees + a
fake runner; zero quota, no Docker, no network). A typed error the injected
runner raises *stays typed* (never re-wrapped); a spawn failure, a runner
failure and the watchdog firing become `HarnessFailedError`.

**Operator CLI** (`python bench/evaluate.py`): `--instance-id`
`--patch-file` (+ `--model-name --dataset --split --timeout
--overall-timeout --workdir --run-id --python`). Exit 0 = *ran* (resolved
*or* not — the JSON result on stdout carries the outcome); 2/3/4/5 = the
typed error classes (stderr carries the typed message; a missing/directory/
non-UTF-8 patch file is exit 2; an unexpected exception is exit 5 — never a
traceback).

## AC → code map

| AC | Where it lives |
|---|---|
| 1. delegates entirely to the official CLI on a standard predictions file | `_harness_argv` (exactly the official CLI — the argv is *pinned* by a unit test), the subprocess is the *only* harness interaction, the predictions file carries exactly the three standard fields (pinned by a test), the module never imports `swebench` (pinned by an AST test) |
| 2. fresh container, never the agent's mutated one | the harness's own `containers.create` on the published eval image (there is no agent-container parameter); the per-call unique `run_id` default ⇒ the eval container name is structurally distinct (pinned by the run-id / container-name unit tests); the integration positive control asserts the eval container id (from the harness's own log) ≠ the agent container id |
| 3. returns `{resolved, logs}`, no bespoke apply/grade logic | the typed `EvaluationResult` / `EvaluationLogs` contract (key set pinned for both paths); `resolved` is only ever the harness's bool `report.json` value (type-checked — no `bool()` coercion) or the harness's own empty-patch semantics; the no-`swebench`-import AST test |
| 4. positive / negative / corruption / isolation controls | the integration tests (CI, public images, zero quota — auto-skip without a Docker daemon) + the unit-level layers: the empty-patch short-circuit (docker-free negative control), the `APPLY_PATCH_FAIL` marker triage (corruption guard — the harness's apply chain is the sole authority, no structural pre-check), the anchored file-specific timeout markers, the stale-report / stale-log guards, the typed validation matrix (zero side effects asserted), the robustness edges (spawn failure, watchdog, pre-spawn FS failure, non-bool / non-object / non-UTF-8 reports, missing returncode, path-like workdir), the CLI exit codes |

## Tests — `bench/tests/test_evaluate.py`

**Unit (zero quota, no Docker, no network — CI and local).** The harness
subprocess is replaced by the injected `harness_runner` (the documented seam)
and the harness's CWD-relative artifact layout is fabricated on disk (the
exact `logs/run_evaluation/<run_id>/<model>/<iid>/` tree the pinned swebench
5.0.2 writes, incl. the pin's `<report_dir>/<model>.<run_id>.json` final
report):

- **AC1:** `test_harness_argv_is_exactly_the_official_cli` (the argv is
  *pinned* — any bespoke flag or a missing official one breaks it; good
  python/dataset/split values flow), `test_harness_runs_with_cwd_as_workdir`,
  `test_predictions_file_is_exactly_the_standard_format` (exactly the three
  standard fields, one JSONL line), `test_no_swebench_import_in_evaluate_module`
  (AST — a string match would also flag a string literal),
  `test_model_name_slash_is_escaped_like_the_harness` (the log-dir /
  final-report-name escape must match the harness or the report is never
  found).
- **AC2:** `test_default_run_id_is_unique_per_call`,
  `test_eval_container_name_never_equals_agent_container_name` (asserted
  against the run id values the module *actually emits* — not strings the
  test constructs).
- **AC3:** `test_report_json_is_the_sole_source_of_resolved` (true and false
  passthrough, verbatim), `test_return_contract_shape` (the exact key set +
  verbatim artifact paths), `test_container_id_extracted_from_harness_log`
  (from the harness's own line; absent → None, never a crash).
- **AC4 unit:** `test_negative_control_no_op_patch_is_unresolved` (``""`` and
  `None` — **no harness spawn**, the harness's own filter semantics; the
  empty-patch literal carries the same key set with the run-identity values
  None), `test_empty_patch_with_invalid_input_is_invalid_input` (validation
  precedes the short-circuit), `test_nonempty_patch_always_reaches_harness_and_apply_fail_is_typed`
  (header-less garbage — the forms a header check would have wrongly
  rejected — reaches the harness; the chain's marker is the typed error),
  `test_truncated_wellformed_patch_reaches_harness_and_is_corruption`
  (a truncation that still leaves both diff headers — structurally
  well-formed — is only rejectable by the harness's chain), the triage
  edges (`test_no_report_timeout_marker_is_evaluation_timeout` — all markers
  × the file placement; `test_timeout_marker_in_wrong_file_is_not_terminal_timeout`
  — anchored + file-specific; `test_apply_fail_and_timeout_markers_corruption_wins`;
  `test_apply_fail_in_wrong_file_is_not_corruption`; `test_no_report_no_marker_is_infra_error`;
  `test_exit_nonzero_is_harness_failed_with_stderr_tail` — the combined
  stdout + stderr tail; `test_report_present_but_exit_nonzero_is_harness_failed`
  — the exit-≠ 0-always-error rule; `test_exit_nonzero_with_apply_fail_marker_is_harness_failed`),
  the report-validation edges (`test_unreadable_report_is_harness_failed`,
  `test_non_object_report_is_harness_failed`, `test_non_utf8_report_is_harness_failed`,
  `test_report_missing_instance_entry_is_harness_failed`,
  `test_non_bool_resolved_verdict_is_harness_failed` — parametrized over the
  non-bool verdicts), `test_infra_failure_report_is_harness_failed` (the
  harness's own environment-fault classification → the retry class;
  `test_plain_unresolved_is_not_infra_failed` is the control), the
  stale-protection tests (`test_preexisting_report_is_unlinked_and_not_trusted`,
  `test_stale_report_surviving_unlink_failure_is_not_trusted`,
  `test_stale_guard_accepts_a_fresh_report_when_unlink_fails` — the positive
  leg, `test_stale_log_marker_is_not_a_terminal_verdict`,
  `test_updated_log_marker_is_this_runs_verdict` — the control), the
  validation matrices (`test_input_validation_matrix` — instance id charset /
  patch / run_id charset / model name / timeout; `test_extra_input_validation_matrix`
  — dataset / split / workdir / python / harness_runner / overall_timeout
  incl. nan/inf and NUL argv-carrying strings — both assert zero filesystem
  side effects), the robustness edges (`test_relative_workdir_is_resolved_to_absolute_before_spawn`,
  `test_workdir_none_creates_tempdir_and_surfaces_it`, `test_pre_spawn_filesystem_failure_is_typed`,
  `test_runner_spawn_failure_is_typed_not_raw`,
  `test_timeout_expired_from_runner_is_typed_with_partial_output` — surfaces
  the eval container name + the partial output, decoded readably,
  `test_overall_timeout_flows_to_the_runner`,
  `test_injected_runner_typed_error_stays_typed`,
  `test_process_object_without_returncode_is_typed`,
  `test_default_harness_runner_watchdog_fires` — the real runner's own
  wiring, `test_tail_decodes_bytes_readably`,
  `test_auto_tempdir_is_surfaced_on_typed_failure` — the caller-owned
  cleanup stays reachable on failure paths, `test_pathlike_workdir_is_validated_like_a_str`),
  and the operator CLI (`test_cli_*` — help, JSON-on-stdout + flag flow,
  missing/directory/non-UTF-8 patch file → 2, the typed error exit codes
  2/3/4/5, the unexpected-exception catch-all → 5 with a typed message and
  no traceback).

**Integration (CI — public images, zero quota; auto-skip without a Docker
daemon, which this dev workspace lacks).** Each carries a
`pytest.mark.timeout` watchdog (a wedged pull must fail the test, not hang
the job):

- `test_integration_positive_control` — `pallets__flask-5014`, the single
  flask Verified instance (smallest eval image + suite: 1 FAIL_TO_PASS, 59
  PASS_TO_PASS) with the published Verified run's gold patch →
  `resolved: True`, **including the isolation assertion** (the eval
  container id — a real container id from the harness's own log — ≠ the
  agent container id; the agent's container, never present here, is never
  referenced — there is not even a parameter for it). Fallback if the 2026
  image rebuild flakes on it: `psf__requests-1142` (1 F2P, 5 P2P).
- `test_integration_corruption_guard_truncated_gold_patch` — the gold patch
  truncated mid-hunk (the hunk header declares 9 new-side lines, the
  truncation leaves 5 — a structurally well-formed truncation that only the
  harness's chain can reject) through the real harness → a clean typed
  `PatchCorruptionError`, never a crash.

**Packaging/CI plumbing:** `pyproject.toml` — `evaluate` added to
`py-modules`; `pytest-timeout` added (the integration tests' watchdog —
without the plugin the marker is inert, a plain warning); `requirements.lock`
— the matching pin. `.github/workflows/ci.yml` is **unchanged**: the
existing `bench` job (`pip install -e bench/` + `pytest bench/`) covers this
work — the unit tests run everywhere (zero quota, no Docker), the two
integration tests auto-skip without a Docker daemon and run on
`ubuntu-latest`.

## Files

| file | change |
|---|---|
| `bench/evaluate.py` | new — the delegate (module docstring carries the full contract) |
| `bench/tests/test_evaluate.py` | new — the test matrix above |
| `bench/PLAN-WP4.md` | this plan |
| `bench/pyproject.toml` | `py-modules` += `evaluate`; `dependencies` += `pytest-timeout>=2` |
| `bench/requirements.lock` | + `pytest-timeout==2.4.0` |
| `bench/README.md` | the WP4 section (the table row, the "evaluator" section, the status line) |

## Contingencies

| risk | contingency |
|---|---|
| the 2026 image rebuild flakes on `pallets__flask-5014` in CI | the `psf__requests-1142` fallback (1 F2P, 5 P2P — the next-smallest Verified instance) is recorded here and in the test's docstring |
| a wedged Docker pull hangs the CI job | the `pytest.mark.timeout(2700)` watchdog on both integration tests (a wedged pull fails the test at 45 min, well inside the job's 60 min limit) |
| the dev workspace has no container runtime | the integration tests auto-skip (the unit matrix is the local/CI-zero-quota coverage; the controls run in CI on `ubuntu-latest`) |
| the pinned harness changes its log layout / markers | the bench package pins `swebench==5.0.2` exactly — the layout and the markers are stable by pin (the facts above are verified against the pin's source and re-verified against the wheel for this implementation) |
| a re-used (workdir, run_id) pair returns a stale verdict / a stale terminal marker | the stale-report protection (unlink + mtime fallback) + the stale-log mtime guard (the safe direction: a wrong terminal verdict is worse than a retried infra class) — both pinned by tests |

## Progress log

- **Plan done** (2026-10-28): all ten environment facts verified against the
  pinned `swebench==5.0.2` wheel source (`run_evaluation.py`,
  `constants/__init__.py`, `reporting.py`, `grading.py`, `utils.py`,
  `docker_utils.py`) + the WP0 notes (`acp-surface.md` §5).
