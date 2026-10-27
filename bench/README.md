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

## Layout

| Path | What |
|---|---|
| `PLAN.md` | Plan of record (architecture, WP0–WP6, policies) |
| `PLAN-WP0.md` | WP0 implementation plan (phases, AC map, contingencies) |
| `PLAN-WP4.md` | WP4 implementation plan (AC map, harness-fact record, contingencies) |
| `notes/acp-surface.md` | **WP0 notes** — the recon record answering every "?" |
| `notes/models-list.json` | Raw `devin models list --format json` (2026-10-26) |
| `notes/auth-status.txt` | `devin auth status` output (quota evidence) |
| `notes/*` | Additional recon evidence (version, image registry scans…) |
| `probes/acp_probe.py` | NDJSON JSON-RPC probe for `devin acp` (4 modes) |
| `acp_client.py` | **WP1** — NDJSON JSON-RPC 2.0 client core (pure stdlib) |
| `mock_devin.py` | **WP2** — `mock_devin` test double: a fake `devin` binary (canned ACP agent, modes `ok`/`no_patch`/`hang`/`quota`/`crash`, canned `models list` fixture) |
| `runner.py` | **WP3a + WP3b** — sequential queue (concurrency 1 by default), quota-budget state machine (`running → budget_exhausted → sleeping → resume`), overlap guard + concurrency flag contract, abort on 2 consecutive `agent_error`; checkpoint/resume with pass@1-correct retry semantics (`JsonlCheckpoint`, `resume_queue`), the dynamic cost-tier model gate (fail-closed, `gate_cost_tier`/`parse_models_list`/`fetch_models_list`), the zero-quota pre-flight (`Preflight`, before every batch + every instance) (pure stdlib) |
| `evaluate.py` | **WP4** — the SWE-bench evaluation leg: a thin delegate to `python -m swebench.harness.run_evaluation` (standard predictions file, fresh harness-created container, `{resolved, logs}` return contract, typed error hierarchy `InvalidInputError`/`PatchCorruptionError`/`EvaluationTimeoutError`/`HarnessFailedError`; never imports `swebench` — the subprocess is the only harness interaction; pure stdlib + subprocess) |
| `tests/` | **WP1** — unit tests + in-process mock `devin` server; **WP2** — `mock_devin` integration tests (driven through the real WP1 client); **WP3a** — `runner` unit tests (instrumented fake sessions); **WP3b** — resume fixtures (incl. EOF corruption + retry-class rules), cost-tier gate fixtures (paid model, shape change, paid override), pre-flight stub-failure tests; **WP4** — `evaluate` unit tests (injected harness runner + fabricated harness trees, zero quota) + CI integration tests (positive control on the public flask image, corruption guard, isolation; skip without a Docker daemon) |
| `conftest.py` | pytest path shim for the flat layout |
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
  module's no-`swebench`-import contract is pinned by a source test.
  `overall_timeout` is the optional outer watchdog (the project's hang
  policy — `-t` bounds only the *in-container* test exec; the HF dataset
  download, the image pull and the Docker daemon are not bound by `-t`);
  pre-spawn filesystem work, a spawn failure and the watchdog firing are
  typed `HarnessFailedError`, never a raw escape.
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
  own empty-patch semantics (below); `logs` carries the harness artifact
  paths (`instance`, `test_output`, `final_report`), the verbatim
  per-instance `report`, `container`, and the run identity (`workdir` — a
  fresh tempdir when the caller passed None, surfaced so the caller-owned
  cleanup is never unreachable — `run_id`, `predictions`). A non-zero
  harness exit is always an error, *even when a report exists* (the
  harness writes the per-instance report before the final-report leg).
- **Stale-report protection:** the harness's own `exclude_completed` treats
  a pre-existing per-run `report.json` as "already run" and silently skips
  the instance — `evaluate` never trusts a pre-existing report: it unlinks
  one before the spawn (on an unlink failure the pre-run mtime is recorded
  and the post-run report must be newer), so a re-used (workdir, run_id)
  pair can never return the previous run's verdict for a patch that was
  never evaluated.
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
  `PatchCorruptionError` (wins over a timeout marker), the anchored
  `Timeout error: N seconds exceeded` marker in `test_output.txt` (or the
  harness's timeout message / `>>>>> Tests Timed Out` logger line in
  `run_instance.log`) → `EvaluationTimeoutError` (terminal in WP3 — hence
  the loose markers are anchored and file-specific, so a docker-client
  flake cannot zero out a retryable instance), anything else →
  `HarnessFailedError` (the WP3 `eval_error` retry class). A clean typed
  error, never a crash.

## SWE-bench pin (WP0 result)

`swebench==5.0.2` (lock: `requirements.lock`). Verified split: 500 instances,
12 repos, eval images `swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:latest`
(`_1776_` = Docker Hub escape of `__`), **all 12 bases = Ubuntu 22.04 +
miniconda py311 + `testbed` env** (see `notes/acp-surface.md` §5/§6).
