# Plan: Devin ACP Benchmark Adapter (SWE-bench Verified)

**Goal.** An automated, resumable, **sequential** harness that runs SWE-bench
Verified (500 instances) through `devin acp` with a chosen SWE model
(e.g. `swe-2-high`), produces a publishable pass@1 report (incl. the
leaderboard artifacts), and consumes Devin **free-tier** quota responsibly.

**Non-goal.** Agentless/raw-model runs (no completions endpoint exists for
SWE models; that path is out of scope). Results are "Devin (agent) on
SWE-bench Verified" and must be labeled as such.

---

## 1. Architecture

```
┌────────────────────────────────────────────────────────┐
│ runner.py  — sequential queue, quota budget,          │
│            checkpoint/resume (JSONL), pre-flight,     │
│            image/container pruning                    │
├───────────────────────────────────────────────────────┤
│ adapter.py — per instance:                            │
│   1. docker run sweb.eval.<instance_id> (repo @ base)│
│   2. inject devin (real binary), auth token,        │
│      ephemeral XDG dirs, isolated --config          │
│   3. `git -C /testbed remote remove origin` + strip │
│      tags/reflogs (upstream-leak guard)             │
│   4. spawn `devin acp --model <M>` with conda testbed PATH │
│   5. acp_client (NDJSON JSON-RPC 2.0): initialize   │
│      → session/new (cwd=/testbed) → session/prompt  │
│      → auto-answer session/request_permission per   │
│      policy → session/close before exit             │
│   6. wall-clock cap → session/cancel               │
│   7. patch extraction (see §4) → patch.diff        │
├───────────────────────────────────────────────────────┤
│ evaluate.py — delegates to                            │
│   `python -m swebench.harness.run_evaluation`       │
│   on a standard predictions file, in a FRESH        │
│   container (never the agent's mutated container)   │
├───────────────────────────────────────────────────────┤
│ report.py — pass@1, per-repo table, failure         │
│            taxonomy, methodology block,             │
│            all_preds.jsonl + trajs/ artifacts       │
└───────────────────────────────────────────────────────┘
```

The adapter only replaces "model call" in the classic split; evaluation is
the official SWE-bench harness path (repo reset → `model.patch` → hidden
`test_patch` (FAIL_TO_PASS / PASS_TO_PASS) → repo-specific `eval.sh` →
repo-specific log parsers), which keeps the result publishable.

**Wire protocol (verified in WP0, non-negotiable for WP1):**
- Framing is **NDJSON** — newline-delimited JSON-RPC 2.0 lines over stdio.
  **Not** LSP `Content-Length` framing.
- Use the **wire method names** implemented by `devin acp` (they differ
  from the ACP SDK method names): `session/new`, `session/prompt`,
  `session/cancel`, `session/request_permission`, and `session/close`
  (call `session/close` before terminating the process).
- Ignore unsolicited vendor notifications emitted during startup
  (e.g. `_cognition.ai/mcp/serversChanged`) — log, never treat as
  errors, never block on them.

**Fallback transport:** if `devin acp` proves flaky, the fallback is
`devin -p "<task>"` (print mode). In print mode devin defaults to
`--respect-workspace-trust true` (aborts in an untrusted `/testbed`) and
`--permission-mode auto` (read-only tools), so the fallback command must
explicitly be:
`devin -p --respect-workspace-trust false --permission-mode dangerous`
(or `accept-edits`), with the same prompt template. The adapter isolates
both transports behind `AcpSession` so the swap is one class.

## 2. Free-tier & sequential-execution policy (hard requirements)

1. **One session at a time, always.** Default concurrency = 1; the runner
   asserts no two `devin acp` processes overlap (process-level lock +
   assert). A `--concurrency N>1` flag exists but is refused by default
   unless `--i-know-about-quota` is passed.
2. **Quota budget per run.** Before starting, the runner checks config:
   `max_sessions_per_day`, `max_wall_per_session` (default 45 min),
   `daily_pause` (default 12 h between run batches, aligned to quota reset
   — reset window determined in WP0).
