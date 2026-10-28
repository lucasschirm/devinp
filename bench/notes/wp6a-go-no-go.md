# WP6a Pilot A — live measurement record (issue #21)

Recorded by the operator session that ran the harness (observation only —
no harness code was modified). The harness's own auto-generated record is
`pilot-a/wp6a-go-no-go.md` at the workspace root (reproduced verbatim below).

**Final verdict printed by the harness: `GO`** (run 2, exit 0).
Run 1 printed `NO-GO (failed: 11 resume after the intentional interrupt
(WP3b))` — expected mid-pilot, since item 11 can only pass after the resume.

## Commands run (verbatim) + wall clock + exit codes

Working directory for both: `/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate`

1. `BENCH_LIVE=1 /tmp/benchvenv/bin/python bench/live_pilot.py --mode sandbox --run-id pilotA --interrupt-after 1`
   - start (UTC): 2026-10-28T20:42:19Z · end (UTC): 2026-10-28T20:46:51Z · elapsed ≈ 4 min 32 s
   - **exit code: 42** (the intentional interrupt after 1 of 3 instances — as designed)
2. `BENCH_LIVE=1 /tmp/benchvenv/bin/python bench/live_pilot.py --mode sandbox --run-id pilotA --resume`
   - start (UTC): 2026-10-28T20:47:05Z · end (UTC): 2026-10-28T20:56:28Z · elapsed ≈ 9 min 23 s
   - **exit code: 0** (remaining 2 instances completed)

Note on workdir: the task brief mentioned `bench/work/`; the harness's
actual default is `<cwd>/pilot-a`, so artifacts live at
`<workspace-root>/pilot-a/` (outside `bench/` — written by the harness
itself, left in place). The workdir contains `run-pilotA.jsonl` (the JSONL
checkpoint), `report/` (`report.md`, `all_preds.jsonl`), `runs/<instance>/`
(`patch.diff`, `trajectory.jsonl`, `eval.log`), `clone-cache/` (3 bare
repos, retained), `host-check/` (emptied after pruning), `pilot.lock`,
`pilot-pilotA.state.json`, and `wp6a-go-no-go.md`.

## Complete stdout — command 1 (verbatim)

```
[pilot] devin: devin 3000.11.92 (7f4e64980e22) · model: swe-2-high · mode: sandbox · run: pilotA
[pilot] INTERRUPT (exit 42): intentional interrupt after 1 instance(s) (the remaining instances are the resume class, PLAN §2.5)
[pilot] WP5 report: ArtifactCheckError: artifact cross-check failed:
  - line 1 (pallets__flask-5014): referenced patch_diff does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
  - line 1 (pallets__flask-5014): referenced trajectory does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/trajectory.jsonl'
  - line 1 (pallets__flask-5014): referenced eval_log does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/eval.log'
  - pallets__flask-5014: status 'failed_test' requires an existing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
  - all_preds.jsonl: pallets__flask-5014 references a missing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
[pilot] go/no-go: NO-GO (failed: 11 resume after the intentional interrupt (WP3b))
[pilot] artifacts: /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/run-pilotA.jsonl · /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/report · /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/wp6a-go-no-go.md
[pilot] resume with: live_pilot.py --resume --run-id pilotA … (the finished instance(s) are skipped — never re-run)
```

(The harness does not print per-instance record lines to stdout; the
per-instance records live in `run-pilotA.jsonl` — table below.)

## Complete stdout — command 2 (verbatim)

```
[pilot] devin: devin 3000.11.92 (7f4e64980e22) · model: swe-2-high · mode: sandbox · run: pilotA
[pilot] resuming: skipped {'pallets__flask-5014': 'failed_test'}; to run: ['mwaskom__seaborn-3069', 'psf__requests-1142']
[pilot] run finished: done
[pilot] WP5 report: ArtifactCheckError: artifact cross-check failed:
  - line 1 (pallets__flask-5014): referenced patch_diff does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
  - line 1 (pallets__flask-5014): referenced trajectory does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/trajectory.jsonl'
  - line 1 (pallets__flask-5014): referenced eval_log does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/eval.log'
  - line 2 (mwaskom__seaborn-3069): referenced patch_diff does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/mwaskom__seaborn-3069/patch.diff'
  - line 2 (mwaskom__seaborn-3069): referenced trajectory does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/mwaskom__seaborn-3069/trajectory.jsonl'
  - line 2 (mwaskom__seaborn-3069): referenced eval_log does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/mwaskom__seaborn-3069/eval.log'
  - line 3 (psf__requests-1142): referenced patch_diff does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/psf__requests-1142/patch.diff'
  - line 3 (psf__requests-1142): referenced trajectory does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/psf__requests-1142/trajectory.jsonl'
  - line 3 (psf__requests-1142): referenced eval_log does not exist: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/psf__requests-1142/eval.log'
  - mwaskom__seaborn-3069: status 'resolved' requires an existing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/mwaskom__seaborn-3069/patch.diff'
  - pallets__flask-5014: status 'failed_test' requires an existing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
  - all_preds.jsonl: mwaskom__seaborn-3069 references a missing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/mwaskom__seaborn-3069/patch.diff'
  - all_preds.jsonl: pallets__flask-5014 references a missing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/pallets__flask-5014/patch.diff'
  - all_preds.jsonl: psf__requests-1142 references a missing patch.diff: '/home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/psf__requests-1142/patch.diff'
[pilot] go/no-go: GO
[pilot] artifacts: /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/run-pilotA.jsonl · /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/report · /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a/wp6a-go-no-go.md
```

