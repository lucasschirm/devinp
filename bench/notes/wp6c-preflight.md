# WP6c pre-flight (issue #44) — what is closed by code, what is an operator decision

Everything measurable was measured here; everything that is a **decision**
is listed as an open question at the bottom and is **not** answered by this
branch. The harness enforces the difference: `--pilot c` refuses to spend a
session while the decisions are unfilled (`live_pilot.
require_full_run_authorization`), so "pre-flight not closed" is a startup
error rather than a surprise 20 sessions into the run.

## What the code now enforces

| Blocker | Mechanism | Enforced where |
|---|---|---|
| B2 authorization | `bench/live_pilot_budget.json` must carry numeric caps **and** non-PENDING `authorized_by` / `completion_deadline` / `abort_rule` / `basis` / `calibrated_at` — a `PENDING` value is **not** an authorization | `live_pilot.require_full_run_authorization`, called in `run_pilot` **before** `require_live`, so the refusal happens before the env gate, before connecting and before any session |
| B3 model | `run_meta.json` records the exact `model` id and the models-list sha256; `--import-run` refuses a source whose recorded `mode` **or** `model` differs from this run's (`IMPORT_PROVENANCE`), and `--pilot c` also refuses a source that did not record **both** (a half-recorded methodology is still unattributable); the full-run checklist item 20 refuses a run whose meta fields are still `pending` | `live_pilot.run_pilot`, `source_provenance` + `IMPORT_PROVENANCE`, checklist item 20, `verify_submission.py` |
| B4 durability | `scripts/archive_run.py` snapshots a workdir into an append-only, content-addressed bundle and **verifies the copy**; `scripts/run_window.sh` runs it after every window when `WP6C_ARCHIVE_DEST` is set, and exits 5 if the verification fails | `archive_run.verify`, `run_window.sh` |
| B7 abort discipline | the abort rule itself must be written into the budget file before `--pilot c` will start; one invocation per window (`scripts/run_window.sh`, which supplies `--stop-at-window-cap` itself → exit 42 instead of sleeping in-process); the exit-4 contract: a failed window stops instead of looping | `FULL_RUN_BUDGET_FIELDS`, `run_window.sh`, `live_pilot.WindowCapReached` |
| E1 manifest | `bench/verified_instances.json` — 500 Verified instances, 12 repos, produced by `scripts/pin_verified_instances.py` (fail-closed on a short repo), schema validated against WP5's `validate_instances` | `pin_verified_instances.py`, `tests/test_pin_verified.py` |
| E2 full-run mode | `--pilot c` (manifest default, `full-run` workdir, `wp6c-full-run.md` record), checklist items 18–23 | `live_pilot.PILOTS`, `checklist_items` |
| E3 methodology + label | `report/run_meta.json` (written by the run, read by report/verify/package) and the `Devin (agent) on SWE-bench Verified` label | `live_pilot.SUBMISSION_LABEL`, `report.render_md(label=)`, `verify_submission.py` |
| E4 verification/packaging | `scripts/verify_submission.py` (exit 0/2/3), `scripts/package_submission.py` (verifies first, refuses a non-empty target unless `--force`) | those files, `tests/test_verify_submission.py` |
| E5 operations | `scripts/run_status.py` (progress, ETA, window discipline, disk vs limit, queue from WP3's own `resume_queue`), `scripts/run_window.sh`, per-window drill in `tests/test_run_ops.py` | those files |

## Environment facts measured for this branch (2026-08-07, the Orca dev workspace)

These are **this machine's** facts, recorded so nobody re-derives them; the
run host is a separate machine and must be measured the same way.

- `devin` on `PATH`: `devin 3000.12.43 (517d03d73926)` — the version issue
  #44 names. `live_pilot.build_plan` records `devin --version` into the
  plan and into `run_meta.json` (`devin_cli_version`), so the run's meta
  carries the version that actually produced it; the CLI reports no separate
  ACP-protocol version field.
- **No Docker daemon in this workspace** (`command -v docker` → nothing).
  Consequence: the WP4 evaluation leg cannot run here at all — which is why
  the new tests drive the pipeline through the mock and never claim the
  container mode was exercised. B3's "container mode on the current CLI"
  remains an operator measurement.
- Disk here: 78 GB free on the workspace mount (`df`, 2026-08-07). The
  floor is not a new knob: the pre-flight's `disk` leg (WP3, PLAN §7
  sizing) runs before every batch and every session, and
  `scripts/run_status.py` prints the workdir size plus the free space on
  its filesystem so an operator sees the trend across windows. What is
  still an operator decision is how much headroom the **run host** must
  keep (the clone cache plus 500 eval containers is the sizing input).
- Python here: 3.12.3, `swebench==5.0.2` (the pin).

## Open items the branch cannot close (operator)

1. **B1 the run itself** — confirm the run is still wanted after the 4-week
   pause, and re-confirm it against the leaderboard page.
2. **B2 quota + deadline + abort rule** — fill `bench/live_pilot_budget.json`
   (the committed template holds `PENDING`). Until then `--pilot c` exits 2.
3. **B3 model + mode** — decide cost-tier and mode; re-run the ACP handshake
   smoke on the run host (`tests/test_live_pilot.py::test_real_devin_smoke`,
   `BENCH_LIVE=1`, no quota) **and** one paid session (`BENCH_ALLOW_PAID_MODEL=1`,
   costs credits) before pinning `--model`/`--mode`. Record the answer in the
   budget file's `basis` and in the run's `--title`.
4. **B4 durable destination** — pick the off-host destination and set
   `WP6C_ARCHIVE_DEST` for every window; then `run_window.sh` archives and
   verifies per window, and `archive_run.py --verify --bundle …` proves it
   afterwards.
5. **B5 submission window + account** — re-measure the date from the live
   leaderboard page (the pinned `2026-09-06` may have moved), and confirm the
   leaderboard registration/alias (the label string is
   `Devin (agent) on SWE-bench Verified`, `live_pilot.SUBMISSION_LABEL`).
6. **B6 container mode + the ACP handshake** — same procedure as B3's second
   half, on the run host, with Docker present.
7. **B7 the windowing plan** — how many windows, at what cadence, and the
   abort rule. The tooling now matches the WP6c plan's shape (one invocation
   per UTC window, cap reached → exit 42, resume in the next window); the
   cadence and the abort rule are yours, and `abort_rule` is the field that
   records it.

## Zero-quota proof

`pytest bench/` (the CI command) exercises all of the above through
`mock_devin` and fabricated fixtures. Nothing on this branch opens a real
session: `BENCH_LIVE` is unset in CI and the `bench_live` marker excludes the
real-binary tests (`tests/test_live_gate.py`).
