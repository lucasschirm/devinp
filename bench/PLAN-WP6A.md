# WP6a Implementation Plan — Pilot A: 3 real instances (go/no-go gate)

Implements issue #21 (part of #9). Plan of record: `bench/PLAN.md` §5 WP6.1.
Depends on: #10 (WP0 recon, `notes/acp-surface.md`), #11/#12 (WP1 `acp_client`),
#13 (WP2 `mock_devin`), #14/#15 (WP3 `runner`), #17 (WP4 `evaluate`), #18 (WP5
`report`) — all merged into `dev`.
Estimate: L (5 pts, real quota).

**Acceptance (issue #21):**
1. 3 instances (small repos) run end-to-end: NDJSON/wire names, permission
   flow, `session/close` all conform to WP0 notes against the **real**
   binary.
2. Per-instance wall time measured; visible usage (if exposed) recorded;
   `max_sessions_per_day` calibrated.
3. Pre-flight, pruning, and resume paths exercised for real (one intentional
   interrupt).
4. Go/No-go decision recorded with the measured numbers.

Testable by: `BENCH_LIVE=1` smoke harness (sequential, 3 instances); go/no-go
checklist against calibrated budget.

## Environment facts (2026-11-15, pilot host = Orca workspace)

1. **No container runtime in any available environment.** This workspace,
   the recon workspace, and the WP0 delegated workspace: `docker` / `podman`
   absent, no `/var/run/docker.sock` (WP0 `notes/acp-surface.md` §3 + open
   item 2 recorded the same). ⇒ The `sweb.eval.*` container legs (in-image
   agent session, `run_evaluation` in a fresh image, image pruning) **cannot
   be exercised here**. They stay implemented as the primary (PLAN-conformant)
   path and are exercised wherever docker exists.
2. **The `devin` CLI is bash-blocked in the operator session** (hook directs
   delegation via `devin_delegate`; WP0 Appendix A used exactly this routing).
   Read-only commands (`devin version`, `devin models list`) work directly:
   `devin 3000.11.92 (7f4e64980e22)`; account = **Devin Pro** (WP0 §4: the
   quota facts are Pro-plan; daily + weekly allowance, calendar-basis reset;
   account-level remaining only in the web app — the CLI exposes no quota
   fields).
3. **The real binary + auth are present on the host** (static ELF 200 MB,
   wrapper at `~/.local/bin/devin`, credentials at
   `$XDG_DATA_HOME/devin/credentials.toml`) — the live pilot therefore runs
   the **real** `devin acp` binary, `--model swe-2-high`, real auth, in
   **sandbox mode** (the documented no-container fallback below), via a
   delegated Devin session (WP0 pattern).
4. **Trusted-cwd is load-bearing (WP0 §8 A/B finding):** an untrusted cwd
   makes the prompt turn die (~2 s, stdout EOF, no updates); a trusted
   workspace cwd works. `devin acp` exposes **no trust flag** (WP0 §1
   correction (b)) — the sandbox must live under a trusted workspace root
   (the delegated session's workspace), and the harness detects the dead-turn
   signature (no `session/update` within the probe window after the prompt)
   and records `suspected_untrusted_cwd`.
5. **`session/close` is best-effort (WP0 §1):** the real server dumps a
   schema on close and the process survives close ⇒ the harness always
   group-kills after close (the WP1 `AcpSession.close()` contract).

## Design

### `bench/live_pilot.py` — the `BENCH_LIVE=1` smoke harness (new)

Sequential, 3-instance, go/no-go harness. Reuses the existing seams — no
re-implementation: `runner.Runner` + `runner.JsonlCheckpoint` +
`runner.resume_queue` (WP3b checkpoint/resume), `runner.Preflight` (WP3b
zero-quota pre-flight), `runner.gate_cost_tier`/`fetch_models_list` (WP3b
fail-closed model gate), `acp_client.AcpSession` + `SwebenchPolicy` (WP1/WP1b),
`evaluate.evaluate` (WP4, container mode only), `report.generate` (WP5, the
pilot report + leaderboard artifacts).

**Gates (enforced in order):**
1. `BENCH_LIVE=1` env var — hard requirement for any non-`--dry-run` run
   (mirrors `bench/conftest.py`'s gate; `--dry-run` is zero-quota: config +
   instance-file validation, budget math, checklist skeleton — CI safe).
2. **Pre-flight (real, WP3b `Preflight` legs, injected `run`/`disk_usage_fn`):**
   - `binary` — `shutil.which(devin_bin)` finds an executable (a missing
     binary is a loud pre-flight failure — the `devin version` probe is
     informational only).
   - `auth` — `devin auth status` exits 0.
   - `docker_daemon` / `docker_image` — **container mode only** (the
     per-instance image leg is **pull-if-missing**: a cold host pre-
     flights the zero-quota `docker pull`, and the setup's pull then
     no-ops — an inspect-only leg would abort before the setup could
     run).
   - `sandbox_runtime` — sandbox mode: `git` + a `python3` interpreter present
     (the no-container substitute for the daemon/image legs; a failed leg
     aborts before burning a session, recorded `preflight_failed`).
   - `disk` — free disk ≥ 2 × largest expected image (container: the
     sweb.eval image floor, PLAN §7) / 2 × largest expected checkout (sandbox).
   The pilot's **eager** pre-flight loop inspects only the instances this
   invocation will **run** (the `resume_queue` view) — a `--resume` must
   not pre-flight finished instances' container images (the prune `docker
   rmi`'d them) or re-pull images a finished run already consumed.
   - `versions` — **container mode only**: the host `_versions` tree
     resolves (a missing tree is a pre-flight failure, not a mid-batch
     setup fatal).
3. **Model gate (real, WP3b):** `gate_cost_tier(fetch_models_list(devin_bin),
   model)` — fail-closed; `swe-2-high` is `cost_tier == "Free"` (WP0 §7).

**Two execution modes (one `SessionSpec` seam; the test injects
`make_session`):**

* **`container` (PLAN-conformant, default when docker is present):** per
  instance — `docker run -d swebench/sweb.eval.x86_64.<owner>__<repo>-<PR>:latest`
  as `sweb.eval.<instance_id>` (distinct from WP4's eval container name
  `sweb.eval.<instance_id>.<run_id>`), inject the **real** binary (the
  `_versions` tree — never the wrapper, WP0 §2; the host tree is resolved
  portably: `DEVIN_VERSIONS_DIR` env, then the XDG locations — a missing
  tree is a clear pre-session error), per-instance **ephemeral
  `XDG_DATA_HOME`/`XDG_CONFIG_HOME`** seeded only with the auth token, isolated
  config (empty MCP servers / rules / plugins — `devin acp` has no `--config`
  flag, WP0 §1 correction (a) ⇒ the isolated XDG *is* the isolation),
  `git -C /testbed remote remove origin` + strip tags/reflogs + **delete all
  local branches** (after `git checkout --detach` — the clone's default
  branch sits at the upstream tip *past* `base_commit`, i.e. one `git log`
  from the merged fix commit; the in-image guard does the same, PLAN §3.6 —
  **the guard record is kept in the instance meta** (the checklist item 14
  evidence)), the session's `devin_bin` is a
  **single-line** `docker exec` shim (the `exec` + every flag form one
  command — a multi-line shim would `exec docker exec -i` with no
  container and die), spawn `devin acp --model <M>` with
  `PATH=/opt/miniconda3/envs/testbed/bin:$PATH`, `CONDA_DEFAULT_ENV=testbed`,
  `cwd=/testbed`, `DEVIN_REFUSAL_FALLBACK` unset (PLAN §3.4 — `AcpSession`
  enforces this itself). The devin process is a **host** process (the shim
  forwards into the container with `docker exec -w /testbed`), so the
  `AcpSession` **spawn cwd is a host-side scratch dir** while the
  **session cwd** (what `session/new` sends the agent) stays `/testbed`
  (the conflation of the two in one `cwd` field would `Popen(cwd=
  "/testbed")` on the host — a `FileNotFoundError` before the shim ever
  ran). The host-side git ops (patch extraction, the
  post-session leak verification) run on a **host copy** of `/testbed`
  (`docker cp` — the in-container path does not exist on the host;
  the copy is scratch and pruned with the instance). Eval = WP4
  `evaluate()` (fresh harness container, official `run_evaluation`). Pruning =
  `docker rmi` the instance image + `docker rm` the exited agent container
  + delete the host copy + the WP4 `eval/<instance_id>` workdir after eval
  (PLAN §7; shared base/
  env layers retained).
* **`sandbox` (documented no-container fallback — the pilot's actual mode in
  this environment, deviation recorded in the checklist + meta):** per
  instance — `git clone <repo> <workdir>/runs/<instance_id>/testbed` @
  `base_commit` (a shared bare clone cache acts as the retained "base
  layer"), the same leak guard (remote removed, tags/reflogs stripped, **all
  local branches deleted** — the clone's default branch sits at the upstream
  tip *past* `base_commit`, one `git log` from the merged fix commit —
  verified post-session; **the pre-session guard record is kept in the
  instance meta**), the same ephemeral-XDG + isolated-config +
  refusal-fallback hygiene, **host-credential env sanitization** (a
  denylist of secret-ish keys — token/secret/key/password/passwd/credential/
  apikey/auth — is stripped from the agent's environment so the agent
  process's in-scope `env`/`printenv` can't dump the host's `GITHUB_TOKEN`
  / cloud API keys; the stripped key names are recorded in the instance
  meta), `cwd=<workdir>/runs/<instance_id>/testbed` **under the
  operator's trusted workspace root** (WP0 §8), host `python3` on PATH (the
  conda `testbed` env is a container-only fact — deviation recorded).
  **Host check** (the closest end-to-end leg available without a container
  runtime, clearly labeled a deviation): a **fresh** clone at `base_commit`
  (never the agent's mutated tree — the isolation intent of PLAN §1/WP4),
  `model_patch` + `test_patch` applied, minimal `pip install -e .`, then
  `python -m pytest -rA --tb=no <FAIL_TO_PASS> <PASS_TO_PASS>`; per-node-id
  results from the `-rA` lines (the repo-specific `log_parser`s are
  image-leg-only). Verdict: all F2P + P2P pass → `resolved`; tests ran, an
  F2P failed → `failed_test`; infra failure (install/apply/parse) →
  `eval_error`. `graded_by: "host_check(pytest -rA)"` in the instance meta.
  If the host check cannot run at all (e.g. no pip/network), the instance
  lands in `eval_error` with `eval.skip_reason: "no_container_runtime"`
  (the retry class; the **gate does not depend on the eval verdict**).
  A spawned-tool failure that is not a returncode (a `FileNotFoundError`
  — a venv without `bin/pip` — or a `subprocess.TimeoutExpired` — a hung
  pip/pytest) is likewise the `eval_error` infra class, not a run-killing
  fatal.  Pruning = delete both trees (agent sandbox + fresh-checkout
  clone); the bare clone cache is retained (the "shared base layers
  retained" analogue). The WP4 `eval/<instance_id>` workdir is pruned in
  **both** modes (potentially GBs per instance).

**Patch-extraction hardening (the review rounds):** the porcelain status
is read with `git status --porcelain -z` (NUL-separated, RAW paths — the
line mode quotes special paths, e.g. a non-ASCII filename, and would
silently drop the agent's files); the diff is rendered with
`-c core.quotepath=false` so the diff paths round-trip to the eval leg's
`git apply`; a failing `git add -N` (intent-to-add staging) is a loud
`PilotError`, never silent; the binary-content guard `lstat`s first (a
FIFO / socket is not opened — `open()` on a FIFO blocks indefinitely).

**Status mapping (harness → the WP3 `TERMINAL_STATUSES` set, unchanged):**

| observed | container | sandbox |
|---|---|---|
| pre-flight leg fails | `preflight_failed` | `preflight_failed` |
| rate-limit / quota text (stop reason / **live-drained** stderr / non-zero exit) | `quota_limited` | `quota_limited` |
| wall cap (45 min) → `AcpTimeoutError` (client already `session/cancel`ed + group-killed) | `timeout` | `timeout` |
| `end_turn` + empty diff | `no_patch` | `no_patch` |
| process crash / transport / protocol / refusal stop reason | `agent_error` | `agent_error` |
| `end_turn` + patch; eval verdict | `resolved` / `failed_test` / `eval_error` | host check → `resolved` / `failed_test` / `eval_error` (else `eval_error` + `skip_reason`) |

**Intentional interrupt (AC3):** `--interrupt-after N` — after the N-th
instance's checkpoint line is flushed, the harness aborts with a labeled
`intentional_interrupt` (a host-crash-class abort: the remaining instances
have **no** checkpoint line). The follow-up invocation (`--resume` + the same
`--run-id`) goes through `resume_queue`: the finished instance (a
`NEVER_RERUN` terminal status) is **skipped** — never re-run, no session
spent on it — and the rest run. The pilot verifies: exactly one line per
finished instance, the interrupted run's session count + the resume run's
session count = 3 total, and the skip is observable in the run log. (The
interrupt lands **between** instances — zero extra quota; a mid-session kill
is the WP2/WP3 unit-tested path.)

**Budget + calibration (AC2):** `BudgetConfig(max_sessions_per_day,
max_wall_per_session, daily_pause)` (WP0 §4 defaults; the pilot's calibrated
values come from `bench/live_pilot_budget.json`, "budget is config, not
code"). Per instance the harness records: wall (setup / session / eval
phases), visible usage verbatim (`usage.{inputTokens, outputTokens,
cachedReadTokens, totalTokens}` — WP0 §1 confirms the real server exposes
it; the fallback proxy per PLAN §2.6 is sessions + wall + prompt/patch byte
sizes), prompt bytes, patch bytes, permission request/decision counts,
vendor notification count. The **calibration** output: per-session wall
median/mean + total tokens; `wall_limited_per_day = floor(24 h / median wall)`
as the wall-limited outer estimate; the quota-limited number stays
**conservative (10, WP0 default)** because the CLI exposes no remaining-quota
field (WP0 §4 — web-app only) and the account is Pro-plan (daily + weekly,
calendar reset) — the decision + its basis are recorded in the go/no-go note.

**Go/No-go checklist (AC4):** ordered, itemized `{item, expected, observed,
pass}`: (1) `BENCH_LIVE` gate, (2) model gate (Free tier), (3) pre-flight
legs, (4) NDJSON framing (the `initialize` result; no malformed-frame error;
vendor notifications logged-not-errored), (5) the 5 wire method names used
(incl. `session/request_permission` when observed), (6) permission flow
(requests observed, all answered per policy; zero observed is recorded as such
— WP0's trivial-task caveat), (7) `session/close` sent + group-kill + **no
orphan** (pgid check — WP0 §1; a `close()` failure leaves the check
**unperformed** → the item FAILS, it does not silently pass), (8) visible
usage captured or proxy recorded, (9) per-instance wall ≤ cap, (10) pruning
(per-instance record present **in the same checkpoint line** and free of
failures; base retained), (11) resume after the intentional interrupt
(terminal skipped, no duplicates, session arithmetic — a plain crash/abort
resume does **not** satisfy the item; the previous run must have been the
intentional interrupt), (12) budget within
`max_sessions_per_day` / wall budget, (13) eval leg verdict (container:
official harness; sandbox: host check or documented skip — **deviation,
non-gating**), (14) leak guard **verified post-session, gated on the
recorded results** (the pre-session guard's `remotes_after` is empty + the
post-session verification is clean: no `origin` remote, no post-base tags,
**no local branch pointing past `base_commit`** — a branch tip not
reachable from the base carries the merged fix commit). The budget item
(12) counts the **whole run pair** (the deduped records' sessions + wall —
consistent with the wall sum), so a `--resume` can't reset the daily cap.
**GO ⟺ items 1–12 + 14 all pass.** An **aborted** run (the WP3 terminal
`aborted` — 2 consecutive `agent_error`, or a per-instance pre-flight
abort) exits `4` with the abort recorded in the state file and **no
checklist rendered** (it is the host-crash class: the operator fixes and
resumes). An **interrupted** run decides nothing: it is a PARTIAL record
(e.g. 1/3 instances — no gating item requires all pinned instances to
have a completed line), so it renders `**Verdict: n/a (interrupted —
resume to complete)**` instead of a GO the resume invocation would
silently supersede; the pair's verdict is the resume invocation's. A
host-crash (`run_failed`) invocation records `final_state: run_failed` in
the state file (never `done`). The decision + measured numbers are
written to `<workdir>/wp6a-go-no-go.md` (auto-generated) and committed to
`bench/notes/wp6a-go-no-go.md` with the run artifacts referenced.

**Artifacts (per run, under `<workdir>/`):** `run-<run_id>.jsonl` (WP3b
checkpoint, the `report.py` source of truth), `pilot-<run_id>.state.json`
(the pilot's interrupt / abort / resume bookkeeping — including the
cumulative `resume_exercised` flag), `pilot.lock` (the runner's
overlap guard), per-instance `runs/<instance_id>/{patch.diff,
trajectory.jsonl (the AcpSession transcript dump), eval.log (the
host-check log: install/apply/pytest -rA + the verdict inputs)}` — the
exact shapes `report.write_artifacts` / `check_artifacts` cross-check
(the WP5 reference is the relative `meta.artifacts_dir =
"runs/<instance_id>"` resolved against the JSONL's directory; a mismatch
surfaces as an `ArtifactCheckError`, which fails the run — exit 4, no
verdict rendered), the retained `clone-cache/` (sandbox: the shared
bare-clone "base layers"), then `report.generate(...)` → `report/
{report.md (the WP5 methodology meta — `run_id`, `devin_cli_version`,
`model`, `cost_tier`, `prompt_template_hash`, `permission_policy`,
`caps`, `harness_version`, `images` — embedded in the report; the meta
is passed in-memory, not a separate file), all_preds.jsonl,
trajs/<instance_id>.jsonl}`.

### `bench/pilot_instances.json` (new) — the pinned 3 instances

Smallest 3 repos of the Verified split (WP0 §5: flask 1 · seaborn 2 ·
requests 8), one instance each, pinned verbatim from the
`SWE-bench/SWE-bench_Verified` `test` split (fetched 2026-11-15 via the
HF datasets-server rows API; `source` + `fetched_at` recorded in the file):

| instance_id | repo | base_commit | image | F2P / P2P |
|---|---|---|---|---|
| `pallets__flask-5014` | pallets/flask | `7ee9ceb71e86…` | `swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest` | 1 / 59 |
| `mwaskom__seaborn-3069` | mwaskom/seaborn | `54cab15bdacf…` | `swebench/sweb.eval.x86_64.mwaskom_1776_seaborn-3069:latest` | 2 / 94 |
| `psf__requests-1142` | psf/requests | `22623bd8c265…` | `swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest` | 1 / 5 |

### `bench/tests/test_live_pilot.py` (new) — zero-quota CI + one `bench_live`

* `--dry-run` (no `BENCH_LIVE`): config/instance-file validation, budget math,
  checklist skeleton — no devin spawn.
* **Full sandbox pilot against `mock_devin`** (the WP2 double as the injected
  `devin_bin`; `--mode` per instance via the injected `make_session`'s
  `extra_args`; fixture git repos created in `tmp_path` stand in for the
  3 instances): terminal statuses, checkpoint lines, pruning (sandbox tree
  removed, clone cache retained), checklist computed with pass counts.
* **Failure-mode mapping through the harness:** mock `hang` → `timeout`,
  `crash` → `agent_error`, `quota` → `quota_limited`, `no_patch` → `no_patch`.
* **Interrupt + resume (the AC3 path, zero quota):** `--interrupt-after 1` →
  the 2nd invocation `--resume` → instance 1 skipped (its single line
  intact), instances 2–3 ran in run 2, no duplicate lines, session arithmetic
  asserted.
* **`BENCH_LIVE` gate + real binary:** the live test is
  `@pytest.mark.bench_live` (the `tests/test_live_gate.py` static guard
  passes — it names the real binary only inside the marked test): a
  minimal real-`devin acp` handshake (`initialize` + `session/new` + a
  trivial prompt + close) in a scratch dir — quota-cheap, the WP0 probe
  analogue; skipped by default.
* `BENCH_LIVE` absent + a non-dry-run invocation → refusal (exit ≠ 0, no
  devin spawn).

### Docs

* `bench/README.md` — the WP6a line + the `live_pilot.py` /
  `pilot_instances.json` layout rows + the sandbox-mode deviation note.
* `bench/notes/wp6a-go-no-go.md` — the decision record: the environment
  deviations (no container runtime; delegated real-binary execution), the
  measured per-instance wall time + usage, the calibrated budget, the
  checklist table, and the GO/NO-GO verdict (filled from the live run).
* `bench/PLAN-WP6A.md` — this file.

## Live-run execution (the real quota leg)

The operator session cannot run `devin` (bash-blocked; WP0 Appendix A): the
live pilot runs in a **delegated Devin session** (the WP0 pattern), in this
workspace (the sandbox under the trusted workspace root — WP0 §8), as:
`BENCH_LIVE=1 python bench/live_pilot.py --mode sandbox --run-id pilotA
--interrupt-after 1`, then the resume invocation. The run is sequential
(concurrency 1), `swe-2-high` (Free tier, WP0 §7), 45-min cap, the calibrated
budget from `live_pilot_budget.json`.

## Contingencies

1. **Untrusted-cwd dead turn** (WP0 §8) → the harness detects the no-update
   signature, records `suspected_untrusted_cwd`, and the delegated session
   re-runs with the sandbox moved under the workspace root (the checklist
   item records the fix).
2. **Real `session/request_permission` shape unobserved in WP0** (open item
   1) → the pilot's SWE tasks must run pytest ⇒ exec permission requests are
   expected; the observed shape is recorded verbatim in the go/no-go note
   (WP1's policy handler was built on the mock contract + schema names).
3. **Host-check infra failure** (pip/network/deps of 2023-era repos) →
   `eval_error` + the documented skip; the gate does not depend on the eval
   verdict (AC1–AC3 stand on the agent-phase conformance + the budget).
4. **Quota hit mid-pilot** (`quota_limited` stop) → the WP3b state machine
   stops the batch, checkpoints, schedules resume; the pilot resumes in the
   next window (the resume path then doubles as the AC3 proof).
5. **`session/close` handler dump / process survival** (WP0 §1) → already the
   WP1 contract (best-effort close + group kill); the checklist item asserts
   the no-orphan check.

## AC map

* **AC1 (3 instances end-to-end, real binary, conformance)** → live run in
  sandbox mode (real `devin acp`, real auth, `swe-2-high`); checklist items
  4–7 (NDJSON, wire names, permission flow, close+kill) from the real
  transcript; container-mode path implemented (primary, PLAN-conformant) and
  exercised wherever docker exists.
* **AC2 (wall + visible usage + `max_sessions_per_day` calibration)** → per-
  instance wall phases + verbatim usage (WP0 §1: the real server exposes
  `usage.{inputTokens, outputTokens, cachedReadTokens, totalTokens}`);
  calibration per the Budget + calibration section.
* **AC3 (pre-flight / pruning / resume for real, one intentional interrupt)**
  → the real pre-flight legs (auth + sandbox runtime + disk), the per-instance
  pruning (sandbox trees removed, cache retained), `--interrupt-after 1` +
  `--resume` with the `resume_queue` skip assertion.
* **AC4 (go/no-go decision recorded with the measured numbers)** →
  `notes/wp6a-go-no-go.md` (auto-generated checklist + the measured table).
