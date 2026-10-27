"""``runner.py`` — WP3a (issue #14) + WP3b (issue #15): sequential
queue, quota-budget state machine, overlap guard, checkpoint/resume
with pass@1-correct retry semantics, the dynamic cost-tier model
gate, and the zero-quota pre-flight.

Scope (PLAN.md §5 WP3 "WP3a" split, issue #14; checkpoint/resume +
cost-tier gate + pre-flight are WP3b, issue #15 — this release;
post-eval pruning is WP3c, issue #16). The module is pure stdlib — no
SDK dependency, no real ``devin`` binary — and is unit-tested against
instrumented fake sessions + fixtures (``tests/test_runner.py``), zero
quota.

Acceptance criteria (issue #14) and where they live:

1. **Default concurrency = 1; no two agent sessions ever overlap.** A
   FIFO of instance ids is executed one at a time. The *assert* is the
   :class:`OverlapGuard`: every session's start→run→close executes inside
   a guard slot, and a slot attempt while the guard is at its cap raises
   :class:`OverlapViolation` — a fail-loud assert, not a silent wait (an
   overlapping schedule is a bug to surface, not a delay to absorb).
   :class:`ProcessLock` (an opt-in cross-process ``flock``) extends the
   same guarantee to *sibling* runner processes on the same host
   (PLAN §2.1 "process-level lock + assert").
2. **``--concurrency N>1`` refused by default unless
   ``--i-know-about-quota``** — one place, :func:`check_concurrency_flag`,
   used by both the CLI (exit 2) and :class:`Runner.__init__` (defense
   in depth). A granted N>1 runs N concurrent workers (the user takes the
   quota risk on themselves); the guard cap becomes N.
3. **Backoff state machine** (:data:`TRANSITIONS` — pure, table-driven):
   ``running → budget_exhausted → sleeping (until window) → resume →
   running``, with the terminal states ``aborted`` / ``done``. Both the
   local daily budget cap (:class:`BudgetConfig`) and an external
   rate-limit signal (a session's terminal status ``quota_limited`` —
   PLAN §2.3: "ACP stop reason, CLI stderr, or non-zero exit with quota
   text") converge on ``budget_exhausted``.
4. **Rate-limit signal → mark ``quota_limited``, stop the batch,
   checkpoint, sleep to next window — batch-level backoff, no
   per-instance retry.** On ``quota_limited`` the instance is *marked*
   (reporter ``record_instance``), the batch *stops* (no further instance
   starts), the *checkpoint* hook fires (WP3b's JSONL flush; the in-memory
   recorder by default), the runner *sleeps to the next quota reset
   window* (next UTC midnight — the "calendar basis" reset's fine-grained
   leg, WP0 §4; the sleep's *duration* is the remainder of the current
   window — the nominal 12 h ``daily_pause`` is the default config, not
   a guarantee of the realized pause), and the run *resumes* in the new
   window with the budget reset. **The ``quota_limited`` instance is not
   re-queued in this run** — its re-run is the checkpoint-resume retry
   class (PLAN §2.5), applied by WP3b's resume logic on the next process
   start. The remaining queue continues in the new window (the 500 run is
   one process spanning days across windows, PLAN §6 WP6.3).
5. **Abort on 2 consecutive ``agent_error`` (environment-drift
   suspicion).** "Consecutive" = in completion order, reset by *any* other
   terminal status **and by the window reset** (a new day is a fresh
   suspicion — the drift evidence is window-scoped; a deliberate deviation
   from AC5's literal reset rule, pinned by
   ``test_window_reset_breaks_agent_error_chain``). Under a granted N>1
   it is the same counter in completion order — environment drift is a
   *global* suspicion, so two agent errors with no clean completion in
   between abort the run either way (a late-arriving trigger preempts the
   backoff sleep via the (budget_exhausted, abort) transition). The run
   ends in ``aborted`` with a reason; no further instance starts;
   in-flight sessions (N>1) finish.

Acceptance criteria (issue #15, PLAN §2.4–§2.7) and where they live:

6. **Resume from ``run-<id>.jsonl``** — a partial line at EOF is
   dropped, not fatal; no instance runs twice → :func:`load_checkpoint`
   (the EOF torn-line rule; a mid-file corruption is fail-loud),
   :class:`JsonlCheckpoint` (one fsync'd line per finished instance —
   the line is on disk before the runner proceeds; on open it also
   runs the *write-side* half of the rule — ``_repair_checkpoint_file``
   — truncating a torn tail / newline-completing a complete record
   missing its newline, so a resumed run never glues its first record
   onto the torn fragment), :func:`resume_queue` / :func:
   `resume_from_checkpoint` (the pass@1 retry-class rules).
7. **pass@1 retry classes (PLAN §2.5)** → the :data:`NEVER_RERUN_
   STATUSES` / :data:`RETRY_STATUSES` split, enforced by
   :func:`resume_queue`: ``resolved`` / ``failed_test`` / ``timeout`` /
   ``no_patch`` — and ``agent_error`` (a legitimate agent outcome: a
   policy refusal is *terminal*, PLAN §3.4; the systemic case is the
   2-consecutive abort, AC5, not a per-instance re-run) — are **never
   re-run**; ``quota_limited`` / ``eval_error`` / a host-crash partial
   state (an instance with *no* line — the line is written only on
   completion) are **retried — the only retry class**.
   ``preflight_failed`` sits in the retry class too: an infra state that
   burned no session (PLAN §2.7's "stop before burning a session"
   exists precisely so the re-run is free — the check re-runs on resume),
   so it cannot inflate pass@1.
8. **Startup model gate (PLAN §2.4)** → :func:`gate_cost_tier` +
   :func:`parse_models_list` (fail-closed): the allowlist is *dynamic*
   (recomputed from ``devin models list --format json`` every startup);
   a configured paid (or unverified/absent-tier) model is refused; a
   model missing from the list is refused (its tier cannot be
   verified); any JSON *shape* change is a typed refusal
   (:class:`ModelGateError`; the CLI exits 2 and the batch never
   starts).
   A paid override requires **both** an explicit ``allow_paid`` config
   **and** a positive numeric token budget (the CLI ``--allow-paid`` +
   ``--token-budget``).
9. **Zero-quota pre-flight (PLAN §2.7)** → :class:`Preflight` +
   :func:`run_preflight_checks` (ordered, short-circuiting): ``devin
   auth status`` ok → Docker daemon responsive (bounded — a hung daemon
   is a failure, not a wait) → instance image present (per instance) →
   free disk ≥ **2 × the largest expected image** (the PLAN §7 floor).
   Run before **every batch** (the per-window batch phase of
   :meth:`Runner.run` — a new quota window is a new batch) **and**
   before **each instance**. Any failure raises :class:`PreflightFailed`
   naming the leg; the runner records the instance ``preflight_failed``
   (the reason lands in the JSONL meta; a batch-phase failure records an
   ``instance_id: null`` marker line that consumes no queue slot),
   releases the budget claim (no session was ever started), and aborts
   the run (``aborted`` — an infra failure is not a quota window to
   sleep for; the operator fixes it and resumes, where the instance is
   retry-eligible, AC2/AC7).

Session contract (the pluggable seam the adapter fills):

* ``session_factory(instance_id) -> AgentSession`` — builds the session
  object (no live process yet).
* ``session.start()`` — starts the agent session; the session is *live*
  for overlap purposes from ``start()`` to ``close()``, inside the guard
  slot.
* ``session.run() -> str`` — drives the instance to a **terminal** status
  from :data:`TERMINAL_STATUSES`. An unknown status raises
  :class:`RunnerProtocolError` (fail-loud — the mock_devin strict-CLI
  philosophy: a contract mistake surfaces, it is not absorbed).
* ``session.close()`` — idempotent, best-effort (PLAN §1 hygiene:
  ``AcpSession``'s context-manager guarantees close on all paths).
* Factory/``start()`` failure = host/infra failure: the budget claim is
  **released** (no session ever started — it must not consume the daily
  budget) and the exception propagates. ``run()``/``close()`` failure = a
  *partial* session: the budget claim **stands** (the work was
  consumed), the exception propagates the same way. Both are PLAN §2.5
  host-crash partial states — the retry class is the operator's / WP3b
  resume's, never a silent in-run retry.

Reporter contract: callbacks may be invoked concurrently under a granted
N>1 (one per finishing worker); the default :class:`RecordingReporter` is
safe under the GIL (list appends). Custom reporters should synchronize.
"""

from __future__ import annotations

import abc
import argparse
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Final, Iterable, Protocol, Sequence

__all__ = [
    "TERMINAL_STATUSES",
    "AGENT_ERROR",
    "QUOTA_LIMITED",
    "ABORT_AFTER_CONSECUTIVE_AGENT_ERRORS",
    "NEVER_RERUN_STATUSES",
    "RETRY_STATUSES",
    "resume_queue",
    "resume_from_checkpoint",
    "load_checkpoint",
    "JsonlCheckpoint",
    "ModelGateError",
    "ModelGateResult",
    "gate_cost_tier",
    "parse_models_list",
    "fetch_models_list",
    "PreflightFailed",
    "Preflight",
    "run_preflight_checks",
    "BackoffState",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "transition",
    "InvalidTransition",
    "BudgetConfig",
    "DailyBudget",
    "OverlapGuard",
    "OverlapViolation",
    "ProcessLock",
    "ProcessLockBusy",
    "AgentSession",
    "RunResult",
    "Reporter",
    "RecordingReporter",
    "Runner",
    "RunnerError",
    "RunnerProtocolError",
    "RunnerConfigError",
    "ConcurrencyRefusedError",
    "next_quota_window",
    "check_concurrency_flag",
    "main",
]

# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class RunnerError(Exception):
    """Base class for every typed failure the runner can produce."""