3. **Rate-limit handling.** On any rate-limit/refusal signal (ACP stop
   reason, CLI stderr, or non-zero exit with quota text): mark instance
   `quota_limited`, stop the batch, checkpoint, schedule resume. Backoff
   is batch-level (sleep to next window), not per-instance retry.
4. **Model allowlist is dynamic, not hardcoded.** At runner startup the
   runner calls `devin models list --format json` and **only** permits
   models with `cost_tier == "Free"` (currently: `swe-2-medium`,
   `swe-2-high`, `swe-2-max`). `swe-1-6` and `swe-1-7` are **paid**
   ("Low cost", $0.50/$2.50 per 1M in/out) and must never be selectable
   under the free policy; same for all `*-lightning` variants. A paid
   model requires an explicit config override **plus** a token budget
   check. If the startup check fails or the JSON shape changes, the runner
   aborts the batch (fail-closed).
5. **Checkpoint/resume with pass@1-correct retry semantics.** Progress
   file `run-<id>.jsonl` — one line per finished instance (status,
   artifact paths, usage, timestamps). On resume:
   - `resolved` / `failed_test` / `timeout` / `no_patch` → **never
     re-run.** `timeout` (45-min cap exceeded) and `no_patch` (agent
     finished without edits) are legitimate agent outcomes; re-running
     them would turn pass@1 into a filtered pass@k and inflate scores.
   - `quota_limited`, `eval_error` (Docker/harness failure), host crash
     (partial state) → **eligible for retry** (that's the only retry
     class).
6. **Usage tracking.** Capture whatever usage the ACP stream/CLI exposes
   (WP0 verifies; if unavailable, track sessions + wall time +
   prompt/patch byte sizes as the quota proxy).
7. **Pre-flight health checks (zero quota, before every batch *and*
   before each instance):** `devin auth status` ok, Docker daemon
   responsive (hello + image presence), minimum free disk (≥ 2 × largest
   expected image, see §7). Any check fails → stop before burning a
   session; log the reason into the JSONL as `preflight_failed`.

## 3. Container execution & session hygiene (per instance)