## Measured numbers per instance (from `run-pilotA.jsonl`)

| instance_id | terminal status | session wall `session_s` | total wall `wall_s` (setup/session/eval) | usage (total/input/output/cachedRead tokens) | permission req/allow/deny | vendor notifs | stop_reason | patch bytes |
|---|---|---|---|---|---|---|---|---|
| pallets__flask-5014 | failed_test | 251.109 s | 268.637 s (0.691/251.109/13.946) | 31015 / 30692 / 323 / 30464 | 27 / 27 / 0 | 22 | end_turn | 435 |
| mwaskom__seaborn-3069 | resolved | 411.451 s | 447.094 s (0.144/411.451/31.714) | 68578 / 67953 / 625 / 67168 | 37 / 36 / 1 | 34 | end_turn | 2240 |
| psf__requests-1142 | eval_error | 104.785 s | 113.014 s (0.204/104.785/4.390) | 29279 / 28941 / 338 / 28128 | 8 / 8 / 0 | 16 | end_turn | 1239 |

Common fields (all three): `suspected_untrusted_cwd=false`,
`orphan_check=true`, `stderr_tail=""`, `graded_by="host_check(pytest -rA)"`,
`leak_verify={"remotes": [], "tags": [], "clean": true}`,
`eval_skip_reason=null` except psf (see below), prompt_bytes
655/1648/841, updates 510/736/350. Sessions used: 3 total (1 in run 1 +
2 in run 2 — the resume skip held; exactly one checkpoint line per
instance, no duplicates).

Eval-leg detail (non-gating, checklist item 13):

- `pallets__flask-5014` — `failed_test`: host pytest rc=1; the F2P test
  `tests/test_blueprints.py::test_empty_name_not_allowed` failed; 59 P2P
  failed + 44 node ids missing. eval.log shows `60 errors in 0.20s`, all
  collection-time `AttributeError` (a 2023-era flask vs host-python
  incompatibility signature — environment-plausible, not verified further).
  The agent did produce a 435-byte patch.
- `mwaskom__seaborn-3069` — `resolved`: host pytest rc=0; all 2 F2P + 94
  P2P passed on the fresh checkout + 2240-byte patch.
- `psf__requests-1142` — `eval_error` (the documented retry class):
  `eval_skip_reason` = pip install infra failure — the 2017-era vendored
  urllib3 does `from collections import MutableMapping`, which fails on
  the host's Python 3.12 (`ImportError` during "Getting requirements to
  build wheel"). The agent session itself completed normally
  (`end_turn`, 1239-byte patch extracted).

## Go/no-go checklist — final run output, verbatim

Reproduced from `pilot-a/wp6a-go-no-go.md` (auto-generated after run 2):