class RunnerProtocolError(RunnerError):
    """A session/adapter broke the runner contract (e.g. an unknown
    terminal status, a guard release without an acquire)."""


class RunnerConfigError(RunnerError):
    """A budget/CLI configuration value is invalid."""


class ConcurrencyRefusedError(RunnerError):
    """``--concurrency N>1`` was requested without ``--i-know-about-quota``
    (PLAN §2.1 — one session at a time by default)."""


class OverlapViolation(RunnerError):
    """An agent session start was attempted while the overlap guard is at
    its cap — the "no two sessions overlap" assert (PLAN §2.1)."""


class InvalidTransition(RunnerError):
    """A state-machine event that is not legal in the current state."""

    def __init__(self, state: "BackoffState", event: str) -> None:
        super().__init__(f"invalid transition: {state.value} --{event}-->")
        self.state = state
        self.event = event


class ProcessLockBusy(RunnerError):
    """A sibling runner process already holds the process-level lock."""

    def __init__(self, path: str) -> None:
        super().__init__(f"process lock is held by a sibling runner: {path}")
        self.path = path


class ModelGateError(RunnerError):
    """The startup model gate refused the batch (fail-closed, PLAN
    §2.4 / issue #15 AC3): a paid model without a valid override, a
    model that cannot be verified against ``devin models list --format
    json``, or a JSON shape change ("If the startup check fails or the
    JSON shape changes, the runner aborts the batch")."""


class PreflightFailed(RunnerError):
    """A pre-flight health check failed (PLAN §2.7: "stop before
    burning a session"). :attr:`check` names the failed leg (auth /
    docker_daemon / docker_image / disk) for the JSONL reason."""

    def __init__(self, check: str, detail: str) -> None:
        super().__init__(f"preflight failed ({check}): {detail}")
        self.check = check
        self.detail = detail


# ---------------------------------------------------------------------------
# terminal status taxonomy (PLAN §2.5 / WP5)
# ---------------------------------------------------------------------------

#: The terminal instance statuses the runner understands (the WP5 failure
#: taxonomy). ``agent_error`` feeds the drift-suspicion abort counter;
#: ``quota_limited`` **is** the PLAN §2.3 rate-limit signal — a session
#: that terminates with it stops the batch. The others (``resolved``,
#: ``failed_test``, ``timeout``, ``no_patch``, ``eval_error``,
#: ``preflight_failed``) are recorded and the queue continues.
TERMINAL_STATUSES: Final[frozenset] = frozenset({
    "resolved",
    "failed_test",
    "timeout",
    "no_patch",
    "quota_limited",
    "agent_error",
    "eval_error",
    "preflight_failed",
})

AGENT_ERROR = "agent_error"
QUOTA_LIMITED = "quota_limited"

#: Consecutive ``agent_error`` terminal statuses that abort the run
#: (environment-drift suspicion, PLAN §5 WP3 / issue #14 AC5).
ABORT_AFTER_CONSECUTIVE_AGENT_ERRORS = 2


# ---------------------------------------------------------------------------
# the backoff state machine (pure, table-driven)
# ---------------------------------------------------------------------------


class BackoffState(str, Enum):
    """The budget/backoff states of a run (issue #14 AC3)."""

    RUNNING = "running"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SLEEPING = "sleeping"  # until the next quota reset window
    RESUME = "resume"  # window reached; budget counters reset next
    ABORTED = "aborted"  # terminal: 2 consecutive agent_error (drift)
    DONE = "done"  # terminal: queue drained


#: state-machine events.
EVENT_BUDGET_EXHAUSTED = "budget_exhausted"  # local daily cap reached
EVENT_RATE_LIMITED = "rate_limited"  # external quota signal (a quota_limited session)
EVENT_ABORT = "abort"  # environment-drift suspicion
EVENT_QUEUE_EMPTY = "queue_empty"  # nothing left to schedule
EVENT_SLEEP_START = "sleep_start"  # begin sleeping to the next window
EVENT_WAKE = "wake"  # the window is reached
EVENT_RESET = "reset"  # resume: the new window's budget counters reset

#: Terminal states — no events are legal out of them.
TERMINAL_STATES: Final[frozenset] = frozenset({
    BackoffState.ABORTED,
    BackoffState.DONE,
})

#: The full transition table (issue #14 AC3). Everything *not* listed is
#: invalid and raises :class:`InvalidTransition` — including every event
#: out of the terminal states. ``(budget_exhausted, abort)`` is legal:
#: under a granted N>1 a late-arriving second ``agent_error`` (the
#: drift-suspicion trigger) can land while another worker has already
#: moved the state to ``budget_exhausted`` — the abort preempts the
#: backoff sleep (an environment-drift suspicion beats a quota wait).
#: ``(budget_exhausted, queue_empty)`` is a defensive entry: the
#: runner's own logic detects an empty queue before entering
#: ``budget_exhausted`` (so its done normally lands via
#: ``(running, queue_empty)``), but a concurrent N>1 worker can drain
#: the queue between one worker's exhausted check and the state drain,
#: and the table must answer that race with done (no pointless sleep)
#: rather than an invalid transition.
TRANSITIONS: Final[dict] = {
    (BackoffState.RUNNING, EVENT_BUDGET_EXHAUSTED): BackoffState.BUDGET_EXHAUSTED,
    (BackoffState.RUNNING, EVENT_RATE_LIMITED): BackoffState.BUDGET_EXHAUSTED,
    (BackoffState.RUNNING, EVENT_ABORT): BackoffState.ABORTED,
    (BackoffState.RUNNING, EVENT_QUEUE_EMPTY): BackoffState.DONE,
    (BackoffState.BUDGET_EXHAUSTED, EVENT_SLEEP_START): BackoffState.SLEEPING,
    (BackoffState.BUDGET_EXHAUSTED, EVENT_QUEUE_EMPTY): BackoffState.DONE,
    (BackoffState.BUDGET_EXHAUSTED, EVENT_ABORT): BackoffState.ABORTED,
    (BackoffState.SLEEPING, EVENT_WAKE): BackoffState.RESUME,
    (BackoffState.RESUME, EVENT_RESET): BackoffState.RUNNING,
}


def transition(state: BackoffState, event: str) -> BackoffState:
    """One state-machine step (pure — no I/O, no clock).

    Raises :class:`InvalidTransition` for a (state, event) pair that is
    not in :data:`TRANSITIONS`.
    """
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(state, event) from None


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetConfig:
    """Per-run quota budget (PLAN §2.2; defaults per WP0 §4).

    * ``max_sessions_per_day`` — conservative default **10** (WP0 §4:
      calibrate in Pilot A).
    * ``max_wall_per_session`` — default **45 min** (PLAN §2.2); the
      runner-level daily wall budget is this × the session cap (the
      per-*instance* wall cap is enforced by the adapter/``AcpSession``,
      WP1).
    * ``daily_pause`` — default **12 h** between run batches (PLAN
      §2.2). The *actual* sleep is **not 12 h verbatim**: it aligns to
      the quota reset boundary (next UTC midnight — WP0 §4 "calendar
      basis" reset, the fine-grained leg), so its duration is the
      remainder of the current window — a signal at 23:00 UTC sleeps ~1
      h, one at 00:01 ~24 h. The 12 h nominal is the default config, not
      a guarantee of the realized pause. The weekly budget is a hard
      outer cap that stays config/operational (not a state-machine leg in
      WP3a).
    """

    max_sessions_per_day: int = 10
    max_wall_per_session: float = 45 * 60.0
    daily_pause: float = 12 * 3600.0

    def __post_init__(self) -> None:
        if not isinstance(self.max_sessions_per_day, int) \
                or isinstance(self.max_sessions_per_day, bool) \
                or self.max_sessions_per_day < 1:
            raise RunnerConfigError(
                f"max_sessions_per_day must be an int >= 1, "
                f"got {self.max_sessions_per_day!r}"
            )
        if isinstance(self.max_wall_per_session, bool) \
                or not (isinstance(self.max_wall_per_session, (int, float))
                       and self.max_wall_per_session > 0):
            raise RunnerConfigError(
                f"max_wall_per_session must be a number > 0, "
                f"got {self.max_wall_per_session!r}"
            )
        if isinstance(self.daily_pause, bool) \
                or not (isinstance(self.daily_pause, (int, float))
                       and self.daily_pause > 0):
            raise RunnerConfigError(
                f"daily_pause must be a number > 0, "
                f"got {self.daily_pause!r}"
            )


class DailyBudget:
    """The per-window (per-day) budget tracker.

    Exhausted when the session count reaches ``max_sessions_per_day``
    **or** the wall time spent in sessions reaches
    ``max_wall_per_session × max_sessions_per_day``. Reset on ``resume``
    (a new window is a fresh budget).
    """

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config
        self.sessions_used = 0
        self.wall_used_s = 0.0

    @property
    def config(self) -> BudgetConfig:
        return self._config

    @property
    def wall_budget_s(self) -> float:
        return self._config.max_wall_per_session \
            * self._config.max_sessions_per_day

    def exhausted(self) -> bool:
        return (
            self.sessions_used >= self._config.max_sessions_per_day
            or self.wall_used_s >= self.wall_budget_s
        )

    def try_claim(self) -> bool:
        """Atomically claim one session slot; False when exhausted."""
        if self.exhausted():
            return False
        self.sessions_used += 1
        return True

    def release_claim(self) -> None:
        """Give back a claim for a session that never started."""
        if self.sessions_used < 1:
            raise RunnerProtocolError(
                "release_claim with no outstanding claim"
            )
        self.sessions_used -= 1

    def record_wall(self, wall_s: float) -> None:
        self.wall_used_s += wall_s

    def reset(self) -> None:
        self.sessions_used = 0
        self.wall_used_s = 0.0


