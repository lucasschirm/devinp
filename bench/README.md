# bench/ — SWE-bench Verified × `devin acp` (recon → harness)

Work packages per `bench/PLAN.md`. **WP0 (recon, issue #10) complete** —
results in `notes/acp-surface.md` (all six ACs answered; open items §9 are
Pilot-A pre-flights). **WP1 (client core, issue #11) complete** —
`acp_client.py` + unit tests against the mock server (zero quota, CI).
**WP1b (issue #12) complete** — permission policy + refusal-fallback
isolation. **WP2 (mock test double, issue #13) complete** —
`mock_devin.py` (canned ACP agent + failure modes + `models list`
fixture) + integration tests driven through the real WP1 client
(zero quota, CI). **WP3a (issue #14) complete** — `runner.py`
(sequential queue, quota-budget state machine, overlap guard +
concurrency flag contract, abort on 2 consecutive `agent_error`)
+ unit tests against instrumented fake sessions (zero quota, CI).
**WP3b (issue #15) complete** — `runner.py` (checkpoint/resume with
pass@1-correct retry semantics: `JsonlCheckpoint` + `resume_queue`, a
torn EOF line drops not fatal, only `quota_limited`/`eval_error`/
host-crash/preflight states retry; the dynamic cost-tier model gate
(fail-closed — paid model refused, shape change refused, a paid
override needs an explicit config + a token budget); the zero-quota
pre-flight (auth → daemon → image → disk ≥ 2 × largest image, before
every batch and every instance → `preflight_failed`)) + unit tests
against fixtures + stub-failure legs (zero quota, CI).

**WP4 (issue #17) complete** — `evaluate.py` (the thin delegate to the
official `swebench.harness.run_evaluation`: standard predictions file,
fresh harness-created container — never the agent's mutated one,
returns `{resolved, logs}`, no bespoke apply/grade logic, typed error
hierarchy; positive/negative/corruption controls + CLI) + unit tests
(zero quota, CI; the container controls are CI integration tests on
public images — they skip where no Docker daemon exists).
**WP5 (issue #18) complete** — `report.py` (aggregation: pass@1 overall
+ per-repo table from the run JSONL with WP3b resume-dedup +
terminal-status exclusion; the failure taxonomy; the methodology block
— devin CLI version, model + `cost_tier`, prompt template hash,
permission policy, caps, harness version, images, date range, sessions
used; the leaderboard artifacts `all_preds.jsonl` +
`trajs/<instance_id>.jsonl`; the artifact cross-check) + unit tests
(fixtures → exact expected numbers, zero quota, CI).
**WP6a (issue #21) — Pilot A (the go/no-go gate)** — `live_pilot.py`
(the `BENCH_LIVE=1` smoke harness: the pinned 3-instance sequential
run, the intentional interrupt + `--resume`, the `max_sessions_per_day`
calibration, the 14-item go/no-go checklist) + `pilot_instances.json`
(the three smallest-repo Verified instances, pinned) + `PLAN-WP6A.md`
(two modes: `container` = the PLAN-conformant leg; `sandbox` = the
documented no-container fallback — the pilot's actual mode where no
container runtime exists) + zero-quota CI tests in
`tests/test_live_pilot.py` (the full pipeline driven through
`mock_devin`; the failure-mode mapping; interrupt/resume pass@1
semantics; the fail-closed model gate; the patch guards; the
calibration math). The live run + measured go/no-go record:
`notes/wp6a-go-no-go.md`.
**WP6b (issue #22) — Pilot B (the 25-instance stratified pilot)** — the
same harness extended: `--pilot b` (a 25-instance file across all 12
Verified repos, pinned + deterministic: `pilot_b_instances.json`,
`scripts/pin_pilot_b_instances.py`), `--import-run/--import-workdir`
(import the Pilot-A checkpoint lines — the never-rerun statuses reused
verbatim with their original timestamp, the retry-class status re-run
per pass@1), the per-UTC-window budget gate (item 12), and checklist
items 15–17 (stratification, the 2-run overlap/variance, the
root-caused `eval_error`s — the PLAN §2.5/§2.6 variance report + the
AC1–AC4 map) + zero-quota CI tests. `PLAN-WP6B.md` (the plan of
record; the import is the WP6a→WP6b reuse mechanism). The live run +
measured record: `notes/wp6b-pilot-b.md`.
**WP6c pre-flight (issue #44) — the 500-instance run (#23)** — the
harness extended to the full run, and the run made operable:
`--pilot c` (the pinned 500-instance manifest, its own workdir and record,
checklist items 18–23), `--stop-at-window-cap` + `scripts/run_window.sh`
(one invocation per quota window instead of one 10–50-day process),
`scripts/run_status.py` (read-only progress/ETA/disk dashboard for a run in
flight), `scripts/verify_submission.py` + `scripts/package_submission.py`
(the submission must be machine-verifiable and reproducible),
`scripts/archive_run.py` (per-window durable copy, verified), and
`bench/live_pilot_budget.json` (the budget + the B2 authorization the full
run refuses to start without). The blockers that are decisions, not code,
are listed unanswered in `notes/wp6c-preflight.md`.

## Layout

| Path | What |
|---|---|
| `PLAN.md` | Plan of record (architecture, WP0–WP6, policies) |
| `PLAN-WP0.md` | WP0 implementation plan (phases, AC map, contingencies) |
| `PLAN-WP4.md` | WP4 implementation plan (AC map, harness-fact record, contingencies) |
| `PLAN-WP5.md` | WP5 implementation plan (AC map, WP3-schema fact record, design decisions pinned by the tests, contingencies) |
| `PLAN-WP6A.md` | WP6a implementation plan (AC map, the two run modes, the gate ordering, the checklist, the deviation record) |
| `PLAN-WP6B.md` | WP6b implementation plan (the AC1–AC4 map, the `--import-run` checkpoint import + the never-rerun/retry-class reuse rule, the per-window budget, the checklist items 15–17) |
| `verified_instances.json` | The **WP6c** pinned 500-instance Verified manifest (issue #44 E1) — the exact instances the run submits, produced by `scripts/pin_verified_instances.py` (fail-closed: a split short of 500, a repo with too few rows, an id outside the split or a duplicate is an error, never a silent truncation; the schema is validated through the WP5 reader before commit, so the run cannot start on a file WP5 would reject) |
| `live_pilot_budget.json` | The **WP6c** budget + authorization file (issue #44 B2) — the numeric caps every pilot reads, plus the fields only an operator can fill (`authorized_by`, `completion_deadline`, `abort_rule`, `basis`, `calibrated_at`). Committed with `PENDING`: `--pilot c` refuses to spend a session while any of them says so, and a `PENDING` cap is a config error for any pilot |
| `live_pilot.py` | **WP6a+WP6b** — the `BENCH_LIVE=1` live-pilot harness: the pinned sequential run through WP1–WP5 (pre-flight → fail-closed model gate → sessions → patch extraction → checkpoint → pruning → report), the intentional interrupt (`--interrupt-after`, exit 42, host-crash class) + `--resume` via `resume_queue`, the `max_sessions_per_day` calibration, the go/no-go checklist (WP6a: items 1–14; WP6b: items 15–17 + the per-UTC-window item 12; **WP6c**: `--pilot c` for the 500-instance run + items 18–23 + the B2 authorization gate + `--stop-at-window-cap`; exit codes 0/2/3/4/42), the WP6b `--import-run/--import-workdir` checkpoint import (the never-rerun statuses reused verbatim, the retry-class re-run, the referenced artifacts copied so the WP5 cross-check holds) |
| `pilot_instances.json` | The three pinned Pilot-A instances (one from each of the smallest Verified repos: pallets/flask, mwaskom/seaborn, psf/requests — verbatim SWE-bench Verified fields incl. `test_patch` for the host check) |
| `pilot_b_instances.json` | The 25 pinned Pilot-B instances — stratified across all 12 Verified repos (1 each; the remaining 13 seats dealt round-robin in descending repo size, capped at each repo's row count in the split — in the pinned set: django 3, sympy 3, nine repos 2, flask 1 — its single split row) — verbatim Verified fields incl. `test_patch` + a `repo` key per instance + the `stratified_repos` meta (checklist item 15); produced by `scripts/pin_pilot_b_instances.py` (deterministic — the selection rule in its header; `_meta` records the source, the fetch date, the per-repo selection, and the per-repo split row counts) |
| `notes/acp-surface.md` | **WP0 notes** — the recon record answering every "?" |
| `notes/models-list.json` | Raw `devin models list --format json` (2026-10-26) |
| `notes/auth-status.txt` | `devin auth status` output (quota evidence) |
| `notes/wp6c-preflight.md` | **WP6c** — what the code now enforces for the 500-instance run, the environment facts measured here, and the blockers that remain operator decisions (B1 quota, B2 authorization, B3/B6 model + mode, B4 destination, B5 deadline + account, B7 cadence) |
| `notes/*` | Additional recon evidence (version, image registry scans…) |
| `probes/acp_probe.py` | NDJSON JSON-RPC probe for `devin acp` (4 modes) |
| `scripts/verify_submission.py` | **WP6c** (issue #44 E4) — prove a finished run is submittable: re-verifies the artifacts with WP5's own `build_report` (exit 3 on an integrity failure), then the parts only the submission needs — the persisted `run_meta.json` (no `pending` methodology field, the grading leg stated honestly), the checkpoint reads clean (`CHECKPOINT`: a line the WP5 loader dropped is an **unknown outcome** it excludes from the denominator — the silent-shrink case a submission cannot carry), queue completeness against the pinned manifest with the retry-class gaps listed separately, the pass@1 the report renders recomputed from the checkpoint, and the declared submission label — required in the report's **header** (`report.label_in_header`), not as a substring somewhere in the file; `--bundle` also proves the `runs/<run_id>/<instance_id>/` layout. Exit 0 verified · 2 bad input · 3 integrity |
| `scripts/package_submission.py` | **WP6c** (issue #44 E4) — package what `verify_submission.py` verified: `runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,eval.log}` + `all_preds.jsonl` + `report.md` + `run_meta.json` + the checkpoint + `SHA256SUMS`, and a README written for the reviewer (the label, the methodology, the deviations, the reproduction command). Verifies first and refuses a non-empty target unless `--force`; `--allow-incomplete` stamps an interim bundle as not-leaderboard-submittable instead of pretending |
| `scripts/run_status.py` | **WP6c** (issue #44 E5) — the read-only dashboard for a run in flight: instances decided / 500, the queue still open (derived through WP3's `resume_queue`, never a second implementation), pass@1 so far, sessions per UTC day against the cap, the ETA from the measured median wall, disk free, and the state the previous window left (window cap / interrupt / abort). The cap comes from the operator's `max_sessions_per_day`: an unconfigured cap falls back quietly, a *broken* one (`PENDING`, `0`, negative, `NaN`, an unreadable budget file) prints a `NOTE` naming both the value it used and the file/flag to fix — a silent 10 would quietly mis-date a 500-instance run. Exit 0 = readable, 2 = unreadable input |
| `scripts/run_window.sh` | **WP6c** (issue #44 E5 + B7) — ONE operator window: runs the harness exactly once, tees to `<workdir>/logs/window-<UTC>.log`, prints how to read the exit code, and exits with the code it got (`42` = window cap, resume next window; `4` = STOP, do not loop-retry). The resume/status hints it prints are the command it actually ran — one command per line, every argument quoted the way the shell needs — because their whole purpose is to be pasted. `WP6C_KEEP_RUNNING` is read as a flag (`0`/`false`/`no`/`off`, any case, keep the in-process sleep instead). Never retries, never backgrounds, never edits the checkpoint. `WP6C_ARCHIVE_DEST` also archives + verifies the window's artifacts (exit 5 if that fails) |
| `scripts/archive_run.py` | **WP6c** (issue #44 B4) — make a window's artifacts durable: an append-only, content-addressed bundle under `<dest>/<run_id>/windows/<UTC>/` with a `MANIFEST.tsv`, the copy verified byte-for-byte before success is reported, and `--verify --bundle …` to re-prove any past window. The answer to the Pilot B artifact loss (issue #22) |
| `acp_client.py` | **WP1** — NDJSON JSON-RPC 2.0 client core (pure stdlib) |
| `mock_devin.py` | **WP2** — `mock_devin` test double: a fake `devin` binary (canned ACP agent, modes `ok`/`no_patch`/`hang`/`quota`/`crash`, canned `models list` fixture) |
| `runner.py` | **WP3a + WP3b** — sequential queue (concurrency 1 by default), quota-budget state machine (`running → budget_exhausted → sleeping → resume`), overlap guard + concurrency flag contract, abort on 2 consecutive `agent_error`; checkpoint/resume with pass@1-correct retry semantics (`JsonlCheckpoint`, `resume_queue`), the dynamic cost-tier model gate (fail-closed, `gate_cost_tier`/`parse_models_list`/`fetch_models_list`), the zero-quota pre-flight (`Preflight`, before every batch + every instance) (pure stdlib) |
| `evaluate.py` | **WP4** — the SWE-bench evaluation leg: a thin delegate to `python -m swebench.harness.run_evaluation` (standard predictions file, fresh harness-created container, `{resolved, logs}` return contract, typed error hierarchy `InvalidInputError`/`PatchCorruptionError`/`EvaluationTimeoutError`/`HarnessFailedError`; never imports `swebench` — the subprocess is the only harness interaction; pure stdlib + subprocess) |
| `report.py` | **WP5** — the report leg: pass@1 overall + per-repo table (WP3b resume-dedup; the retry class excluded from the denominator — the pass@1-correct semantics), the failure taxonomy, the methodology block, the leaderboard artifacts (`all_preds.jsonl` + `trajs/`), the artifact cross-check, and `parse_report_pass1` (the one parser for the pass@1 a rendered report claims — `live_pilot.py` item 21 and `scripts/verify_submission.py` both read it, so the claim and the check cannot drift apart) (typed errors `InvalidInputError`/`ReportProtocolError`/`ArtifactCheckError`; reads the run JSONL by import from `runner.load_checkpoint` — one source of truth for the WP3 schema; pure stdlib + `runner`) |
| `tests/` | **WP1** — unit tests + in-process mock `devin` server; **WP2** — `mock_devin` integration tests (driven through the real WP1 client); **WP3a** — `runner` unit tests (instrumented fake sessions); **WP3b** — resume fixtures (incl. EOF corruption + retry-class rules), cost-tier gate fixtures (paid model, shape change, paid override), pre-flight stub-failure tests; **WP4** — `evaluate` unit tests (injected harness runner + fabricated harness trees, zero quota) + CI integration tests (positive control on the public flask image, corruption guard, isolation; skip without a Docker daemon); **WP5** — `report` unit tests (fixture JSONL → exact expected numbers: resume-dedup, terminal-status exclusion, artifact cross-checks, trajs byte-for-byte, methodology, determinism, CLI exit codes; zero quota); **WP6c (issue #44)** — `test_pilot_c.py` (the `--pilot c` kind, the B2 authorization gate incl. `PENDING`-not-authorization, items 18–23, `report/run_meta.json`, the submission label, the mode-gated import), `test_verify_submission.py` (verify/package: every failure class, a package round-trip through the verifier, deterministic output), `test_run_ops.py` (the window wrapper's exit-code contract, a real two-window resume drill, the status math, the durable archive + its verification failures), `test_pin_verified.py` (the pinned 500-instance manifest); **WP6a** — `test_live_pilot.py` (the full pilot pipeline through `mock_devin`, zero quota; the failure-mode mapping; interrupt + resume pass@1 semantics; the pre-flight batch marker; the paid-model gate refusal; the patch guards incl. pre-staged junk + collapsed-dir expansion; the host-check verdicts; the calibration/checklist math; one `BENCH_LIVE`-gated real-binary handshake smoke; **WP6b** — the `--import-run` import (the never-rerun reuse: the original timestamp preserved, no session spent; the retry-class re-run; the idempotent seeding; the referenced-artifact copy; the fail-closed source validation), the checklist items 15 (stratification), 16 (the 2-run overlap/variance), 17 (the root-caused `eval_error`), the per-UTC-window item 12 + the root-cause taxonomy (env/parser/patch)); **CI (issue #20)** — the `BENCH_LIVE` gate (real-Devin tests excluded by default; `BENCH_LIVE=1` opts in; the static guard fails the suite on an unmarked real-Devin test; zero quota) |
| `conftest.py` | pytest path shim for the flat layout + the `BENCH_LIVE` gate (`@pytest.mark.bench_live` — real-Devin tests are excluded by default; CI never consumes quota, PLAN §6) |
| `pyproject.toml` / `requirements.lock` | Python ≥3.11; `swebench==5.0.2` pinned; `pytest-timeout` (the integration tests' watchdog) |

## The probe

```bash
cd bench
python3 probes/acp_probe.py selftest          # zero-cost, mock ACP server
python3 probes/acp_probe.py passive  --model swe-2-high --seconds 30
python3 probes/acp_probe.py handshake --model swe-2-high --cwd /tmp/task
python3 probes/acp_probe.py framing   --model swe-2-high   # 2 spawns, optional
```

- `handshake` includes a **framing canary** (LSP `Content-Length` prefix line
  before `initialize`) so framing + full lifecycle evidence come from **one**
  ACP session. **Run 2 live result:** trivial task completed, `stop_reason:
  end_turn`, real usage fields captured, 4.3 s prompt / 19.8 s total.
- Reports: JSON (`--out`) + raw frame JSONL (`--frames-out`); stderr tail
  persisted in the report's `timing.stderr_tail`.
- Hygiene: own process group (group-killed on all paths),
  `DEVIN_REFUSAL_FALLBACK` unset, optional isolated XDG + `--config`.
- **Known real-server behaviors (WP0 findings):** untrusted cwd kills the
  prompt turn (EOF, no stop reason); `session/close` handler-errors and the
  process survives close → always group-kill after close; non-JSON lines
  (e.g. LSP headers) yield JSON-RPC `-32700` parse errors, not crashes.

## The client (WP1)

`acp_client.py` — the NDJSON JSON-RPC 2.0 client core (PLAN.md §5 WP1):

```bash
cd bench
python -m pytest tests/          # zero-cost, mock server only, no quota
```

- **Wire contract (WP0-verified):** NDJSON framing (not LSP `Content-Length`),
  wire names `initialize` → `session/new` (cwd=/testbed) → `session/prompt`
  → streamed `session/update` → `session/cancel` → `session/close` *before*
  process exit. Vendor notifications (`_cognition.ai/*`) are logged and
  ignored — never an error, never blocking.
- **Hygiene:** own process group; `session/close` best-effort then
  SIGTERM→SIGKILL group kill on **all** exit paths (WP0: the real server
  survives `session/close`); `DEVIN_REFUSAL_FALLBACK` always unset; refusal
  stop-reason → terminal `AcpRefusalError` (no model switch).
- **Typed errors, never hangs:** every read is deadline-bounded;
  malformed/short reads → `AcpMalformedFrameError`, EOF → `AcpTransportError`,
  stall → `session/cancel` + group kill + `AcpTimeoutError`.
- **Permission policy** (`SwebenchPolicy`, PLAN §3.7): allow read / edit / exec
  inside `/testbed` + `/tmp`; deny web tools and out-of-scope writes
  (default deny).
- Stop-reason + `usage.{inputTokens,…}` captured verbatim (WP0 §1).

## The mock (WP2, issue #13)

`mock_devin.py` — a single-file, pure-stdlib script that masquerades as
the `devin` binary: the **key testability lever** (full pipeline with
zero quota, PLAN.md §5 WP2). It speaks the WP0-verified ACP subset
(NDJSON + wire names + startup vendor notifications + real-wire
toolCall shape; the `initialize`/`session/new` results mirror the WP0
live capture — capability blocks, 5 modes incl. `plan`/`bypass`,
`configOptions`) and runs a canned agent that reads the task, applies a
canned patch to the session-cwd repo (via `git apply` when the cwd is a
git repo), answers its *own* permission flow (the patch is applied only
after the client's `session/request_permission` response — a denial skips
it), and emits the stop reason + usage. Multi-turn: a second
`session/prompt` is answered like the first. It also serves the canned
`devin models list --format json` fixture (WP0 shape; Free is exactly
`swe-2-medium`/`swe-2-high`/`swe-2-max`; paid `swe-1-6`/`swe-1-7`/
`swe-2-*-lightning`; the non-SWE families `claude-opus-4.6` and
`gpt-6-sol` are verbatim copies from the WP0 capture; cost tiers cover
Free/Low/Med/High; the `adaptive` variant has no `cost_tier` — the WP3
fail-closed gate data) and a canned `devin auth status` (the PLAN §2.7
pre-flight probe).

```bash
cd bench
python3 mock_devin.py models list --format json
python3 mock_devin.py auth status
MOCK_DEVIN_MODE=ok MOCK_DEVIN_STATE=/tmp/ev.jsonl \
  python3 mock_devin.py acp --model swe-2-high
python -m pytest tests/          # zero-cost, mock only, no quota
```

- **Modes + terminal signal contract** (what the WP3 runner maps to the
  JSONL status — the JSONL itself, resume dedup and artifact atomicity
  are WP3, issue #15): `ok` → `end_turn` + usage + canned patch applied;
  `no_patch` → `end_turn` + usage, no edits (terminal for pass@1);
  `hang` → stall → client cap → cancel + group kill (`timeout`);
  `quota` → stop reason `quota_limited` + stderr note (no usage;
  retry class); `crash` → mid-prompt death, exit 1 (`agent_error`).
- **Strict CLI:** unknown flags/modes/subcommands are rejected (exit 2)
  so an adapter/runner mistake surfaces instead of being absorbed; with
  no `--model`, `DEVIN_MODEL` is honored (as in the real `devin acp`).
- **Canned-patch failure is an event, not a crash:** if both the
  `git apply` and the direct write fail (e.g. an unwritable session
  cwd), the mock records `patch_failed` and the turn still completes.
- **Event file** (`MOCK_DEVIN_STATE` / `--state-file`): JSON-line log
  (task text as received, cwd, every permission response, cancel/close)
  — the tests assert the round-trip and the permission-flow ordering.
- **WP0 wire conformance:** `-32601` for unknown methods, `-32700`
  parse errors for non-JSON lines — the server keeps serving, never
  crashes (the WP0 negative-control behavior).

## The runner (WP3a + WP3b, issues #14 + #15)

`runner.py` — the sequential queue, quota-budget state machine and
overlap guard (PLAN.md §2.1–2.3, §5 WP3; pruning is WP3c, issue #16):

```bash
cd bench
python3 runner.py                        # validates the flag contract (exit 0)
python3 runner.py --concurrency 3        # exit 2 — refused by default
python3 runner.py --concurrency 3 --i-know-about-quota
python3 runner.py --model swe-2-high --models-json notes/models-list.json
python -m pytest tests/test_runner.py    # zero-cost, fake sessions + fixtures
```

- **Sequential by default** (concurrency 1): a FIFO of instance ids is
  executed one at a time; the `OverlapGuard` asserts no two agent
  sessions ever overlap (fail-loud `OverlapViolation`, not a silent
  wait), and an opt-in `ProcessLock` (`flock`) extends the guarantee to
  sibling runner processes (PLAN §2.1 "process-level lock + assert").
- **Flag contract:** `--concurrency N>1` is refused (typed error; CLI
  exit 2) unless `--i-know-about-quota`; a granted N>1 runs N worker
  threads with guard cap N (the user's quota risk).
- **Backoff state machine** (pure, table-driven —
  `running → budget_exhausted → sleeping (until window) → resume →
  running`, terminal `aborted`/`done`, every other (state, event) pair
  a typed `InvalidTransition`): both the local daily budget cap
  (`max_sessions_per_day`, `max_wall_per_session` × cap) and an external
  rate-limit signal (a session's `quota_limited` terminal status,
  PLAN §2.3) converge on `budget_exhausted`.
- **Rate-limit → stop the batch:** the instance is marked
  `quota_limited`, no further instance starts, the checkpoint hook
  fires (exactly once per backoff — a double quota stop under a
  granted N>1 is one stop; the checkpoint carries the stop reason,
  `rate_limited` or `budget_exhausted`), the run sleeps to the next
  quota reset window (next UTC midnight — WP0 §4 "calendar basis"
  reset; the sleep's *duration* is the remainder of the current
  window — the nominal 12 h `daily_pause` is the default config, not a
  guarantee of the realized pause), resumes with the budget reset, and
  the `quota_limited` instance is **not** re-queued in this run (its
  re-run is the WP3b checkpoint-resume retry class, PLAN §2.5).
- **Abort on 2 consecutive `agent_error`** (environment-drift
  suspicion; "consecutive" = completion order, reset by any other
  terminal status **or a window reset** — a new day is a fresh
  suspicion, a deliberate, documented deviation from AC5's literal
  reset rule, pinned by
  `test_window_reset_breaks_agent_error_chain`).
- **Session contract** (the adapter seam): `factory(id) ->
  session.start() -> session.run() -> status` (a terminal status from
  the WP5 taxonomy — unknown statuses fail loud) `-> session.close()`;
  a factory/`start()` failure releases the budget claim, a
  `run()`/`close()` failure stands (partial session — PLAN §2.5
  host-crash class, the exception propagates); the `run_finished` hook
  fires on normal completion and is *not* reported on a propagated
  failure.

WP3b (issue #15, PLAN §2.4–2.7):

- **Checkpoint / resume with pass@1-correct retry semantics** (PLAN
  §2.5): `JsonlCheckpoint` is the `run-<id>.jsonl` progress file —
  one fsync'd line per finished instance (status, artifact paths /
  usage in `meta`, timestamp); a killed mid-write run leaves a torn
  last line, which `load_checkpoint` **drops, not fatal** (a corrupted
  mid-file line fails loud) and which `JsonlCheckpoint`'s open
  **repairs on the write side** (`_repair_checkpoint_file`): the
  **last line** — whether or not newline-terminated (the reader's
  contract blesses a non-record *last* line with a trailing newline as
  a droppable torn tail too) — if a complete record missing only its
  trailing newline is *completed* (newline written), otherwise
  *truncated* via `ftruncate` (atomic w.r.t. the preserved prefix — a
  kill mid-repair cannot destroy fsync'd records; the boundary is
  computed **on the raw bytes** — `rfind(b"\n")` on the binary read —
  so the truncation point is byte-exact regardless of what a decoder
  would replace or how many bytes a character occupies; both reader
  and repair decode each line with a **strict** UTF-8 decode — a
  non-last line that fails to decode is a typed
  `RunnerProtocolError` (a replacement decode would mask an invalid
  byte inside a string field as a U+FFFD character, a valid-but-
  unknown record the resume logic treats as stale → a silent re-run
  of a finished instance), while a torn *last* line with an invalid
  byte (a kill can tear a multibyte sequence at EOF) is a droppable
  tail) — so a resumed run never glues its first record onto the
  tail (mid-file corruption on the next load, or a silently dropped
  *completed* record — a re-run of a finished instance, a pass@1
  violation). `resume_queue` (pure)
  splits the full instance list into `to_run` / `skipped`:
  `resolved` / `failed_test` / `timeout` / `no_patch` (and
  `agent_error` — a terminal agent outcome) are **never re-run**;
  `quota_limited` / `eval_error` / `preflight_failed` and a
  host-crash partial state (a missing line) **retry — the only retry
  class**; a duplicated line resolves to the last line, so no instance
  runs twice.
- **Startup cost-tier model gate** (PLAN §2.4, fail-closed):
  `gate_cost_tier` / `parse_models_list` allow only `cost_tier ==
  "Free"` variants of `devin models list --format json` (the allowlist
  is dynamic — recomputed every startup); a paid (or unverified /
  absent-tier) model is refused, a model missing from the list is
  refused, a JSON shape change is a typed `ModelGateError` refusal;
  a paid override requires **both** an explicit `allow_paid` config
  **and** a positive (finite) token budget (CLI: `--model`,
  `--allow-paid`, `--token-budget`, `--models-json` / `--devin-bin`;
  a refusal exits 2 before the batch starts; the gate flags without
  `--model` exit 2 too — the gate is a no-op there, and a silent
  no-op that "validated" nothing is a footgun; `--models-json` and
  `--devin-bin` are mutually exclusive (the gate's input is the file
  OR the live binary — a silently inert `--devin-bin` is the same
  footgun class). The Runner's default clock is **wall time**
  (`time.time`): the backoff target is the next *epoch* UTC midnight
  (WP0 §4 "calendar basis"), so a monotonic (boot-relative) clock
  would align the sleep ~24 h off the real quota reset (the WP3a
  default, review finding — WP3b's per-window pre-flight makes the
  phase alignment load-bearing); wall-clock step-backs (NTP / VM
  snapshots) clamp to 0.0 at the measurement sites — a negative wall
  would be subtracted from the budget's `wall_used_s`. The gate's
  live fetch and the pre-flight's `devin`/Docker commands are
  fail-closed end to end: a malformed `proc` (missing / non-
  sliceable `stdout` / `stderr` / `returncode`) is a typed refusal,
  not a leaked `AttributeError`/`TypeError`.
- **Zero-quota pre-flight** (PLAN §2.7): `Preflight` runs the ordered,
  short-circuiting legs — `devin auth status` ok → Docker daemon
  responsive (bounded — a hung daemon is a failure, not a wait) →
  instance image present (per instance — the shipped default
  `image_for` matches the WP0 pin
  `swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:latest`, with
  the `swebench/` namespace and the Docker-Hub escape of `__`;
  without them `docker image inspect` never matches the pulled
  image and the leg fails on every instance; the seam stays
  injectable) → free disk ≥ **2 × the
  largest expected image** — before **every batch** (a new quota
  window is a new batch) and **before each instance**; a failure
  raises `PreflightFailed` naming the leg, the runner marks the
  instance `preflight_failed` (the reason lands in the JSONL meta;
  a batch-phase failure records an `instance_id: null` marker line),
  releases the budget claim (no session was ever started), and aborts
  the run — the operator fixes the infra and resumes, where the
  instance is retry-eligible.

## The evaluator (WP4, issue #17)

`evaluate.py` — the SWE-bench evaluation leg (PLAN.md §5 WP4): a **thin
delegate** to the official harness — the whole design is "delegate, then
read what the harness wrote" (no bespoke apply/grade logic, never
imports `swebench`):

```bash
cd bench
python3 evaluate.py --instance-id pallets__flask-5014 --patch-file p.diff   # exit 0 = ran (resolved or not); JSON on stdout
python -m pytest tests/test_evaluate.py   # unit: zero quota, no Docker (injected harness runner + fabricated harness trees)
# the positive/corruption/isolation controls are CI integration tests (public images, no quota; skip without a Docker daemon)
```

Exit codes: 0 = ran (resolved *or* not — the JSON on stdout carries the
outcome), 2 `InvalidInputError`, 3 `PatchCorruptionError`,
4 `EvaluationTimeoutError`, 5 other harness/infra failure (the documented
contract — never a traceback with exit 1).

- **Delegation (AC1):** one call = one instance; writes the standard
  predictions file (exactly `instance_id` / `model_name_or_path` /
  `model_patch`) and spawns exactly the official CLI — `python -m
  swebench.harness.run_evaluation -d SWE-bench/SWE-bench_Verified -s test
  -p … -i … -id … --max_workers 1 -t … --report_dir …` (WP0-verified),
  with `cwd = workdir` (the per-instance artifact dir — the harness's log
  root is CWD-relative, and the pin's `make_run_report` writes the final
  run report to `<report_dir>/<model>.<run_id>.json`, so every artifact
  lands under one directory). The argv is pinned by a unit test; the
  module's no-`swebench`-import contract is pinned by an AST test.
  `overall_timeout` is the outer watchdog — bounded by default (the
  project's hang policy: `-t` bounds only the *in-container* test exec;
  the HF dataset download, the image pull and the Docker daemon are not
  bound by `-t`, and a wedged one must not hang the sequential benchmark
  — with `None`, the default, the watchdog is `timeout +
  DEFAULT_OVERALL_TIMEOUT_MARGIN_S` s; pass an
  explicit value to override); pre-spawn filesystem work, a spawn failure
  and the watchdog firing (its typed error surfaces the
  possibly-still-running eval container name + the partial output, and
  re-reads the log paths *after* the kill so it carries the artifacts the
  harness already wrote — not the pre-spawn snapshot) are typed
  `HarnessFailedError`, never a raw escape (a `Path.resolve()`
  symlink-loop `RuntimeError` on the workdir and the `OverflowError` a
  huge-int `timeout`/`overall_timeout` would raise at spawn included).
  Typed errors an injected runner raises *stay typed*. Every typed error
  raised after the workdir is resolved carries `error.workdir` (with
  `workdir=None` that is the auto-created tempdir), so the caller-owned
  cleanup stays reachable on failure paths — not just via `logs.workdir`
  on success.
- **Fresh container (AC2):** the harness creates the eval container itself
  (`containers.create` on the published eval image, ghost-removing any
  same-name container first); the per-call unique `run_id` default makes
  the eval container name `sweb.eval.<iid>.<run_id>` structurally distinct
  from the agent's `sweb.eval.<iid>` — and there is no parameter for the
  agent's (contaminated) container at all. The container id comes from the
  harness's own log line → `logs.container` (the AC4 isolation assertion:
  eval container id ≠ agent container id).
- **`{resolved, logs}` (AC3, typed — `EvaluationResult` / `EvaluationLogs`
  `TypedDict`):** `resolved` is only ever the harness's `report.json`
  value — **and it must be a bool** (a corrupted-but-parseable verdict is
  a typed failure, never a silent `bool()` coercion) — or the harness's
  own empty-patch semantics (below); a `resolved: false` report with the
  harness's own `infra_failure` flag (unparseable test output from an
  environment fault — the harness counts these separately,
  `infra_failure_ids`) is a typed `HarnessFailedError` (the WP3 retry
  class), never a terminal verdict; `logs` carries the harness artifact
  paths (`instance`, `test_output`, `final_report`), the verbatim
  per-instance `report`, `container`, and the run identity (`workdir` — a
  fresh tempdir when the caller passed None, surfaced in `logs.workdir`
  (success) and `error.workdir` (typed failures) so the caller-owned
  cleanup is never unreachable — `run_id`, `predictions`). A non-zero
  harness exit is always an error, *even when a report exists* (the
  harness writes the per-instance report before the final-report leg).
- **Stale-report protection:** the harness's own `exclude_completed` treats
  a pre-existing per-run `report.json` as "already run" and silently skips
  the instance (and `run_instance` itself early-returns a pre-existing
  report) — `evaluate` never trusts a pre-existing report: it unlinks one
  before the spawn (on an unlink failure the pre-run mtime is recorded and
  the post-run report must be strictly newer — a missing pre-run mtime
  makes freshness unverifiable → reject), so a re-used (workdir, run_id)
  pair can never return the previous run's verdict for a patch that was
  never evaluated. The same re-use risk for the *terminal-class triage
  markers*: a pre-existing `run_instance.log` / `test_output.txt` whose
  mtime this run did not update is stale — its markers are not this run's
  verdict, and triage downgrades them to the retryable class.
- **Empty patches (the negative control):** the harness itself filters
  empty/`None` `model_patch` (skipped; counted in `empty_patch_ids`, never
  in `resolved_ids`); `evaluate` mirrors that exactly by short-circuiting
  to `resolved: False` with `empty_patch: True` — no harness spawn, no
  dataset download, no Docker, no tempdir.
- **Corruption guard (AC4):** the harness's own 4-command git-apply chain is
  the *sole authority* on applicability, so no structural pre-check is made
  (the chain applies patch forms without `---` / `+++` file headers —
  rename-only / mode-only `diff --git` hunks, `GIT binary patch`
  sections, tab-separated `diff -u` headers, context/normal diffs — which a
  header check would wrongly reject). Every non-empty patch reaches the
  harness; the chain's `APPLY_PATCH_FAIL` marker in `run_instance.log` →
  `PatchCorruptionError` (wins over a timeout marker), the
  tail-anchored `Timeout error: <n> seconds exceeded.` marker in
  `test_output.txt` (the harness's final write — the number is *this run's
  `-t` value*, `run_evaluation.py:376`), `>>>>> Tests Timed Out` in
  `test_output.txt` anchored as a *whole line* (the dataset eval-script's
  marker, graded *inside* the test output — `test_output.txt` also
  captures the patched code's stdout), or the
  harness's own `Test timed out after <n> seconds.` message in
  `run_instance.log` — the number is *this run's `-t` value*
  (`run_evaluation.py:379`) — checked on the log with the harness's
  patch-diff echo regions (`Git diff before:` / `Git diff after:`) masked,
  because the harness reflects the patch text into the instance log in
  exactly those two places (and the container-id extraction runs on the
  same masked log, so a forged `Container for <iid> created:` line inside
  an echo cannot displace the harness's real one) → `EvaluationTimeoutError`
  (terminal in WP3 — hence the loose markers are anchored and file-specific,
  so a docker-client flake cannot zero out a retryable instance),
  anything else →
  `HarnessFailedError` (the WP3 `eval_error` retry class). A clean typed
  error, never a crash.

## The report (WP5, issue #18)

`report.py` — the aggregation, the failure taxonomy, the methodology
block, and the leaderboard artifacts (PLAN.md §5 WP5; it reads the run
JSONL **by import** from `runner.load_checkpoint` — the WP3b torn-EOF
rule — and the pass@1-correct split from the runner's
`TERMINAL_STATUSES` / `RETRY_STATUSES`):

```bash
cd bench
python3 report.py --jsonl run-001.jsonl --out out/ [--meta-file meta.json]
python3 report.py --jsonl run-001.jsonl --out out/ --check
python -m pytest tests/test_report.py   # fixtures → exact numbers, zero quota
```

Exit codes: 0 = OK (generated / checked clean), 2 `InvalidInputError`
(bad flag / unreadable / non-JSON meta file), 3 `ArtifactCheckError`
(the cross-check failed — the report is *not* trusted), 4
`ReportProtocolError` (a run-JSONL schema violation — a corrupted
mid-file line fails loud; a torn **last** line drops, not fatal, and
is noted in the report).

- **pass@1 = resolved / terminal** (AC1), where *terminal* = the
  legitimate agent outcomes = `TERMINAL_STATUSES − RETRY_STATUSES`
  = `resolved` / `failed_test` / `timeout` / `no_patch` /
  `agent_error` (PLAN §2.5: `timeout` / `no_patch` are legitimate agent
  outcomes — re-running them "would turn pass@1 into a filtered
  pass@k"; `agent_error` is a terminal agent outcome). The retry class
  (`quota_limited` / `eval_error` / `preflight_failed`) is **excluded**
  from the denominator (the outcome is undecided — pending retry) but
  counted in the taxonomy. Dedup is the WP3b resume rule — a
  duplicated line resolves to the **last** line (file order, not
  timestamp). A `instance_id: null` line is a **batch marker** (a
  pre-flight failure before the first instance) — counted in the
  taxonomy section, never as an instance record. The per-repo table
  groups by the SWE-bench Verified instance-id prefix (the repo key is
  the prefix before the **last** `-` — correct for dash-bearing repo
  names like `scikit-learn`); a repo with zero terminal instances
  renders `n/a`.
- **The per-instance `meta` contract** (defined by WP5; the adapter WP
  conforms to it): `artifacts_dir` (optional base for relative paths —
  default: the run JSONL's directory) + the references `patch_diff` /
  `trajectory` / `eval_log` (null = no such artifact; a non-null
  reference **must** resolve to an existing file) + an opaque `usage`
  (carried into the report, not interpreted — WP0 §2.6 proxies or ACP
  `usage.{inputTokens,…}`).
- **The run-level methodology meta** is a separate operator file
  (`--meta-file` — the WP3 checkpoint writes instance lines only):
  `run_id` / `devin_cli_version` / `model` / `cost_tier` /
  `prompt_template_hash` / `permission_policy` / `caps` /
  `harness_version` / `images` / `model_name_or_path`?. Two fields are
  **computed from the JSONL** (exact): the **date range** (min/max
  `ts`, ISO-8601 UTC) and **sessions used** = *session attempts* —
  lines with `instance_id ≠ null` and `status ≠ preflight_failed` (a
  pre-flight failure stops *before* a session starts, PLAN §2.7; a
  `quota_limited` line is a started-then-refused attempt; a resume
  re-run counts a second attempt). A missing file / field renders
  `unknown` — the block never silently omits a field.
- **Leaderboard artifacts** (AC4): `all_preds.jsonl` — one line **per
  deduped instance** (all 8 final statuses, incl. pending retry-class),
  **sorted by `instance_id`**, exactly the harness's standard
  predictions fields (`instance_id` / `model_name_or_path` /
  `model_patch` — WP4-verified against the pinned `swebench==5.0.2`);
  `model_patch` = the full `patch.diff` contents, else `""` — the
  harness's own empty-patch semantics keeps a no-patch instance out of
  `resolved_ids` (it lands in `empty_patch_ids`).
  `model_name_or_path` = meta `model_name_or_path` → meta `model` →
  `"devin"` (the WP4 default). `trajs/<instance_id>.jsonl` — a
  **byte-for-byte copy** of each referenced, existing trajectory (no
  re-serialization; no reference / no file → no entry). Output is
  byte-stable for stable inputs.
- **The artifact cross-check** (AC5) — run on generation (fail-loud,
  exit 3, *before* the report is declared complete; the report still
  renders with the violations so the operator sees *why*) and
  standalone (`--check`, no writes): **R1** — every JSONL line's
  referenced files exist (checked over **every** line — a superseded
  line's references too, WP2 artifact atomicity), plus the semantic
  rule: a `resolved` / `failed_test` final status must reference an
  existing `patch.diff` (a graded verdict requires a patch); **R2** —
  `all_preds.jsonl` re-read: one line per deduped instance, the exact
  field set, sorted, and every non-empty `model_patch` matches
  `patch.diff` byte-for-byte.
- **The markdown report** (`report.md`): Summary (pass@1 overall,
  instance counts incl. pending, date range, sessions used, the
  dropped-tail note when a torn EOF line was dropped), the per-repo
  table, the failure-taxonomy table (all 8 statuses, zeros included),
  the methodology block (every field, one per line), the artifact
  inventory + cross-check status.

## Pilot A (WP6a, issue #21 — the go/no-go gate for Pilot B)

`live_pilot.py` — the `BENCH_LIVE=1` smoke harness that runs the three
pinned instances (`pilot_instances.json`) **sequentially** through the
real `devin` binary (WP1 client + WP3 queue/checkpoint + WP4/WP5 seams):

```bash
cd bench
# run 1 — the intentional interrupt after 1 instance (exit 42, the
# host-crash class — resume-eligible per PLAN §2.5)
BENCH_LIVE=1 python live_pilot.py --mode sandbox --run-id pilotA \
  --interrupt-after 1
# run 2 — resume the remaining 2 (the pass@1 contract: the finished
# instance is never re-run)
BENCH_LIVE=1 python live_pilot.py --mode sandbox --run-id pilotA \
  --resume
```

- **The gate ordering (fail-closed, zero-quota first):** `BENCH_LIVE`
  opt-in → the real zero-quota pre-flight (auth → daemon → image →
  disk, before every batch and every instance) → the dynamic cost-tier
  model gate (`swe-2-high` = Free; a paid model is refused without an
  explicit override + token budget) → the sessions. A pre-flight
  failure records the batch-level `preflight_failed` marker
  (`instance_id: null` — it consumes no queue slot on resume) and
  burns no session.
- **The two modes** (`PLAN-WP6A.md`): `container` — the PLAN-conformant
  leg (the official `swebench.harness.run_evaluation` image); `sandbox` —
  the documented no-container fallback (the pilot's actual mode where no
  container runtime exists): the agent works in a git checkout under the
  workdir and the eval leg is the **host check** — a fresh bare-clone
  checkout + `git apply` of the model + test patches + a fresh venv +
  `pytest -rA` on `FAIL_TO_PASS` + `PASS_TO_PASS` (`graded_by:
  host_check(pytest -rA)`; the deviation is recorded in the checklist
  and the run meta). `container` mode is the primary path where a
  container runtime exists.
- **The measurement (the gate's output):** per-instance wall time
  (session wall from the JSONL), visible usage if the wire exposes it
  (the WP0 §1 usage fields; absent usage → the recorded §2.6 proxy),
  permission requests/allowed/denied via the WP1b policy, vendor
  notification count, the patch (PLAN §4 guards: the exclusion list —
  pre-staged junk included — the binary guard, the size cap, the
  porcelain cross-check), the pruning (PLAN §7 — the agent checkout
  removed, the artifacts + the clone cache retained), and the
  `max_sessions_per_day` calibration (the measured wall arithmetic
  printed + item 12 of the checklist).
- **The go/no-go verdict** (items 1–12 + 14 gating; item 13 — the eval
  leg — is deviation-tolerant, non-gating): `GO ⟺` the NDJSON/wire
  names + the permission flow + `session/close` conformance hold, all
  three instances run end-to-end, the interrupt + resume path is
  exercised, the budget holds, and the leak guard (PLAN §3.6) passes.
  The verdict + the measured numbers are recorded in
  `notes/wp6a-go-no-go.md`.
- **Zero-quota CI:** `tests/test_live_pilot.py` runs the same pipeline
  against `mock_devin` (the full 3-instance run, the failure-mode
  mapping, interrupt + resume pass@1 semantics, the gate refusals, the
  patch guards, the host-check verdicts, the checklist math) — one
  real-binary handshake smoke is `@pytest.mark.bench_live`-gated
  (skipped by default; CI never consumes quota, PLAN §6).

## Pilot B (WP6b, issue #22 — the 25-instance stratified pilot)

The WP6a harness, extended (`PLAN-WP6B.md`): the 25 pinned instances
across **all 12** Verified repos (`pilot_b_instances.json` — the
`stratified_repos` meta drives checklist item 15) run sequentially in
sandbox mode through the same WP1–WP5 pipeline, **importing the
Pilot-A checkpoint** so the three Pilot-A instances are reused, never
re-run:

```bash
cd bench
# the window model (the WP3 Runner — the harness' queue engine,
# default sleeper): at the per-process daily cap (10 sessions in the
# current UTC window) the run sleeps to the next UTC-midnight
# window, resets the budget (a new window is a fresh budget), and
# continues the remaining queue in the same process (the WP3 500-run
# model — one process spans days across windows). Item 12 gates the
# budget per window. (--pilot b selects pilot_b_instances.json by
# default.)
BENCH_LIVE=1 python live_pilot.py --pilot b --mode sandbox \
  --run-id pilotB --workdir ./pilot-b --resume \
  --import-run pilotA \
  --import-workdir <wp6a-workspace>/pilot-a \
  --interrupt-after 8
# continuation — same workdir, no --import-* (the lines are already
# in the checkpoint; seeding is idempotent, and the import
# re-derives from the state file so item 16 still gates). This ONE
# invocation spans the remaining windows (the cap sleep is
# in-process — no per-window re-invocation; if the process dies,
# re-invoke with --resume — the resume class):
BENCH_LIVE=1 python live_pilot.py --pilot b --mode sandbox \
  --run-id pilotB --workdir ./pilot-b --resume
```

- **The import (issue #22 AC2 — reuse, never re-run):**
  `--import-run RUN --import-workdir DIR` reads `DIR/run-RUN.jsonl`
  (fail-closed before the pre-flight: a missing / corrupt source is a
  config error, exit 2, no session spent). The terminal, non-retry
  statuses (the runner's `NEVER_RERUN_STATUSES` — `resolved` /
  `failed_test` / `timeout` / `no_patch` / `agent_error` — the
  session was burned, the outcome is final) are copied verbatim into
  this run's checkpoint with their **original timestamp** (they count
  toward the source run's window, never this run's daily budget) +
  the `meta.imported_from` marker, and the referenced artifacts are
  copied so this run's WP5 cross-check holds. The retry-class
  statuses (the runner's `RETRY_STATUSES` — `quota_limited` /
  `eval_error` / `preflight_failed`; the only pass@1 retry class,
  PLAN §2.5 — e.g. the Pilot-A `psf__requests-1142` no-container
  skip) are **not** imported: the instance runs in this run. A repeat
  `--resume` never re-seeds (no duplicate import lines). The import
  is a pilot-B mechanism: `--import-run` under `--pilot a` is a
  config error (items 15–17, which verify it, never render there),
  and `--import-workdir` requires `--import-run`. On the continuation
  windows (a `--resume` without `--import-run`) the source lines are
  re-derived from the state file, so item 16 still gates on the
  completing record. The run id charset is the SWE-bench id charset
  (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `..` — no path traversal).
- **The measurement (issue #22 AC1/AC3/AC4):** the same per-instance
  measurement as Pilot A, plus — (AC1) the per-repo stratification
  (item 15: every repo in `stratified_repos` has a final line;
  per-repo wall/token variance in the report); (AC3) every
  `eval_error` line is root-caused to `env` / `parser` / `patch`
  (item 17 gates on it; the harness auto-classifies every failure
  point — the host-check install/apply/pytest legs, the WP4 typed
  errors, the oversize-patch cap — and records
  `meta.eval_error_root_cause` + `meta.eval_error_root_cause_detail`
  (the flat sibling key)
  on the checkpoint line itself, which is the record's source of
  truth); (AC4) the per-UTC-window budget gate (item 12 buckets the
  checkpoint by day; a single-window run renders the verbatim WP6a
  text) + the 2-run overlap/variance report (item 16: the imported
  Pilot-A lines vs the Pilot-B finals — status / session wall /
  tokens / patch bytes; the `--interrupt-after` interrupt + the
  `--resume` continuation exercise the AC4 resume path on the
  real-quota run).
- **The verdict:** `GO ⟺` items 1–12 + 14–17 pass (item 13 remains
  deviation-tolerant, non-gating — `graded_by` is recorded). The
  verdict + the measured numbers (the per-repo variance, the overlap
  table, the failure taxonomy, the window budget arithmetic) are
  recorded in `notes/wp6b-pilot-b.md`.

## The 500-instance run (WP6c pre-flight, issue #44)

`--pilot c` runs the 500 Verified instances in `verified_instances.json`
through the same WP1–WP5 pipeline. What the pre-flight added is not a new
pipeline but the parts that only a 10–50-day submission run needs:

```bash
cd bench
# 0. the manifest is pinned once, in the repo (issue #44 E1)
python scripts/pin_verified_instances.py            # → verified_instances.json

# 1. B2: fill in live_pilot_budget.json — max_sessions_per_day,
#    max_wall_per_session, calibrated_at, basis, authorized_by,
#    completion_deadline, abort_rule. A PENDING value is not an
#    authorization: --pilot c exits 2 (before the live-run gate, before
#    connecting, before any session) until every one of them is filled in.
$EDITOR live_pilot_budget.json

# 2. one window per UTC day. run_window.sh supplies --stop-at-window-cap
#    itself, tees the window log, archives + verifies when
#    WP6C_ARCHIVE_DEST is set, and prints what the exit code means.
#    (First window: no --resume.)
WP6C_ARCHIVE_DEST=/mnt/offhost/devinp \
  scripts/run_window.sh --pilot c --mode container --run-id full500 \
      --workdir ./full-run --resume

# 3. progress, read-only, while a window is live
python scripts/run_status.py --workdir ./full-run --run-id full500

# 4. when the queue is empty: package (it verifies first and refuses a run
#    that does not verify), then re-verify the packaged bundle's layout
python scripts/package_submission.py \
    --jsonl ./full-run/run-full500.jsonl \
    --report ./full-run/report \
    --manifest ./verified_instances.json \
    --out ./submission/full500
python scripts/verify_submission.py \
    --jsonl ./full-run/run-full500.jsonl \
    --report ./full-run/report \
    --manifest ./verified_instances.json \
    --bundle ./submission/full500
```

- **The window model (E5, B7).** A run this long is one invocation per
  window, not one long-lived process: `--stop-at-window-cap` makes the
  harness **exit 42** when the per-UTC-day cap is reached (the WP3 runner
  would otherwise sleep to the next window inside the process — right for
  Pilot B, wrong for an operator who reboots). `scripts/run_window.sh`
  **adds that flag itself** — a wrapper that documented the per-window
  model while leaving the flag to the operator would sleep for days
  instead, never printing the resume line and never reaching its own
  archive step — and `WP6C_KEEP_RUNNING=1` opts back into the in-process
  model. The wrapper is otherwise thin: one harness invocation, no retry,
  output tee'd to `<workdir>/logs/window-<UTC>.log`, and the exit code
  explained
  (`42` resume · `4` **STOP**, do not loop-retry · `3` the live-run gate ·
  `2` configuration · `5` the archive failed). It never touches the
  checkpoint.
- **Progress without guessing (E5).** `scripts/run_status.py` reads the
  checkpoint and says what is decided, what is still queued (derived
  through WP3's `resume_queue` — never a second implementation of the
  resume rule, which is the only way this dashboard can disagree with the
  run), pass@1 so far, sessions per UTC day vs the cap, the ETA from the
  measured median wall, and disk free. Read-only, so an operator can run it
  while a window is live.
- **Authorization is a gate, not a checklist item (B2).** `--pilot c` reads
  `live_pilot_budget.json` and refuses (exit 2) while `authorized_by` /
  `completion_deadline` / `abort_rule` / `basis` / `calibrated_at` are
  missing or still say `PENDING` — and it does so **before** the
  `BENCH_LIVE` gate, because an unauthorized 500-session run is a
  configuration failure, not an environment one (setting the env var is
  one keystroke away and the answer is still no). The committed template carries the WP6a
  caps (so pilots a/b are unchanged) and `PENDING` authorizations — filling
  them in is the operator's decision, and the harness will not guess it.
- **Methodology that is written down, not remembered (E3).** Every run
  persists `report/run_meta.json` — model id + `cost_tier`, mode, the models
  sha256, the devin CLI version, the instance count + manifest sha256, the
  run dates, and the **submission label**
  (`Devin (agent) on SWE-bench Verified`, `live_pilot.SUBMISSION_LABEL`).
  The report renders the label from that file, so a label cannot be true in
  one artifact and absent from another; `--import-run` refuses a source run
  whose recorded `mode` or `model` differs from this run's (a mixed
  methodology has no single pass@1, and imported lines are never re-run,
  so the mix could never be un-mixed afterwards). The cost tier is not
  compared: this run's tier is decided later by the WP3 models-list gate,
  so there is nothing to compare at import time. A source with *no*
  recorded methodology is tolerated by `--pilot b` (the WP6b pair predates
  the meta) and refused by `--pilot c`, which may not publish a pass@1 it
  cannot attribute.
- **Items 18–23 (the full-run checklist).** 18 completeness against the
  pinned manifest (retry-class gaps are named: they are re-runnable, an
  undecided instance is not); 19 artifact integrity (WP5's own cross-check);
  20 methodology completeness; 21 pass@1 recomputed from the checkpoint
  equals what `report.md` renders; 22 the submission label in `run_meta.json`
  **and** in the report; 23 batch discipline across UTC windows (never more
  than the cap in any one window, and the queue never breaks on a
  `--resume`).
- **Verification and packaging (E4).** `scripts/verify_submission.py`
  re-runs WP5's `build_report` (so R1/R2 integrity is WP5's own verdict,
  exit 3 on failure) and then checks the submission-only properties: every
  manifest instance decided, the grading queue unbroken, the rendered pass@1
  reproducible, `run_meta.json` complete and honest about the grading leg,
  the label consistent, and — with `--bundle` — the
  `runs/<run_id>/<iid>/{patch.diff,trajectory.jsonl,eval.log}` layout.
  `scripts/package_submission.py` verifies first, then writes that layout
  plus `SHA256SUMS` and a reviewer README (the label, the methodology table,
  the deviations, the reproduction command), refusing a non-empty target
  unless `--force`.
- **Durability (B4).** `scripts/archive_run.py` copies a window's artifacts
  to `--dest` in an append-only bundle and **verifies the copy**
  (`--verify --bundle …` re-proves it later); `run_window.sh` runs it when
  `WP6C_ARCHIVE_DEST` is set and exits 5 if the verification fails. This is
  the fix for the Pilot B artifact deletion (issue #22, closed by waiver).
- **Zero-quota CI:** `tests/test_pilot_c.py` (the pilot kind, the B2 gate,
  items 18–23, the meta, the label, the mode-gated import),
  `tests/test_verify_submission.py` (verify + package, incl. every failure
  class and a package round-trip through the verifier),
  `tests/test_run_ops.py` (the window wrapper's exit-code contract against a
  stub harness **and** a real two-window resume drill against the real
  harness, the status math, the archive + its verification failures) and
  `tests/test_pin_verified.py` (the pinned manifest: 500, 12 repos, no
  duplicates, schema-valid, reproducible).

## SWE-bench pin (WP0 result)

`swebench==5.0.2` (lock: `requirements.lock`). Verified split: 500 instances,
12 repos, eval images `swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:latest`
(`_1776_` = Docker Hub escape of `__`), **all 12 bases = Ubuntu 22.04 +
miniconda py311 + `testbed` env** (see `notes/acp-surface.md` §5/§6).