```
**Verdict: GO**

- run: `pilotA` · mode: sandbox · model: swe-2-high · devin: devin 3000.11.92 (7f4e64980e22)
- budget: max_sessions_per_day=10 · max_wall_per_session=2700s

| # | item | expected | observed | pass |
|---|---|---|---|---|
| 1 | 1 BENCH_LIVE gate | BENCH_LIVE=1 (the real run is opted in) | BENCH_LIVE=1 | PASS |
| 2 | 2 model gate (fail-closed) | swe-2-high cost_tier == Free (WP0 §7) | allowed=True tier='Free' reason=None | PASS |
| 3 | 3 pre-flight (zero quota, every batch + instance) | auth + runtime + disk legs pass before any session | all legs passed | PASS |
| 4 | 4 NDJSON framing + vendor notifications (WP0 §1) | the initialize result is received; vendor notifications are logged, never errors | initialize ok on 3 instance(s); vendor notifications observed: 72 | PASS |
| 5 | 5 wire method names (WP0 §1) | initialize / session/new / session/prompt / session/request_permission (when observed) / session/close | the WP1 AcpSession issued the 5 wire methods (the WIRE map); stop reasons received | PASS |
| 6 | 6 permission flow (WP1b policy) | every request answered per policy (exec in scope allowed; web denied; out-of-scope writes denied) | 72 request(s) observed; all answered per policy: True | PASS |
| 7 | 7 session/close + group kill + no orphans (WP0 §1) | close sent best-effort; the process group killed; no orphan devin survives | orphan check: pass | PASS |
| 8 | 8 visible usage (WP0 §1) or the §2.6 proxy | usage.{inputTokens, outputTokens, cachedReadTokens, totalTokens} captured verbatim, else the sessions + wall + byte-size proxy | usage seen: True; total visible tokens: 128872; proxy (wall + prompt/patch bytes) recorded per instance | PASS |
| 9 | 9 per-instance wall ≤ cap | ≤ 2700s (the 45-min cap) | max session wall: 411.5s | PASS |
| 10 | 10 pruning (PLAN §7) | the per-instance state removed after eval; the shared base retained | prune_instance ran per terminal instance (the per-instance record is in the checkpoint meta); the clone cache / base layers retained | PASS |
| 11 | 11 resume after the intentional interrupt (WP3b) | the finished instance is skipped (never re-run); no duplicate checkpoint lines; the session arithmetic holds (3 total across the run pair) | resumed: pallets__flask-5014 skipped (failed_test) — after the intentional interrupt | PASS |
| 12 | 12 within the calibrated budget | sessions ≤ 10; wall ≤ 27000s | sessions this invocation: 2; wall used: 828.7s | PASS |
| 13 | 13 eval leg verdict (deviation-tolerant, NON-gating) | container: the official harness; sandbox: the host check or the documented no-runtime skip | pallets__flask-5014: failed_test (host_check(pytest -rA)); mwaskom__seaborn-3069: resolved (host_check(pytest -rA)); psf__requests-1142: eval_error (host_check(pytest -rA)) [skip: pip install: ger.py" | n/a |
| 14 | 14 leak guard (PLAN §3.6) | no origin remote, no tags, reflogs expired (verified post-session) | the pre-session guard ran on every sandbox checkout; the post-session verification is recorded per instance in the checkpoint meta (leak_verify) | PASS |
```

## `max_sessions_per_day` calibration observation

Measured per-instance session walls (s): `[104.785, 251.109, 411.451]`
(psf__requests-1142 / pallets__flask-5014 / mwaskom__seaborn-3069).

The pilot's printed arithmetic (Calibration section, verbatim):

- per-session wall (s): [104.785, 251.109, 411.451]
- median: 251.109 · mean: 255.782
- visible usage seen: True · total visible tokens: 128872
- wall-limited sessions/day: 344 · chosen `max_sessions_per_day`: 10
- basis: the CLI exposes no remaining-quota field (WP0 §4); Pro-plan
  daily + weekly allowance, calendar reset; the quota-limited number
  stays the conservative config value (the WP0 default 10); the
  wall-limited number is the outer estimate from the measured median wall

Check of the arithmetic: floor(86400 s / 251.109 s) = 344 — consistent
with the printed `wall-limited sessions/day: 344`. The chosen
`max_sessions_per_day=10` remains the conservative WP0 default because
the CLI exposes no remaining-quota field. Total session wall actually
consumed across the pilot: 767.345 s ≈ 12.8 min for 3 sessions — far
inside both limits. Total visible tokens: 128872.

## Quota warnings, vendor notifications, anomalies

Observed:

- **Vendor ACP notifications: 72 total** (22 + 34 + 16 per instance),
  logged by the harness as counts — checklist item 4 records them as
  "logged, never errors". No notification caused an error.
- **Permission requests: 72 total** (27 + 37 + 8), all answered per
  policy; **1 denial** (mwaskom__seaborn-3069): a `webfetch` tool call
  was rejected — "Tool execution was rejected: User rejected this tool
  call" — consistent with the policy's "web denied" leg. The trajectory
  shows the agent acknowledged the rejection and continued to `end_turn`.
- **WP5 report `ArtifactCheckError` (both invocations, verbatim above):**
  the artifact cross-check looked for `patch.diff` / `trajectory.jsonl` /
  `eval.log` at `<workdir>/<instance_id>/`, but the harness actually
  wrote them under `<workdir>/runs/<instance_id>/` (verified on disk —
  all 3 files exist there per instance). Side effects: `report.md`'s
  cross-check reports "FAIL (14 violations)" and `all_preds.jsonl`
  carries `"model_patch": ""` for all 3 instances despite real patches
  existing in `runs/`. The harness caught the error, continued to the
  checklist, and exited normally — recorded here as a harness artifact-
  path inconsistency, NOT fixed (another session owns the code).