# ---------------------------------------------------------------------------
# the dynamic cost-tier model gate (WP3b, issue #15 AC3, PLAN §2.4)
# ---------------------------------------------------------------------------


def parse_models_list(models_json: object) -> "dict[str, str]":
    """The strict, fail-closed shape check of ``devin models list
    --format json`` (the WP0 capture, ``notes/models-list.json``):
    ``{"families": [{ "variants": [{ "model_uid": str, "cost_tier":
    str? }, …] }, …]}``.

    Returns ``{model_uid: cost_tier}`` — a variant with an *absent*
    ``cost_tier`` (the WP0 ``adaptive`` router) maps to ``"unknown"``
    (an unverified tier is **not** Free — the gate keys on ``cost_tier``,
    never on model names/prefixes). Any structural deviation (wrong top
    level, missing/wrong-typed ``families`` / ``variants`` / 
    ``model_uid``, a non-string ``cost_tier``) raises :class:`
    ModelGateError` — a shape change is a refusal, not an interpretation
    (fail-closed, issue #15 AC3). Duplicated uids: the last occurrence
    wins (the file order's record of truth).
    """
    if not isinstance(models_json, dict):
        raise ModelGateError(
            f"shape change: top level is not a JSON object "
            f"(got {type(models_json).__name__}) — fail-closed"
        )
    if "families" not in models_json \
            or not isinstance(models_json["families"], list):
        raise ModelGateError(
            "shape change: `families` is missing or not a list — "
            "fail-closed"
        )
    out: dict = {}
    for fam in models_json["families"]:
        if not isinstance(fam, dict):
            raise ModelGateError(
                f"shape change: a family entry is not a JSON object "
                f"(got {type(fam).__name__}) — fail-closed"
            )
        variants = fam.get("variants")
        if not isinstance(variants, list):
            raise ModelGateError(
                "shape change: a family's `variants` is missing or not a "
                "list — fail-closed"
            )
        for v in variants:
            if not isinstance(v, dict):
                raise ModelGateError(
                    f"shape change: a variant entry is not a JSON object "
                    f"(got {type(v).__name__}) — fail-closed"
                )
            uid = v.get("model_uid")
            if not isinstance(uid, str):
                raise ModelGateError(
                    f"shape change: a variant's `model_uid` is missing or "
                    f"not a string (got {uid!r}) — fail-closed"
                )
            tier = v.get("cost_tier")
            if tier is not None and not isinstance(tier, str):
                raise ModelGateError(
                    f"shape change: a variant's `cost_tier` is not a string "
                    f"(got {tier!r}) — fail-closed"
                )
            out[uid] = tier if tier is not None else "unknown"
    return out


@dataclass(frozen=True)
class ModelGateResult:
    """The startup gate's verdict for one configured model."""

    allowed: bool
    model: str
    cost_tier: "str | None"  # the listed tier ("unknown" if absent; None if unlisted)
    free_models: "tuple"  # the selectable set under the free policy
    reason: "str | None"  # the refusal reason (None when allowed)
    paid_override: bool = False  # True only when a paid model was granted


def _valid_token_budget(budget: object) -> bool:
    """The paid override's token-budget leg: a positive, **finite**
    number — a bool is not a budget (the house validation style), and
    ``inf`` is not a budget (an unbounded token budget is not a
    budget)."""
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
            or budget <= 0:
        return False
    return isinstance(budget, int) or math.isfinite(budget)


def gate_cost_tier(models_json: object, configured_model: str, *,
                  allow_paid: bool = False,
                  token_budget: "int | float | None" = None
                  ) -> ModelGateResult:
    """The startup model gate (PLAN §2.4, issue #15 AC3) — pure.

    * ``cost_tier == "Free"`` → allowed. The allowlist is *dynamic*:
      it is recomputed from the live list on every startup; nothing is
      hardcoded (PLAN §2.4: ``swe-1-6``/``swe-1-7`` paid, all
      ``*-lightning`` variants paid — verified per startup, not per
      release).
    * a listed **paid** tier (or an unverified/absent tier — fail-
      closed) → refused — unless **both** ``allow_paid`` (the explicit
      config override) **and** a valid positive token budget are given
      (PLAN §2.4: "A paid model requires an explicit config override
      **plus** a token budget check").
    * a model **missing from the list** → refused (its tier cannot be
      verified — the override cannot bless it either).
    * a JSON **shape change** → refused (``parse_models_list`` raises
      :class:`ModelGateError` before this returns — the batch never
      starts).
    """
    tiers = parse_models_list(models_json)
    free = tuple(sorted(uid for uid, tier in tiers.items() if tier == "Free"))
    tier = tiers.get(configured_model)
    if tier is None:
        return ModelGateResult(
            False, configured_model, None, free,
            f"{configured_model!r} is not in the models list — its cost "
            "tier cannot be verified; fail-closed (PLAN §2.4)",
        )
    if tier == "Free":
        return ModelGateResult(True, configured_model, tier, free, None)
    # A paid (or unverified/absent) tier.
    if allow_paid and _valid_token_budget(token_budget):
        return ModelGateResult(True, configured_model, tier, free, None,
                              paid_override=True)
    if allow_paid:
        return ModelGateResult(
            False, configured_model, tier, free,
            f"{configured_model!r} is {tier!r} (not Free) and the paid "
            "override is invalid: a positive numeric token budget is "
            f"required (got {token_budget!r}) — PLAN §2.4",
        )
    return ModelGateResult(
        False, configured_model, tier, free,
        f"{configured_model!r} is {tier!r} (not Free) under the free "
        "policy — refused; a paid model requires an explicit override "
        "(allow_paid) **plus** a token budget (PLAN §2.4)",
    )


def fetch_models_list(devin_bin: str = "devin", timeout_s: float = 30.0,
                     run: "Callable | None" = None) -> object:
    """Run ``devin models list --format json`` and parse it (the gate's
    live input). **Fail-closed**: an unrunnable binary, a non-zero
    exit, or unparseable output raises :class:`ModelGateError` — the
    batch is refused, never started on an unverified allowlist. 
    ``run`` is injectable (tests stub the subprocess; the default is
    ``subprocess.run``)."""
    do_run = run if run is not None else subprocess.run
    try:
        proc = do_run([devin_bin, "models", "list", "--format", "json"],
                     capture_output=True, timeout=timeout_s)
    except Exception as exc:
        raise ModelGateError(
            f"fail-closed: `devin models list --format json` could not be "
            f"run: {type(exc).__name__}: {exc}"
        ) from exc
    if getattr(proc, "returncode", 1) != 0:
        tail = getattr(proc, "stderr", b"") or b""
        if isinstance(tail, str):
            tail = tail.encode("utf-8", "replace")
        raise ModelGateError(
            f"fail-closed: `devin models list --format json` exited "
            f"{proc.returncode}: "
            f"{tail[-200:].decode('utf-8', 'replace').strip()!r}"
        )
    try:
        # ``getattr`` (not direct attribute access): a stubbed ``run``
        # returning an object without a ``stdout`` attribute must also
        # be a fail-closed typed refusal, not a leaked AttributeError
        # (None → ``json.loads`` TypeError — both wrapped below; the
        # seam exists for tests).
        return json.loads(getattr(proc, "stdout", None))
    except (ValueError, TypeError) as exc:
        # ValueError covers JSONDecodeError (a str subclass); TypeError
        # covers a non-str/bytes stdout (e.g. None from a stubbed
        # ``run`` — the seam exists for tests) — fail-closed either way.
        raise ModelGateError(
            f"fail-closed: the models list output is not valid JSON: {exc}"
        ) from exc

# ---------------------------------------------------------------------------
# overlap guard + process lock (PLAN §2.1)
# ---------------------------------------------------------------------------


