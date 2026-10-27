"""WP3a (issue #14) — ``runner.py``: sequential queue, quota-budget state
machine, overlap guard.

All tests are unit-level against instrumented fake sessions
(:class:`FakeSession` + :class:`Registry`) — zero quota, no real
``devin`` binary, CI safe. Acceptance mapping (issue #14):

* AC1 — default concurrency = 1; the runner asserts no two agent
  sessions ever overlap → :func:`test_default_concurrency_is_one`,
  :func:`test_sequential_queue_fifo_order` (the default path),
  :func:`test_overlap_guard_sequential_invariant` (instrumented fake
  sessions: the ``active_sessions == 1`` invariant under interleaved
  scheduling), :func:`test_overlap_guard_nested_start_violation`
  (the assert fires — fail loud, not a silent wait),
  :func:`test_overlap_guard_release_without_acquire`,
  :func:`test_process_lock_blocks_sibling_runner` (PLAN §2.1
  "process-level lock"), :func:`test_guard_and_claim_released_when_
  session_raises` (the slot never leaks)
* AC2 — ``--concurrency N>1`` refused by default unless
  ``--i-know-about-quota`` → :func:`test_check_concurrency_flag_matrix`,
  :func:`test_concurrency_gt1_refused_without_flag`,
  :func:`test_concurrency_gt1_granted_with_flag`,
  :func:`test_cli_refuses_concurrency_without_flag`,
  :func:`test_cli_accepts_default`,
  :func:`test_cli_accepts_granted_flag`,
  :func:`test_cli_refuses_invalid_budget`,
  :func:`test_concurrency_gt1_granted_runs_concurrently` (a granted N>1
  actually runs N-wide: the override is honored, not swallowed)
* AC3 — backoff state machine
  ``running → budget_exhausted → sleeping (until window) → resume`` →
  :func:`test_transition_table` (table-driven: every (state, event)
  pair, valid + invalid), :func:`test_invalid_transition_is_typed`,
  :func:`test_local_budget_exhaustion_sleeps_to_window_and_resumes`,
  :func:`test_wall_budget_exhaustion`, :func:`test_daily_budget_tracker`,
  :func:`test_next_quota_window`
* AC4 — rate-limit signal → mark ``quota_limited``, stop the batch (no
  further instance starts), checkpoint, sleep to next window —
  batch-level backoff, no per-instance retry →
  :func:`test_rate_limit_stops_batch_no_starts_after_signal` (incl. the
  checkpoint-before-sleep ordering, the exact state chain, the sleep
  landing on the window boundary, and the no-retry assertion),
  :func:`test_quota_limited_breaks_agent_error_chain`
* AC5 — abort on 2 consecutive ``agent_error`` (environment drift
  suspicion) → :func:`test_abort_on_two_consecutive_agent_errors`,
  :func:`test_consecutive_agent_error_counter_resets`

Plus the contract edges: :func:`test_unknown_status_is_typed`,
:func:`test_factory_failure_releases_claim`,
:func:`test_start_failure_releases_claim_and_closes`,
:func:`test_single_instance_no_sleep_when_done`,
:func:`test_empty_queue_ends_immediately`,
:func:`test_n_gt1_exhausted_drains_without_sleep`,
:func:`test_n_gt1_multiple_windows`, :func:`test_run_is_one_shot`,
:func:`test_budget_config_validation`.
"""

from __future__ import annotations

import fcntl
import threading

import pytest

import runner as r


# ---------------------------------------------------------------------------
# instrumented fakes (the issue #14 "testable by" lever)
# ---------------------------------------------------------------------------


class FakeClock:
    """A deterministic monotonic-ish clock the tests advance explicitly."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class Sleeper:
    """A sleeper that records its calls and advances the clock — the
    "sleep to the next window" observable."""

    def __init__(self, clock: FakeClock, events: list) -> None:
        self._clock = clock
        self._events = events
        self.calls: list = []

    def __call__(self, delay: float) -> None:
        self._events.append(("sleep", delay))
        self.calls.append(delay)
        self._clock.advance(delay)


class Registry:
    """The test's independent instrument of live sessions — a probe
    separate from the runner's own guard high-water mark (the issue #14
    "instrumented fake sessions" lever). ``max`` is the high-water mark
    of ``active``: the ``active_sessions == 1`` invariant under the
    default policy is ``max == 1``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max = max(self.max, self.active)

    def exit(self) -> None:
        with self._lock:
            self.active -= 1


