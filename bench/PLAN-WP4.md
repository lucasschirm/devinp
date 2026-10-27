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
   artifact dir the runner owns) and `--report_dir workdir`; every artifact
   lands under one per-instance directory (PLAN §5 WP2 artifact atomicity).
3. **Per-instance report** (`report.json`): `{instance_id: {resolved,
   patch_is_None, patch_exists, patch_successfully_applied, infra_failure,
   FAIL_TO_PASS, PASS_TO_PASS, tests?}}`. **Final report**
   (`<report_dir>/<model>.<run_id>.json`, `schema_version: 2`) carries
   `resolved_ids` / `unresolved_ids` / `error_ids` / `empty_patch_ids`.
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
   The definitive typed signals are harness-written markers:
   `>>>>> Patch Apply Failed` (`APPLY_PATCH_FAIL`) in `run_instance.log`
   after the 4-command git-apply chain fails, and `Timeout error: N seconds
   exceeded` / `>>>>> Tests Timed Out` in `test_output.txt`. evaluate.py
   triages *only* these markers — it never re-implements apply/grade.
6. **Fresh-container mechanics (AC2):** the harness's `create_container` runs
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
7. **Dataset:** `load_swebench_dataset("SWE-bench/SWE-bench_Verified", "test")`
   — public Hugging Face, no quota; 500 instances, 12 repos (WP0 §5).

## Design — `bench/evaluate.py` (pure stdlib; `swebench` only via subprocess)

Public API (one call = one instance — the runner is sequential, one JSONL line
per instance, so the module stays a 1:1 delegate with no batch logic):

```python
evaluate(
    instance_id: str,
    model_patch: str | None,
    *,
    model_name_or_path: str = "devin",        # e.g. "devin-swe-2-high"
    dataset: str = "SWE-bench/SWE-bench_Verified",
    split: str = "test",
    workdir: Path | str | None = None,        # per-instance artifact dir = harness cwd;
                                             # default: fresh tempdir
    run_id: str | None = None,                # default unique per call
                                             # (eval-<iid>-<uuid8>) → fresh container name
    timeout: int = 1800,                     # harness -t (per-instance test timeout)
    python: str | None = None,               # interpreter with swebench installed;
                                             # default sys.executable
    harness_runner: Callable | None = None,  # injection seam (tests): (argv, cwd) -> CompletedProcess
) -> dict   # {"resolved": bool, "logs": {...}}
```

Flow:

1. **Typed validation (pre-harness, zero cost):**
   * `instance_id` a non-empty str, else `InvalidInputError`.
   * `model_patch` a str or None, else `InvalidInputError`.
   * A non-empty patch must carry unified-diff file headers (`--- ` / `+++ `
     lines), else `PatchCorruptionError` — a garbage/truncated-without-headers
     patch fails clean *before* any dataset download or Docker work. (A
     truncated patch that is still structurally well-formed reaches the
     harness's git-apply chain — that is the *definitive* corruption guard,
     step 4; both layers report `PatchCorruptionError`.)
   * **Empty/None patch → short-circuit**
     `{"resolved": False, "logs": {"empty_patch": True, ...}}` — the harness's
     own filter semantics (fact 4); no harness spawn, no dataset download, no
     Docker.
2. **Predictions file** — `<workdir>/predictions.jsonl`, one JSON line with
   *exactly* the three standard fields (`instance_id`,
   `model_name_or_path`, `model_patch`), the WP0-verified format.
3. **The delegation (AC1)** — the *only* harness interaction:
   `[python, "-m", "swebench.harness.run_evaluation", "-d", dataset, "-s",
   split, "-p", <preds>, "-i", <iid>, "-id", <run_id>, "--max_workers", "1",
   "-t", <timeout>, "--report_dir", <workdir>]`, run with `cwd=workdir`
   (fact 2). No custom apply/grade flags; no other harness touch.
4. **Outcome resolution — reading only what the harness wrote (AC3):**
   * exit ≠ 0 → `HarnessFailedError` (typed; stderr tail in the message —
     e.g. unknown instance id, dataset fetch failure, unreachable daemon).
   * `report.json` present → `resolved = report[iid]["resolved"]` — the harness
     is the *sole* source of the resolved flag.
   * no `report.json` → triage on harness-written markers only (fact 5):
     `APPLY_PATCH_FAIL` in `run_instance.log` → `PatchCorruptionError` (the
     clean typed error for a truncated patch — AC4); the timeout marker in
     `test_output.txt` → `EvaluationTimeoutError`; anything else →
     `EvaluationInfraError` (Docker/infra failure; log tail in the message).
   * A typed error carries `instance_id`, the log paths, and the relevant
     harness output tail — a clean typed error, never a traceback escape.