- **Run-1 mid-pilot verdict** printed `NO-GO` solely on checklist item
  11 (resume not yet exercised) — expected behavior, resolved by run 2.
- **Pruning:** the per-instance `testbed`/`xdg` trees and `host-check`
  checkouts were removed after each instance (observed live: they existed
  during the session, gone after); the `clone-cache/` bare repos and the
  `runs/<iid>/` artifact files were retained. The JSONL meta `pruning`
  field itself reads `null` for all three (the checklist still records
  item 10 PASS from the on-disk evidence).
- **Timing note:** all three instances finished far under the 45-min cap
  (max session wall 411.5 s ≈ 6.9 min).

Not observed (explicit negative results — valid deliverables, not
invented):

- **No quota/rate-limit signal** — no `quota_limited` status, no 429 /
  "rate limit" / "usage limit" text in any `stderr_tail` (all empty) or
  stop reason (all `end_turn`). Not observed.
- **No timeout** — no instance approached the 2700 s cap. Not observed.
- **No agent_error / no_patch / preflight_failed** — not observed.
- **No `suspected_untrusted_cwd`** — all three false. Not observed.
- **No orphan devin processes** — `orphan_check=true` per instance;
  a post-run `ps` shows no live_pilot-spawned `devin acp` survivor.
- **No account-level quota visibility** — consistent with WP0 §4 (the
  CLI exposes no remaining-quota field); nothing to record beyond the
  visible usage fields above. Web-app quota state: not checked (out of
  scope for this session).
- **`session/request_permission` wire frames** do not appear literally
  in the trajectory dump (0 frames matching that method string) — the
  permission flow is recorded via the meta counts and the tool-call
  rejection update instead; whether the harness counts requests from a
  different frame shape: not verified.
- **Vendor notifications content** — only the count (72) is recorded in
  the checkpoint; their payloads were not individually inspected.

## Review round (PR #37 `pr-review` — the fixes + the checklist semantics)

The first `pr-review` round raised 4 Major / 5 Medium / 3 Minor findings
plus review-body notes. All were addressed (the zero-quota regression
tests live in `tests/test_live_pilot.py`):

**Major**

1. **Malformed `docker exec` shim (container mode)** — the shim was
   multi-line; `exec docker exec -i` would run with no container and die,
   so a container-mode session could never start. Now a **single-line**
   shim (the `exec` + every flag form one command; regression test asserts
   the shape).
2. **`git -C /testbed` on the host filesystem (container mode)** — the
   agent's `cwd` is the in-container `/testbed`, but the host-side patch
   extraction + leak verification ran `git -C /testbed` on the *host*.
   Now `SessionSpec.container` + `PilotSession._extract_repo_dir()`:
   container mode copies the tree out (`docker cp`) and the host-side
   git ops run on the scratch host copy (pruned with the instance).
3. **Checklist item 10 hardcoded `pass=True`** — the pruning meta was
   written *after* the JSONL line was persisted, so the "record" it
   claimed never landed in the line. Now the prune hook runs **before**
   the line is merged + persisted (same-line record), and the item
   gates on the record being present and failure-free on every line.
4. **Checklist item 14 ignored the recorded guard result** — it rendered
   from `devin_ok` with the guard results discarded. Now the pre-session
   guard record is kept in the session meta (both modes) and the item
   gates on `remotes_after == []` + the post-session verification being
   clean on every instance.

**Medium**

1. **`$`-anchored exclusion patterns never matched** — the porcelain
   collapses untracked dirs to trailing-slash entries (`build/`) and
   files inside them end in the filename (`build/lib/foo.py`); a
   `$`-anchored pattern matched neither, so generated `.py` artifacts
   would leak into the patch. Patterns now use `(/|$)` boundaries.
2. **`stderr_tail` dead during the session** — it is only populated at
   `close()`, so the mid-session quota/rate-limit signal (PLAN §2.3)
   could never fire. `AcpSession.stderr_tail_now()` exposes the
   drain thread's live tail; the pilot reads it during the session and
   falls back to the close()-captured value afterwards.
3. **Checklist item 7 passed with no check performed** — a `close()`
   failure left `orphan_check` `None`, which rendered as pass. Now an
   unperformed check FAILS the item (the failure is recorded in the
   checkpoint meta `close_error`).
4. **Checklist item 11 passed for any resume** — now the previous run
   must have been the *intentional* interrupt (the state file's
   `interrupted` flag, read before this run overwrites it); a plain
   crash/abort resume does not satisfy the item.
5. **`aborted` final state exited 0 unrecorded** — the WP3 terminal
   abort (2 consecutive `agent_error`, or a per-instance pre-flight
   abort) now exits `4`, records `aborted` + `final_state` in the state
   file (visible to the resume invocation), and renders **no checklist**
   (no GO for an aborted run).

