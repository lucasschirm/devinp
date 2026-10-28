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