class FakeSession(r.AgentSession):
    """An instrumented fake agent session. The registry counts it live
    from ``start()`` to ``close()`` (the overlap-relevant window, per
    the runner contract)."""

    def __init__(self, registry: Registry, status: str, *,
                 clock: "FakeClock | None" = None, on_start=None,
                 barrier: "threading.Barrier | None" = None,
                 hold_s: float = 0.0, start_raises=None,
                 close_raises: bool = False) -> None:
        self.registry = registry
        self.status = status
        self.clock = clock
        self.on_start = on_start
        self.barrier = barrier
        self.hold_s = hold_s
        self.start_raises = start_raises
        self.close_raises = close_raises
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.registry.enter()
        self.started = True
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        if self.on_start is not None:
            self.on_start()
        if self.start_raises is not None:
            raise self.start_raises

    def run(self) -> str:
        if self.clock is not None and self.hold_s:
            self.clock.advance(self.hold_s)
        return self.status

    def close(self) -> None:
        if self.close_raises:
            raise RuntimeError("close failed")
        self.closed = True
        self.registry.exit()


class Env:
    """A standard test environment: clock + sleeper + registry + reporter."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.reporter = r.RecordingReporter()
        self.sleeper = Sleeper(self.clock, self.reporter.events)
        self.registry = Registry()
        self.calls: list = []  # [(instance_id, clock_at_factory_call)]

    def make(self, instances, statuses, **kw) -> r.Runner:
        custom = kw.pop("factory", None)

        def factory(inst: str) -> r.AgentSession:
            self.calls.append((inst, self.clock()))
            if custom is not None:
                return custom(inst)
            return FakeSession(self.registry, statuses[inst], clock=self.clock)

        return r.Runner(
            instances, factory, clock=self.clock, sleeper=self.sleeper,
            reporter=self.reporter, **kw
        )


def chain(result) -> list:
    """The run's transition log as (from, event, to) value tuples."""
    return [(s.value, e, t.value) for (s, e, t) in result.transition_log]


# ---------------------------------------------------------------------------
# AC3 — the state machine (table-driven)
# ---------------------------------------------------------------------------


def test_transition_table():
    """Every (state, event) pair: the 9 valid transitions map exactly
    (incl. (budget_exhausted, abort) — a late-arriving drift trigger
    preempts the backoff sleep); all other pairs (including every event
    out of the terminal states) are invalid — the fail-loud contract of
    the table."""
    states = list(r.BackoffState)
    events = [r.EVENT_BUDGET_EXHAUSTED, r.EVENT_RATE_LIMITED, r.EVENT_ABORT,
              r.EVENT_QUEUE_EMPTY, r.EVENT_SLEEP_START, r.EVENT_WAKE,
              r.EVENT_RESET]
    valid = {
        (r.BackoffState.RUNNING, r.EVENT_BUDGET_EXHAUSTED):
            r.BackoffState.BUDGET_EXHAUSTED,
        (r.BackoffState.RUNNING, r.EVENT_RATE_LIMITED):
            r.BackoffState.BUDGET_EXHAUSTED,
        (r.BackoffState.RUNNING, r.EVENT_ABORT): r.BackoffState.ABORTED,
        (r.BackoffState.RUNNING, r.EVENT_QUEUE_EMPTY): r.BackoffState.DONE,
        (r.BackoffState.BUDGET_EXHAUSTED, r.EVENT_SLEEP_START):
            r.BackoffState.SLEEPING,
        (r.BackoffState.BUDGET_EXHAUSTED, r.EVENT_QUEUE_EMPTY):
            r.BackoffState.DONE,
        (r.BackoffState.BUDGET_EXHAUSTED, r.EVENT_ABORT):
            r.BackoffState.ABORTED,
        (r.BackoffState.SLEEPING, r.EVENT_WAKE): r.BackoffState.RESUME,
        (r.BackoffState.RESUME, r.EVENT_RESET): r.BackoffState.RUNNING,
    }
    assert len(states) * len(events) == 42
    for st in states:
        for ev in events:
            if (st, ev) in valid:
                assert r.transition(st, ev) is valid[(st, ev)]
            else:
                with pytest.raises(r.InvalidTransition):
                    r.transition(st, ev)
    # the documented chain (issue #14 AC3), step by step:
    st = r.BackoffState.RUNNING
    st = r.transition(st, r.EVENT_RATE_LIMITED)
    st = r.transition(st, r.EVENT_SLEEP_START)
    assert st is r.BackoffState.SLEEPING  # "sleeping (until window)"
    st = r.transition(st, r.EVENT_WAKE)
    assert st is r.BackoffState.RESUME
    st = r.transition(st, r.EVENT_RESET)
    assert st is r.BackoffState.RUNNING


