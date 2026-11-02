# WP6b Pilot B — live measurement record (issue #22)

The 25-instance stratified pilot (all 12 Verified repos, 2 each; the 3
largest capped at 3 — see `pilot_b_instances.json` + the
`scripts/pin_pilot_b_instances.py` selection rule), sandbox mode, the
Pilot-A checkpoint imported via `--import-run pilotA` (the
never-rerun statuses reused verbatim, the retry-class `eval_error`
re-run per pass@1). `PLAN-WP6B.md` is the plan of record.

**Status: IN PROGRESS** — the live run is delegated (the operator
session is bash-blocked from `devin`; a delegated session runs it, the
WP0 §3.4 pattern). The commands below are the calibrated plan; the
measured numbers are recorded here per window as the run completes.
The window model (the WP3 `Runner` — the harness' queue engine,
default sleeper): at the per-process daily cap (10 sessions in the
current UTC window) the run sleeps to the next UTC-midnight window,
resets the budget counters (a new window is a fresh budget), and
continues the remaining queue **in the same process** (the WP3
500-run model — one process spans days across windows). So a single
continuation invocation spans windows 2–3; the operator does not
re-invoke per window. If the process dies (a host crash — the
resume class), a fresh `--resume` invocation resumes from the
checkpoint (a fresh process is a fresh per-process budget — item 12's
per-window gate records the attempts per UTC day and flags an
overage). `quota_limited` (the retry class) is a mid-window session
refusal (the 429 marker), not the cap mechanism. The pilot's exit
codes are 0/2/3/4/42.

## Calibrated budget (the WP6a calibration — the 3-window plan)

- Pilot A measured 3 instances in 1 window (3/10 sessions, 4324 s of
  27000 s wall, ~15 s session wall each).
- 23 fresh instances (25 − 2 imported) ÷ 10 sessions/window =
  **3 windows** (10 + 10 + 3). The `max_sessions_per_day` budget is
  the operator's 10-session/day quota (PLAN §2.6, `--max-sessions` is
  not a flag — the runner window model).
- The per-window gate (checklist item 12, WP6b) renders the
  `sessions (attempts, incl. reruns): N/10 · wall Ss/27000s` per UTC
  window.

## Commands (the plan — verbatim form; wall clocks + exit codes per
window recorded below)

```bash
cd bench
# window 1 — the intentional interrupt after 8 sessions (exit 42, the
# host-crash class — resume-eligible per PLAN §2.5; the AC4 resume
# leg) + the import of the Pilot-A checkpoint. At the per-process
# daily cap the run sleeps to the next UTC-midnight window (budget
# reset — a new window is a fresh budget) and continues in the same
# process (the WP3 500-run model).
BENCH_LIVE=1 /tmp/benchvenv/bin/python live_pilot.py --pilot b \
  --mode sandbox --run-id pilotB --workdir ./pilot-b --resume \
  --import-run pilotA \
  --import-workdir /home/lucas/orca/workspaces/devinp/wp6a-pilot-a-3-real-instances-go-no-go-gate/pilot-a \
  --interrupt-after 8
# continuation — same workdir, no --import-* (the lines are already
# in the checkpoint; the seeding is idempotent, and the import
# re-derives from the state file so item 16 still gates). This ONE
# invocation spans windows 2–3 (the cap sleep is in-process); if the
# process dies, re-invoke with --resume (the resume class).
BENCH_LIVE=1 /tmp/benchvenv/bin/python live_pilot.py --pilot b \
  --mode sandbox --run-id pilotB --workdir ./pilot-b --resume
# (no per-window re-invocation — the runner sleeps to the next UTC-
# midnight window at the cap, resets the budget, and continues; the
# pilot's exit codes are 0/2/3/4/42)
```

## Measured numbers per instance (from `run-pilotB.jsonl`)

_recorded per window as the run completes; the final table is the
per-repo stratification (AC1) — wall / tokens / patch bytes per repo,
2 instances each (3 for the capped repos)._

## 2-run overlap/variance (issue #22 AC2 — Pilot A vs Pilot B)

The imported Pilot-A lines (`flask`, `seaborn` — never-rerun; their
Pilot-A finals are the import itself) vs the Pilot-B finals:
status / session wall / tokens / patch bytes. `psf__requests-1142`
was `eval_error` in Pilot A (the no-container skip, root cause `env`)
and is re-run in Pilot B per pass@1 — its Pilot-B final is the
fresh, superseding line (the retry class, not a re-run).

## Failure taxonomy (issue #22 AC3)

Every `eval_error` line is root-caused to `env` / `parser` / `patch`
(item 17 gates on it).

## Budget windows (issue #22 AC4)

The per-UTC-window arithmetic (item 12) + the interrupt/resume pair
(`--interrupt-after 8` → exit 42 → `--resume` continuation).

## Go/no-go checklist — final run output, verbatim

_recorded when the final window completes (items 1–12 + 14–17 gating;
item 13 deviation-tolerant, non-gating)._

## Quota warnings, vendor notifications, anomalies

_recorded per window._
