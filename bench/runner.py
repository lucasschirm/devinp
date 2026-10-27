"""``runner.py`` — WP3a (issue #14): sequential queue, quota-budget state
machine, overlap guard.

Scope (PLAN.md §5 WP3 "WP3a" split, issue #14; checkpoint/resume +
cost-tier gate + pre-flight are WP3b, issue #15; post-eval pruning is
WP3c, issue #16). The module is pure stdlib — no SDK dependency, no real
``devin`` binary — and is unit-tested against instrumented fake sessions
(``tests/test_runner.py``), zero quota.

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

Session contract (the pluggable seam WP3b's adapter fills):

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
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Final, Protocol, Sequence

__all__ = [
    "TERMINAL_STATUSES",
    "AGENT_ERROR",
    "QUOTA_LIMITED",
    "ABORT_AFTER_CONSECUTIVE_AGENT_ERRORS",
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
# session + reporter contracts
# ---------------------------------------------------------------------------


class AgentSession(abc.ABC):
    """One instance's agent session (the seam WP3b's adapter fills).

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

    def record_instance(self, instance_id: str, status: str,
                       meta: dict) -> None:
        """An instance reached a terminal status (the ``quota_limited``
        "mark" of issue #14 AC4)."""

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

    def record_instance(self, instance_id: str, status: str,
                       meta: dict) -> None:
        self.events.append(("instance", instance_id, status, dict(meta)))

    def checkpoint(self, reason: str, meta: dict) -> None:
        self.events.append(("checkpoint", reason, dict(meta)))

    def run_finished(self, result: "RunResult") -> None:
        self.events.append(("run_finished", result.final_state))


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
    """The WP3a runner: sequential queue + quota-budget state machine +
    overlap guard (issue #14).

    Construct with the instance queue, a ``session_factory`` (WP3b's
    adapter seam) and — for tests — injectable ``clock`` / ``sleeper`` /
    ``next_window``. ``lock_file`` enables the cross-process guard
    (PLAN §2.1); it is off by default so parallel test runs never
    collide, and the CLI exposes an explicit path.

    * The default concurrency is **1** (sequential); a granted N>1 runs
      N worker threads (the user's quota risk).
    * ``run()`` is one-shot per Runner instance (a second call raises
      :class:`RunnerError`).
    * ``run()`` is bounded by the session's *own* wall-clock cap (the
      adapter / ``AcpSession`` 45-min cap, WP1) — the runner does not
      impose a second timeout (a hung session is an adapter contract
      breach, not a runner condition).
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
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        next_window: "Callable[[float], float] | None" = None,
        reporter: "Reporter | None" = None,
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
        double-scheduled).
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

        Budget-claim lifecycle: a factory/``start()`` failure releases
        the claim (no session ever started); a ``run()``/``close()``
        failure stands (the partial session consumed the work). The
        guard slot is released on *all* paths (finally), and a
        half-started session is best-effort closed on every failure
        path (PLAN §1 hygiene).
        """
        s = None
        started = False
        guard_held = False
        t0 = self._clock()
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
        wall = self._clock() - t0
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
            wall_s=round(self._clock() - t0, 3),
            transition_log=list(self._transition_log),
        )


# ---------------------------------------------------------------------------
# CLI — the flag contract surface (issue #14 AC2)
# ---------------------------------------------------------------------------


def main(argv: "Sequence[str] | None" = None) -> int:
    """The runner CLI (WP3a).

    WP3a owns the flag contract + budget configuration; the adapter +
    checkpoint JSONL wiring (and thus an actual batch run from the CLI)
    lands in WP3b (issue #15). Until then the CLI validates the flags,
    prints the resulting configuration, and exits 0 — the execution path
    is exercised programmatically by ``tests/test_runner.py``.
    """
    p = argparse.ArgumentParser(
        prog="runner.py",
        description="devinp bench runner (WP3a, issue #14): sequential queue, "
                    "quota-budget state machine, overlap guard.",
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
    print("runner (WP3a, issue #14) — configuration:")
    print(f"  concurrency: {args.concurrency}"
          f" (overlap guard cap {args.concurrency}; "
          f"default 1 = sequential)")
    print(f"  max_sessions_per_day: {cfg.max_sessions_per_day}")
    print(f"  max_wall_per_session: {cfg.max_wall_per_session:.1f} s")
    print(f"  daily_pause: {cfg.daily_pause:.0f} s (nominal — the "
          f"backoff sleep aligns to the quota reset window: the next "
          f"UTC midnight, WP0 §4)")
    print(f"  lock_file: {args.lock_file or 'none (in-process guard only)'}")
    print("WP3a: the queue/budget/flag machinery is validated; the adapter + "
          "checkpoint JSONL wiring is WP3b (issue #15).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