1. **Binary injection.** Official `sweb.eval.<instance_id>` images contain
   no devin or credentials. On Linux, `$(which devin)` is often a
   **wrapper script** that references `XDG_DATA_HOME` and execs the real
   ELF at `~/.local/share/devin/cli/_versions/current/bin/devin` — mount
   the **real binary** (and its `_versions` tree if needed), not the
   wrapper. WP0 verifies host glibc compatibility and the CA certificate
   bundle against the oldest sweb.eval base images (devin needs working
   HTTPS to Cognition's API).
2. **Ephemeral state.** Never mount the host `XDG_DATA_HOME`/`XDG_CONFIG_HOME`
   into a container: `devin acp` opens a multi-GB SQLite `session_db` on
   boot (WAL lock contention + host bloat across 500 sessions). Create a
   **per-instance ephemeral** `XDG_DATA_HOME`/`XDG_CONFIG_HOME` populated
   **only** with the auth token; delete on teardown.
3. **Isolated config.** Pass an isolated `--config` with **empty MCP
   servers, rules, and plugins** so host-level customizations
   (e.g. `~/.config/devin/mcp_config.json`) cannot contaminate benchmark
   runs.
4. **No silent model switching.** Explicitly **unset**
   `DEVIN_REFUSAL_FALLBACK` (and never pass `--refusal-fallback`) so a
   policy refusal cannot switch the session to another model mid-run; a
   refusal is a terminal `agent_error` for that instance.
5. **Conda testbed environment.** SWE-bench dependencies live in
   `/opt/miniconda3/envs/testbed`, not system Python. Spawn `devin acp`
   with `PATH=/opt/miniconda3/envs/testbed/bin:$PATH` and
   `CONDA_DEFAULT_ENV=testbed` so the agent's test runs use the
   repository's environment.
6. **Network/leak control.** Denying "network" at the ACP tool layer does
   **not** stop bash from reaching the internet (`git fetch`, `curl`).
   Before starting the session, the adapter runs
   `git -C /testbed remote remove origin` (and strips post-base tags and
   reflogs) so git history/upstream cannot leak into the agent's context;
   at the ACP permission layer, deny web-search/fetch **tools**.
7. **ACP permission policy:**
   - **Allow:** read; edit/write inside `/testbed` and `/tmp`;
     **shell execution (exec/terminal) inside `/testbed` and `/tmp`** —
     the agent must be able to reproduce bugs and run pytest.
   - **Deny:** web-search/fetch tools, writes outside `/testbed`+`/tmp`,
     anything else.

## 4. Patch extraction (no blind untracked staging)

Blindly staging **all** untracked files before `git diff HEAD` captures
temporary reproduction scripts (`repro.py`), `.pytest_cache`, coverage
dbs, and build artifacts → patch-size-cap failures and `git apply`
errors at evaluation. Instead:

1. The prompt template **instructs the agent to delete temporary
   reproduction files** when done.
2. The adapter applies an **exclusion filter** before diffing:
   `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.coverage`,
   `htmlcov/`, `*.egg-info/`, `build/` (and any file matching these
   patterns is removed from the diff, not the working tree).
3. Any remaining untracked files are included **only if inside package
   source/test directories**, after resetting environment/submodule drift
   (`git submodule update --init --recursive` state restored, env file
   drift reverted).
4. Guards: non-empty, size cap, no binary content; `git status
   --porcelain` cross-check so the diff and working tree agree.

## 5. Work packages (each with acceptance tests)

### WP0 — Recon (0.5–1 d, ~1 real session)
- Record the ACP surface of `devin acp` in `bench/notes/acp-surface.md`:
  confirm **NDJSON** framing and the wire method names
  (`session/new`, `session/prompt`, `session/cancel`,
  `session/request_permission`, `session/close`), stop-reason/usage
  metadata fields, and the startup vendor notifications to ignore.
- Verify host glibc + CA compatibility of the devin binary against the
  oldest sweb.eval base images; verify the wrapper-vs-real-binary layout
  of the host install.
- Determine free-tier quota: `devin auth status`, billing docs, one timed
  probe session; record reset window.
- Pin the exact SWE-bench harness version + eval images for the Verified
  split; confirm `swebench.harness.run_evaluation` input format
  (predictions file: `instance_id`, `model_name_or_path`,
  `model_patch`) and the conda testbed env layout.
- **Acceptance:** the notes file answers every "?" above; a hand-driven
  single ACP session with `--model swe-2-high` completes a trivial task
  in a sandbox container; `devin models list --format json` shape is
  recorded (incl. `cost_tier`).

### WP1 — `acp_client.py` (pure, no Devin)
- Minimal **NDJSON** JSON-RPC 2.0 client (no SDK dependency): spawn
  process, `initialize`, `session/new`, `session/prompt`, stream
  `session/update`, answer `session/request_permission` per policy,
  `session/cancel`, `session/close` **before** terminating the process.
- Ignores (logs) unsolicited vendor notifications during startup.
- **Tests (unit, CI):**
  - Framing: NDJSON line round-trip (incl. multi-line string escaping and
    interleaved vendor notifications) against an in-process mock server.
  - Happy path against `mock_devin` (WP2): full session yields a
    transcript with the expected notification sequence and a
    `session/close` before process exit.
  - Permission policy: mock sends a web-fetch tool request → denied; an
    exec in `/testbed` → allowed; an edit outside `/testbed`+`/tmp` →
    denied.
  - Timeout: mock stalls → client `session/cancel`s + kills the process
    group (assert no orphan via process-table check in test).
  - Malformed/short read: typed error, never hangs.
  - Refusal-fallback isolation: client env never contains
    `DEVIN_REFUSAL_FALLBACK`; a refusal stop-reason → terminal error, no
    model switch.

### WP2 — `mock_devin` (test double, the key testability lever)
- A Python script masquerading as the `devin` binary: speaks the ACP
  subset from WP0 (NDJSON + wire names), runs a canned "agent": reads
  the task, applies a canned patch to the cwd repo, answers its own
  permission flow, emits a stop reason. Modes: `ok`, `no_patch`, `hang`,
  `quota`, `crash`.
- **Tests (integration, CI, zero quota):**
  - Full pipeline dry-run: `mock_devin --mode ok` through adapter →
    `evaluate.py` (stubbed evaluator first; real `run_evaluation` in the
    pilot gate).
  - Each failure mode produces the correct terminal instance status in
    the JSONL (`no_patch`, `timeout`, `quota_limited`, `agent_error`).
  - Checkpoint/resume with pass@1 semantics: kill runner after 3
    instances → resume → `timeout`/`no_patch` lines are **skipped**,
    `quota_limited`/`eval_error` lines are re-run, no duplicates.
  - Artifact atomicity: `runs/<run_id>/<instance_id>/` files are complete
    on disk **before** the JSONL summary line is appended.

### WP3 — `runner.py`
- Sequential queue (concurrency policy §2), quota budget, backoff state
  machine (`running → budget_exhausted → sleeping(until window) →
  resume`), per-instance wall-clock cap, pre-flight checks (§2.7),
  abort on 2 consecutive `agent_error` (environment drift suspicion),
  post-eval **image/container pruning** (§7).
- **Tests (unit, CI):**
  - State machine: table-driven transitions incl. rate-limit → stop batch
    (assert no instance starts after the signal).
  - Overlap guard: instrumented fake session objects; assert
    `active_sessions == 1` invariant under interleaved scheduling.
  - Resume correctness against fixture JSONL (incl. partial-line
    corruption at EOF — last line dropped, not fatal) and the §2.5 retry
    class rules.
  - Cost-tier gate: fixture `models list` JSON with a paid model
    configured → runner refuses the batch (fail-closed); shape change →
    refuses.
  - Pruning: after each evaluated instance, the instance image + exited
    container are removed; shared base/env layers retained.

### WP4 — `evaluate.py` (SWE-bench evaluation)
- **Delegates entirely** to `python -m swebench.harness.run_evaluation`
  on a standard predictions file
  (`instance_id`, `model_name_or_path`, `model_patch`), in a **fresh**
  container. No logic of ours inside; never reuses the agent's mutated
  container (its working tree, submodules, and installed packages are
  contaminated by the run).
- **Tests (integration, CI with public images, no quota):**
  - **Positive control:** a known-resolved patch (from a published
    SWE-bench Verified run) → `resolved: true`.
  - **Negative control:** no-op/empty patch → `resolved: false`.
  - **Corruption guard:** truncated `model_patch` → clean typed error,
    not a crash.
  - **Isolation:** evaluating after a "mutated" agent container state
    still grades the fresh checkout (assert eval container id ≠ agent
    container id).

### WP5 — `report.py`
- Aggregates JSONL → pass@1 overall + per-repo table + failure taxonomy
  (`timeout`, `no_patch`, `quota_limited`, `agent_error`, `resolved`,
  `failed_test`, `preflight_failed`, `eval_error`) + methodology block
  (devin CLI version, model + cost_tier, prompt template hash,
  permission policy, caps, harness version, images, date range, sessions
  used) **plus the leaderboard artifacts**: `all_preds.jsonl` (full
  `model_patch` strings per instance) and `trajs/<instance_id>.jsonl`
  (per-instance action trajectories).
- **Tests (unit, CI):** fixture JSONL → exact expected numbers (incl.
  resume-dedup, terminal-status exclusion, and artifact cross-checks:
  every JSONL line's referenced files exist and the patch content in
  `all_preds.jsonl` matches `patch.diff`).

### WP6 — Pilots (real quota, gated, sequential)
1. **Pilot A (3 instances, small repos):** protocol conformance vs. real
   `devin acp` (NDJSON/wire names, permission flow, close); measure
   per-instance wall time + visible usage; calibrate
   `max_sessions_per_day`. **Go/No-go** for Pilot B.
2. **Pilot B (25 instances):**
   - **Stratified** across **all 12** SWE-bench Verified repositories so
     every repo's conda environment and test parser is validated before
     the 500-instance run.
   - The 3 Pilot A instances are **reused inside** the 25 for the 2-run
     overlap/variance check (no extra sessions spent).
   - Full failure taxonomy in the wild; a real interrupt demonstrates
     §2.5 resume semantics.
3. **Full (500):** sequential batches across quota windows (≈ several
   days). Mid-run checkpoint is the recovery story for crashes/
   interruptions.

## 6. Test matrix summary

| Layer | What runs it | Cost | Devin? |
|---|---|---|---|
| Unit (WP1/3/5) | CI, every commit | ~$0 | mock only |
| Integration (WP2/4) | CI, daily | swebench images only | mock + public images |
| Real smoke (WP0) | manual, opt-in env flag | ~1 free session | yes |
| Pilots (WP6) | scheduled, sequential | free-tier budget | yes |

CI must **never** consume quota: real-Devin tests are behind
`BENCH_LIVE=1` and excluded by default.

## 7. Resource management (disk)

500 `sweb.eval.x86_64.*` images consume ~1–2 TB. `runner.py` prunes
**instance-specific** images (`docker rmi sweb.eval.x86_64.<instance_id>`)
and exited containers **after each instance is evaluated**, retaining only
shared base/env layers. The pre-flight disk check (§2.7) is sized
against the largest expected image × 2.

## 8. Repository & CI integration

1. **Keep bench/ out of npm publishing.** `publish.yml` publishes a patch
   release on every push to `main` — add
   `paths-ignore: ['bench/**']` to its `on.push` trigger so benchmark
   commits never release. The npm tarball is already safe (`"files":
   ["bin", "data"]` in `package.json` excludes `bench/`); keep it that
   way.
2. **Python packaging for bench/.** Add `bench/pyproject.toml`
   (Python ≥ 3.11; `swebench`, `pytest` pinned) with a lock file.
3. **CI.** Add a Python job to `.github/workflows/ci.yml` (setup-python
   3.11, `pip install -e bench/` or lockfile install, `pytest bench/`)
   so the WP1/WP2/WP3/WP5 unit + mock-integration tests actually run in
   CI; the existing Node job stays unchanged.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| ACP surface differs from what WP0 recorded | WP0 recon is the gate; fallback transport `devin -p --respect-workspace-trust false --permission-mode dangerous` behind the same `AcpSession` interface |
| Free-tier quota/reset window unknown | WP0 probe; conservative default budget (e.g. 10 sessions/day); budget is config, not code; dynamic `cost_tier` gate is fail-closed |
| Host devin install is a wrapper script / glibc or CA mismatch in old eval images | Mount the real binary; WP0 compatibility check against the oldest base images |
| Host config (MCP/rules/plugins) or refusal-fallback contaminates runs | Per-instance ephemeral XDG dirs + isolated `--config`; `DEVIN_REFUSAL_FALLBACK` unset; refusal = terminal |
| Agent leaves repo in non-diffable state or stages artifacts | Prompt instruction to clean temp files + artifact exclusion filter + source/test-dir-only untracked inclusion (§4); `no_patch` taxonomy catches the rest |
| Re-running failed instances inflates the metric | `timeout`/`no_patch` terminal for pass@1; only infra/quota interruptions retry (§2.5) |
| 500 images exhaust disk | Per-instance image/container pruning after evaluation; pre-flight disk check (§7) |
| Orphan `devin acp` processes leak quota | Spawn under a process group; `session/close` + `kill -TERM/-KILL` group on all exit paths (WP1 test asserts) |
| bench/ commits trigger npm releases | `paths-ignore: ['bench/**']` in publish.yml; `files` array excludes bench/ (§8) |

## 10. Definition of done

1. CI green: Node job (unchanged) + new Python job — all unit +
   mock-integration tests (WP1–5) pass with zero Devin usage.
2. `publish.yml` skips `bench/**` pushes; `npm pack` contents exclude
   `bench/`.
3. Pilot A report exists with measured per-instance time + quota
   calibration.
4. Pilot B / full run produces: `run-<id>.jsonl`, `all_preds.jsonl`,
   `trajs/<instance_id>.jsonl`, per-instance
   `runs/<run_id>/<instance_id>/{patch.diff, trajectory.jsonl,
   eval.log}`, and the markdown report with the methodology block —
   suitable for submission (SWE-bench leaderboard / Cognition
   publication).
5. Resuming a killed run mid-batch is demonstrated (WP2 test + a real
   interrupt in Pilot B) with correct pass@1 retry semantics.