class OverlapGuard:
    """The "no two agent sessions overlap" assert (issue #14 AC1).

    Every session's start→run→close executes inside :meth:`acquire` /
    :meth:`release`. A :meth:`acquire` while the guard is at its cap
    raises :class:`OverlapViolation` — **fail loud, do not wait**: the
    default cap is 1 (sequential policy), a granted N>1 cap is N.
    :attr:`max_observed` is the runner's own high-water mark for the
    invariant (tests probe it independently as well).
    """

    def __init__(self, max_concurrency: int = 1) -> None:
        if max_concurrency < 1:
            raise RunnerConfigError(
                f"max_concurrency must be >= 1, got {max_concurrency!r}"
            )
        self._max = max_concurrency
        self._lock = threading.Lock()
        self._active = 0
        self._max_observed = 0

    @property
    def max_concurrency(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def max_observed(self) -> int:
        with self._lock:
            return self._max_observed

    def acquire(self) -> None:
        with self._lock:
            if self._active >= self._max:
                raise OverlapViolation(
                    f"session start attempted with the guard at its cap "
                    f"({self._active}/{self._max}) — the sequential policy "
                    f"asserts no two agent sessions overlap (PLAN §2.1)"
                )
            self._active += 1
            self._max_observed = max(self._max_observed, self._active)

    def release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise OverlapViolation(
                    "release without an outstanding acquire"
                )
            self._active -= 1


class ProcessLock:
    """Cross-process guard (PLAN §2.1 "process-level lock").

    The :class:`OverlapGuard` covers one runner's *own* scheduling; this
    covers *sibling* runner processes on the same host — two runner
    processes must never run agent sessions at the same time (each one
    is already burning its own quota budget). ``flock`` is held for the
    duration of one :meth:`Runner.run` call (released in a finally).
    POSIX (the benchmark targets Linux — the sweb.eval images).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._fd: Any = None

    @property
    def path(self) -> str:
        return self._path

    def acquire(self) -> None:
        d = os.path.dirname(self._path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProcessLockBusy(self._path) from exc
        except OSError as exc:
            os.close(fd)
            raise RunnerError(
                f"process lock failed on {self._path}: {exc}"
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------------
# checkpoint / resume with pass@1-correct retry semantics
# (WP3b, issue #15 AC1 + AC2, PLAN §2.5)
# ---------------------------------------------------------------------------

#: The terminal statuses that are **never re-run** on resume (PLAN
#: §2.5). ``resolved`` / ``failed_test`` / ``timeout`` / ``no_patch``
#: are legitimate agent outcomes — re-running them would turn pass@1
#: into a filtered pass@k and inflate scores (the PLAN §2.5 / risk-
#: table guard). ``agent_error`` is terminal too: a policy refusal is a
#: *terminal* ``agent_error`` for that instance (PLAN §3.4 — no silent
#: model switch, no retry); the systemic environment-drift case is the
#: 2-consecutive abort (issue #14 AC5), not a per-instance re-run.
NEVER_RERUN_STATUSES: Final[frozenset] = frozenset({
    "resolved",
    "failed_test",
    "timeout",
    "no_patch",
    "agent_error",
})

#: The **only** retry class (PLAN §2.5): states that interrupted
#: *before* a legitimate agent outcome, so nothing was burned and the
#: re-run is free for pass@1. ``quota_limited`` — the session was
#: refused; ``eval_error`` — a Docker/harness failure, not an agent
#: outcome; ``preflight_failed`` — an infra check failed *before* the
#: session (PLAN §2.7's "stop before burning a session" exists precisely
#: so this re-run is free; the check re-runs before every batch and
#: every instance on resume). A host-crash partial state is the same
#: class with no line at all — the instance is simply absent from the
#: checkpoint (the line is written only on completion) and therefore
#: re-run.
RETRY_STATUSES: Final[frozenset] = frozenset({
    "quota_limited",
    "eval_error",
    "preflight_failed",
})


def resume_queue(instances: Sequence[str],
                records: "Iterable[dict]") -> "tuple[list, dict]":
    """The pass@1-correct resume decision (pure, issue #15 AC1 + AC2).

    Given the full instance list (queue order) and the checkpoint
    records, returns ``(to_run, skipped)``:

    * an instance with a :data:`NEVER_RERUN_STATUSES` line → **skipped**
      (never re-run).
    * an instance with a :data:`RETRY_STATUSES` line → **re-run** (the
      only retry class).
    * an instance with **no line** (never ran, or a host-crash partial
      state) → **re-run**.
    * a record with ``instance_id: null`` (a batch-level pre-flight
      marker) → ignored (it consumes no queue slot).
    * a record whose ``instance_id`` is not in ``instances`` (a stale
      line from an earlier run configuration) → ignored.
    * a record with a status outside :data:`TERMINAL_STATUSES` →
      :class:`RunnerProtocolError` (fail-loud, the WP3a contract
      philosophy).
    * a **duplicated** line for one instance (a double-write) → the
      *last* line wins; the instance appears at most once in ``to_run``
      ("no instance runs twice", issue #15 AC1).
    """
    known = set(instances)
    last: dict = {}
    for rec in records:
        if not isinstance(rec, dict):
            raise RunnerProtocolError(
                f"checkpoint record is not a dict (got "
                f"{type(rec).__name__})"
            )
        iid = rec.get("instance_id")
        if iid is None:
            continue
        status = rec.get("status")
        if status not in TERMINAL_STATUSES:
            raise RunnerProtocolError(
                f"checkpoint record for {iid!r} carries unknown status "
                f"{status!r} (valid: {sorted(TERMINAL_STATUSES)})"
            )
        if iid in known:
            last[iid] = status  # duplicate: the last line wins
    to_run: list = []
    skipped: dict = {}
    for iid in instances:
        status = last.get(iid)
        if status is None or status in RETRY_STATUSES:
            to_run.append(iid)
        else:
            skipped[iid] = status
    return to_run, skipped


def _scan_checkpoint_lines(raw: str) -> "tuple[list, bool]":
    """The shared line scan (:func:`load_checkpoint` + the write-side
    repair). Returns ``(records, dropped_last)``. A non-record line
    *anywhere but the last* raises :class:`RunnerProtocolError`
    (fail-loud: a mid-file line was fsync'd complete by the writer —
    garbage there is not a kill artifact); a non-record line at EOF is
    a torn write → dropped (not fatal)."""
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]  # the final newline is not a line
    records: list = []
    dropped = False
    for i, line in enumerate(lines):
        is_last = i == len(lines) - 1
        if line.strip() == "":
            if is_last:
                continue  # a degenerate empty EOF — not a torn record
            raise RunnerProtocolError(
                f"corrupted checkpoint line {i + 1} (blank) — not a kill "
                f"artifact; fail-loud"
            )
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if is_last:
                dropped = True
                continue
            raise RunnerProtocolError(
                f"corrupted checkpoint line {i + 1} (not JSON) — not a "
                f"kill artifact; fail-loud"
            )
        if not _is_checkpoint_record(obj):
            if is_last:
                dropped = True
                continue
            raise RunnerProtocolError(
                f"corrupted checkpoint line {i + 1} (not a record) — not a "
                f"kill artifact; fail-loud"
            )
        records.append(obj)
    return records, dropped


def load_checkpoint(path: str) -> "tuple[list, bool]":
    """Read a progress file ``run-<id>.jsonl`` → ``(records,
    dropped_last)``.

    The **last** line that is not a complete record (a truncated JSON,
    or a complete JSON with the wrong shape) is **dropped, not fatal**
    — a kill mid-write leaves a torn line at EOF (issue #15 AC1). A
    corrupted line *anywhere else* raises :class:`RunnerProtocolError`
    (fail-loud). A missing file is a fresh run (no records, nothing
    dropped).

    The *write-side* half of the rule lives in :class:`JsonlCheckpoint`
    (:func:`_repair_checkpoint_file` on open): the reader drops the
    torn tail for the resume *decision*; the writer truncates (or
    newline-completes) it before appending, so a resumed run never
    glues its first record onto the torn fragment.
    """
    if not os.path.exists(path):
        return [], False
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return _scan_checkpoint_lines(raw)


def _is_checkpoint_record(obj: object) -> bool:
    """The record shape (a kill can tear the line at any byte offset, so
    a torn *shape* at EOF is indistinguishable from a torn JSON — both
    are the drop zone)."""
    if not isinstance(obj, dict):
        return False
    if "instance_id" not in obj or "status" not in obj:
        return False
    iid = obj["instance_id"]
    if iid is not None and not isinstance(iid, str):
        return False
    return obj["status"] in TERMINAL_STATUSES


def _repair_checkpoint_file(path: str) -> None:
    """The write-side half of the EOF torn-line rule (issue #15 AC1 —
    "no instance runs twice", the review-found blocker): before the
    writer appends, the file must end on a clean line boundary, or the
    first appended record glues onto the torn tail (a glued line is
    mid-file corruption on the next load — or a silently dropped
    *completed* record, i.e. a re-run of a finished instance).

    * the file is clean (the last line is a complete record, or the
      file is empty) → nothing to do.
    * the **last line** (whether or not newline-terminated — the
      reader's contract blesses a non-record *last* line with a
      trailing newline as a droppable torn tail too, and leaving such
      a line in place would have the first appended record glue it
      into a *mid-file* line) is a **complete record** missing only
      the trailing newline (a kill after the JSON but before the
      ``"\\n"``) → complete it (write the newline — the record is
      preserved, and it is what the reader keeps).
    * the last line is a **non-record** (a torn fragment — truncated
      JSON / wrong shape, indistinguishable from a torn write — or a
      blank / non-JSON line) → **truncate** it to the last clean
      boundary. (Appending a ``"\\n"`` would *not* fix it — the
      fragment would remain a mid-file line and still fail on the
      next load; it must be removed.)
    * a corrupted line *anywhere else* → :class:
      `RunnerProtocolError` (fail-loud, before the file is touched).

    The truncation uses ``ftruncate`` (open ``"r+b"``) — atomic w.r.t.
    the preserved prefix: a kill (or a write error, e.g. ENOSPC) can
    no longer destroy previously-fsync'd durable records (the round-1
    ``open("w")`` rewrite truncated to 0 on open — a silent pass@1
    violation; the round-2 fix).
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if raw == "":
        return  # nothing to repair
    # Fail-loud on a mid-file corruption before touching the file —
    # even when the tail is clean (a corrupted line anywhere else was
    # fsync'd complete; garbage there is not a kill artifact).
    _scan_checkpoint_lines(raw)
    # The kill zone is the **last line** — whether or not it is
    # newline-terminated (see the docstring; a newline-terminated
    # non-record last line is droppable per the reader's contract and
    # must not survive the repair).
    body = raw[:-1] if raw.endswith("\n") else raw
    last_line = body.rsplit("\n", 1)[-1]
    boundary = len(raw) - len(last_line) - (1 if raw.endswith("\n") else 0)
    complete = False
    if last_line.strip():
        try:
            complete = _is_checkpoint_record(json.loads(last_line))
        except json.JSONDecodeError:
            pass
    if complete:
        if raw.endswith("\n"):
            return  # clean — a complete record, newline-terminated
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")  # complete the record — it survives the resume
        return
    # A non-record last line — truncate to the last clean boundary.
    with open(path, "r+b") as f:
        f.truncate(boundary)  # ftruncate — the preserved prefix is intact
        f.flush()
        os.fsync(f.fileno())


def resume_from_checkpoint(path: str,
                          instances: Sequence[str]) -> "tuple[list, dict]":
    """The convenience path: :func:`load_checkpoint` then
    :func:`resume_queue`. A missing file = a fresh run (everything is
    re-run). Pair it with a fresh :class:`JsonlCheckpoint` on the same
    path for the resume run — its open repairs (truncates /
    newline-completes) the torn tail before the first append.
    """
    records, _ = load_checkpoint(path)
    return resume_queue(instances, records)
# ---------------------------------------------------------------------------
# session + reporter contracts
# ---------------------------------------------------------------------------


class AgentSession(abc.ABC):
    """One instance's agent session (the seam the adapter fills).

    The runner drives ``start() -> run() -> close()`` inside an
    :class:`OverlapGuard` slot; the session is *live* (and therefore
    overlap-relevant) from ``start()`` to ``close()``.
    """

    @abc.abstractmethod
    def start(self) -> None:
        """Start the agent session. May raise (host/infra failure)."""

    @abc.abstractmethod
    def run(self) -> str:
        """Drive the instance to a terminal status (TERMINAL_STATUSES).

        May raise (partial session — host-crash class, PLAN §2.5).
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Tear down; idempotent and best-effort (PLAN §1 hygiene)."""


class Reporter(Protocol):
    """The runner's outward hooks (WP3b's checkpoint JSONL implements
    these; the default :class:`RecordingReporter` records in memory)."""

    def run_started(self, meta: dict) -> None:
        """The run begins (meta: concurrency, budget, queue size)."""

    def record_instance(self, instance_id: "str | None", status: str,
                       meta: dict) -> None:
        """An instance reached a terminal status (the ``quota_limited``
        "mark" of issue #14 AC4). ``instance_id=None`` is a batch-level
        marker (a pre-flight failure before the first instance, WP3b) —
        it is recorded but consumes no queue slot on resume."""

    def checkpoint(self, reason: str, meta: dict) -> None:
        """Persist progress (WP3b: the JSONL flush). Fired on the
        rate-limit stop, before the sleep to the next window."""

    def run_finished(self, result: "RunResult") -> None:
        """The run ended (not called on a propagated host-crash)."""


class RecordingReporter:
    """The default reporter: appends every event to :attr:`events` in
    call order (list appends — GIL-safe under a granted N>1)."""

    def __init__(self) -> None:
        self.events: list = []

    def run_started(self, meta: dict) -> None:
        self.events.append(("run_started", dict(meta)))

    def record_instance(self, instance_id: "str | None", status: str,
                       meta: dict) -> None:
        self.events.append(("instance", instance_id, status, dict(meta)))

    def checkpoint(self, reason: str, meta: dict) -> None:
        self.events.append(("checkpoint", reason, dict(meta)))

    def run_finished(self, result: "RunResult") -> None:
        self.events.append(("run_finished", result.final_state))


class JsonlCheckpoint:
    """The progress file ``run-<id>.jsonl`` (PLAN §2.5) — one line per
    finished instance: ``{"instance_id", "status", "meta" (the artifact
    paths / usage the adapter passes in), "ts"}``.

    Implements the WP3a :class:`Reporter` hooks: ``record_instance``
    appends the line and makes it durable (write + flush + fsync — the
    line is on disk *before* the runner proceeds: the WP2
    artifact-atomicity guarantee, "files are complete on disk before
    the JSONL summary line is appended"); ``checkpoint`` is a no-op
    (the per-line flush *is* the checkpoint flush — a rate-limit stop
    checkpoint therefore loses nothing); ``run_started`` /
    ``run_finished`` are run-level events, not instance lines, and are
    not written.

    ``instance_id=None`` marks a batch-level pre-flight failure (before
    the first instance — it consumes no queue slot; the resume logic
    ignores the line, the reason stays in the file for the report).

    Durable across kills: append mode (a resume appends to the same
    file); every line is fsync'd.

    **The write-side half of the EOF torn-line rule** (issue #15 AC1,
    the review-found blocker): on open, :func:`_repair_checkpoint_file`
    repairs the tail before the first append — a torn fragment is
    truncated to the last clean boundary, and a complete record missing
    only its trailing newline is completed — so a resumed run never
    glues its first record onto the torn tail (which would be mid-file
    corruption on the next load, or a silently dropped *completed*
    record — a re-run of a finished instance, a pass@1 violation).
    """

    def __init__(self, path: str,
                 clock: "Callable[[], float]" = time.time,
                 _file: "Any | None" = None) -> None:
        self._path = path
        self._clock = clock
        if _file is None:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            _repair_checkpoint_file(path)  # never append onto a torn tail
            _file = open(path, "a", encoding="utf-8")
        self._file = _file
        self._closed = False

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._file.close()

    def __enter__(self) -> "JsonlCheckpoint":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record_instance(self, instance_id: "str | None", status: str,
                       meta: dict) -> None:
        if status not in TERMINAL_STATUSES:
            raise RunnerProtocolError(
                f"record_instance with unknown status {status!r} "
                f"(valid: {sorted(TERMINAL_STATUSES)})"
            )
        line = {"instance_id": instance_id, "status": status,
                "meta": dict(meta), "ts": self._clock()}
        self._file.write(json.dumps(line) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def checkpoint(self, reason: str, meta: dict) -> None:
        # The per-line flush is the checkpoint: everything that finished
        # is already on disk; there is nothing extra to persist.
        pass

    def run_started(self, meta: dict) -> None:
        pass  # run-level — not an instance line

    def run_finished(self, result: "RunResult") -> None:
        pass  # run-level — not an instance line
# ---------------------------------------------------------------------------
# zero-quota pre-flight (WP3b, issue #15 AC4, PLAN §2.7)
# ---------------------------------------------------------------------------


def run_preflight_checks(
        checks: "Sequence[tuple[str, Callable[[], None]]]" ) -> None:
    """Run the pre-flight legs in order; the **first** failure raises
    (short-circuit — "any check fails → stop", PLAN §2.7: a failed leg
    means the session would not start, so the later legs — and their
    cost / noise — are not spent). A non-``PreflightFailed`` exception
    from a leg is wrapped (it *is* that leg's failure)."""
    for name, fn in checks:
        try:
            fn()
        except PreflightFailed:
            raise
        except Exception as exc:
            raise PreflightFailed(
                name, f"{type(exc).__name__}: {exc}"
            ) from exc


class Preflight:
    """The concrete zero-quota health check (PLAN §2.7 / issue #15
    AC4), run before **every batch** and **before each instance**
    (``instance_id`` is None in the batch phase).

    The legs, in order (short-circuiting — see
    :func:`run_preflight_checks`):

    1. ``auth`` — ``devin auth status`` exits 0 (the "auth status ok").
    2. ``docker_daemon`` — the daemon is **responsive**: ``docker
       info`` exits 0 within ``timeout_s`` (a hung daemon is a
       failure, not a wait).
    3. ``docker_image`` — (per instance only) ``docker image inspect
       <image>`` exits 0 (the image is present).
    4. ``disk`` — free disk ≥ ``min_free_bytes`` = **2 × the largest
       expected image** (the PLAN §7 floor; the AC4 "≥ 2 × largest
       expected image").

    Any failure raises :class:`PreflightFailed` naming the leg; the
    runner records the reason into the checkpoint JSONL as
    ``preflight_failed``. ``run`` / ``disk_usage_fn`` / ``image_for``
    are injectable (the unit tests stub-fail each leg — zero quota).
    """

    def __init__(self,
                 devin_bin: str = "devin",
                 docker: str = "docker",
                 *,
                 disk_path: str = "/",
                 largest_image_bytes: "int | float",
                 timeout_s: float = 15.0,
                 image_for: "Callable[[str], str] | None" = None,
                 run: "Callable[[list, float], Any] | None" = None,
                 disk_usage_fn: "Callable[[str], Any] | None" = None
                 ) -> None:
        if isinstance(largest_image_bytes, bool) \
                or not (isinstance(largest_image_bytes, (int, float))
                       and largest_image_bytes > 0):
            raise RunnerConfigError(
                f"largest_image_bytes must be a number > 0, "
                f"got {largest_image_bytes!r}"
            )
        if isinstance(timeout_s, bool) \
                or not (isinstance(timeout_s, (int, float)) and timeout_s > 0):
            raise RunnerConfigError(
                f"timeout_s must be a number > 0, got {timeout_s!r}"
            )
        self._devin = devin_bin
        self._docker = docker
        self._disk_path = disk_path
        self._timeout_s = timeout_s
        # PLAN §7: the pre-flight disk check is sized against the
        # largest expected image × 2.
        self.min_free_bytes = 2.0 * largest_image_bytes
        self._image_for = image_for if image_for is not None \
            else (lambda iid: f"sweb.eval.x86_64.{iid}")
        self._run = run if run is not None else self._default_run
        self._disk_usage = disk_usage_fn if disk_usage_fn is not None \
            else shutil.disk_usage

    @staticmethod
    def _default_run(cmd: list, timeout_s: float) -> Any:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s)

    def _cmd(self, name: str, cmd: list) -> None:
        try:
            proc = self._run(cmd, self._timeout_s)
        except Exception as exc:
            raise PreflightFailed(
                name, f"{cmd[0]!r} could not be run: "
                     f"{type(exc).__name__}: {exc}"
            ) from exc
        if getattr(proc, "returncode", 1) != 0:
            tail = getattr(proc, "stderr", b"") or b""
            if isinstance(tail, str):
                tail = tail.encode("utf-8", "replace")
            raise PreflightFailed(
                name, f"{cmd[0]!r} exited {proc.returncode}: "
                      f"{tail[-200:].decode('utf-8', 'replace').strip()!r}"
            )

    def __call__(self, instance_id: "str | None") -> None:
        checks: "list" = [
            ("auth",
             lambda: self._cmd("auth", [self._devin, "auth", "status"])),
            ("docker_daemon",
             lambda: self._cmd("docker_daemon", [self._docker, "info"])),
        ]
        if instance_id is not None:
            img = self._image_for(instance_id)
            checks.append(
                ("docker_image",
                 lambda: self._cmd("docker_image",
                                  [self._docker, "image", "inspect", img])))
        checks.append(("disk", self._disk_check))
        run_preflight_checks(checks)

    def _disk_check(self) -> None:
        try:
            usage = self._disk_usage(self._disk_path)
        except Exception as exc:
            raise PreflightFailed(
                "disk", f"disk usage of {self._disk_path!r} could not be "
                f"measured: {type(exc).__name__}: {exc}"
            ) from exc
        free = getattr(usage, "free", None)
        if not isinstance(free, (int, float)) or free < self.min_free_bytes:
            raise PreflightFailed(
                "disk",
                f"free {getattr(usage, 'free', 'unknown')} < required "
                f"{self.min_free_bytes:.0f} (2 × the largest expected "
                f"image, PLAN §7)"
            )


# ---------------------------------------------------------------------------
# results + the runner
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """The outcome of one :meth:`Runner.run` (a run spans windows — the
    500-instance run is one process across days, PLAN §6 WP6.3).

    ``results`` is in completion order (deterministic under the default
    sequential mode; interleaved under a granted N>1).
    """

    final_state: str
    results: list  # [(instance_id, status)] in completion order
    aborted_reason: "str | None"
    backoffs: int  # number of sleep-to-window backoffs taken
    sessions_completed: int  # total across all windows of this run
    wall_s: float  # the run's wall time (clock start → end)
    transition_log: list = field(default_factory=list)
    # [(from_state: BackoffState, event: str, to_state: BackoffState)]


def next_quota_window(ts: float) -> float:
    """The next quota reset boundary strictly after ``ts``.

    WP0 §4: the allowance refreshes on a calendar basis; the
    fine-grained reset is the calendar day ⇒ the next **UTC midnight**.
    ``ts`` is expected on the **epoch/wall** basis (the Runner's
    default clock is ``time.time`` for exactly this reason — see the
    ``clock`` default note in :class:`Runner.__init__`), so the result
    is a true UTC midnight in epoch seconds.
    """
    day = 86400.0
    return (math.floor(ts / day) + 1) * day


def check_concurrency_flag(concurrency: int, i_know_about_quota: bool) -> int:
    """The flag contract (issue #14 AC2, PLAN §2.1) — one place, used by
    the CLI and :class:`Runner.__init__`.

    ``concurrency > 1`` is **refused** (typed
    :class:`ConcurrencyRefusedError`) unless ``i_know_about_quota`` is
    passed.
    """
    if not isinstance(concurrency, int) or isinstance(concurrency, bool):
        raise ConcurrencyRefusedError(
            f"--concurrency must be an integer, got {concurrency!r}"
        )
    if concurrency < 1:
        raise ConcurrencyRefusedError(
            f"--concurrency must be >= 1, got {concurrency!r}"
        )
    if concurrency > 1 and not i_know_about_quota:
        raise ConcurrencyRefusedError(
            f"--concurrency {concurrency} (>1) is refused by default: the "
            "free-tier policy is one session at a time (PLAN.md §2.1). "
            "Pass --i-know-about-quota to take responsibility for "
            "concurrent quota use."
        )
    return concurrency


class Runner:
    """The WP3a + WP3b runner: sequential queue + quota-budget state
    machine + overlap guard (issue #14) + checkpoint/resume, the
    startup cost-tier model gate, and the zero-quota pre-flight
    (issue #15).

    Construct with the instance queue, a ``session_factory`` (the
    adapter seam), a ``reporter`` (a :class:`JsonlCheckpoint` for the
    ``run-<id>.jsonl`` progress file, issue #15 AC1) and — for tests —
    injectable ``clock`` / ``sleeper`` / ``next_window``. ``lock_file``
    enables the cross-process guard (PLAN §2.1); it is off by default
    so parallel test runs never collide, and the CLI exposes an
    explicit path. ``preflight`` (PLAN §2.7, issue #15 AC4) is the
    per-batch + per-instance health hook — it must raise
    :class:`PreflightFailed` naming the failed leg; a failure marks the
    instance ``preflight_failed``, releases the budget claim, and aborts
    the run (the batch-phase failure records an ``instance_id: None``
    marker line).

    * The default concurrency is **1** (sequential); a granted N>1 runs
      N worker threads (the user's quota risk).
    * ``run()`` is one-shot per Runner instance (a second call raises
      :class:`RunnerError`).
    * ``run()`` is bounded by the session's *own* wall-clock cap (the
      adapter / ``AcpSession`` 45-min cap, WP1) — the runner does not
      impose a second timeout (a hung session is an adapter contract
      breach, not a runner condition).
    * The **batch pre-flight runs before every worker round** (a new
      quota window is a new batch, PLAN §2.7), and the per-instance
      pre-flight runs before every session; both short-circuit on the
      first failed leg.
    * A propagated session failure (host-crash class, PLAN §2.5) re-
      raises out of ``run()``; the partial state is the resume class's,
      and ``run_finished`` is *not* reported (the run did not finish).
    """

    def __init__(
        self,
        instances: Sequence[str],
        session_factory: Callable[[str], AgentSession],
        *,
        config: "BudgetConfig | None" = None,
        concurrency: int = 1,
        i_know_about_quota: bool = False,
        # The default is **wall time** (epoch seconds), not
        # ``time.monotonic``: the backoff target is computed by
        # ``next_quota_window`` as the next *epoch* UTC midnight (WP0 §4
        # "calendar basis" reset), so the clock feeding it must be the
        # same epoch basis — a monotonic (boot-relative) clock would
        # align the sleep to a boot-dependent phase, waking up to ~24 h
        # off the real quota reset (review finding on the WP3a default;
        # WP3b's per-window pre-flight makes the phase alignment
        # load-bearing). Tests inject their own clock/sleeper, so the
        # default only governs a real (uninjected) run.
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        next_window: "Callable[[float], float] | None" = None,
        reporter: "Reporter | None" = None,
        preflight: "Callable[[str | None], None] | None" = None,
        lock_file: "str | None" = None,
    ) -> None:
        check_concurrency_flag(concurrency, i_know_about_quota)  # AC2, in depth
        if config is None:
            config = BudgetConfig()
        if not isinstance(config, BudgetConfig):
            raise RunnerConfigError(
                f"config must be a BudgetConfig, got {type(config)!r}"
            )
        self._config = config
        self._concurrency = concurrency
        self._session_factory = session_factory
        self._clock = clock
        self._sleeper = sleeper
        self._next_window = next_window if next_window is not None \
            else next_quota_window
        self._reporter = reporter if reporter is not None \
            else RecordingReporter()
        self._preflight = preflight
        self._lock = threading.Lock()
        self._queue: deque = deque(instances)
        self._budget = DailyBudget(self._config)
        self._guard = OverlapGuard(max_concurrency=concurrency)
        self._proc_lock = ProcessLock(lock_file) if lock_file else None
        self._state = BackoffState.RUNNING
        self._stop_new = False
        self._stop_reason: "str | None" = None
        self._consecutive_errors = 0
        self._abort_reason: "str | None" = None
        self._backoffs = 0
        self._sessions_completed = 0
        self._results: list = []
        self._transition_log: list = []
        self._fatal: "BaseException | None" = None
        self._running = False

    # -- introspection ---------------------------------------------------

    @property
    def config(self) -> BudgetConfig:
        return self._config

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def budget(self) -> DailyBudget:
        return self._budget

    @property
    def guard(self) -> OverlapGuard:
        return self._guard

    @property
    def state(self) -> BackoffState:
        with self._lock:
            return self._state

    @property
    def transition_log(self) -> list:
        with self._lock:
            return list(self._transition_log)

    # -- the run ---------------------------------------------------------

    def run(self) -> RunResult:
        """Drive the whole run (windows included) to a terminal state.

        Returns a :class:`RunResult`; re-raises a propagated session
        failure (host-crash class). The cross-process lock is held for
        the whole call (released in a finally). The ``run_finished``
        hook fires on normal completion — *not* on a propagated failure
        (the run did not finish). A ``ProcessLockBusy`` failure does not
        consume the one-shot: the runner may retry once the lock frees.
        The one-shot check-and-set is atomic under the runner lock (a
        concurrent ``run()`` on the same instance is refused, not
        double-scheduled). With a ``lock_file``, a *concurrent*
        second ``run()`` on the same instance is refused as
        :class:`ProcessLockBusy` (a ``RunnerError`` subclass — the
        cross-process lock is checked first, which is what keeps a
        lock-busy failure retryable); a *sequential* post-completion
        retry reports "already running".
        """
        t0 = self._clock()
        if self._proc_lock is not None:
            self._proc_lock.acquire()
        try:
            with self._lock:
                if self._running:
                    raise RunnerError(
                        "runner is already running — "
                        "one run() per instance"
                    )
                self._running = True
            self._reporter.run_started({
                "concurrency": self._concurrency,
                "budget": {
                    "max_sessions_per_day":
                        self._config.max_sessions_per_day,
                    "max_wall_per_session":
                        self._config.max_wall_per_session,
                    "daily_pause": self._config.daily_pause,
                },
                "queue_size": len(self._queue),
                "lock_file": self._proc_lock.path
                if self._proc_lock is not None else None,
            })
            # One worker round per window: after a sleep-to-window
            # backoff the run resumes and the *remaining* queue is
            # processed in the new window (the 500 run is one process
            # across days, PLAN §6 WP6.3).
            while True:
                self._batch_preflight()  # per window — a new batch (PLAN §2.7)
                if self._concurrency == 1:
                    self._worker_body()  # inline — the sequential default
                else:
                    self._worker_pool()
                if self._fatal is not None:
                    raise self._fatal  # host-crash class (PLAN §2.5)
                if self._drain_state_machine():
                    break
            result = self._make_result(t0)
            self._reporter.run_finished(result)
            return result
        finally:
            if self._proc_lock is not None:
                self._proc_lock.release()
            # ``_running`` is deliberately *not* reset: the Runner is
            # one-shot (its queue is consumed by the first run() — a
            # second run() would double-schedule; use a fresh Runner, or
            # WP3b's checkpoint/resume for partial state).

    # -- pre-flight (WP3b, issue #15 AC4, PLAN §2.7) ----------------------

    def _batch_preflight(self) -> None:
        """The per-window batch phase of the pre-flight (PLAN §2.7:
        "before every batch" — a new quota window is a new batch: the
        auth / daemon / disk state may have changed since the last
        one). On a failure: the reason is recorded into the checkpoint
        JSONL as a ``preflight_failed`` line (``instance_id: None`` — a
        batch-level marker that consumes no queue slot), no session is
        ever started, and the run **aborts** (an infra failure is not a
        quota window to sleep for — the operator fixes it and resumes,
        where the affected instances are retry-eligible, AC2). A
        non-``PreflightFailed`` exception from the hook propagates as a
        fatal (a broken hook is a runner bug, not a check failure)."""
        if self._preflight is None:
            return
        with self._lock:
            if self._state in TERMINAL_STATES:
                return
        try:
            self._preflight(None)
        except PreflightFailed as exc:
            # The record hook runs under the runner lock (the WP3a hook
            # discipline — the batch phase is single-threaded, but the
            # discipline is the contract).
            with self._lock:
                self._reporter.record_instance(
                    None, "preflight_failed",
                    {"reason": str(exc), "phase": "batch"})
                if self._state in (BackoffState.RUNNING,
                                  BackoffState.BUDGET_EXHAUSTED):
                    self._abort_reason = f"preflight_failed (batch): {exc}"
                    self._emit(EVENT_ABORT)  # running -> aborted

    def _handle_preflight_failure(self, inst: str, reason: str,
                                 wall_s: float) -> None:
        """A per-instance pre-flight failure (PLAN §2.7): record the
        ``preflight_failed`` mark, release the budget claim (no session
        was ever started — the WP3a claim lifecycle), and **stop the
        batch with an abort** (an infra failure is not a quota signal:
        no sleep-to-window; the operator resumes after fixing the infra
        — the instance is then retry-eligible, AC2). A pre-flight
        failure is a clean boundary for the drift-suspicion counter
        (like a quota stop). Not counted in ``sessions_completed``
        (no session ran) and no wall recorded (the claim is released)."""
        with self._lock:
            self._results.append((inst, "preflight_failed"))
            self._reporter.record_instance(
                inst, "preflight_failed",
                {"reason": reason, "wall_s": wall_s, "phase": "instance"})
            self._budget.release_claim()
            self._consecutive_errors = 0
            if self._state in (BackoffState.RUNNING,
                              BackoffState.BUDGET_EXHAUSTED):
                self._stop_new = True
                self._abort_reason = f"preflight_failed: {reason}"
                self._emit(EVENT_ABORT)  # (running|budget_exhausted) -> aborted

    # -- worker layer ----------------------------------------------------

    def _worker_body(self) -> None:
        """One worker: claim → execute → handle, until the queue is
        drained or the batch stops. Used inline (N=1) or per thread."""
        while True:
            inst = self._claim_next()
            if inst is None:
                return
            try:
                self._execute(inst)
            except BaseException as exc:  # a partial session (PLAN §2.5)
                with self._lock:
                    if self._fatal is None:
                        self._fatal = exc
                    self._stop_new = True
                return

    def _worker_pool(self) -> None:
        """A granted N>1: N worker threads over the shared queue."""
        threads = []
        for _ in range(self._concurrency):
            t = threading.Thread(target=self._worker_body, daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    def _claim_next(self) -> "str | None":
        """Under the lock: decide the next scheduling action.

        Returns the next instance id (with its budget claim already
        taken) or None — having emitted the corresponding state-machine
        event (queue_empty / budget_exhausted) when applicable.
        """
        with self._lock:
            if self._state is not BackoffState.RUNNING or self._stop_new:
                return None
            if self._budget.exhausted():
                # The queue-empty check *inside* the exhausted branch:
                # an exhausted budget with nothing left ends the run
                # (done) without a pointless sleep-to-window.
                if not self._queue:
                    self._emit(EVENT_QUEUE_EMPTY)
                else:
                    self._stop_reason = "budget_exhausted"
                    self._emit(EVENT_BUDGET_EXHAUSTED)
                return None
            if not self._queue:
                self._emit(EVENT_QUEUE_EMPTY)
                return None
            inst = self._queue.popleft()
            # The exhausted() check above (same lock) guarantees the
            # claim succeeds; try_claim re-checks for defense in depth.
            if not self._budget.try_claim():
                self._queue.appendleft(inst)
                self._stop_reason = "budget_exhausted"
                self._emit(EVENT_BUDGET_EXHAUSTED)
                return None
            return inst

    def _execute(self, inst: str) -> None:
        """One instance, inside the overlap-guard slot.

        The pre-flight (WP3b, PLAN §2.7: "before each instance") runs
        **before** any session is built — on a failure the budget claim
        is released (no session ever started), the instance is marked
        ``preflight_failed`` (the reason lands in the JSONL meta), and
        the batch stops with an abort. The *session* wall (the budget
        ``record_wall`` charge) excludes the zero-quota pre-flight time
        (a pre-flight consumes no session quota — the claim is
        released on its failure); the pre-flight wall is recorded in
        the mark's meta. Budget-claim lifecycle:
        a factory/``start()`` failure releases the claim (no session
        ever started); a ``run()``/``close()`` failure stands (the
        partial session consumed the work). The guard slot is released
        on *all* paths (finally), and a half-started session is
        best-effort closed on every failure path (PLAN §1 hygiene).
        """
        s = None
        started = False
        guard_held = False
        t0 = self._clock()
        if self._preflight is not None:
            try:
                self._preflight(inst)
            except PreflightFailed as exc:
                self._handle_preflight_failure(
                    inst, str(exc), max(0.0, self._clock() - t0))
                return
        t0 = self._clock()  # the session wall excludes the pre-flight
        try:
            try:
                s = self._session_factory(inst)
                self._guard.acquire()  # the assert (issue #14 AC1)
                guard_held = True
                s.start()
                started = True
                status = s.run()
                if status not in TERMINAL_STATUSES:
                    raise RunnerProtocolError(
                        f"session for {inst!r} returned unknown terminal "
                        f"status {status!r} (valid: "
                        f"{sorted(TERMINAL_STATUSES)})"
                    )
                s.close()
            except BaseException:
                # Best-effort teardown on every failure path — the
                # adapter's close must kill the process group (PLAN §1);
                # the contract is idempotent, so closing a never-started
                # or half-started session is a no-op for a conforming
                # implementation. (A factory failure leaves ``s`` as
                # None — no object to close.)
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
                if not started:
                    # No session ever started: give the budget claim
                    # back — it must not consume the daily budget.
                    # Under the lock: a granted N>1 can race this
                    # against a concurrent worker's try_claim/record.
                    with self._lock:
                        self._budget.release_claim()
                raise
        finally:
            if guard_held:
                self._guard.release()
        wall = max(0.0, self._clock() - t0)
        self._handle(inst, status, wall)

    # -- status handling ---------------------------------------------------

    def _handle(self, inst: str, status: str, wall_s: float) -> None:
        """Record the terminal status and apply its state effects.

        The ``record_instance`` hook runs under the runner lock together
        with the state changes (the hook must be fast — WP3b's JSONL
        append is a single write — and must *not* call back into
        :class:`Runner` — the lock is non-reentrant); the
        ``checkpoint`` hook (AC4) runs outside the lock in the
        single-threaded drain, before the sleep-to-window.
        """
        with self._lock:
            self._results.append((inst, status))
            self._reporter.record_instance(inst, status, {"wall_s": wall_s})
            self._sessions_completed += 1
            self._budget.record_wall(wall_s)
            if status == AGENT_ERROR:
                self._consecutive_errors += 1
                if (self._state in (BackoffState.RUNNING,
                                   BackoffState.BUDGET_EXHAUSTED)
                        and self._consecutive_errors
                        >= ABORT_AFTER_CONSECUTIVE_AGENT_ERRORS):
                    self._abort_reason = (
                        f"{ABORT_AFTER_CONSECUTIVE_AGENT_ERRORS} "
                        f"consecutive agent_error (environment drift "
                        f"suspicion)"
                    )
                    self._emit(EVENT_ABORT)
            else:
                self._consecutive_errors = 0
            if status == QUOTA_LIMITED:
                # "stop the batch" (issue #14 AC4): no further instance
                # starts; in-flight sessions (N>1) finish. Only the
                # worker whose status actually stopped the run (the
                # fresh RUNNING -> budget_exhausted transition) records
                # the stop reason — a late-arriving quota_limited
                # status (N>1) does not re-stop. A *late* arrival after
                # a local-cap stop upgrades the reason to
                # rate_limited — the external signal is the more
                # operationally significant cause (the first-cause
                # order is otherwise arbitrary under N>1). The
                # checkpoint fires exactly once per backoff, in the
                # single-threaded drain, before the sleep.
                self._stop_new = True
                if self._state is BackoffState.RUNNING:
                    self._emit(EVENT_RATE_LIMITED)
                    self._stop_reason = "rate_limited"
                elif (self._state is BackoffState.BUDGET_EXHAUSTED
                        and self._stop_reason != "rate_limited"):
                    self._stop_reason = "rate_limited"

    # -- state machine -----------------------------------------------------

    def _drain_state_machine(self) -> bool:
        """After a worker round: sleep-to-window / wake / resume, or
        report the terminal state. Returns True when the run is over."""
        while True:
            with self._lock:
                state = self._state
            if state in TERMINAL_STATES:
                return True
            if state is BackoffState.RUNNING:
                with self._lock:
                    if self._queue:
                        # Defensive (a granted N>1 can leave the queue
                        # after a worker round): run another worker
                        # round — do not spin here.
                        return False
                    self._emit(EVENT_QUEUE_EMPTY)  # -> done
                return True
            if state is BackoffState.BUDGET_EXHAUSTED:
                ckpt = None
                with self._lock:
                    if not self._queue:
                        # The N>1 race: the queue drained between a
                        # worker's exhausted check and this drain —
                        # done, without a pointless sleep.
                        self._emit(EVENT_QUEUE_EMPTY)
                        return True
                    # "checkpoint" (AC4) — before the sleep-to-window.
                    # Exactly once per backoff: the drain is
                    # single-threaded (a granted N>1 can no longer
                    # interleave — the pool has joined), so two
                    # quota_limited completions yield one checkpoint.
                    ckpt = (
                        self._stop_reason or "budget_exhausted",
                        {"sessions_used": self._budget.sessions_used,
                         "queue_remaining": len(self._queue)},
                    )
                    self._emit(EVENT_SLEEP_START)  # -> sleeping
                # Outside the lock — the hooks must not re-enter the
                # runner's internals; the order is checkpoint *then*
                # sleep (AC4).
                if ckpt is not None:
                    self._reporter.checkpoint(*ckpt)
                target = self._next_window(self._clock())
                self._sleeper(max(target - self._clock(), 0.0))
                with self._lock:
                    self._emit(EVENT_WAKE)  # -> resume
                continue
            if state is BackoffState.RESUME:
                with self._lock:
                    # A new window is a fresh budget (PLAN §2.2/§2.5):
                    # the daily counters, the drift-suspicion counter
                    # (a deliberate, documented deviation from AC5's
                    # literal reset rule — a new day is a fresh
                    # suspicion) and the batch stop all reset.
                    self._budget.reset()
                    self._consecutive_errors = 0
                    self._stop_new = False
                    self._stop_reason = None
                    self._emit(EVENT_RESET)  # -> running
                return False  # a new window round
            raise RunnerProtocolError(f"unhandled state {state!r} in drain")

    def _emit(self, event: str) -> None:
        """One state-machine event (call with the lock held)."""
        to = transition(self._state, event)
        self._transition_log.append((self._state, event, to))
        if event == EVENT_SLEEP_START:
            self._backoffs += 1
        self._state = to

    # -- result -------------------------------------------------------------

    def _make_result(self, t0: float) -> RunResult:
        return RunResult(
            final_state=self._state.value,
            results=list(self._results),
            aborted_reason=self._abort_reason,
            backoffs=self._backoffs,
            sessions_completed=self._sessions_completed,
            wall_s=round(max(0.0, self._clock() - t0), 3),
            transition_log=list(self._transition_log),
        )


# ---------------------------------------------------------------------------
# CLI — the flag contract surface (issue #14 AC2)
# ---------------------------------------------------------------------------


def main(argv: "Sequence[str] | None" = None) -> int:
    """The runner CLI (WP3a + WP3b).

    WP3a owns the flag contract + budget configuration; WP3b adds the
    **startup model gate** (issue #15 AC3, PLAN §2.4): with
    ``--model`` the batch is gated against the ``devin models list
    --format json`` payload — a local file (``--models-json``, the
    fixture / offline path) or a live ``--devin-bin`` invocation — and
    a refusal exits 2 before anything starts (fail-closed). The
    adapter + real session factory wiring (an actual batch run from
    the CLI) lands with the adapter WP; the checkpoint JSONL, resume
    and pre-flight are available now (the programmatic API + this
    gate), and the execution path is exercised programmatically by
    ``tests/test_runner.py``.
    """
    p = argparse.ArgumentParser(
        prog="runner.py",
        description="devinp bench runner (WP3a issue #14 + WP3b issue "
                    "#15): sequential queue, quota-budget state machine, "
                    "overlap guard, cost-tier model gate, checkpoint/"
                    "resume, pre-flight.",
    )
    p.add_argument(
        "--concurrency", type=int, default=1,
        help="agent sessions to run concurrently (default 1 — the free-tier "
             "policy is one session at a time, PLAN §2.1)",
    )
    p.add_argument(
        "--i-know-about-quota", action="store_true",
        help="lifts the refusal of --concurrency N>1 (you take the "
             "concurrent-quota risk on yourself)",
    )
    p.add_argument("--max-sessions-per-day", type=int, default=10,
                   help="per-window session budget (WP0 §4: default 10; "
                        "calibrate in Pilot A)")
    p.add_argument("--max-wall-per-session-min", type=float, default=45.0,
                   help="per-session wall cap in minutes (PLAN §2.2: 45)")
    p.add_argument("--daily-pause-h", type=float, default=12.0,
                   help="nominal pause between run batches in hours; the "
                        "actual sleep is aligned to the quota reset window "
                        "(next UTC midnight, WP0 §4)")
    p.add_argument("--lock-file", default=None,
                   help="cross-process flock path (default: none — the "
                        "in-process overlap guard is always on)")
    # WP3b (issue #15 AC3, PLAN §2.4) — the startup model gate.
    p.add_argument("--model", default=None,
                   help="the model to run the batch on — gated at startup "
                        "by cost tier (PLAN §2.4); without it the gate "
                        "does not run (WP3a behavior)")
    p.add_argument("--allow-paid", action="store_true",
                   help="the explicit paid-model override (requires "
                        "--token-budget; PLAN §2.4)")
    p.add_argument("--token-budget", type=float, default=None,
                   help="the positive token budget that permits "
                        "--allow-paid")
    p.add_argument("--models-json", default=None,
                   help="a file with the `devin models list --format json` "
                        "payload (fixture / offline gate); default: run "
                        "the CLI via --devin-bin")
    p.add_argument("--devin-bin", default="devin",
                   help="the devin binary for the startup model gate")
    args = p.parse_args(argv)
    try:
        check_concurrency_flag(args.concurrency, args.i_know_about_quota)
        cfg = BudgetConfig(
            max_sessions_per_day=args.max_sessions_per_day,
            max_wall_per_session=args.max_wall_per_session_min * 60.0,
            daily_pause=args.daily_pause_h * 3600.0,
        )
    except (ConcurrencyRefusedError, RunnerConfigError) as exc:
        print(f"runner: {exc}", file=sys.stderr)
        return 2
    # WP3b (issue #15 AC3): the gate flags are inert without --model —
    # refuse them there (an explicit contract: a silent no-op that
    # "validated" nothing is a footgun; if a default model is ever
    # added, the gate must run for it too).
    if args.model is None and (args.allow_paid
                              or args.token_budget is not None
                              or args.models_json is not None
                              or args.devin_bin != "devin"):
        print("runner: --allow-paid / --token-budget / --models-json / "
              "--devin-bin require --model (the startup model gate does "
              "not run without a configured model)", file=sys.stderr)
        return 2
    # The gate's input is one of the file or the live binary — the
    # combination is refused (``--models-json`` would silently win, and
    # a silently inert ``--devin-bin`` is the same footgun class as
    # the flags-without-``--model`` refusal above).
    if args.models_json is not None and args.devin_bin != "devin":
        print("runner: --models-json and --devin-bin are mutually "
              "exclusive (the gate's input is the file OR the live "
              "binary)", file=sys.stderr)
        return 2
    # WP3b (issue #15 AC3, PLAN §2.4): the startup model gate — a
    # refusal exits 2 before the batch starts (fail-closed).
    if args.model is not None:
        try:
            if args.models_json is not None:
                with open(args.models_json, "r", encoding="utf-8") as f:
                    models_json = json.load(f)
            else:
                models_json = fetch_models_list(args.devin_bin)
            gate = gate_cost_tier(
                models_json, args.model,
                allow_paid=args.allow_paid,
                token_budget=args.token_budget,
            )
        except (ModelGateError, OSError, ValueError) as exc:
            print(f"runner: model gate refused the batch "
                  f"(fail-closed): {exc}", file=sys.stderr)
            return 2
        if not gate.allowed:
            print(f"runner: model gate refused the batch: {gate.reason}",
                  file=sys.stderr)
            return 2
        print(f"  model gate: {gate.model} = {gate.cost_tier}"
              + (" (paid override — the token budget was accepted)"
                 if gate.paid_override else "")
              + f"; free models: {', '.join(gate.free_models) or 'none'}")
    print("runner (WP3a issue #14 + WP3b issue #15) — configuration:")
    print(f"  concurrency: {args.concurrency}"
          f" (overlap guard cap {args.concurrency}; "
          f"default 1 = sequential)")
    print(f"  max_sessions_per_day: {cfg.max_sessions_per_day}")
    print(f"  max_wall_per_session: {cfg.max_wall_per_session:.1f} s")
    print(f"  daily_pause: {cfg.daily_pause:.0f} s (nominal — the "
          f"backoff sleep aligns to the quota reset window: the next "
          f"UTC midnight, WP0 §4)")
    print(f"  lock_file: {args.lock_file or 'none (in-process guard only)'}")
    print("WP3b: the startup model gate is validated above; the "
          "checkpoint JSONL (JsonlCheckpoint), resume (resume_queue / "
          "resume_from_checkpoint) and pre-flight (Preflight) are the "
          "programmatic API. The real adapter session factory (an "
          "actual batch run from the CLI) lands with the adapter WP.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
