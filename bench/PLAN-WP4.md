# WP4 Implementation Plan — `evaluate.py`: thin delegate to `swebench.harness.run_evaluation`

Implements issue #17 (part of #9). Plan of record: `bench/PLAN.md` §5 WP4.
Depends on: #10 (WP0 — the `swebench==5.0.2` pin; the `run_evaluation` input
format and the Verified split/images verified in the pinned source).
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

1. **CLI (WP0-verified, re-confirmed against the pinned source):**
   `python -m swebench.harness.run_evaluation -d <dataset> -s test -p
   <predictions.jsonl> -i <instance_id> -id <run_id> -t <timeout>
   --max_workers N --report_dir <dir>`.
2. **Log layout is CWD-relative:** `RUN_EVALUATION_LOG_DIR =
   Path("logs/run_evaluation")`. The harness therefore writes
   `<cwd>/logs/run_evaluation/<run_id>/<model_name_or_path (with `/` → `__`)>
   /<instance_id>/{report.json, run_instance.log, test_output.txt}`.
   ⇒ evaluate.py runs the harness with `cwd = workdir` (the per-instance
   artifact dir the runner owns) and `--report_dir workdir`; every
   per-instance artifact lands under one per-instance directory (PLAN §5
   WP2 artifact atomicity). The **final run report** is written by the pin's
   `make_run_report` to `<report_dir>/<model>.<run_id>.json` (verified in the
   pinned source, `reporting.py:170-178` — `--report_dir` *is* honored in the
   5.0.2 pin, default `.`), so `--report_dir workdir` lands it beside the
   CWD-relative log tree.
3. **Per-instance report** (`report.json`): `{instance_id: {resolved,
   patch_is_None, patch_exists, patch_successfully_applied, infra_failure,
   infra_failure_reason?, tests_status?}}` — `infra_failure_reason` is
   conditional (set with the flag) and `FAIL_TO_PASS` / `PASS_TO_PASS`
   (alongside `FAIL_TO_FAIL` / `PASS_TO_FAIL`) live nested under
   `tests_status` (per `get_eval_report`, `grading.py:350-391`). **Final
   report** (`<report_dir>/<model>.<run_id>.json`, `schema_version: 2`)
   carries `resolved_ids` / `unresolved_ids` / `error_ids` /
   `empty_patch_ids` / **`infra_failure_ids`** (with per-instance
   `infra_failure_reasons` — `reporting.py:105-107`): on unparseable test
   output from an environment fault, `grading.py:373-374` sets
   `infra_failure` + reason while keeping `resolved` False — the harness's
   *own* environment-fault classification, tallied separately from
   `unresolved_ids`. evaluate.py maps that classification to
   `HarnessFailedError` (the WP3 `eval_error` retry class) — never a
   terminal "not resolved" verdict.
4. **Empty patches are filtered by the harness itself**
   (`get_dataset_from_preds`): an empty/`None` `model_patch` instance is
   *skipped* — no container is created, no report is written, and the final
   report counts it in `empty_patch_ids` (never in `resolved_ids`). Official
   harness semantics: empty patch ⇒ not resolved. evaluate.py mirrors that
   exactly by short-circuiting (no harness spawn) — the *semantics* are the
   harness's (verified in source), and the short-circuit only saves the HF
   dataset download + Docker handshake on a guaranteed no-op.