def test_invalid_transition_is_typed():
    with pytest.raises(r.InvalidTransition) as exc:
        r.transition(r.BackoffState.DONE, r.EVENT_WAKE)
    assert exc.value.state is r.BackoffState.DONE
    assert exc.value.event == r.EVENT_WAKE
    assert "done" in str(exc.value) and "wake" in str(exc.value)


def test_next_quota_window():
    # not on a boundary: the next UTC midnight, strictly after
    t = 1_000_000.0  # not a midnight
    w = r.next_quota_window(t)
    assert w > t
    assert w % 86400 == 0
    assert w - t < 86400
    # exactly on a boundary: the *next* midnight (strictly after)
    assert r.next_quota_window(86400.0 * 5) == 86400.0 * 6
    # the window after the window
    assert r.next_quota_window(w) == w + 86400.0


def test_daily_budget_tracker():
    cfg = r.BudgetConfig(max_sessions_per_day=2, max_wall_per_session=10.0)
    b = r.DailyBudget(cfg)
    assert not b.exhausted()
    assert b.try_claim()
    assert b.try_claim()
    assert b.exhausted()  # the session cap
    assert not b.try_claim()
    b.release_claim()
    assert not b.exhausted()
    b.reset()
    assert b.sessions_used == 0
    # the wall budget (max_wall × cap = 20 s)
    assert b.wall_budget_s == 20.0
    b.record_wall(20.0)
    assert b.exhausted()
    b.reset()
    assert not b.exhausted()
    with pytest.raises(r.RunnerProtocolError):
        b.release_claim()  # no outstanding claim


# ---------------------------------------------------------------------------
# AC2 — the flag contract
# ---------------------------------------------------------------------------


def test_check_concurrency_flag_matrix():
    assert r.check_concurrency_flag(1, False) == 1
    assert r.check_concurrency_flag(1, True) == 1
    assert r.check_concurrency_flag(3, True) == 3
    with pytest.raises(r.ConcurrencyRefusedError):
        r.check_concurrency_flag(3, False)
    with pytest.raises(r.ConcurrencyRefusedError):
        r.check_concurrency_flag(0, True)
    with pytest.raises(r.ConcurrencyRefusedError):
        r.check_concurrency_flag(-2, True)
    with pytest.raises(r.ConcurrencyRefusedError):
        r.check_concurrency_flag(2.0, True)  # a float is not a concurrency
    with pytest.raises(r.ConcurrencyRefusedError):
        r.check_concurrency_flag(True, True)  # bool is an int subclass — refused


def test_concurrency_gt1_refused_without_flag():
    e = Env()
    with pytest.raises(r.ConcurrencyRefusedError):
        e.make(["i0"], {"i0": "resolved"}, concurrency=2)


def test_concurrency_gt1_granted_with_flag():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"}, concurrency=2,
                   i_know_about_quota=True)
    assert runner.concurrency == 2
    assert runner.guard.max_concurrency == 2


def test_cli_refuses_concurrency_without_flag(capsys):
    assert r.main(["--concurrency", "3"]) == 2
    err = capsys.readouterr().err
    assert "--concurrency 3" in err
    assert "refused" in err
    assert "--i-know-about-quota" in err


def test_cli_accepts_default(capsys):
    assert r.main([]) == 0
    out = capsys.readouterr().out
    assert "concurrency: 1" in out
    assert "max_sessions_per_day: 10" in out


def test_cli_accepts_granted_flag(capsys):
    assert r.main(["--concurrency", "3", "--i-know-about-quota"]) == 0
    out = capsys.readouterr().out
    assert "concurrency: 3" in out


def test_cli_refuses_invalid_budget(capsys):
    assert r.main(["--max-sessions-per-day", "0"]) == 2
    err = capsys.readouterr().err
    assert "max_sessions_per_day" in err


# ---------------------------------------------------------------------------
# AC1 — the sequential queue + the overlap guard
# ---------------------------------------------------------------------------


def test_default_concurrency_is_one():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"})
    assert runner.concurrency == 1
    assert runner.guard.max_concurrency == 1


