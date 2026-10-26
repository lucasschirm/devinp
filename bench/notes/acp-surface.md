# WP0 Recon Notes — `devin acp` surface, free-tier quota, SWE-bench pinning

**Issue:** #10 · **Plan:** `bench/PLAN.md` §5 WP0 · **Recon date:** 2026-10-26
**Probe tool:** `bench/probes/acp_probe.py` (selftest-validated against a mock ACP server)

This file answers every "?" from the WP0 work package. Sections:
1. [ACP surface](#1-acp-surface) · 2. [Host devin install layout](#2-host-devin-install-layout) ·
3. [glibc + CA compatibility](#3-glibc--ca-compatibility) · 4. [Free-tier quota + reset window](#4-free-tier-quota--reset-window) ·
5. [SWE-bench harness + images (pinned)](#5-swebench-harness--images-pinned) ·
6. [Conda testbed layout](#6-conda-testbed-layout) · 7. [`devin models list --format json` shape](#7-devin-models-list--format-json-shape) ·
8. [Hand-driven `swe-2-high` session](#8-hand-driven-swe-2-high-session) · 9. [Open items / risks](#9-open-items--risks)

---

## 1. ACP surface

> **Live capture completed 2026-10-26** via `acp_probe.py handshake --model
> swe-2-high` (delegated; raw: `notes/acp-probe-handshake*.json` + `.frames.jsonl`).

- **Framing: NDJSON — confirmed with positive + negative evidence.** The probe's
  framing canary (an LSP-style `Content-Length: 89\r\n\r\n` prefix line before
  the first request) produced, on the **real server**:
  1. a JSON-RPC error for the header line: `{"id": null, "error": {"code":
     -32700, "message": "Parse error", "data": {"line": "Content-Length: 89"}}}`;
  2. a parse error for the blank separator line (same shape);
  3. the canary JSON line itself routed as a normal message:
     `{"id": 9000, "error": {"code": -32601, "message": "Method not found"}}`.
  4. the subsequent **bare-NDJSON** `initialize` was answered in 50 ms.
  ⇒ the server is strictly **line-oriented JSON-RPC 2.0**; `Content-Length`
  headers are not framing — they are (rejected) lines. Non-JSON lines yield
  JSON-RPC error objects, not a crash (the server kept serving).
- **Wire method names — confirmed live:** `initialize` (on connect),
  `session/new`, `session/prompt`, `session/cancel`, `session/request_permission`
  (server-initiated, with id — the client must answer it), `session/close` (the
  probe always sends it before exit; `sessionCapabilities.close._meta
  "cognition.ai/force": true` in the initialize result suggests the server
  forces close semantics). `session/update` = streaming notifications.
- **`initialize` result (real):** `protocolVersion: 1`;
  `agentCapabilities`: `loadSession: true`;
  `promptCapabilities: {image: true, audio: false, embeddedContext: true}`;
  `mcpCapabilities: {http: true, sse: true}`;
  `sessionCapabilities: {list, delete, additionalDirectories, close}`; plus a
  `_meta` block of `cognition.ai/*` flags (multiRootWorkspace,
  sessionListContentSearch, sessionRename, persistOnCreate, sessionShare,
  documentLifecycle, userEdits, terminalLife…).
- **`session/new` result (real):** `{sessionId: "breezy-vessel", modes:
  {currentModeId: "accept-edits", availableModes: [{id: "accept-edits",
  name: "Code"}, {id: "smart", name: "Smart"}, {id: "ask", …}…]}}` — devin
  exposes **modes** (accept-edits/Code, smart, ask, plus a `bypass` mode per
  the command list); the ACP client can switch modes via `session/set_mode`
  (to be confirmed — see §9).
- **`session/update` notification types observed live:** `config_option_update`,
  `current_mode_update`, `available_commands_update`, `session_info_update`
  (the session title was set to the prompt text). (Agent-message/tool-call
  variants arrive later in the stream — run 1 died before any agent activity;
  run 2 pending.)
- **Available commands (27, from `available_commands_update`):** login,
  logout, status, billing, workspace, ask, plan, code, smart, bypass, compact,
  context, fast, loop, goal, recap, session-stats, rename, share, mcp, bug,
  help, orchestration, declarative-repo-setup, upload-secrets,
  pr-observer:pr-tasks, pr-observer:observe-pr.
- **Startup vendor notifications to ignore:** `_cognition.ai/mcp/serversChanged`
  (observed **twice** at startup, `params: {}`) — confirmed real, unsolicited,
  non-interactive. The probe logs them and never blocks on them (PLAN.md §1).
- **Stop-reason / usage metadata — confirmed live (run 2):** the
  `session/prompt` response returned `stopReason: "end_turn"` with a real
  usage object: **`usage.{inputTokens, outputTokens, cachedReadTokens,
  totalTokens}`** (trivial task: in 11 996 / out 30 / total 12 026).
  WP1 should capture this verbatim; if a model/context omits it, fall back
  to PLAN §2.6 proxies.
- **`session/request_permission`:** server-initiated request (with id) per the
  schema (`ClientRequest`/`ClientNotification` types in the CLI log);
  **not observed in the two probe runs** (the trivial task under
  `accept-edits` mode never needed one). WP1's policy handler is built on
  the mock contract + schema names; the real shape is an open item (§9).
- **`session/close` real behavior (important for WP1):** the probe's
  `session/close` produced a **"Handler errored" dump** (stderr spewed the
  whole `agent_client_protocol_schema` type list — a handler bug/panic
  dump, not a clean response) and **the agent process did NOT exit**
  (`process_survived_close`; group-kill required, orphan check clean).
  ⇒ WP1 must: send `session/close`, wait briefly, then **kill the process
  group** on all paths — never rely on close for termination. (PLAN.md §1's
  "call close before terminating" still holds as hygiene; termination is via
  the group kill.)
- **ACP schema type names (from the CLI log,
  `agent_client_protocol_schema::v1::agent::`):** `InitializeRequest`,
  `AuthenticateRequest`, `NewSessionRequest`, `LoadSessionRequest`,
  `ListSessionsRequest`, `DeleteSessionRequest`, `CloseSessionRequest`,
  `PromptRequest`, `SetSessionModeRequest`, `SetSessionConfigOptionRequest`,
  `CancelNotification`, `ClientNotification`, `ClientRequest` (13 types).
- **`devin acp` CLI surface (full `--help` in `notes/devin-acp-help.txt`):**
  `--model <M>` (fuzzy; also `DEVIN_MODEL` env — the in-process ACP server
  the REPL spawns inherits the env), `--refusal-fallback <M>` (also
  `DEVIN_REFUSAL_FALLBACK`, comma-separated — **the adapter must keep both
  unset**, PLAN §3.4 ✓), `--agent-type` (e.g. `summarizer`), `--cloud`
  (proxies stdio ACP to Devin cloud WebSocket — not our path).
  **Corrections to PLAN assumptions:** (a) `devin acp` has **no `--config`
  flag** — isolated config must be achieved via `XDG_CONFIG_HOME` (or
  whatever config file the isolated XDG points at); (b) there is **no
  workspace-trust flag in acp mode** — trust is a function of the cwd
  (see §8 run 1 vs run 2), so WP1 must make `/testbed` trusted (config or
  trust file in the isolated XDG) — a pre-Pilot-A check.

## 2. Host devin install layout

**Verified 2026-10-26 (recon host, Orca workspace):**

- `$(which devin)` = `/home/lucas/.local/bin/devin` — a **POSIX shell wrapper script**,
  *not* the binary. Generated by `scripts/orca/Dockerfile` (per its header comment
  "do not hand-edit; re-run the build").
- Wrapper content (verbatim):
  ```sh
  #!/bin/sh
  # Generated by scripts/orca/Dockerfile — do not hand-edit; re-run the build.
  export XDG_DATA_HOME="/home/lucas/.devin-xdg-data"
  exec "/home/lucas/.local/share/devin/cli/_versions/current/bin/devin" "$@"
  ```
- Real binary: `~/.local/share/devin/cli/_versions/current/bin/devin`
  — ELF 64-bit LSB **static-pie** executable, x86-64, **statically linked**,
  200,366,312 bytes (200 MB), version **3000.11.92** (7f4e64980e22).
- `~/.local/share/devin/cli/_versions/` = `3000.11.92`, `_download`, `current`
  (symlink to the versioned dir).

**Consequence for the adapter (PLAN §3.1):** mount/copy the **real binary**
(and its `_versions` tree), never the wrapper; the wrapper also pins
`XDG_DATA_HOME`, which the adapter must NOT inherit — per-instance ephemeral
XDG dirs only.

**Auth token location (for ephemeral XDG seeding, PLAN §3.2):** `devin auth
status` reports the credentials file as
`$XDG_DATA_HOME/devin/credentials.toml` (host: `/home/lucas/.devin-xdg-data/devin/credentials.toml`).
API endpoints: `https://server.codeium.com` (API server), `https://api.devin.ai`
(Devin API), webapp `https://app.devin.ai`.

## 3. glibc + CA compatibility

**Binary forensics (2026-10-26, recon host):**

- `file`: `ELF 64-bit LSB pie executable, x86-64, static-pie linked, stripped`.
- `ldd`: `statically linked` → **zero dynamic GLIBC dependencies** (no dynamic
  symbol table; no program interpreter). **glibc version mismatch cannot occur**
  — the risk in PLAN.md §9 for dynamic binaries does not apply to this binary.
- The only residual kernel-ABI requirement is a modern enough Linux kernel for
  a static-pie x86-64 binary; all sweb.eval base images run ≥ Linux 5.x
  (Ubuntu 22.04 base, §5), so this is satisfied with wide margin.
  (In-image smoke test of the real binary remains a Pilot A item, §9.)
- CA behavior: `grep -a` on the binary finds references to
  `/etc/ssl/certs/ca-certificates.crt` (Debian/Ubuntu bundle path) and
  `/etc/ssl/certs/cacert.pem` — i.e. devin reads **standard system CA paths**.
  Ubuntu 22.04 ships `/etc/ssl/certs/ca-certificates.crt` by default → the CA
  requirement is met by the base image as-is; no bundle injection needed
  (in-image HTTPS verification remains a Pilot A item, §9).

**Result:** glibc: **compatible by construction (static binary)** · CA: **standard
system paths, present in Ubuntu 22.04 base**. In-image smoke test of the real
binary + a network check: open item §9 (no container runtime in recon workspace).

## 4. Free-tier quota + reset window

> **Account fact (2026-10-26):** `devin auth status` shows the recon account
> is on the **Devin Pro** plan (Plan: Pro, Enterprise: no, team
> `devin-team$account-6e0f…`, 836 allowed models). The WP0 plan assumed a
> free-tier account; all quota facts below are therefore **Pro-plan facts**,
> and the free-tier numbers are recorded where public. The WP1 model gate
> (`cost_tier == "Free"`, §7) is unaffected — it gates model *selection*, not
> the account plan.

- `devin auth status` (full output: `bench/notes/auth-status.txt`): logged in,
  credentials at `$XDG_DATA_HOME/devin/credentials.toml`, **no quota/remaining
  fields exposed by the CLI** → per PLAN.md §2.6, WP1 usage tracking falls
  back to sessions + wall time + prompt/patch byte sizes (plus per-session
  usage from Devin *Session Insights*, docs below).
- **Cognition docs** (`docs.devin.ai/admin/billing/self-serve` +
  `/admin/billing/usage`, fetched 2026-10-26):
  - **Free plan:** "limited Devin usage" — **no numbers published** in the
    docs; free users get Devin Review + DeepWiki access.
  - **Pro plan:** a **daily and a weekly usage quota** (covers Devin sessions,
    Devin CLI, Devin Desktop), plus pay-as-you-go on-demand credits past the
    quota. "The daily allowance is more than 1/7 of the weekly, so you can
    keep working through weekends". Allowance **refreshes on a calendar
    basis** ⇒ **reset window = calendar day + calendar week**.
  - **Metering:** usage accrues from work actually performed (action
    count/complexity + a small fraction of VM time). **No consumption while**
    waiting for a response, waiting on test suites, or during setup/clone.
    Auto-sleep after 30 min inactivity (5 min for API-created sessions).
    Windows sessions cost ~9% more; macOS = Linux price (promotional).
  - **Visibility:** per-session usage in *Session Insights*; account-level
    (quota remaining, credit balance) only in the web app
    (*Settings > Usage & limits*) — not in the CLI.
- **Timed probe session:** wall time + visible usage from the live `devin acp`
  run: *(pending — §8)*.
- **Recommended budget for WP1 config (calibrate in Pilot A):**
  `max_sessions_per_day` (conservative default 10), `max_wall_per_session`
  45 min, `daily_pause` aligned to the **calendar-day** reset (the
  fine-grained one), weekly budget as a hard outer cap.

## 5. SWE-bench harness + images (pinned)

**Pinned (2026-10-26):** `swebench==5.0.2` (latest stable at recon time),
lock: `bench/requirements.lock` (77 pins; `datasets==5.1.0`, `pytest==9.1.1`).
`python -m swebench.harness.run_evaluation` imports and runs against this pin.

**`run_evaluation` input format — verified in the pinned source
(`swebench/harness/utils.py`, `run_evaluation.py` of swebench 5.0.2):**

- Predictions file: `.jsonl` (one dict per line) or `.json` (list, or
  `{instance_id: prediction}` dict). Loader: `get_predictions_from_file`.
- **Required per prediction:** `instance_id` (hard-checked; raises
  `ValueError` if missing). Used by the harness: `model_name_or_path`
  (`.get(..., "None")`, `/`→`__` in log dir) and `model_patch` (written to
  `/tmp/patch.diff` in the eval container; `""`/`None` allowed — the harness
  explicitly filters empty-patch predictions at `run_evaluation.py:630`).
- CLI (verified from the pinned `main()` argparse):
  ```
  python -m swebench.harness.run_evaluation \
    -d SWE-bench/SWE-bench_Verified -s test \
    -p predictions.jsonl -id <run_id> \
    [-i <instance_ids...>] [--max_workers 4] [-t 1800] [--report_dir .]
  ```
  (defaults: `--dataset_name SWE-bench/SWE-bench_Lite`, `--split test`,
  `--max_workers 4`, `-t 1800`; `-p` and `-id` are required; `--modal` exists
  but local docker is the path we use.)

**Verified split (loaded via `load_swebench_dataset("SWE-bench/SWE-bench_Verified", "test")`):**

- **500 instances, 12 repositories:**
  django/django 231 · sympy/sympy 75 · sphinx-doc/sphinx 44 ·
  matplotlib/matplotlib 34 · scikit-learn/scikit-learn 32 · astropy/astropy 22 ·
  pydata/xarray 22 · pytest-dev/pytest 19 · psf/requests 8 · pylint-dev/pylint 10 ·
  mwaskom/seaborn 2 · pallets/flask 1.
- Instance dict fields (18): `instance_id, repo, base_commit, patch, test_patch,
  FAIL_TO_PASS, PASS_TO_PASS, eval_script, eval_type, log_parser, version,
  image, environment_setup_commit, hints_text, difficulty, created_at` (+2).
- **Eval image naming** (from the `image` field; 500 unique, one per instance):
  `swebench/sweb.eval.x86_64.<owner>__<repo>-<PR>:latest` where Docker Hub
  forbids `__` so the dataset stores the escaped form
  `swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:latest`
  (`_1776_` is the escape for `__`, per `swebench.image_builder.image_spec`
  `.name` — "docker hub doesn't allow dunders in image names").
  Example: `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`.
- **Base image (all 12 repos — uniform):** inspected via Docker Hub registry API
  (image index → amd64 manifest → config `history`, no container runtime needed):
  - Base OS: **Ubuntu 22.04** (`LABEL org.opencontainers.image.version=22.04`)
    for **every** of the 12 representative images (one per repo, all `:latest`).
  - Miniconda: **py311** (`Miniconda3-py311_23.11.0-2-Linux-x86_64.sh`),
    `PATH=/opt/miniconda3/bin:...`, `conda init --all`, conda-forge appended.
  - User `nonroot` (gecos "dog") created; `WORKDIR /testbed/`;
    `/root/.bashrc` contains `source /opt/miniconda3/etc/profile.d/conda.sh &&
    conda activate testbed`.
  - Image built 2026-08-13/15 (recent rebuilds; `:latest` tags).
  - **Per-repo base table** (one representative image per repo, inspected via
    the Docker Hub registry API: `auth token → image index → amd64 manifest →
    config blob `history``)::

    | repo | base OS | miniconda | testbed env (bashrc) | image built |
    |---|---|---|---|---|
    | astropy/astropy | 22.04 | py311 | yes | 2026-08-15 |
    | django/django | 22.04 | py311 | yes | 2026-08-13 |
    | matplotlib/matplotlib | 22.04 | py311 | yes | 2026-08-13 |
    | mwaskom/seaborn | 22.04 | py311 | yes | 2026-08-13 |
    | pallets/flask | 22.04 | py311 | yes | 2026-08-13 |
    | psf/requests | 22.04 | py311 | yes | 2026-08-13 |
    | pydata/xarray | 22.04 | py311 | yes | 2026-08-13 |
    | pylint-dev/pylint | 22.04 | py311 | yes | 2026-08-13 |
    | pytest-dev/pytest | 22.04 | py311 | yes | 2026-08-13 |
    | scikit-learn/scikit-learn | 22.04 | py311 | yes | 2026-08-15 |
    | sphinx-doc/sphinx | 22.04 | py311 | yes | 2026-08-13 |
    | sympy/sympy | 22.04 | py311 | yes | 2026-08-13 |

    Reproduce: `GET auth.docker.io/token?service=registry.docker.io&scope=repository:<img>:pull`
    (scope without the `:latest` tag), then `registry-1.docker.io/v2/<img>/manifests/latest`
    → amd64 digest → `/manifests/<digest>` → config blob (follow the 307 with
    `-L`).
  - ⇒ The "oldest sweb.eval base image" for the compat check is **Ubuntu 22.04**
    (glibc 2.35) — see §3: the static devin binary is compatible by construction.

## 6. Conda testbed layout

**Verified from image config `history` (registry API, all 12 repos):**

- `/opt/miniconda3` = miniconda py311 (23.11.0); `PATH` includes
  `/opt/miniconda3/bin` first.
- `conda init --all` + conda-forge channel appended.
- `testbed` conda env is the repo environment; `/root/.bashrc` auto-activates it
  (`conda activate testbed`).
- Repo workdir: `/testbed/` (WORKDIR).
- **Adapter consequence (PLAN §3.5):** spawn `devin acp` with
  `PATH=/opt/miniconda3/envs/testbed/bin:$PATH`, `CONDA_DEFAULT_ENV=testbed`.
- In-image confirmation that `/opt/miniconda3/envs/testbed/bin/python` runs and
  the `testbed` env actually exists at `/opt/miniconda3/envs/testbed`:
  open item §9 (requires a container runtime; the `history` proves the
  build-time layout).

## 7. `devin models list --format json` shape

**Captured 2026-10-26** (full output: `bench/notes/models-list.json`, 419 KB,
44 families, 816 variants).

- Top level: `{"families": [ <family>, ... ]}`.
- Family: `family_label`, `family_uid`, `slug`, `aliases[]`, `variants[]`.
- Variant (all keys across the list): `model_uid`, `label`, `description`,
  `is_beta`, `is_new`, `cost_summary`, and **optional** `cost_tier`,
  `max_context_tokens`, `max_output_tokens`.
- `cost_tier` values: `Free` (3), `Low cost` (61), `Med cost` (179),
  `High cost` (572), absent (1 — the `adaptive` router).
- **Free tier = exactly `{swe-2-medium, swe-2-high, swe-2-max}`** — matches the
  PLAN.md expectation. Their `cost_summary` is empty (no $ rates).
- **Paid SWE models (must never be selectable under the free policy):**
  - `swe-1-6`, `swe-1-7`, `swe-1-7-medium` → `Low cost`,
    `$0.5 / 1M Input · $0.2 / 1M Cached · $2.5 / 1M Output`.
  - `swe-2-medium-lightning`, `swe-2-high-lightning`, `swe-2-max-lightning`
    → `High cost`, `$1 / 1M Input · $0.1 / 1M Cached · $5 / 1M Output`.
- **WP1 gate (fail-closed):** parse `families[].variants[]`, permit only
  `cost_tier == "Free"`; abort the batch if the JSON shape deviates
  (e.g. `cost_tier` missing on a known uid) — per PLAN.md §2.4.

## 8. Hand-driven `swe-2-high` session

**Run 1 (2026-10-26, delegated, `--cwd /tmp/wp0-task` — untrusted cwd):**
process up in ~0.1 s; `initialize` answered (0.05 s); `session/new` →
sessionId `breezy-vessel` (0.088 s); prompt sent; **stdout hit EOF ~2 s after
the prompt** with no agent activity frames and no stop reason. `wp0_probe.txt`
not created.

**Run 2 (same session, `--cwd <workspace root>` — trusted cwd): SUCCESS.**
- `wp0_probe.txt` **created, contents `wp0 ok`** — trivial task complete.
- `stop_reason: end_turn`; usage `totalTokens=12026` (in 11 996 / out 30).
- Prompt wall time **4.33 s**; total process lifetime 19.8 s (incl. startup
  + `session/close` handler dump + group kill).
- New vendor notifications observed: `_cognition.ai/thinking_complete`,
  `_cognition.ai/turn_stats` (log, ignore, never block — PLAN §1).
- `process_survived_close` (see §1 close behavior); orphan check clean.

**Diagnosis (confirmed by the A/B):** untrusted `cwd` (/tmp) kills the
prompt turn (server-side turn completed in ~2 s idle then EOF); a trusted
workspace cwd works end-to-end. ⇒ **adapter pre-Pilot-A check: make
`/testbed` trusted** (isolated-XDG config / trust file — mechanism TBD,
`devin acp` exposes no trust flag). Wall time for quota calibration:
**4.3 s prompt / 19.8 s total** for a trivial file-create task — far below
the 45-min cap; real SWE tasks will dominate, Pilot A measures per-instance.

**Sandbox-container caveat (recorded honestly):** runs 1–2 executed in the
delegated Devin **workspace** (itself a sandboxed environment), *not* inside
an `sweb.eval.*` container — no container runtime existed in either available
environment. The binary-in-container part of AC6 rides on Pilot A (which
must have docker to pull the eval images anyway).

## 9. Open items / risks

1. **Real `session/request_permission` shape** — not observed (trivial task
   under `accept-edits` never triggered one). WP1 builds on the mock contract
   + schema type names; Pilot A's real SWE tasks will exercise it (the agent
   must run pytest → exec permission requests expected).
2. **In-container smoke test of the real devin binary** (Ubuntu 22.04 eval
   base: `devin --version` + HTTPS check) — no container runtime in either
   available environment (recon: unprivileged, CapEff=0, no /dev/fuse;
   delegated workspace: `docker: command not found`). §3/§6 stand on binary
   forensics + image config `history`; Pilot A (which requires docker) runs
   this as a pre-flight for free.
3. **Trusted-cwd mechanism for `/testbed`** — `devin acp` has no trust
   flag; trust is cwd-derived (run 1 vs run 2). Pilot A pre-flight: find
   the trust file/config in an isolated `XDG_CONFIG_HOME` that makes an
   arbitrary directory trusted.
4. **`testbed` conda env existence** at `/opt/miniconda3/envs/testbed` —
   proven at build time by `history`; live confirmation with Pilot A's docker.
5. **Quota numbers are Pro-plan** (see §4): re-measure on a free-tier
   account if the benchmark must run under one; the `cost_tier == "Free"`
   model gate is plan-agnostic. `session/close` handler error (schema dump,
   process survives) — treat close as best-effort + group kill (WP1).
6. **`session/set_mode` / `set_config_option` wire names** — schema types
   exist (`SetSessionModeRequest`, `SetSessionConfigOptionRequest`); the
   wire names are presumably `session/set_mode` / `session/set_config_option`
   (ACP convention) but were not exercised in the probes — confirm in
   Pilot A if the adapter needs mode switching (it likely does: default
   `accept-edits` may not auto-allow exec).

---

## Appendix A — environment facts (recon workspace, 2026-10-26)

- `devin` 3000.11.92; wrapper layout §2 (identical in the delegated Devin
  workspace — same user/home, `which devin` = `/home/lucas/.local/bin/devin`,
  `current -> 3000.11.92`, real binary present).
- The `devin` CLI is bash-blocked in the recon session (all invocations, incl.
  read-only; hook directs delegation via `devin_delegate`) → all live devin
  work routed through delegated Devin sessions (2 delegated sessions used:
  one read probe — cancelled early due to output bloat, one consolidated
  read+ACP-probe session; the `devin acp` handshake is the budgeted real
  session).
- No container runtime available (see open item 2).
- Host: glibc 2.39 (Ubuntu), 87 GB free disk, Python 3.12.3 (bench venv: 3.12).
- `publish.yml` (main) has no `paths-ignore: ['bench/**']` yet — §8.1 of
  PLAN.md remains a follow-up; `package.json` `"files": ["bin","data"]` already
  excludes `bench/` from the npm tarball.

## Appendix B — probe selftest evidence

`python3 bench/probes/acp_probe.py selftest` → **PASS**: sent
`initialize, session/new, session/prompt, session/close`; server frames
`_cognition.ai/mcp/serversChanged` (startup notification) +
`session/request_permission` (answered deny for `web_fetch`);
`stop_reason: end_turn`; `usage_fields: usage, usage.input_tokens,
usage.output_tokens`; process group clean on exit (no orphans).