5. **Return contract (AC3):**

   ```python
   {"resolved": bool,
    "logs": {
       "instance":     Path-str | None,  # harness run_instance.log
       "test_output":  Path-str | None,  # harness test_output.txt
       "report":       dict    | None,   # harness per-instance report.json
       "final_report": Path-str | None,  # harness final run report
       "container":    str     | None,   # id of the container the harness created
       "empty_patch":  bool               # short-circuit marker (fact 4)
    }}
   ```

   Paths, not contents: the WP5 report / WP3 checkpoint consume artifact
   paths (PLAN §5 WP2 "Artifact atomicity"); embedding multi-MB test output
   in memory is not the contract.

Error hierarchy (all carry `.instance_id` + log paths where applicable):

```
EvaluationError(Exception)
├── InvalidInputError      # bad instance_id / non-str patch
├── PatchCorruptionError   # no diff structure (pre) / APPLY_PATCH_FAIL (post)
├── EvaluationTimeoutError # harness test timeout
└── HarnessFailedError     # subprocess exit ≠ 0 / infra (no report, no marker)
```

A small operator CLI (`python bench/evaluate.py --instance-id … --patch-file …
[--model-name …] [--dataset …] [--split …] [--timeout …] [--workdir …]`):
exit 0 with the JSON result on stdout (resolved *or* unresolved), 2
`InvalidInputError`, 3 `PatchCorruptionError`, 4 `EvaluationTimeoutError`, 5
`HarnessFailedError`/other.

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
* **AC3 (`{resolved, logs}`)** → the return contract (step 5); `resolved` is
  only ever the harness's `report.json` value (step 4) or the documented
  harness empty-patch semantics (step 1) — no bespoke grading.
* **AC4 corruption guard** → two layers, both typed: the zero-cost pre-check
  (step 1) and the definitive post-harness triage on the harness's own
  `APPLY_PATCH_FAIL` marker (step 4).

## Tests — `bench/tests/test_evaluate.py`

**Unit (local + CI; no Docker, no network, zero quota):**

* **Delegation argv pin** — the exact official CLI args (dataset default
  `SWE-bench/SWE-bench_Verified`, `-s test`, `-i`, `-id`, `--max_workers 1`,
  `-t`, `--report_dir`); `cwd == workdir`; the predictions file carries
  exactly the three standard fields.
* **Fresh-container naming** — the default `run_id` is unique per call; the
  harness container name `sweb.eval.<iid>.<run_id>` ≠ the agent container
  name `sweb.eval.<iid>`.
* **Validation matrix** — empty/non-str `instance_id`, non-str `model_patch`
  (bytes/dict) → `InvalidInputError`; header-less garbage patch →
  `PatchCorruptionError` with **no harness spawn** (injected runner not
  called — no dataset download, no Docker).
* **Negative control (docker-free)** — `""` and `None` patch →
  `resolved is False`, `empty_patch: True`, **no harness spawn**.
* **Post-harness triage against fabricated harness trees** (no Docker — the
  tree is the exact CWD-relative layout of fact 2): `report.json` present
  (resolved true / false) → passthrough of the harness value; no report +
  `APPLY_PATCH_FAIL` in `run_instance.log` → `PatchCorruptionError`; timeout
  marker in `test_output.txt` → `EvaluationTimeoutError`; no report, no
  marker → `EvaluationInfraError`; exit ≠ 0 → `HarnessFailedError` with the
  stderr tail.
* **Container-id extraction** — from a fixture `run_instance.log` (the
  harness's own `Container for … created/started: <id>` line); absent log →
  `None` (never a crash).
* **CLI** — `--help`; bad input → exit 2; success (injected runner) → JSON
  with `resolved` on stdout.

**Integration (CI — public images, zero quota; auto-skip without a Docker
daemon, which this dev workspace lacks, so these run in CI):**

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
| `bench/pyproject.toml` | add `evaluate` to `py-modules` |
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