def test_sequential_queue_fifo_order():
    e = Env()
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "no_patch", "i2": "timeout"})
    result = runner.run()
    assert [i for i, _ in result.results] == ["i0", "i1", "i2"]
    assert [s for _, s in result.results] == [
        "resolved", "no_patch", "timeout"]
    assert [c for c, _ in e.calls] == ["i0", "i1", "i2"]
    assert result.final_state == "done"
    assert result.backoffs == 0
    assert result.sessions_completed == 3


def test_overlap_guard_sequential_invariant():
    """The issue #14 "testable by" core: instrumented fake sessions;
    the ``active_sessions == 1`` invariant under interleaved scheduling
    (the observed live count is exactly 1 during every session's
    start, and the high-water mark never exceeds 1)."""
    e = Env()
    observations = []
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "resolved", "i2": "resolved"},
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved",
                       on_start=lambda: observations.append(e.registry.active),
                   ))
    result = runner.run()
    assert e.registry.max == 1  # never more than one live session
    assert observations == [1, 1, 1]  # exactly 1 during every start
    assert runner.guard.max_observed == 1  # the runner's own high-water mark
    assert runner.guard.active == 0  # nothing left held
    assert result.final_state == "done"


def test_overlap_guard_nested_start_violation():
    """The assert fires (fail loud, not a silent wait): a mis-scheduled
    nested session start — while a session is live under the default
    policy — is refused by the guard, and the run continues (the assert
    surfaces the bug; it does not corrupt the schedule)."""
    e = Env()
    violations = []

    def on_start():
        try:
            runner.guard.acquire()  # late binding — runs during run()
        except r.OverlapViolation as exc:
            violations.append(exc)

    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "resolved", "i2": "resolved"},
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved", on_start=on_start))
    result = runner.run()
    assert len(violations) == 3  # every start saw the guard at its cap
    assert e.registry.max == 1  # the violation never leaked a slot
    assert result.final_state == "done"


def test_overlap_guard_release_without_acquire():
    g = r.OverlapGuard(max_concurrency=1)
    with pytest.raises(r.OverlapViolation):
        g.release()
    g.acquire()
    with pytest.raises(r.OverlapViolation):
        g.acquire()  # at the cap
    g.release()
    assert g.active == 0
    assert g.max_observed == 1