**Minor**

1. **Unguarded `devin_version` probe** — a missing binary / a hang
   escaped the documented exit-4 path as a traceback. Now the probe
   returns `unknown …`; the pre-flight's `which devin` leg is the loud
   gate that follows.
2. **Doc/code gate-order mismatch** — the code ran the model gate before
   the pre-flight, contradicting PLAN-WP6A.md. Now the pre-flight runs
   first (zero quota, before any session), matching the documented order.
3. **Hardcoded `/home/lucas/…` `_versions` bind (container mode)** — now
   resolved via `_host_versions_dir()` (`DEVIN_VERSIONS_DIR` env, then
   the XDG locations; a missing tree is a clear pre-session error).

**Review-body notes** — `bench/pyproject.toml` `py-modules` +=
`live_pilot`; a `RunnerError` (incl. `RunnerProtocolError`) from the WP3
layer no longer escapes `main()` as a traceback (exit 4, the resume
class); `PilotReporter.done_count` no longer counts `preflight_failed`
marks as sessions (the budget claim is released — they consumed no
session). `preflight_failures` in the checklist context is the batch
marker's record: a batch failure exits 4 before any checklist renders.

**Note on the recorded verdict:** the live `GO` above was printed by the
pre-review checklist (item 10/14 rendered from assumptions). The live
run's *evidence* satisfies the tightened semantics (orphan checks pass on
all three; the guard ran per instance; the interrupt + resume pair ran;
pruning ran per instance) — but the pruning record of that run landed in
the *next* line under the old code, so a re-render of the *old* JSONL
under the new checklist would gate item 10 on the recorded state. The
tightened checklist is the deliverable; if a live re-verification is
required, the documented two-command sequence (interrupt + resume) is
the run pair to re-execute.

## Review round 2 (PR #37 `pr-review` — the second pass)

The second `pr-review` pass (on the round-1 fixes) raised 5 Major /
5 Medium / 6 Minor findings. All addressed (zero-quota regression
tests in `tests/test_live_pilot.py`):

**Major**

1. **Collapsed untracked dir ≥2 components deep dropped whole** —
   `?? src/newpkg/` failed the lexical gate on the *dir* path (no
   source/test marker, not a `.py`), so the per-file expansion never
   ran and the agent's new package silently never reached the patch.
   Now every collapsed untracked dir is expanded to its files and the
   guards run per file (a dir excluded as a whole — `.venv/`,
   `node_modules/`, `__pycache__/` — short-circuits to a single skip
   entry, no per-file walk of a 10k-file venv).
2. **Generated dirs swept into the diff** — `.venv/`, `node_modules/`
   (plus `.tox`, `dist`, `.mypy_cache`) passed the inclusion gate, so
   a routine agent `python -m venv .venv` intent-to-added every
   `.py` under the tree, blew the diff past `PATCH_SIZE_CAP_BYTES`,
   and the completed session was discarded (retried at full quota,
   same cap). Those dir names are now exclusion patterns.
3. **Eager pre-flight vs container setup / resume** — the pilot's
   batch loop pre-flighted *every pinned* instance before the session
   factory, so the `docker_image` leg fired before the setup's
   `docker pull` (a cold host aborted `preflight_failed` — the pull
   unreachable) and before `resume_queue` filtering (a `--resume`
   inspected finished instances' images, which the prune `docker rmi`'d).
   Now: the resume state + `to_run` are read **before** the pre-flight;
   the eager loop inspects only the instances this invocation will
   run; and the per-instance image leg is **pull-if-missing** (the
   zero-quota pull belongs in the pre-flight; the setup's pull then
   no-ops).
4. **Checklist read raw checkpoint records** — a superseded retry-class
   line (an instance-level `preflight_failed` carries no
   orphan/leak/pruning meta) flipped items 7/10/14 on the resumed run,
   a false NO-GO for an instance that completed cleanly. The checklist
   now uses the deduped last-line-per-instance view (the WP5
   `rep.dedupe` contract).
5. **`ArtifactCheckError` swallowed, verdict still rendered** — a
   corrupt report could not fail the gate (the round-1 live record
   shows exactly this: `WP5 report: ArtifactCheckError` … then
   `go/no-go: GO`). Now a failed report pipeline records the failure
   in the state file (`final_state: report_failed`), removes the
   stale record, and exits 4 with **no verdict rendered** (the resume
   class).

**Medium**

1. **`-rA` parser keyed on the raw line** — real pytest prints
   `FAILED <id> - <reason>` (the suffix landed in the key, so the
   outcome was also filed under `missing`) and `SKIPPED [N]
   <file>:<line>: <reason>` (never matched `::`/`test_`, so it never
   parsed). Now: the reason suffix is stripped, the `[N]` token is
   handled, and a file-level outcome (a collection `ERROR`, a
   `file:line` `SKIPPED`) is attributed to every requested node id
   under that file.
