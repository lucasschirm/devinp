# bench/ — SWE-bench Verified × `devin acp` (recon → harness)

Work packages per `bench/PLAN.md`. **WP0 (recon, issue #10) complete** —
results in `notes/acp-surface.md` (all six ACs answered; open items §9 are
Pilot-A pre-flights). **WP1 (client core, issue #11) complete** —
`acp_client.py` + unit tests against the mock server (zero quota, CI).

## Layout

| Path | What |
|---|---|
| `PLAN.md` | Plan of record (architecture, WP0–WP6, policies) |
| `PLAN-WP0.md` | WP0 implementation plan (phases, AC map, contingencies) |
| `notes/acp-surface.md` | **WP0 notes** — the recon record answering every "?" |
| `notes/models-list.json` | Raw `devin models list --format json` (2026-10-26) |
| `notes/auth-status.txt` | `devin auth status` output (quota evidence) |
| `notes/*` | Additional recon evidence (version, image registry scans…) |
| `probes/acp_probe.py` | NDJSON JSON-RPC probe for `devin acp` (4 modes) |
| `acp_client.py` | **WP1** — NDJSON JSON-RPC 2.0 client core (pure stdlib) |
| `tests/` | **WP1** — unit tests + in-process mock `devin` server |
| `conftest.py` | pytest path shim for the flat layout |
| `pyproject.toml` / `requirements.lock` | Python ≥3.11; `swebench==5.0.2` pinned |

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

## SWE-bench pin (WP0 result)

`swebench==5.0.2` (lock: `requirements.lock`). Verified split: 500 instances,
12 repos, eval images `swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:latest`
(`_1776_` = Docker Hub escape of `__`), **all 12 bases = Ubuntu 22.04 +
miniconda py311 + `testbed` env** (see `notes/acp-surface.md` §5/§6).