class _RaisingRunSession(r.AgentSession):
    """A session whose ``run()`` dies mid-session (PLAN §2.5 host-crash
    class) — the guard slot and the registry window must still close."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.closed = False

    def start(self) -> None:
        self.registry.enter()

    def run(self) -> str:
        raise RuntimeError("partial session")

    def close(self) -> None:
        self.closed = True
        self.registry.exit()


def test_guard_and_claim_released_when_session_raises():
    """A partial session (PLAN §2.5 host-crash class): the exception
    propagates, the guard slot is released (no leak), the budget claim
    stands (the work was consumed), the session's live window closes
    (best-effort close), and the run does not report run_finished (it
    did not finish)."""
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   factory=lambda inst: _RaisingRunSession(e.registry))
    with pytest.raises(RuntimeError, match="partial session"):
        runner.run()
    assert runner.guard.active == 0  # the slot never leaked
    assert runner.budget.sessions_used == 1  # the claim stands (partial)
    assert e.registry.active == 0  # the live window closed (close called)
    assert not any(ev[0] == "run_finished" for ev in e.reporter.events)


def test_factory_failure_releases_claim():
    e = Env()

    def factory(inst):
        e.calls.append((inst, e.clock()))
        raise OSError("docker pull failed")

    runner = r.Runner(["i0", "i1"], factory, clock=e.clock,
                     sleeper=e.sleeper, reporter=e.reporter)
    with pytest.raises(OSError, match="docker pull failed"):
        runner.run()
    assert runner.budget.sessions_used == 0  # released — never started
    assert runner.guard.active == 0


def test_unknown_status_is_typed():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   factory=lambda inst: FakeSession(e.registry, "mystery"))
    with pytest.raises(r.RunnerProtocolError, match="mystery"):
        runner.run()


def test_start_failure_releases_claim_and_closes():
    e = Env()
    sess = FakeSession(e.registry, "resolved",
                      start_raises=OSError("spawn failed"))
    runner = e.make(["i0"], {"i0": "resolved"}, factory=lambda inst: sess)
    with pytest.raises(OSError, match="spawn failed"):
        runner.run()
    assert sess.closed  # best-effort teardown of the half-started session
    assert runner.budget.sessions_used == 0  # released
    assert e.registry.active == 0


# ---------------------------------------------------------------------------
# AC5 — abort on 2 consecutive agent_error
# ---------------------------------------------------------------------------


def test_abort_on_two_consecutive_agent_errors():
    e = Env()
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "agent_error", "i1": "agent_error",
                    "i2": "resolved"})
    result = runner.run()
    assert result.final_state == "aborted"
    assert [i for i, _ in result.results] == ["i0", "i1"]
    assert "i2" not in [c for c, _ in e.calls]  # never started
    assert "2 consecutive agent_error" in result.aborted_reason
    assert chain(result) == [
        ("running", r.EVENT_ABORT, "aborted")]
    assert "sleep" not in [ev[0] for ev in e.reporter.events]  # no backoff


def test_consecutive_agent_error_counter_resets():
    e = Env()
    runner = e.make(["i0", "i1", "i2", "i3"],
                   {"i0": "agent_error", "i1": "resolved",
                    "i2": "agent_error", "i3": "resolved"})
    result = runner.run()
    assert result.final_state == "done"
    assert result.aborted_reason is None
    assert [s for _, s in result.results] == [
        "agent_error", "resolved", "agent_error", "resolved"]


def test_quota_limited_breaks_agent_error_chain():
    """A quota stop is a clean boundary: it resets the drift-suspicion
    counter (and the budget on resume), so an agent_error on each side
    of the backoff does not abort."""
    e = Env()
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "agent_error", "i1": "quota_limited",
                    "i2": "agent_error"})
    result = runner.run()
    assert result.final_state == "done"
    assert result.aborted_reason is None
    assert result.backoffs == 1


# ---------------------------------------------------------------------------
# AC4 — rate-limit signal → stop the batch
# ---------------------------------------------------------------------------


def test_rate_limit_stops_batch_no_starts_after_signal():
    """The issue #14 "testable by" core: rate-limit → stop-batch with
    the no-starts-after-signal assertion, the checkpoint-before-sleep
    ordering, the exact state chain, the sleep landing on the window
    boundary, and the no-per-instance-retry assertion."""
    e = Env()
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "quota_limited",
                    "i2": "resolved"})
    t0 = e.clock()
    result = runner.run()
    # the exact chain: rate_limited → sleeping → resume → running → done
    assert chain(result) == [
        ("running", r.EVENT_RATE_LIMITED, "budget_exhausted"),
        ("budget_exhausted", r.EVENT_SLEEP_START, "sleeping"),
        ("sleeping", r.EVENT_WAKE, "resume"),
        ("resume", r.EVENT_RESET, "running"),
        ("running", r.EVENT_QUEUE_EMPTY, "done"),
    ]
    assert result.final_state == "done"
    assert [s for _, s in result.results] == [
        "resolved", "quota_limited", "resolved"]
    assert result.backoffs == 1
    # no further instance starts after the signal: the factory calls are
    # exactly i0, i1, i2 — in order, i1 never re-called (no per-instance
    # retry), i2 only after the wake.
    assert [c for c, _ in e.calls] == ["i0", "i1", "i2"]
    wake_t = r.next_quota_window(t0)
    assert e.calls[1][1] < wake_t  # the quota instance started in window 1
    assert e.calls[2][1] >= wake_t  # the next instance only in window 2
    # the sleep lands exactly on the window boundary (WP0 §4)
    assert e.sleeper.calls == [wake_t - t0]
    assert e.clock() == wake_t
    # the ordering: mark (the quota_limited record) → checkpoint → sleep
    marks = [i for i, ev in enumerate(e.reporter.events)
            if ev[0] == "instance" and ev[1] == "i1"
            and ev[2] == "quota_limited"]
    cks = [i for i, ev in enumerate(e.reporter.events)
          if ev[0] == "checkpoint"]
    sls = [i for i, ev in enumerate(e.reporter.events)
          if ev[0] == "sleep"]
    assert len(marks) == 1 and len(cks) == 1 and len(sls) == 1
    assert marks[0] < cks[0] < sls[0]  # mark → checkpoint → sleep
    # the checkpoint carries the stop context
    ck = e.reporter.events[cks[0]]
    assert ck[1] == "rate_limited"
    assert ck[2]["queue_remaining"] == 1  # i2 was still queued
    # the budget was reset on resume: i2 (window 2) could claim
    assert result.sessions_completed == 3


def test_local_budget_exhaustion_sleeps_to_window_and_resumes():
    """The local cap (not an external signal) takes the same chain:
    the budget is a per-window allocation — window 1 runs one session,
    sleeps to the reset, window 2 runs the rest."""
    e = Env()
    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=1))
    t0 = e.clock()
    result = runner.run()
    assert [s for _, s in result.results] == ["resolved", "resolved"]
    assert result.backoffs == 1
    assert chain(result) == [
        ("running", r.EVENT_BUDGET_EXHAUSTED, "budget_exhausted"),
        ("budget_exhausted", r.EVENT_SLEEP_START, "sleeping"),
        ("sleeping", r.EVENT_WAKE, "resume"),
        ("resume", r.EVENT_RESET, "running"),
        ("running", r.EVENT_QUEUE_EMPTY, "done"),
    ]
    wake_t = r.next_quota_window(t0)
    assert e.sleeper.calls[0] > 0
    assert e.clock() == wake_t  # slept to the boundary, no earlier


def test_wall_budget_exhaustion():
    """The runner-level wall budget (max_wall × cap): a session that
    burns the whole window's wall budget backoffs the run to the next
    window even though the session count is far from its cap."""
    e = Env()
    cfg = r.BudgetConfig(max_sessions_per_day=100, max_wall_per_session=1.0)
    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   config=cfg,
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved", clock=e.clock, hold_s=100.0))
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 1  # the wall budget (100 s) exhausted after i0
    assert [s for _, s in result.results] == ["resolved", "resolved"]


def test_single_instance_no_sleep_when_done():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=1))
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 0  # done beats the (never reached) exhaustion
    assert "sleep" not in [ev[0] for ev in e.reporter.events]


def test_empty_queue_ends_immediately():
    e = Env()
    runner = e.make([], {})
    result = runner.run()
    assert result.final_state == "done"
    assert result.results == []
    assert result.backoffs == 0
    assert chain(result) == [
        ("running", r.EVENT_QUEUE_EMPTY, "done")]


# ---------------------------------------------------------------------------
# the granted N>1 path (the override is honored, not swallowed)
# ---------------------------------------------------------------------------


def test_concurrency_gt1_granted_runs_concurrently():
    e = Env()
    barrier = threading.Barrier(2, timeout=10)
    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   concurrency=2, i_know_about_quota=True,
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved", barrier=barrier))
    result = runner.run()
    assert e.registry.max == 2  # two live sessions at once — the override
    assert len(result.results) == 2
    assert result.final_state == "done"
    assert result.backoffs == 0


def test_n_gt1_exhausted_drains_without_sleep():
    """A granted N>1 with a 1-session budget: the second worker finds
    the budget exhausted; when the queue has drained by then the run
    ends (done) without a pointless sleep-to-window."""
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=1),
                   concurrency=2, i_know_about_quota=True)
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 0  # no sleep: nothing left to run
    assert [i for i, _ in result.results] == ["i0"]
    assert "sleep" not in [ev[0] for ev in e.reporter.events]


def test_n_gt1_multiple_windows():
    e = Env()
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "resolved",
                    "i2": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=1),
                   concurrency=2, i_know_about_quota=True)
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 2  # one sleep per window boundary crossed
    assert sorted(i for i, _ in result.results) == ["i0", "i1", "i2"]
    assert result.sessions_completed == 3


# ---------------------------------------------------------------------------
# lifecycle edges
# ---------------------------------------------------------------------------


def test_run_is_one_shot():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"})
    runner.run()
    with pytest.raises(r.RunnerError, match="already running"):
        runner.run()


def test_budget_config_validation():
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_sessions_per_day=0)
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_wall_per_session=0.0)
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(daily_pause=-1.0)
    with pytest.raises(r.RunnerConfigError):
        r.OverlapGuard(max_concurrency=0)


def test_process_lock_blocks_sibling_runner(tmp_path):
    lock = tmp_path / "devinp-runner.lock"
    e = Env()
    r1 = e.make(["i0"], {"i0": "resolved"}, lock_file=str(lock))
    r1.run()  # acquires + releases
    # a sibling process holds the lock:
    fd = open(lock, "a")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        e2 = Env()
        r2 = e2.make(["i0"], {"i0": "resolved"}, lock_file=str(lock))
        with pytest.raises(r.ProcessLockBusy):
            r2.run()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    # released: a fresh runner can run
    e3 = Env()
    r3 = e3.make(["i0"], {"i0": "resolved"}, lock_file=str(lock))
    assert r3.run().final_state == "done"