5. **Per-instance failures are swallowed by the harness**: `run_instance`
   catches `EvaluationError` (and any exception), prints it, and returns
   `None` — the process still **exits 0** and no `report.json` is written.
   The definitive typed signals are harness-written markers, **by the file
   the pinned harness writes / the test output it grades** (`
   run_evaluation.py:376-381`, `grading.py:130-141`): `>>>>> Patch Apply
   Failed` (`APPLY_PATCH_FAIL`) in `run_instance.log` after the 4-command
   git-apply chain fails; in `test_output.txt`: the **tail-anchored**
   `Timeout error: N seconds exceeded.` the harness appends as the file's
   final write, and the dataset eval-script's `>>>>> Tests Timed Out`
   marker (graded *inside* the test output — never written to
   `run_instance.log`); in `run_instance.log`: the harness's `Test timed
   out after N seconds` `EvaluationError` message. *Anchored +
   file-specific*: `EvaluationTimeoutError` is terminal in WP3 while
   `HarnessFailedError` is the retried `eval_error` class, so a loose
   substring in the wrong file must not convert a retryable infra flake
   into a terminal `timeout`. evaluate.py triages *only* these markers
   (plus the `exclude_completed` stale-report protection, fact 6 / step 3)
   — it never re-implements apply/grade.
6. **`exclude_completed` (stale-report protection):** `get_dataset_from_preds`
   treats a pre-existing per-run `report.json` as *already run* and silently
   skips the instance — a re-used (workdir, run_id) pair would return the
   previous run's verdict for a patch that was never evaluated (with the
   stale container id in `logs.container`). evaluate.py never trusts a
   pre-existing report: it unlinks one before the spawn (best effort; on an
   unlink failure it records the pre-run mtime and requires the post-run
   report to be strictly newer — a missing pre-run mtime or an unreadable
   post-run mtime makes freshness unverifiable → reject), and a non-zero
   exit is always an error (step 4). (The second skip path — `run_instance`
   itself early-returning a pre-existing report, `run_evaluation.py:272` —
   is covered by the same unlink.) The same re-use risk for the
   *terminal-class triage markers*: a pre-existing `run_instance.log` /
   `test_output.txt` whose mtime this run did not update is stale — its
   markers are not this run's verdict; triage downgrades them to the
   retryable `HarnessFailedError` class (an unreadable baseline or mtime is
   treated as stale — the safe direction).
7. **Fresh-container mechanics (AC2):** the harness's `create_container` runs
   `client.containers.create(image=<instance eval image>, name=
   "sweb.eval.<instance_id>.<run_id>")` — a brand-new container from the
   published eval image, force-removing any pre-existing container with that
   exact name first (ghost cleanup). The agent's container
   (named `sweb.eval.<instance_id>` by the adapter, PLAN §1) is never
   referenced by the harness. A unique `run_id` per call ⇒ the eval container
   name can never collide with the agent's, and the checkout is always the
   pristine image. The harness logs `Container for <iid> created/started:
   <container.id>` into `run_instance.log` ⇒ evaluate.py extracts the eval
   container id (source of truth: the harness's own log) for the isolation
   assertion (AC4) and for `logs.container`.
8. **Dataset:** `load_swebench_dataset("SWE-bench/SWE-bench_Verified", "test")`
   — public Hugging Face, no quota; 500 instances, 12 repos (WP0 §5).

## Design — `bench/evaluate.py` (pure stdlib; `swebench` only via subprocess)

Public API (one call = one instance — the runner is sequential, one JSONL line
per instance, so the module stays a 1:1 delegate with no batch logic):

```python
evaluate(
    instance_id: str,                      # SWE-bench id charset (seeds the default
    model_patch: str | None,              #   run_id, tempdir prefix, log path)
    *,
    model_name_or_path: str = "devin",        # e.g. "devin-swe-2-high" (escaped '/' → '__';
                                             #   the escaped name must not be '.'/'..')
    dataset: str = "SWE-bench/SWE-bench_Verified",
    split: str = "test",
    workdir: str | os.PathLike | None = None,  # per-instance artifact dir = harness cwd;
                                             # default: fresh tempdir (surfaced in logs,
                                             # caller owns its cleanup)
    run_id: str | None = None,                # default unique per call
                                             # (eval-<iid>-<uuid8>) → fresh container name
    timeout: int = 1800,                     # harness -t (per-instance test timeout)
    overall_timeout: float | None = None,    # outer watchdog in seconds (hang policy —
                                             # -t bounds only the in-container test exec)
    python: str | None = None,               # interpreter with swebench installed;
                                             # default sys.executable
    harness_runner: Callable | None = None,  # injection seam (tests):
                                             # (argv, cwd, overall_timeout) -> CompletedProcess
) -> EvaluationResult   # TypedDict: {"resolved": bool, "logs": EvaluationLogs}
```

Flow:

1. **Typed validation (pre-harness, zero cost, zero FS side effects):**
   * `instance_id` a non-empty str **on the SWE-bench id charset**
     (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `..`) — it seeds the default
     `run_id` (a container name), the tempdir prefix and a log-path
     component — else `InvalidInputError`.
   * `model_patch` a str or None, else `InvalidInputError`.
   * `run_id` (when given) on the Docker container-name character set
     `[A-Za-z0-9._-]`, starting alphanumeric, without `..` — it becomes the
     eval container name suffix and a CWD-relative log path component
     (whitespace / `/` / `..` are refused, not absorbed), else
     `InvalidInputError`.
   * `model_name_or_path` a non-empty str, NUL-free, whose escaped name
     (`/` → `__`) is not `.`/`..` (log-path traversal refused), else
     `InvalidInputError`.
   * `dataset` / `split` non-empty str; `workdir` a non-empty str/path (or
     None); `overall_timeout` a positive number (or None); `python` a
     non-empty str (or None); `harness_runner` callable (or None) — else
     `InvalidInputError` (the same "never a raw TypeError" contract).
   * **Empty/None patch → short-circuit**
     `{"resolved": False, "logs": {"empty_patch": True, …}}` — the harness's
     own filter semantics (fact 4); no harness spawn, no dataset download, no
     Docker, **no tempdir** (nothing for the caller to clean up).
   * **No structural patch pre-check** — the harness's 4-command git-apply
     chain (fact 5) is the *sole authority* on applicability: it accepts
     patch forms without `---`/`+++` file headers (rename-only / mode-only
     `diff --git` hunks, `GIT binary patch` sections, tab-separated
     `diff -u` headers, context/normal diffs), so a header check would
     reject applicable patches before they could ever reach the harness and
     score `resolved`. Every non-empty patch reaches the harness.
2. **`run_id` + `workdir` resolution (pre-spawn, typed):** the `run_id`
   unique-per-call default; a relative `workdir` is resolved to an absolute
   path **before** the spawn — the harness resolves `-p` / `--report_dir`
   against its own cwd (= workdir), so an unresolved relative path would
   double-resolve (`workdir/workdir/…`). All pre-spawn filesystem work
   (tempdir / resolve / mkdir / predictions write) is wrapped in typed
   `HarnessFailedError` (ENOSPC / EACCES / ENAMETOOLONG never escape raw —
   PLAN §7 disk management / §9 risk table: "500 images exhaust disk"), and the
   artifact paths are known from here on, so every typed error after this
   point carries them.
3. **Stale-report protection + predictions file + the delegation (AC1)** — a
   pre-existing per-run `report.json` is unlinked (fact 6; on an unlink
   failure the pre-run mtime is recorded and the post-run report must be
   newer); `<workdir>/predictions.jsonl` is written with *exactly* the three
   standard fields (`instance_id`, `model_name_or_path`, `model_patch`), the
   WP0-verified format; then the *only* harness interaction:
   `[python, "-m", "swebench.harness.run_evaluation", "-d", dataset, "-s",
   split, "-p", <preds>, "-i", <iid>, "-id", <run_id>, "--max_workers", "1",
   "-t", <timeout>, "--report_dir", <workdir>]`, run with `cwd=workdir`
   (fact 2). No custom apply/grade flags; no other harness touch. A runner
   spawn failure (missing `python` → `FileNotFoundError`, fork/exec
   `OSError`), the outer watchdog firing (`subprocess.TimeoutExpired` — the
   typed error surfaces the possibly-still-running eval container name for
   `docker rm -f` and the partial output), a failure an injected runner
   raises, or a malformed process result (no usable `returncode`) is a typed
   `HarnessFailedError`, never a raw traceback escape (typed errors an
   injected runner raises *stay typed* —
   `except EvaluationError: raise` before the broad catch).
4. **Outcome resolution — reading only what the harness wrote (AC3):**
   * exit ≠ 0 → `HarnessFailedError` **always — even when a report
     exists**: the harness writes the per-instance report before the
     final-report leg (`make_run_report`), so a crash after the report is
     not a completed run (the stderr + stdout tails are carried, never
     dropped).
   * fresh `report.json` → `resolved = report[iid]["resolved"]` **with a
     type check — it must be a bool** (a corrupted-but-parseable verdict is
     a typed `HarnessFailedError`, never a silent `bool()` coercion) — the
     harness is the *sole* source of the resolved flag.
   * no `report.json` (exit 0) → triage on harness-written markers only,
     by file (fact 5): `APPLY_PATCH_FAIL` in `run_instance.log` →
     `PatchCorruptionError` (wins over a timeout marker — a corrupt patch
     is terminal and a retry would just re-fail the apply); the
     tail-anchored `Timeout error: N seconds exceeded.` or `>>>>> Tests
     Timed Out` in `test_output.txt`, or `Test timed out after …` in
     `run_instance.log` → `EvaluationTimeoutError`; anything else →
     `HarnessFailedError` (Docker/infra — the WP3 `eval_error` retry
     class; combined labeled tail in the message).
   * a `report.json` with `resolved: false` + `infra_failure: true` (the
     harness's own environment-fault classification, fact 3) →
     `HarnessFailedError` — the WP3 retry class, never a terminal verdict.
   * a `report.json` that is unreadable (non-UTF-8 / invalid JSON) or not a
     JSON object is a typed `HarnessFailedError`, never a raw
     `UnicodeDecodeError` / `AttributeError` escape.
   * a *stale* report (the pre-existing report survived a failed unlink and
     was not updated by this run) is a typed `HarnessFailedError` (fact 6).
   * A typed error carries `instance_id`, the log paths, and a combined tail
     of the harness output — a clean typed error, never a traceback escape.
5. **Return contract (AC3; typed — `EvaluationResult` / `EvaluationLogs`
   `TypedDict` — the repo's first `TypedDict`; the sibling structured
   results are `@dataclass`, and this JSON-shaped contract is the fit; the
   key set is test-enforced, the repo has no type checker in CI):**

   ```python
   {"resolved": bool,
    "logs": {
       "instance":     Path-str | None,  # harness run_instance.log
       "test_output":  Path-str | None,  # harness test_output.txt
       "report":       dict    | None,   # harness per-instance report.json
       "final_report": Path-str | None,  # harness final run report
       "container":    str     | None,   # id of the container the harness created
       "workdir":      str     | None,   # the artifact dir (a fresh tempdir when the
       "run_id":       str     | None,   #   caller passed None — surfaced for the
       "predictions":  str     | None,   #   caller-owned cleanup, never unreachable)
       "empty_patch":  bool               # short-circuit marker (fact 4)
    }}
   ```

   Paths, not contents: the WP5 report / WP3 checkpoint consume artifact
   paths (PLAN §5 WP2 "Artifact atomicity"); embedding multi-MB test output
   in memory is not the contract. On the empty-patch path the run-identity
   values are None (nothing was created); the key set is pinned by tests for
   both paths.

Error hierarchy (all carry `.instance_id` + log paths where applicable):

```
EvaluationError(Exception)
├── InvalidInputError      # typed refusal of any bad call input (zero side effects)
├── PatchCorruptionError   # the harness's own git-apply chain wrote APPLY_PATCH_FAIL
├── EvaluationTimeoutError # harness test timeout (anchored, file-specific markers)
└── HarnessFailedError     # spawn/watchdog failure / exit ≠ 0 (always) / pre-spawn FS /
                           #   infra (no report, no marker) / unreadable or non-object
                           #   report / non-bool verdict / harness infra_failure / stale report