2. **Item 6's `perms_all_answered` was vacuous** — a permission
   transcript entry is appended *before* the response send is
   attempted (send failures swallowed), so `requests == allowed +
   denied` held by construction. Now the client counts
   `permission_send_failures` (a response the server never heard), the
   session meta records it, and item 6 fails on a non-zero count.
3. **Repeat `--resume` on a finished pair rewrote the record to
   NO-GO** — run 2 reset the state file's `interrupted` flag, so the
   AC3 leg failed on the third invocation and the GO was not durable.
   Now the state carries a cumulative `resume_exercised` flag (the
   pair's intentional-interrupt resume), item 11 passes on it, and a
   third invocation re-runs nothing and keeps the GO.
4. **Crash-resume reused the mutated testbed** — a mid-session crash
   leaves `runs/<iid>/testbed` (the prune runs only for a completed
   record), so the retried session inherited the previous attempt's
   edits and the patch was the union of two attempts (pass@1
   contamination). Now the sandbox factory removes the stale agent
   tree (`testbed` + `xdg`) before the re-clone (the clone cache is
   retained), and container mode `docker rm -f`s a crashed attempt's
   container before the `docker run --name`.
5. **README coverage claims** — the pre-staged-junk path was untested
   and no test asserted `meta["pruning"]` lands in the persisted line.
   Added: `test_extract_patch_unstages_pre_staged_junk` and the
   mock-driven full-pilot assertion that the pruning record is in
   every JSONL line.

**Minor**

1. **Dry-run verdict inconsistency** — the skeleton rendered `Verdict:
   NO-GO` (`_verdict` counted `pass=None` as failed) and the early
   exits left a stale verdict file standing. Now `_verdict` returns
   `n/a` for a rendering with no decided items (a dry run decides
   nothing), and every early exit (dry-run skeleton, pre-flight /
   model-gate refusal, an abort) removes the previous invocation's
   record file (`_remove_stale_record`).
2. **Phantom `which devin` docstring** — `make_preflight` had no such
   leg. Now there is one: the `binary` leg (`shutil.which` — a missing
   binary is the loud exit-4 gate that follows the informational
   `devin version` probe).
3. **`inst.get(k) in (None, "")` passed non-string scalars** —
   `base_commit: 0` / `instance_id: false` surfaced later as a raw
   `TypeError` inside the git argv. Now a strict `isinstance(v, str)
   and v` check (bools/numbers rejected) → `PilotConfigError`.
4. **Dead `_mock_pilot` helper** — removed.
5. **`.gitignore` gap** — the live smoke's scratch dir
   (`pilot-a-smoke/` at the repo root) is now ignored.
6. **PLAN artifact drift** — `pilot-a-meta.json` / `session.log` /
   `host-check.log` are not produced (the meta is in-memory to
   `report.generate`; the eval log is `eval.log`). The artifacts
   section now lists the real set (`run-<id>.jsonl`,
   `pilot-<id>.state.json`, `pilot.lock`, `runs/<iid>/{patch.diff,
   trajectory.jsonl, eval.log}`, the retained `clone-cache/`,
   `report/{report.md, all_preds.jsonl, trajs/}`).

## Review round 3 (PR #37 `pr-review` — the third pass)

The third `pr-review` pass raised 1 Blocker / 1 Major / 4 Medium /
7 Minor findings. All addressed (zero-quota regression tests in
`tests/test_live_pilot.py`):

**Blocker**

1. **Container mode could never spawn a session** — `spec.cwd=
   "/testbed"` was passed to the host-side `Popen(cwd=...)` (a
   `FileNotFoundError` on every host before the docker-exec shim ever
   ran; the tests only injected `FakeAcp` in container mode, so the
   spawn path was never exercised). Now `AcpSession` has a
   `session_cwd` seam: the devin process is a **host** process, so its
   spawn cwd is a host-side scratch dir (`runs/<iid>/spawn`); the
   **session** cwd (what `session/new` sends the agent) stays
   `/testbed` (the shim's `docker exec -w /testbed` carries it).
   Regression: `test_container_mode_spawn_cwd_is_host_path`.

**Major**

1. **The leak guard left the clone's local branch at the upstream tip**
   — `apply_leak_guard` stripped remotes/tags/reflogs but left
   `main`/`master` past `base_commit` (the merged fix commit one
   `git log` away); `verify_leak_guard` checked only remotes + tags, so
   item 14 reported `clean` with the gold patch reachable. Now the
   guard (host **and** the in-image container variant, after a
   `git checkout --detach`) deletes **all local branches**, and the
   verification additionally flags any branch tip **not reachable from
   `base_commit`** (`git merge-base --is-ancestor <tip> <base>`;
   fail-closed on unresolvable objects). Regression:
   `test_leak_guard_deletes_local_branches` (a branch at the fix tip is
   caught; a branch at the base is not) +
   `test_container_setup_guard_strips_branches`.

**Medium**

1. **An interrupted run rendered `GO` on a partial record** — item 11
   was undecided and no gating item requires all pinned instances to
   have a completed line, so `--interrupt-after 1` wrote a GO with 1/3
   measured. Now an interrupted run renders `Verdict: n/a
   (interrupted — resume to complete)` — the pair's verdict is the
   resume invocation's. Regression: `test_interrupted_run_verdict_not_go`.
2. **`_is_binary` blocked on FIFOs** — `open(path, "rb")` on an
   agent-created fifo blocks indefinitely. Now `lstat` +
   `S_ISREG` first (a fifo / socket / device is never opened).
   Regression: `test_is_binary_fifo_does_not_block`.
3. **Silent patch drops from quoted paths / unchecked `git add -N`** —
   the line-mode `git status --porcelain` quotes special paths (a
   non-ASCII filename prints as `"w\303\251ird …"`), dropping the
   agent's files with no warning, and a failing `git add -N` was
   unchecked. Now: `--porcelain -z` (raw NUL-separated paths), the diff
   rendered with `-c core.quotepath=false` (paths round-trip to the
   eval leg's `git apply`), and a failing `git add -N` is a loud
   `PilotError`. Regressions: `test_extract_patch_non_ascii_filename_included`
   + `test_extract_patch_add_n_failure_raises`.
4. **Host credentials leaked into the agent env** — the session env was
   the full host `os.environ`, so `GITHUB_TOKEN` / cloud API keys were
   inheritable by the agent's in-scope `env`/`printenv`. Now a
   denylist of secret-ish keys (token/secret/key/password/passwd/
   credential/apikey/auth) is stripped before the session is built
   (both modes), with the stripped key names recorded in the instance
   meta (`env_sanitized`). Regressions: `test_session_env_sanitizes_credentials`
   + `test_prepare_sandbox_strips_credentials`.

**Minor**

1. **`eval/<iid>` pruned only in sandbox mode** — now removed in both
   modes (the WP4 workdir is GBs per instance). Regression:
   `test_prune_removes_eval_workdir_in_both_modes`.
2. **Spawned-tool failures escaped the `eval_error` class** — a
   `FileNotFoundError` (a venv without `bin/pip`) or a
   `subprocess.TimeoutExpired` (a hung pip/pytest) escaped
   `_host_check` as a run-killing fatal. Now both are the
   `eval_error` infra class. Regression:
   `test_host_check_spawn_failure_is_eval_error`.
3. **Item 12 mixed bases** — `sessions_this_invocation` counted the
   current invocation while `wall_used_s` summed the whole run pair, so
   earlier same-day sessions never counted toward the cap. Now the
   sessions count is the deduped-records view (consistent with the
   wall sum).
4. **Container pre-flight had no `_versions` leg** — a missing tree
   surfaced only mid-batch in the setup. Now a `versions` pre-flight
   leg (container mode) fails at the zero-quota gate. Regression:
   `test_preflight_versions_leg_container`.
5. **`_write_state` recorded `final_state: "done"` for a `run_failed`**
   invocation — the durable state file now says `run_failed`.
   Regression: `test_run_failed_state_file_says_run_failed`.
6. **Non-typed errors escaped `main()` as exit-1 tracebacks** — now a
   catch-all maps them to exit 4 (the resume class), inside the
   documented 0/2/3/4/42 contract. Regression:
   `test_main_catch_all_is_exit_4`.
7. **`pilot_instances.json` `fetched_at` postdated the live run** —
   corrected to 2026-10-28 (the date the run consumed the rows).

## Review round 4 (PR #37 `pr-review` — the fourth pass)

The fourth `pr-review` pass raised 1 Blocker / 4 Medium / 7 Minor
findings. All addressed (zero-quota regression tests in
`tests/test_live_pilot.py`):

**Blocker**

1. **The env sanitization was defeated by the client** —
   `AcpSession.start()` built the child env as `dict(os.environ)`
   *overlaid* by the session env, so every key the pilot stripped
   (and the `DEVIN_MODEL` / refusal pops) was re-inherited from the
   host; `meta.env_sanitized` recorded names as stripped that still
   reached the agent. Now a caller-supplied env is **authoritative**
   (the child env is exactly it); the host env is the default only
   for a bare session (container mode's shim — whose agent env comes
   from the `docker exec -e` flags, not the shim process's own env).
   Regression: `test_session_env_authoritative_not_overlaid` (captures
   the real `Popen` env: a set `GITHUB_TOKEN` is absent from a
   filtered session's env and present in a bare session's; the
   refusal-fallback pop holds in every case).

**Medium**

1. **The report-failure state write clobbered the interrupt record** —
   the `report_failed` state hard-coded `interrupted: False` /
   `aborted: False`, overwriting the durable record written earlier;
   the follow-up `--resume` then read `interrupted_before=False`, item
   11 could never pass, and the pair rendered NO-GO despite AC3 being
   exercised. Now the flags are carried through. Regression:
   `test_report_failure_keeps_interrupted_state` (an interrupted run
   whose `rep.generate` fails → exit 4, state keeps `interrupted:
   True` + `final_state: report_failed`).
2. **The budget gate counted the deduped finals only** — a
   superseded attempt (an `eval_error`→rerun, a stale
   `preflight_failed` line) burned a session + wall the gate never
   counted (unlike WP5, which counts attempts). Now `sessions_used` /
   `wall_used_s` count **attempts** (every checkpoint line with an
   instance id). Regression: `test_budget_counts_superseded_attempts`
   (a superseded pair + two more → item 12 says "attempts, incl.
   reruns: 4", where the deduped view would say 3).
3. **`instance_id` was validated only as a non-empty string** — the
   raw id is joined into `runs/<iid>` / `devin-shim-<iid>` paths and
   interpolated into the shim's shell script, so `..` /
   metacharacters wrote outside the workdir or injected commands. Now
   a strict charset (`[A-Za-z0-9._-]+`) + an explicit `..` rejection
   (the WP4 `_charset_ok` / WP5 `_IID_CHARSET` analogue). Regression:
   `test_instance_id_charset_validation`.
4. **An oversize patch was a run-fatal with no checkpoint line** —
   every `--resume` re-ran the full session at quota before hitting
   the same deterministic cap, and the fatal aborted the whole queue
   (the run was unconvergeable). Now a `PilotPatchTooLarge` takes the
   `eval_error` instance route: the checkpoint line is written, the
   queue is not aborted, a resume may retry with a fresh session.
   Regression: `test_oversize_patch_is_eval_error_not_fatal` (cap
   monkeypatched to 1 byte: the run converges, the line is
   `eval_error` with the cap in `eval_skip_reason`).

**Minor**

1. **The printed verdict ignored `interrupted`** — the file rendered
   n/a while the print could read `go/no-go: GO` for a partial
   interrupted run. Now the print uses the same
   `_verdict(items, interrupted=…)` call.
2. **`"429"` matched as a bare substring** — a `1429` byte count or a
   `4290` port falsely produced `quota_limited` (and parked the batch
   in the midnight backoff). The numeric marker now matches as a
   whole number (`_quota_marker_hit`). Regression:
   `test_quota_429_word_boundary`.
3. **`--largest-bytes 0` was silently replaced by the default** (the
   `or` fallback); an explicit zero/negative cap (and the same for
   `--max-sessions-per-day` / `--max-wall-per-session`) is now a
   `PilotConfigError` (exit 2). Regression:
   `test_largest_bytes_zero_is_config_error`.
4. **The pinned `image` field was never consumed** — the name was
   re-derived in setup + the pre-flight. Now a `_image_for(inst)`
   helper (the pinned field when present, else the WP3b derivation)
   feeds the setup pull, the pre-flight image leg, and the prune
   `docker rmi`. Regression: `test_pinned_image_consumed`.
5. **`git add -N` had no `--` separator** — an agent-created
   leading-dash filename was parsed as an option → a run-killing
   `PilotError`. Now `git add -N -- <files>` (the `git reset --`
   below already guarded the same list). Regression:
   `test_add_n_separator_includes_leading_dash_file`.
6. **Only the first `git remote remove` was `&&`-chained to
   `cd /testbed`** — a cd failure ran the rest (incl. the `git
   remote` probe) in the container's default cwd. The whole
   ref-stripping sequence is now a `{ …; }` group under the cd (a cd
   failure skips it and the guard record fails loud). Regression:
   `test_container_guard_runs_in_testbed_group`.
7. **Exit-2 / exit-3 exits kept a stale verdict file** —
   `require_live` and the config errors returned before
   `_remove_stale_record` could run. Now `main()` resolves the
   workdir (exactly as `run_pilot` does) and removes the previous
   run's verdict before the first thing that can fail, so every
   early exit honors the "a stale verdict is never kept" invariant.
   Regression: `test_early_exits_remove_stale_record` (the
   `--largest-bytes 0` exit-2 path and the live-refused exit-3 path
   both remove a planted stale file).