```

A small operator CLI (`python bench/evaluate.py --instance-id … --patch-file …
[--model-name …] [--dataset …] [--split …] [--timeout …] [--overall-timeout …]
[--workdir …] [--run-id …] [--python …]`): exit 0 = ran (resolved *or*
unresolved) with the JSON result on stdout, 2 `InvalidInputError`, 3
`PatchCorruptionError`, 4 `EvaluationTimeoutError`, 5 `HarnessFailedError` /other (including an unexpected exception — the documented contract, never a traceback with exit 1).

## AC → code map

* **AC1 (entirely delegates)** → `_harness_argv()` builds *exactly* the
  official CLI (fact 1); the subprocess is the only harness interaction — a
  unit test pins the argv; `swebench` is imported nowhere in `evaluate.py`
  (verified: the module is pure stdlib + subprocess).
* **AC2 (fresh container)** → the harness's own `containers.create` on the
  published eval image (fact 6); per-call unique `run_id` ⇒ the eval
  container name `sweb.eval.<iid>.<run_id>` can never equal the agent's
  `sweb.eval.<iid>`; the harness force-removes same-name ghosts before
  creating; evaluate.py never accepts or references an agent container at all
  (there is no parameter for one).
* **AC3 (`{resolved, logs}`)** → the return contract (step 5) — typed
  (`EvaluationResult` / `EvaluationLogs` `TypedDict`); `resolved` is only
  ever the harness's *bool* `report.json` value (step 4 — type-checked: a
  non-bool verdict is a typed failure, never a `bool()` coercion) or the
  documented harness empty-patch semantics (step 1) — no bespoke grading.
* **Stale-report protection** → step 3 (fact 6): a pre-existing per-run
  `report.json` is unlinked before the spawn (mtime-fallback on a failed
  unlink) and a non-zero exit is always an error — a result is only ever
  produced from a report *this* run wrote (a re-used (workdir, run_id)
  pair cannot return the previous run's verdict).
* **AC4 corruption guard** → the harness's own 4-command git-apply chain is
  the *sole authority* on applicability (fact 5) — no structural pre-check
  (it would reject patch forms the chain applies: rename/mode-only git
  hunks, `GIT binary patch` sections, tab-separated `diff -u` headers,
  context diffs); a truncated/garbage patch reaches the harness, the chain
  fails, and the harness's own `APPLY_PATCH_FAIL` marker (step 4) becomes a
  clean typed `PatchCorruptionError`, never a crash.

## Tests — `bench/tests/test_evaluate.py`

**Unit (local + CI; no Docker, no network, zero quota):**

* **Delegation argv pin** — the exact official CLI args (dataset default
  `SWE-bench/SWE-bench_Verified`, `-s test`, `-i`, `-id`, `--max_workers 1`,
  `-t`, `--report_dir`), with a parametrized case proving good
  python/dataset/split values actually flow; `cwd == workdir`; the
  predictions file carries exactly the three standard fields; the harness
  artifact paths are asserted *verbatim* (a swapped key or a wrong
  final-report filename formula must not pass silently).
* **Fresh-container naming** — the default `run_id` is unique per call; the
  harness container name `sweb.eval.<iid>.<run_id>` ≠ the agent container
  name `sweb.eval.<iid>` — asserted against the run id values `evaluate`
  actually emits.
* **Return contract** — the exact key set of both halves is pinned for
  *both* the full and the empty-patch paths; the run-identity values
  (`workdir` / `run_id` / `predictions`) are surfaced and asserted.
* **Validation matrix** — empty/non-str/charset-violating `instance_id`
  (slash / `..` / tab / space / NUL / leading dot), non-str `model_patch`
  (bytes/dict), run_id charset (whitespace / `/` / `..` / non-str), model
  name (empty / `..` / `.` / NUL), bad timeout, empty/non-str/NUL-carrying
  `dataset` / `split` / `workdir` / `python` (the argv-carrying strings),
  non-callable `harness_runner`, bad `overall_timeout` (incl. **nan/inf**)
  → `InvalidInputError`, typed before any side effect (injected runner not
  called **and** no `predictions.jsonl` written — zero FS side effects).
* **No bespoke harness import (AST)** — `evaluate` must not import `swebench`
  (a direct import would be the seam for re-implementing the harness's
  apply/grade path); checked structurally, not by string match.
* **Corruption guard (unit, fabricated tree)** — every non-empty patch
  (incl. header-less garbage — the forms a header check would have wrongly
  rejected) reaches the harness; the fabricated `APPLY_PATCH_FAIL` marker →
  `PatchCorruptionError`; a well-formed-but-truncated gold patch → same.
* **Delegation robustness edges** — a relative `workdir` is resolved to an
  absolute path before the spawn (`-p` / `--report_dir` / cwd all
  absolute); `workdir=None` creates a fresh `swebench-eval-…` tempdir that
  is surfaced in `logs.workdir` (caller-owned cleanup); a pre-spawn
  filesystem failure (workdir whose parent is a file — deterministic
  regardless of privileges) → typed `HarnessFailedError` before the spawn;
  a runner spawn failure (`FileNotFoundError` / `OSError`), the outer
  watchdog firing (`subprocess.TimeoutExpired` — the typed error surfaces
  the eval container name + the partial output) and a process object
  without a usable `returncode` → typed `HarnessFailedError`, never a raw
  traceback; a typed error an injected runner raises *stays typed*;
  `overall_timeout` flows through the runner seam; the default runner's own
  wiring is tested directly (trivial success + a real watchdog fire);
  a non-object or non-UTF-8 `report.json` → typed `HarnessFailedError`.
* **Stale-report protection** — a pre-existing per-run `report.json` is
  unlinked before the spawn (a harness that "skips" the instance then gets
  a typed no-report triage, never the stale verdict); on a forced unlink
  failure (monkeypatched), an un-updated report is a typed stale-report
  failure and a demonstrably-updated (newer-mtime) report is accepted
  (fact 6).
* **Negative control (docker-free)** — `""` and `None` patch →
  `resolved is False`, `empty_patch: True`, **no harness spawn**, the same
  key set with None run-identity values; validation still precedes the
  short-circuit (an empty patch with a bad `instance_id` →
  `InvalidInputError`).
* **Post-harness triage against fabricated harness trees** (no Docker — the
  tree is the exact CWD-relative layout of fact 2): `report.json` present
  (resolved true / false) → passthrough of the harness value; a
  `resolved: false` + `infra_failure: true` report (with / without
  `infra_failure_reason`) → `HarnessFailedError` (the harness's own
  environment-fault class — fact 3) while a plain `resolved: false` stays a
  verdict; no report + `APPLY_PATCH_FAIL` in `run_instance.log` →
  `PatchCorruptionError` (and it *wins* when a timeout marker is present
  too; the same marker in `test_output.txt` is *not* corruption); the
  timeout markers parametrized over all markers × the file the pinned
  harness writes / the test output it grades (tail-anchored `Timeout error:
  N seconds exceeded.` in `test_output.txt`, `>>>>> Tests Timed Out` in
  `test_output.txt`, `Test timed out after …` in `run_instance.log`) →
  `EvaluationTimeoutError`, the same text in the *wrong* file → the
  retryable `HarnessFailedError` class (anchored + file-specific);
  a marker that survived from a *previous* run (its file not updated by
  this run — the stale-log guard) → the retryable class, while the same
  marker in an updated file stays terminal;
  no report, no marker → `HarnessFailedError`; exit ≠ 0 →
  `HarnessFailedError` with the **combined labeled tail — even when a
  report exists** (the exit-≠ 0-always-error rule); a non-bool
  `resolved` verdict / a missing instance entry / an unreadable or
  non-object `report.json` → typed `HarnessFailedError`.
* **Container-id extraction** — from a fixture `run_instance.log` (the
  harness's own `Container for … created/started: <id>` line); absent log →
  `None` (never a crash; a warning is logged).
* **CLI** — `--help`; bad input (missing / directory / non-UTF-8 patch
  file) → exit 2; success (injected runner) → JSON with `resolved` on
  stdout; **every flag** (`--model-name` / `--dataset` / `--split` /
  `--workdir` / `--run-id` / `--python` / `--timeout` /
  `--overall-timeout`) flows through to `evaluate`; the typed error
  classes → exit 3/4/5; an *unexpected* exception (a bug) → exit 5 with a
  typed message, never a traceback.

**Integration (CI — public images, zero quota; auto-skip without a Docker
daemon, which this dev workspace lacks, so these run in CI; both carry a
`pytest.mark.timeout` watchdog — `pytest-timeout`, a bench dev dependency —
a wedged pull must fail the test, not hang until the job-level timeout):**

* **Positive control** — `pallets__flask-5014` (the single flask Verified
  instance; the smallest eval image + suite: 1 F2P + 59 P2P), with the gold
  patch of the published Verified run (the merged PR's fix, embedded in the
  test with provenance — base commit `7ee9ceb71e86`, image
  `swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest`) →
  `resolved is True`. Fallback if the 2026 image rebuild flakes on it:
  `psf__requests-1142` (1 F2P, 5 P2P, 743-byte gold patch).
* **Corruption guard (full harness)** — same instance, the gold patch
  truncated mid-hunk → `PatchCorruptionError` (a clean typed error, not a
  crash) via the harness's own apply-failure marker.
* **Isolation** — the positive-control result's `logs.container` (the id the
  harness created, from its own log) is asserted ≠ the agent-container id
  (a stand-in id for the agent's `sweb.eval.<iid>` container, which does not
  exist in CI — the guarantee under test is that `evaluate.py` references no
  agent container at all: the only container it can name is the one the
  harness created from the pristine published image), and the eval container
  name carries the `.<run_id>` suffix the agent name lacks.

## Files

| File | Change |
|---|---|
| `bench/PLAN-WP4.md` | this plan |
| `bench/evaluate.py` | new module (module docstring carries the AC map, repo style) |
| `bench/tests/test_evaluate.py` | new tests (unit + integration) |
| `bench/pyproject.toml` | add `evaluate` to `py-modules`; add `pytest-timeout` (the
integration tests' watchdog) |
| `bench/README.md` | WP4-complete entry + layout row + short evaluator section |

## Contingencies

* **No Docker in this workspace** → integration tests probe `docker` +
  `docker info` and skip otherwise; they run in CI (`ubuntu-latest` ships a
  Docker daemon). The harness itself pulls the public image (no quota).
* **Gold-patch flake on the 2026 image rebuild** → the recorded fallback
  instance (`psf__requests-1142`).
* **HF flakiness in CI** → surfaced as `HarnessFailedError` (typed, stderr
  tail) — a retry class for the runner (`eval_error`, PLAN §2.5), not a
  crash.
