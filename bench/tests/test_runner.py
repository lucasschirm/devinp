"""WP3a (issue #14) + WP3b (issue #15) + WP3c (issue #16) —
``runner.py``: sequential queue, quota-budget state machine, overlap
guard, checkpoint/resume with pass@1-correct retry semantics, the
dynamic cost-tier model gate, the zero-quota pre-flight, and the
post-eval image/container pruning (PLAN §7).

All tests are unit-level against instrumented fake sessions
(:class:`FakeSession` + :class:`Registry`) and fixtures — zero quota,
no real ``devin`` binary, CI safe. Acceptance mapping (issue #14):

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
  :func:`test_quota_limited_breaks_agent_error_chain`,
  :func:`test_quota_limited_last_item_done_without_sleep`,
  :func:`test_n_gt1_single_checkpoint_for_double_quota_stop` (one
  checkpoint per backoff — a double quota stop is one stop),
  :func:`test_late_quota_limited_upgrades_stop_reason` (mixed-cause
  stop reports the external signal)
* AC5 — abort on 2 consecutive ``agent_error`` (environment drift
  suspicion) → :func:`test_abort_on_two_consecutive_agent_errors`,
  :func:`test_n_gt1_late_agent_error_aborts_through_budget_exhausted`
  (the late-arrival trigger preempts the backoff sleep),
  :func:`test_consecutive_agent_error_counter_resets`,
  :func:`test_window_reset_breaks_agent_error_chain` (the deliberate
  window-reset deviation, pinned)

Plus the contract edges: :func:`test_unknown_status_is_typed`,
:func:`test_factory_failure_releases_claim`,
:func:`test_start_failure_releases_claim_and_closes`,
:func:`test_close_failure_stands_claim_and_propagates`,
:func:`test_single_instance_no_sleep_when_done`,
:func:`test_empty_queue_ends_immediately`,
:func:`test_n_gt1_exhausted_drains_without_sleep`,
:func:`test_n_gt1_multiple_windows`, :func:`test_run_is_one_shot`,
:func:`test_concurrent_run_calls_reject_second`,
:func:`test_concurrent_run_with_lock_file_refuses_busy`,
:func:`test_run_finished_fires_on_clean_done`,
:func:`test_process_lock_busy_is_retryable`,
:func:`test_budget_config_validation`.

WP3b acceptance mapping (issue #15, PLAN §2.4–§2.7):

* AC1 — resume from ``run-<id>.jsonl``: a partial line at EOF is
  dropped, not fatal; no instance runs twice →
  :func:`test_resume_never_rerun_agent_outcomes`,
  :func:`test_resume_only_retry_classes_rerun`,
  :func:`test_resume_no_instance_runs_twice`,
  :func:`test_resume_null_and_stale_lines_ignored`,
  :func:`test_resume_unknown_status_is_typed`,
  :func:`test_load_checkpoint_eof_partial_line_dropped_not_fatal`,
  :func:`test_load_checkpoint_mid_file_corruption_is_typed`,
  :func:`test_load_checkpoint_last_line_valid_json_wrong_shape_dropped`,
  :func:`test_load_checkpoint_missing_file_is_fresh_run`,
  :func:`test_load_checkpoint_trailing_newline_not_dropped`,
  :func:`test_jsonl_checkpoint_roundtrip`,
  :func:`test_jsonl_checkpoint_refuses_unknown_status`,
  :func:`test_resume_from_checkpoint_convenience`,
  :func:`test_checkpoint_resume_e2e_host_crash_retry_class`,
  :func:`test_checkpoint_resume_torn_tail_repaired`,
  :func:`test_checkpoint_resume_torn_tail_complete_json_no_newline`,
  :func:`test_checkpoint_resume_mid_file_corruption_refuses_repair`,
  :func:`test_checkpoint_resume_newline_terminated_non_record_last_line_repaired`,
  :func:`test_checkpoint_resume_blank_last_line_repaired`,
  :func:`test_checkpoint_resume_non_ascii_prefix_byte_boundary`,
  :func:`test_checkpoint_resume_torn_tail_mid_multibyte_sequence_dropped`,
  :func:`test_checkpoint_resume_mid_file_invalid_utf8_in_string_is_typed`,
  :func:`test_checkpoint_resume_torn_tail_invalid_utf8_newline_terminated`,
  :func:`test_resume_queue_non_dict_record_is_typed`
* AC2 — pass@1 retry classes → :func:
  `test_resume_never_rerun_agent_outcomes`, :func:
  `test_resume_only_retry_classes_rerun`, :func:
  `test_preflight_failure_is_retry_eligible_on_resume`, :func:
  `test_preflight_failure_resume_cycle`
* AC3 — startup model gate (fail-closed) → :func:
  `test_gate_free_model_passes`, :func:`test_gate_paid_model_refused`,
  :func:`test_gate_absent_tier_refused_fail_closed`, :func:
  `test_gate_unknown_model_refused_fail_closed`, :func:
  `test_gate_paid_override_requires_explicit_config_and_budget`,
  :func:`test_gate_shape_changes_refused_fail_closed`, :func:
  `test_gate_shape_validator_direct`, :func:
  `test_gate_live_shape_against_mock_devin`, :func:
  `test_fetch_models_list_via_mock_devin`, :func:
  `test_fetch_models_list_fail_closed`, :func:
  `test_cli_gate_free_model_passes`, :func:
  `test_cli_gate_paid_model_refused`, :func:
  `test_cli_gate_shape_change_refused`, :func:
  `test_cli_gate_paid_override`, :func:
  `test_cli_gate_paid_override_without_budget`, :func:
  `test_cli_gate_live_devin_bin`, :func:
  `test_cli_gate_flags_without_model_refused`, :func:
  `test_cli_models_json_and_devin_bin_mutually_exclusive`, :func:
  `test_fetch_models_list_none_stdout_fail_closed`, :func:
  `test_fetch_models_list_missing_stdout_attr_fail_closed`, :func:
  `test_fetch_models_list_missing_returncode_fail_closed`, :func:
  `test_gate_inf_token_budget_refused`, :func:
  `test_next_quota_window_wall_clock_alignment`
* AC4 — zero-quota pre-flight before every batch and each instance →
  :func:`test_preflight_checks_short_circuit_at_first_failure`,
  :func:`test_preflight_checks_all_ok`, :func:
  `test_preflight_checks_wraps_foreign_exceptions`, :func:
  `test_preflight_all_ok_with_stubs`, :func:
  `test_preflight_default_image_for_matches_wp0_pin`, :func:
  `test_preflight_stub_failure_legs`, :func:
  `test_preflight_hung_daemon_is_a_failure`, :func:
  `test_preflight_config_validation`, :func:
  `test_preflight_batch_failure_aborts_before_any_session`, :func:
  `test_preflight_instance_failure_marks_releases_claim_aborts`,
  :func:`test_preflight_batch_reruns_each_window`, :func:
  `test_preflight_failure_resume_cycle`, :func:
  `test_preflight_wall_excluded_from_session_wall`, :func:
  `test_wall_clock_step_back_clamps_to_zero`

WP3c acceptance mapping (issue #16, PLAN §7):

* AC1 — after each evaluated instance: ``docker rmi
  sweb.eval.x86_64.<instance_id>`` + remove exited containers →
  :func:`test_pruner_removes_instance_image_and_exited_containers`
  (the exact ``rmi`` of the WP0-pinned reference incl. the ``__``
  → ``_1776_`` escape; the agent + eval containers removed; the
  ordering: the containers before the ``rmi``; a running container
  and the base image untouched), :func:`test_runner_prunes_after_
  evaluated_instances` (the never-re-run trigger matrix, the
  post-mark ordering), :func:`test_pruner_running_container_blocks_
  rmi` (no force)
* AC2 — shared base/env layers retained → :func:
  `test_pruner_base_env_layers_retained` (the layer-refcount model:
  only the instance-unique layer drops)
* AC3 — pre-flight disk check sized 2 × the largest expected image
  (WP3b ``Preflight.min_free_bytes``; the boundary behavior is
  pinned by ``test_preflight_stub_failure_legs``) → :func:
  `test_preflight_disk_sizing_two_x_largest_image`, :func:
  `test_pruner_default_image_for_matches_preflight` (the pre-flight
  checks what the pruner removes)

Plus the pruning discipline: :func:`test_pruner_docker_failures_
recorded_not_fatal` (rc failures, a missing image (an idempotent
double-prune), a hung-daemon timeout), :func:
`test_pruner_timeout_is_passed_and_validated`, :func:
`test_runner_prune_failure_not_fatal`, :func:
`test_runner_broken_pruner_is_fatal` (a raising pruner is a contract
bug → host-crash-class fatal), :func:
`test_runner_no_prune_for_retry_statuses` (the retry class retains
its image for the resume), :func:`test_runner_prune_under_n_gt1`
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import runner as r

#: The WP2 mock double — the gate's live-shape integration (zero quota).
MOCK_PATH = str(Path(__file__).resolve().parent.parent / "mock_devin.py")


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
        reporter = kw.pop("reporter", self.reporter)

        def factory(inst: str) -> r.AgentSession:
            self.calls.append((inst, self.clock()))
            if custom is not None:
                return custom(inst)
            return FakeSession(self.registry, statuses[inst], clock=self.clock)

        return r.Runner(
            instances, factory, clock=self.clock, sleeper=self.sleeper,
            reporter=reporter, **kw
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


class _RaisingCloseSession(r.AgentSession):
    """A session whose ``close()`` fails mid-teardown (PLAN §1: close
    must kill the process group; a failed close is best-effort and
    idempotent) — the live window still closes."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.close_calls = 0
        self._window_closed = False

    def start(self) -> None:
        self.registry.enter()

    def run(self) -> str:
        return "resolved"

    def close(self) -> None:
        self.close_calls += 1
        if not self._window_closed:  # idempotent: the window closes once
            self._window_closed = True
            self.registry.exit()
        raise RuntimeError("teardown failed")


class _PollStateSession(r.AgentSession):
    """A session whose ``run()`` holds until the runner leaves
    ``RUNNING`` (the cap emit lands) before returning its status — the
    deterministic construction for the late-arrival N>1 corners (the
    (budget_exhausted, abort) preemption; the mixed-cause stop
    reason). A loud timeout: a hang is a test failure, not a hang."""

    def __init__(self, registry: Registry, runner: r.Runner,
                 status: str) -> None:
        self.registry = registry
        self.runner = runner
        self.status = status

    def start(self) -> None:
        self.registry.enter()

    def run(self) -> str:
        deadline = time.monotonic() + 5.0
        while self.runner.state is r.BackoffState.RUNNING:
            if time.monotonic() > deadline:
                raise RuntimeError("poll timeout — cap emit never "
                                  "landed")
            time.sleep(0.001)
        return self.status

    def close(self) -> None:
        self.registry.exit()


class _HoldingSession(r.AgentSession):
    """A session that holds *real* time in ``start()`` — keeps the run
    in flight long enough for a concurrent ``run()`` to be attempted."""

    def start(self) -> None:
        time.sleep(0.3)

    def run(self) -> str:
        return "resolved"

    def close(self) -> None:
        pass


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


def test_close_failure_stands_claim_and_propagates():
    """A failed ``close()`` on a *completed* session: the claim stands
    (the work was consumed), the guard slot releases, the live window
    closes (the idempotent close killed the process group), the
    exception propagates, and ``run_finished`` is *not* reported."""
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   factory=lambda inst: _RaisingCloseSession(e.registry))
    with pytest.raises(RuntimeError, match="teardown failed"):
        runner.run()
    assert runner.budget.sessions_used == 1  # the claim stands
    assert runner.guard.active == 0  # the slot never leaked
    assert e.registry.active == 0  # the live window closed
    assert not any(ev[0] == "run_finished"
                   for ev in e.reporter.events)


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


def test_window_reset_breaks_agent_error_chain():
    """The deliberate AC5 deviation, pinned: the drift-suspicion counter
    resets on the window boundary (a new day is a fresh suspicion —
    the drift evidence is window-scoped), so agent_error →
    local-budget sleep → agent_error ends ``done``, not ``aborted``.
    (The reset by *any other terminal status* still holds —
    see ``test_consecutive_agent_error_counter_resets``.)"""
    e = Env()
    runner = e.make(["i0", "i1"],
                   {"i0": "agent_error", "i1": "agent_error"},
                   config=r.BudgetConfig(max_sessions_per_day=1))
    result = runner.run()
    assert result.final_state == "done"
    assert result.aborted_reason is None
    assert result.backoffs == 1
    assert [s for _, s in result.results] == ["agent_error",
                                            "agent_error"]


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
    # the checkpoint (AC4) fired before the sleep, with the local-cap
    # stop reason (the external signal's is "rate_limited")
    cks = [i for i, ev in enumerate(e.reporter.events)
          if ev[0] == "checkpoint"]
    sls = [i for i, ev in enumerate(e.reporter.events)
          if ev[0] == "sleep"]
    assert len(cks) == 1
    assert e.reporter.events[cks[0]][1] == "budget_exhausted"
    assert sls and cks[0] < sls[0]


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
    # every backoff sleep is checkpointed (AC4), with the stop reason
    cks = [ev for ev in e.reporter.events if ev[0] == "checkpoint"]
    assert len(cks) == 1
    assert cks[0][1] == "budget_exhausted"


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


def test_quota_limited_last_item_done_without_sleep():
    """The queue-empty check *inside* the exhausted branch (the N>1
    race's sequential form): the last instance is ``quota_limited`` —
    the batch stops, but there is nothing left to run, so the run ends
    ``done`` without a pointless sleep-to-window (the instance is
    marked in the results; its re-run is WP3b's checkpoint-resume)."""
    e = Env()
    runner = e.make(["i0"], {"i0": "quota_limited"})
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 0
    assert [s for _, s in result.results] == ["quota_limited"]
    assert "sleep" not in [ev[0] for ev in e.reporter.events]
    assert chain(result)[-1] == ("budget_exhausted",
                                r.EVENT_QUEUE_EMPTY, "done")


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


def test_n_gt1_single_checkpoint_for_double_quota_stop():
    """Two workers both finishing ``quota_limited`` in flight (N>1)
    stop the batch *once*: exactly one checkpoint per backoff (the
    drain is single-threaded — the pool has joined), not one per
    worker. The barrier forces both claims to be in flight (cap 2), so
    the double stop is the same stop — one backoff, one checkpoint."""
    e = Env()
    barrier = threading.Barrier(2, timeout=10)

    def factory(inst):
        if inst in ("i0", "i1"):  # both held in flight until both start
            return FakeSession(e.registry, "quota_limited", barrier=barrier)
        return FakeSession(e.registry, "resolved")

    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "quota_limited", "i1": "quota_limited",
                    "i2": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=2),
                   concurrency=2, i_know_about_quota=True, factory=factory)
    result = runner.run()
    cks = [ev for ev in e.reporter.events if ev[0] == "checkpoint"]
    assert len(cks) == 1  # the double stop yields one checkpoint
    assert cks[0][1] == "rate_limited"
    assert result.backoffs == 1
    assert result.final_state == "done"
    assert sorted(i for i, _ in result.results) == ["i0", "i1", "i2"]


def test_n_gt1_late_agent_error_aborts_through_budget_exhausted():
    """(budget_exhausted, abort) end-to-end (the N>1 late-arrival
    drift trigger): the second agent_error lands *after* a worker's
    claim-check has already emitted budget_exhausted (cap 2, both
    claims in flight) — the abort preempts the backoff sleep via the
    table transition; no sleep, i2 never starts."""
    e = Env()
    holder = {}

    def factory(inst):
        if inst == "i1":  # held until the cap emit lands
            return _PollStateSession(e.registry, holder["runner"],
                                    "agent_error")
        return FakeSession(e.registry, "agent_error" if inst == "i0"
                          else "resolved")

    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "agent_error", "i1": "agent_error",
                    "i2": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=2),
                   concurrency=2, i_know_about_quota=True, factory=factory)
    holder["runner"] = runner
    result = runner.run()
    assert result.final_state == "aborted"
    assert ("budget_exhausted", r.EVENT_ABORT, "aborted") \
        in chain(result)  # the late trigger took the table transition
    assert "sleep" not in [ev[0] for ev in e.reporter.events]
    assert result.backoffs == 0  # the abort preempted the backoff
    assert "i2" not in [c for c, _ in e.calls]  # never started


def test_late_quota_limited_upgrades_stop_reason():
    """Mixed-cause stop (N>1): the local cap stops the batch first
    (budget_exhausted), then an in-flight session lands
    ``quota_limited`` — the checkpoint reason is upgraded to
    ``rate_limited`` (the external signal is the more operationally
    significant cause); the mark survives in the instance record."""
    e = Env()
    holder = {}

    def factory(inst):
        if inst == "i0":  # held until the local-cap emit lands
            return _PollStateSession(e.registry, holder["runner"],
                                    "quota_limited")
        return FakeSession(e.registry, "resolved")

    runner = e.make(["i0", "i1"],
                   {"i0": "quota_limited", "i1": "resolved"},
                   config=r.BudgetConfig(max_sessions_per_day=1),
                   concurrency=2, i_know_about_quota=True, factory=factory)
    holder["runner"] = runner
    result = runner.run()
    cks = [ev for ev in e.reporter.events if ev[0] == "checkpoint"]
    assert len(cks) == 1
    assert cks[0][1] == "rate_limited"  # the late signal upgraded it
    assert [s for _, s in result.results] == ["quota_limited", "resolved"]
    assert result.backoffs == 1
    assert result.final_state == "done"


# ---------------------------------------------------------------------------
# lifecycle edges
# ---------------------------------------------------------------------------


def test_run_is_one_shot():
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"})
    runner.run()
    with pytest.raises(r.RunnerError, match="already running"):
        runner.run()


def test_concurrent_run_calls_reject_second():
    """The one-shot check-and-set is atomic under the runner lock: two
    concurrent ``run()`` calls on the same instance — exactly one
    wins, the other is refused (not double-scheduled)."""
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   factory=lambda inst: _HoldingSession())
    barrier = threading.Barrier(2, timeout=10)
    outcomes = []

    def go():
        barrier.wait()
        try:
            outcomes.append(("ok", runner.run().final_state))
        except r.RunnerError:
            outcomes.append(("refused", None))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert sorted(k for k, _ in outcomes) == ["ok", "refused"]


def test_concurrent_run_with_lock_file_refuses_busy(tmp_path):
    """The documented lock_file corner: a *concurrent* second run() on
    the same instance is refused as ProcessLockBusy (the cross-process
    lock is checked first — what keeps a lock-busy failure
    retryable), not "already running"."""
    lock = tmp_path / "devinp-runner.lock"
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"}, lock_file=str(lock),
                    factory=lambda inst: _HoldingSession())
    barrier = threading.Barrier(2, timeout=10)
    outcomes = []

    def go():
        barrier.wait()
        try:
            runner.run()
            outcomes.append("ok")
        except r.ProcessLockBusy:
            outcomes.append("busy")
        except r.RunnerError:
            outcomes.append("refused")

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert sorted(outcomes) == ["busy", "ok"]


def test_run_finished_fires_on_clean_done():
    """The ``run_finished`` hook fires on normal completion (and only
    then — the propagated-failure absence assertions in the error-path
    tests are no longer vacuous): exactly once, last, after every
    instance mark, carrying the final state."""
    e = Env()
    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "no_patch"})
    runner.run()
    events = e.reporter.events
    fins = [i for i, ev in enumerate(events) if ev[0] == "run_finished"]
    marks = [i for i, ev in enumerate(events)
            if ev[0] == "instance"]
    assert fins == [len(events) - 1]  # exactly once, last
    assert events[fins[0]] == ("run_finished", "done")
    assert marks and fins[0] > max(marks)


def test_process_lock_busy_is_retryable(tmp_path):
    """A ``ProcessLockBusy`` failure does not consume the one-shot: the
    runner may retry once the sibling releases (the cross-process lock
    is acquired before ``_running`` flips)."""
    lock = tmp_path / "devinp-runner.lock"
    e = Env()
    r1 = e.make(["i0"], {"i0": "resolved"}, lock_file=str(lock))
    fd = open(lock, "a")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(r.ProcessLockBusy):
            r1.run()
        with pytest.raises(r.ProcessLockBusy):  # still busy, still retryable
            r1.run()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    assert r1.run().final_state == "done"  # the retry works


def test_budget_config_validation():
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_sessions_per_day=0)
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_sessions_per_day=True)  # bool refused uniformly
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_wall_per_session=0.0)
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(max_wall_per_session=True)  # bool refused uniformly
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(daily_pause=-1.0)
    with pytest.raises(r.RunnerConfigError):
        r.BudgetConfig(daily_pause=True)  # bool refused uniformly
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




# ---------------------------------------------------------------------------
# WP3b (issue #15) — checkpoint / resume with pass@1-correct semantics
# ---------------------------------------------------------------------------


def _fixture_lines() -> list:
    """One record per never-re-run status in the taxonomy (the resume
    fixture)."""
    return [
        {"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0},
        {"instance_id": "i1", "status": "failed_test", "meta": {}, "ts": 2.0},
        {"instance_id": "i2", "status": "timeout", "meta": {}, "ts": 3.0},
        {"instance_id": "i3", "status": "no_patch", "meta": {}, "ts": 4.0},
        {"instance_id": "i4", "status": "agent_error", "meta": {}, "ts": 5.0},
    ]


def test_resume_never_rerun_agent_outcomes():
    """AC2: ``resolved`` / ``failed_test`` / ``timeout`` / ``no_patch``
    (and ``agent_error`` — a terminal agent outcome, PLAN §3.4) are
    **never re-run**."""
    to_run, skipped = r.resume_queue(["i0", "i1", "i2", "i3", "i4"],
                                    _fixture_lines())
    assert to_run == []
    assert skipped == {
        "i0": "resolved", "i1": "failed_test", "i2": "timeout",
        "i3": "no_patch", "i4": "agent_error"}


def test_resume_only_retry_classes_rerun():
    """AC2: only ``quota_limited`` / ``eval_error`` /
    ``preflight_failed`` (and the no-line host-crash state — i8)
    re-run; never-re-run stays skipped; queue order is preserved."""
    recs = _fixture_lines() + [
        {"instance_id": "i5", "status": "quota_limited", "meta": {},
         "ts": 6.0},
        {"instance_id": "i6", "status": "eval_error", "meta": {}, "ts": 7.0},
        {"instance_id": "i7", "status": "preflight_failed", "meta": {},
         "ts": 8.0},
    ]
    to_run, skipped = r.resume_queue(
        ["i0", "i5", "i7", "i8", "i6", "i3"], recs)
    # i8: no line (a host-crash partial state / never ran) -> re-run
    assert to_run == ["i5", "i7", "i8", "i6"]
    assert skipped == {"i0": "resolved", "i3": "no_patch"}


def test_resume_no_instance_runs_twice():
    """AC1: a duplicated line (a double-write) resolves to the *last*
    line; the instance appears at most once in the re-run queue."""
    recs = [
        {"instance_id": "i0", "status": "quota_limited", "meta": {},
         "ts": 1.0},
        {"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 2.0},
    ]
    to_run, skipped = r.resume_queue(["i0", "i1"], recs)
    assert to_run == ["i1"]
    assert skipped == {"i0": "resolved"}  # the last line wins


def test_resume_null_and_stale_lines_ignored():
    recs = [
        {"instance_id": None, "status": "preflight_failed", "meta": {},
         "ts": 1.0},
        {"instance_id": "stale", "status": "quota_limited", "meta": {},
         "ts": 2.0},
        {"instance_id": "i0", "status": "quota_limited", "meta": {},
         "ts": 3.0},
    ]
    to_run, _ = r.resume_queue(["i0", "i1"], recs)
    assert to_run == ["i0", "i1"]


def test_resume_unknown_status_is_typed():
    recs = [{"instance_id": "i0", "status": "mystery", "meta": {},
             "ts": 1.0}]
    with pytest.raises(r.RunnerProtocolError, match="mystery"):
        r.resume_queue(["i0"], recs)


def test_load_checkpoint_eof_partial_line_dropped_not_fatal(tmp_path):
    """AC1: a kill mid-write leaves a partial line at EOF — it is
    dropped (not fatal); the complete lines survive."""
    p = tmp_path / "run-1.jsonl"
    p.write_text(
        '{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n'
        '{"instance_id": "i1", "status": "quota_lim'  # the torn write
    )
    records, dropped = r.load_checkpoint(str(p))
    assert dropped is True
    assert len(records) == 1
    assert records[0]["instance_id"] == "i0"
    to_run, _ = r.resume_queue(["i0", "i1", "i2"], records)
    assert to_run == ["i1", "i2"]  # i0 stands; the torn i1 line drops


def test_load_checkpoint_mid_file_corruption_is_typed(tmp_path):
    """A corrupted line *anywhere but the last* is fail-loud (a
    mid-file line was fsync'd complete — garbage there is not a kill
    artifact)."""
    p = tmp_path / "run-1.jsonl"
    p.write_text(
        '{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n'
        'GARBAGE-NOT-JSON\n'
        '{"instance_id": "i2", "status": "timeout", "meta": {}, "ts": 3.0}\n'
    )
    with pytest.raises(r.RunnerProtocolError, match="line 2"):
        r.load_checkpoint(str(p))


def test_load_checkpoint_last_line_valid_json_wrong_shape_dropped(tmp_path):
    """A complete JSON line with the wrong shape at EOF is a torn
    write too (a kill can land after the JSON is complete but the
    shape is indistinguishable) — dropped, not fatal."""
    p = tmp_path / "run-1.jsonl"
    p.write_text(
        '{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n'
        '{"instance_id": "i1", "oops": true}\n'
    )
    records, dropped = r.load_checkpoint(str(p))
    assert dropped is True
    assert len(records) == 1


def test_load_checkpoint_missing_file_is_fresh_run(tmp_path):
    records, dropped = r.load_checkpoint(str(tmp_path / "nope.jsonl"))
    assert records == [] and dropped is False


def test_load_checkpoint_trailing_newline_not_dropped(tmp_path):
    p = tmp_path / "run-1.jsonl"
    p.write_text(
        '{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n'
    )
    records, dropped = r.load_checkpoint(str(p))
    assert dropped is False
    assert len(records) == 1


def test_resume_from_checkpoint_convenience(tmp_path):
    p = tmp_path / "run-1.jsonl"
    p.write_text(
        '{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n'
        '{"instance_id": "i1", "status": "quota_limited", "meta": {}, '
        '"ts": 2.0}\n'
    )
    to_run, skipped = r.resume_from_checkpoint(str(p), ["i0", "i1", "i2"])
    assert to_run == ["i1", "i2"]
    assert skipped == {"i0": "resolved"}


def test_jsonl_checkpoint_roundtrip(tmp_path):
    """A run through a :class:`~runner.JsonlCheckpoint` (the WP3a
    ``checkpoint`` hook is a no-op — the per-line flush is the
    checkpoint): one durable line per finished instance; the line
    shape; the rate-limit stop checkpoint loses nothing; the resume
    decision off the file (i0 skipped, i1 re-run, i2 fresh)."""
    e = Env()
    ckpt = r.JsonlCheckpoint(str(tmp_path / "run-1.jsonl"), clock=e.clock)
    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "quota_limited",
                    "i2": "resolved"},
                   reporter=ckpt)
    result = runner.run()
    ckpt.close()
    assert result.final_state == "done"
    records, dropped = r.load_checkpoint(str(tmp_path / "run-1.jsonl"))
    assert dropped is False
    assert [rec["instance_id"] for rec in records] == ["i0", "i1", "i2"]
    assert [rec["status"] for rec in records] == [
        "resolved", "quota_limited", "resolved"]
    assert all(rec["ts"] >= 1_000_000.0 for rec in records)
    assert all("meta" in rec for rec in records)
    to_run, skipped = r.resume_queue(["i0", "i1", "i2"], records)
    assert to_run == ["i1"]  # quota_limited is the only re-run
    assert skipped == {"i0": "resolved", "i2": "resolved"}


def test_jsonl_checkpoint_refuses_unknown_status(tmp_path):
    ckpt = r.JsonlCheckpoint(str(tmp_path / "run-1.jsonl"),
                            clock=lambda: 1.0)
    with pytest.raises(r.RunnerProtocolError, match="mystery"):
        ckpt.record_instance("i0", "mystery", {})
    ckpt.close()


def test_checkpoint_resume_e2e_host_crash_retry_class(tmp_path):
    """The kill → resume story at the WP3b unit level (the WP2
    acceptance criterion): run 1 ends in a host crash (a partial
    session on i2 — *no* line for i2); the file has exactly the two
    complete lines; the resume queue re-runs ``quota_limited`` (i1) +
    the crash partial (i2), skips ``resolved`` (i0); run 2's factory
    calls are exactly [i1, i2] — no instance runs twice; the final
    file has four lines (i1 twice: ``quota_limited`` then
    ``no_patch`` — the last line is the record of truth); a resume
    *after* run 2 re-runs nothing (``no_patch`` is terminal)."""
    p = str(tmp_path / "run-1.jsonl")
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)

    def factory(inst):
        e.calls.append((inst, e.clock()))
        if inst == "i2":
            raise OSError("host crash mid-session")  # the partial state
        return FakeSession(e.registry,
                          {"i0": "resolved", "i1": "quota_limited"}[inst],
                          clock=e.clock)

    r1 = r.Runner(["i0", "i1", "i2"], factory, clock=e.clock,
                  sleeper=e.sleeper, reporter=ckpt)
    with pytest.raises(OSError, match="host crash"):
        r1.run()
    ckpt.close()
    records, dropped = r.load_checkpoint(p)
    assert dropped is False
    assert [(rec["instance_id"], rec["status"]) for rec in records] == [
        ("i0", "resolved"), ("i1", "quota_limited")]
    to_run, skipped = r.resume_queue(["i0", "i1", "i2"], records)
    assert to_run == ["i1", "i2"]  # the crash partial + quota re-run
    assert skipped == {"i0": "resolved"}
    # run 2 (the crash is gone; i1 lands no_patch this time)
    e2 = Env()
    calls2 = []

    def factory2(inst):
        calls2.append(inst)
        return FakeSession(e2.registry,
                          {"i1": "no_patch", "i2": "resolved"}[inst],
                          clock=e2.clock)

    ckpt2 = r.JsonlCheckpoint(p, clock=e2.clock)
    r2 = r.Runner(to_run, factory2, clock=e2.clock,
                  sleeper=e2.sleeper, reporter=ckpt2)
    result2 = r2.run()
    ckpt2.close()
    assert calls2 == ["i1", "i2"]  # no instance runs twice in the cycle
    assert result2.final_state == "done"
    recs2, _ = r.load_checkpoint(p)
    assert len(recs2) == 4
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1", "i1", "i2"]
    assert [rec["status"] for rec in recs2] == [
        "resolved", "quota_limited", "no_patch", "resolved"]
    to_run3, _ = r.resume_queue(["i0", "i1", "i2"], recs2)
    assert to_run3 == []  # no_patch is terminal — nothing re-runs


# ---------------------------------------------------------------------------
# WP3b (issue #15) — the startup cost-tier model gate (PLAN §2.4)
# ---------------------------------------------------------------------------


def _gate_fixture() -> dict:
    """The WP0 shape (the ``notes/models-list.json`` capture) — Free,
    paid (Low + High cost, incl. ``*-lightning``), a non-SWE family
    (a gate keyed on the "swe" prefix would miss it), and the
    absent-tier ``adaptive`` router."""
    return {
        "families": [
            {"family_label": "SWE-2", "family_uid": "swe-2", "slug": "swe-2",
             "aliases": ["swe"], "variants": [
                {"model_uid": "swe-2-high", "label": "SWE-2 High",
                 "cost_tier": "Free"},
                {"model_uid": "swe-2-medium", "label": "SWE-2 Medium",
                 "cost_tier": "Free"},
                {"model_uid": "swe-2-max", "label": "SWE-2 Max",
                 "cost_tier": "Free"},
                {"model_uid": "swe-2-high-lightning",
                 "label": "SWE-2 High Lightning", "cost_tier": "High cost"},
            ]},
            {"family_label": "SWE-1.7", "family_uid": "swe-1.7",
             "slug": "swe-1.7", "aliases": [], "variants": [
                {"model_uid": "swe-1-7", "label": "SWE-1.7 Max",
                 "cost_tier": "Low cost"},
                {"model_uid": "swe-1-6", "label": "SWE-1.6",
                 "cost_tier": "Low cost"},
            ]},
            {"family_label": "Claude Opus 4.6",
             "family_uid": "claude-opus-4.6", "slug": "claude-opus-4.6",
             "aliases": [], "variants": [
                {"model_uid": "claude-opus-4-6", "label": "Claude Opus 4.6",
                 "cost_tier": "High cost"},
            ]},
            # the absent-tier router (WP0 §7) — an unverified tier is
            # not Free
            {"family_label": "Adaptive", "family_uid": "Adaptive",
             "slug": "adaptive", "aliases": [], "variants": [
                {"model_uid": "adaptive", "label": "Adaptive"},
            ]},
        ]
    }


def test_gate_free_model_passes():
    g = r.gate_cost_tier(_gate_fixture(), "swe-2-high")
    assert g.allowed
    assert g.cost_tier == "Free"
    assert g.reason is None
    assert g.paid_override is False
    assert g.free_models == ("swe-2-high", "swe-2-max", "swe-2-medium")


def test_gate_paid_model_refused():
    """Every paid tier in the fixture is refused — incl. the
    ``*-lightning`` variants and the non-SWE family (the gate keys on
    ``cost_tier``, never on names)."""
    for uid in ("swe-1-7", "swe-1-6", "swe-2-high-lightning",
               "claude-opus-4-6"):
        g = r.gate_cost_tier(_gate_fixture(), uid)
        assert not g.allowed, uid
        assert "not Free" in g.reason
        assert "refused" in g.reason


def test_gate_absent_tier_refused_fail_closed():
    g = r.gate_cost_tier(_gate_fixture(), "adaptive")
    assert not g.allowed
    assert g.cost_tier == "unknown"  # the absent tier is recorded as such
    assert "not Free" in g.reason  # unverified ≠ free — fail-closed


def test_gate_unknown_model_refused_fail_closed():
    g = r.gate_cost_tier(_gate_fixture(), "gpt-who-dis")
    assert not g.allowed
    assert g.cost_tier is None
    assert "cannot be verified" in g.reason


def test_gate_paid_override_requires_explicit_config_and_budget():
    """AC3: the paid override requires **both** an explicit config
    override **and** a token budget — either leg alone leaves the
    refusal."""
    fx = _gate_fixture()
    # explicit config, no budget -> refused
    g = r.gate_cost_tier(fx, "swe-1-7", allow_paid=True)
    assert not g.allowed
    assert "token budget" in g.reason
    # an invalid budget (0 / negative / bool) -> refused
    for bad in (0, -5, True):
        g = r.gate_cost_tier(fx, "swe-1-7", allow_paid=True,
                            token_budget=bad)
        assert not g.allowed, bad
    # no explicit config + a budget -> refused (the budget alone is not
    # the override)
    g = r.gate_cost_tier(fx, "swe-1-7", token_budget=1000)
    assert not g.allowed
    # explicit config + a valid budget -> allowed, and marked as an
    # override
    g = r.gate_cost_tier(fx, "swe-1-7", allow_paid=True,
                         token_budget=1000)
    assert g.allowed
    assert g.paid_override is True
    # the override cannot bless a model missing from the list
    # (fail-closed — its tier cannot be verified)
    g = r.gate_cost_tier(fx, "gpt-who-dis", allow_paid=True,
                         token_budget=1000)
    assert not g.allowed


def test_gate_shape_changes_refused_fail_closed():
    """AC3: a JSON shape change is a *typed* refusal — the batch never
    starts (PLAN §2.4). Each of these is a structural deviation from
    the WP0 capture."""
    shape_breaks = [
        "not-a-dict",              # the top level
        [],                       # the top level
        {"families": "nope"},     # families not a list
        {},                       # families missing
        {"families": [{}]},       # a family without variants
        {"families": [{"variants": "nope"}]},  # variants not a list
        {"families": [{"variants": [{}]}]},    # a variant without model_uid
        {"families": [{"variants": [{"model_uid": 42}]}]},
        {"families": [{"variants":
                      [{"model_uid": "x", "cost_tier": 3}]}]},
        {"families": ["nope"]},   # a non-dict family
        {"families": [{"variants": ["nope"]}]},  # a non-dict variant
    ]
    for bad in shape_breaks:
        with pytest.raises(r.ModelGateError, match="shape change"):
            r.gate_cost_tier(bad, "swe-2-high")
    # an *empty* list is well-shaped — but nothing is verifiable, so
    # the configured model is refused (a semantic refusal, not a
    # shape one)
    g = r.gate_cost_tier({"families": []}, "swe-2-high")
    assert not g.allowed


def test_gate_shape_validator_direct():
    tiers = r.parse_models_list(_gate_fixture())
    assert tiers["swe-2-high"] == "Free"
    assert tiers["swe-1-7"] == "Low cost"
    assert tiers["swe-2-high-lightning"] == "High cost"
    assert tiers["adaptive"] == "unknown"  # the absent tier
    # an empty variants list is well-shaped (nothing to gate on)
    assert r.parse_models_list({"families": [{"variants": []}]}) == {}


def test_gate_live_shape_against_mock_devin():
    """The gate against the *real* WP2 fixture (the ``mock_devin
    models list --format json`` subprocess) — the shape the WP0
    capture recorded, end to end, zero quota: a Free model passes, a
    paid one is refused with its tier in the reason, and the shape
    validator accepts the live fixture (no shape change)."""
    out = subprocess.run(
        [sys.executable, MOCK_PATH, "models", "list", "--format", "json"],
        capture_output=True, timeout=30)
    assert out.returncode == 0
    models_json = json.loads(out.stdout.decode())
    assert r.gate_cost_tier(models_json, "swe-2-high").allowed
    g = r.gate_cost_tier(models_json, "swe-1-7")
    assert not g.allowed
    assert "Low cost" in g.reason
    tiers = r.parse_models_list(models_json)
    assert "swe-2-max" in tiers
    assert "claude-opus-4-6" in tiers
    assert tiers["adaptive"] == "unknown"


def _make_wrapper(tmp_path, target, name="fakebin") -> str:
    """A bash wrapper that execs ``target "$@"`` — the devin_bin seam
    (the mock is a .py script; a single-string binary can't carry an
    interpreter)."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    w = d / name
    w.write_text(f"#!/bin/bash\nexec {target} \"$@\"\n")
    w.chmod(0o755)
    return str(w)


def test_fetch_models_list_via_mock_devin(tmp_path):
    w = _make_wrapper(tmp_path, f"\"{sys.executable}\" \"{MOCK_PATH}\"")
    models_json = r.fetch_models_list(w)
    assert isinstance(models_json, dict)
    assert r.gate_cost_tier(models_json, "swe-2-high").allowed
    assert not r.gate_cost_tier(models_json, "swe-1-7").allowed


def test_fetch_models_list_fail_closed(tmp_path):
    bad = tmp_path / "baddevin"
    bad.write_text("#!/bin/bash\necho 'quota error' >&2\nexit 1\n")
    bad.chmod(0o755)
    with pytest.raises(r.ModelGateError, match="fail-closed"):
        r.fetch_models_list(str(bad))
    badjson = tmp_path / "badjson"
    badjson.write_text("#!/bin/bash\necho 'not json'\nexit 0\n")
    badjson.chmod(0o755)
    with pytest.raises(r.ModelGateError, match="fail-closed"):
        r.fetch_models_list(str(badjson))
    with pytest.raises(r.ModelGateError, match="fail-closed"):
        r.fetch_models_list(str(tmp_path / "nope-bin"))  # unrunnable
    # a hung binary (timeout) is a refusal too
    with pytest.raises(r.ModelGateError, match="fail-closed"):
        r.fetch_models_list(str(bad), timeout_s=0.000001)


# ---------------------------------------------------------------------------
# WP3b (issue #15) — the zero-quota pre-flight (PLAN §2.7)
# ---------------------------------------------------------------------------


class _CheckSpy:
    """A stub leg that records its calls and optionally fails (the
    issue #15 "pre-flight stub-failure tests" lever)."""

    def __init__(self, fail=None) -> None:
        self.fail = fail
        self.called = 0

    def __call__(self) -> None:
        self.called += 1
        if self.fail is not None:
            raise self.fail


def test_preflight_checks_all_ok():
    r.run_preflight_checks([("auth", _CheckSpy()), ("disk", _CheckSpy())])


def test_preflight_checks_short_circuit_at_first_failure():
    docker = _CheckSpy()
    disk = _CheckSpy()
    with pytest.raises(r.PreflightFailed, match="auth"):
        r.run_preflight_checks([
            ("auth", _CheckSpy(fail=r.PreflightFailed("auth",
                                                    "not logged in"))),
            ("docker_daemon", docker),
            ("disk", disk),
        ])
    assert docker.called == 0 and disk.called == 0  # no later legs


def test_preflight_checks_wraps_foreign_exceptions():
    def boom() -> None:
        raise OSError("boom")
    with pytest.raises(r.PreflightFailed) as exc:
        r.run_preflight_checks([("docker_daemon", boom)])
    assert exc.value.check == "docker_daemon"
    assert "OSError" in exc.value.detail


def _fake_run(fail_sub, code, seen, raise_exc=None):
    """A stub subprocess: records the joined command, fails on
    ``fail_sub`` (a substring) with ``raise_exc`` (a hung leg) or a
    non-zero exit, else exits 0."""
    def run(cmd, timeout_s):
        s = " ".join(cmd)
        seen.append(s)
        if fail_sub and fail_sub in s:
            if raise_exc is not None:
                raise raise_exc
            return types.SimpleNamespace(returncode=code, stdout=b"",
                                        stderr=b"detail-boom")
        return types.SimpleNamespace(returncode=0, stdout=b"",
                                    stderr=b"")
    return run


def _ok_disk() -> "types.SimpleNamespace":
    return types.SimpleNamespace(free=10**9)


def test_preflight_all_ok_with_stubs():
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run(None, 1, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    pf(None)  # the batch phase (no image leg)
    pf("swe-bench__x-1")  # the per-instance phase (the image leg)
    assert len(seen) == 5  # 2 (batch: no image leg) + 3 (instance) — the
                          # disk leg uses disk_usage_fn, not run
    assert any("FAKEDEVIN auth status" in s for s in seen)
    assert any("FAKEDOCKER info" in s for s in seen)
    assert any("FAKEDOCKER image inspect swebench/sweb.eval.x86_64."
               "swe-bench_1776_x-1" in s for s in seen)  # the WP0 pin


def test_preflight_stub_failure_legs():
    """The stub-failure matrix: each leg fails in turn (in the
    short-circuit order auth → docker_daemon → docker_image → disk);
    the failed leg is named in the exception and its detail carries
    the stub's stderr tail."""
    # (1) the auth failure — the daemon leg never runs
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run("auth status", 1, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    with pytest.raises(r.PreflightFailed) as exc:
        pf(None)
    assert exc.value.check == "auth"
    assert "detail-boom" in exc.value.detail
    assert len(seen) == 1
    # (2) the daemon failure — auth ran, the image leg never does
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run("FAKEDOCKER info", 1, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    with pytest.raises(r.PreflightFailed) as exc:
        pf("inst-1")
    assert exc.value.check == "docker_daemon"
    assert len(seen) == 2
    # (3) the image failure (per instance only)
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run("inspect", 1, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    with pytest.raises(r.PreflightFailed) as exc:
        pf("inst-1")
    assert exc.value.check == "docker_image"
    assert "detail-boom" in exc.value.detail
    assert len(seen) == 3
    # the batch phase (None) skips the image leg: no failure there
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run("inspect", 1, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    pf(None)
    assert len(seen) == 2
    # (4) the disk failure (free < 2 × largest expected image)
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run(None, 1, []),
                    disk_usage_fn=lambda p: types.SimpleNamespace(free=100))
    with pytest.raises(r.PreflightFailed) as exc:
        pf("inst-1")
    assert exc.value.check == "disk"
    # (5) the disk boundary: free == 2 × largest passes (≥, inclusive)
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run(None, 1, []),
                    disk_usage_fn=lambda p:
                    types.SimpleNamespace(free=2 * 1024))
    pf("inst-1")


def test_preflight_hung_daemon_is_a_failure():
    """The daemon must be *responsive*: a hung ``docker info`` (a
    timeout) is a failure, not a wait (bounded by ``timeout_s``)."""
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run("FAKEDOCKER info", 1, seen,
                                 raise_exc=TimeoutError("daemon hung")),
                    disk_usage_fn=lambda p: _ok_disk())
    with pytest.raises(r.PreflightFailed) as exc:
        pf(None)
    assert exc.value.check == "docker_daemon"


def test_preflight_config_validation():
    with pytest.raises(r.RunnerConfigError):
        r.Preflight(largest_image_bytes=0)
    with pytest.raises(r.RunnerConfigError):
        r.Preflight(largest_image_bytes=True)  # bool refused uniformly
    with pytest.raises(r.RunnerConfigError):
        r.Preflight(largest_image_bytes=1, timeout_s=0)
    with pytest.raises(r.RunnerConfigError):
        r.Preflight(largest_image_bytes=1, timeout_s=True)


def test_preflight_batch_failure_aborts_before_any_session():
    """AC4 (the batch phase): a failed pre-flight before the first
    instance → no session ever starts (the factory is never called,
    the registry never goes live), the reason is recorded as a
    ``preflight_failed`` line (``instance_id: None`` — the batch
    marker), the run ends ``aborted`` (an infra failure is not a
    quota window — no sleep-to-window), and ``run_finished`` still
    fires (a clean, reported end)."""
    e = Env()

    def pf(inst):
        raise r.PreflightFailed("auth", "not logged in")

    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   preflight=pf)
    result = runner.run()
    assert result.final_state == "aborted"
    assert "preflight_failed (batch)" in result.aborted_reason
    assert e.calls == []  # no session was ever started
    assert e.registry.max == 0
    assert result.results == []  # the marker is not an instance result
    assert chain(result) == [("running", r.EVENT_ABORT, "aborted")]
    assert "sleep" not in [ev[0] for ev in e.reporter.events]
    marks = [ev for ev in e.reporter.events
            if ev[0] == "instance" and ev[1] is None]
    assert len(marks) == 1
    assert marks[0][2] == "preflight_failed"
    assert "not logged in" in marks[0][3]["reason"]
    assert marks[0][3]["phase"] == "batch"
    fins = [ev for ev in e.reporter.events
           if ev[0] == "run_finished"]
    assert fins == [("run_finished", "aborted")]


def test_preflight_instance_failure_marks_releases_claim_aborts():
    """AC4 (the per-instance phase): i0 runs, i1's pre-flight fails →
    i1 is marked ``preflight_failed`` (the reason + phase in the
    meta), its budget claim is released (no session was ever started),
    i2 never starts, and the run ends ``aborted`` with no sleep."""
    e = Env()

    def pf(inst):
        if inst == "i1":
            raise r.PreflightFailed(
                "docker_image", "image sweb.eval.x86_64.i1 not present")

    runner = e.make(["i0", "i1", "i2"],
                   {"i0": "resolved", "i1": "resolved",
                    "i2": "resolved"},
                   preflight=pf)
    result = runner.run()
    assert result.final_state == "aborted"
    assert [i for i, _ in result.results] == ["i0", "i1"]
    assert [s for _, s in result.results] == ["resolved",
                                            "preflight_failed"]
    assert [c for c, _ in e.calls] == ["i0"]  # i1's pre-flight failed
    # before its session was ever built; i2 never starts
    marks = [ev for ev in e.reporter.events
            if ev[0] == "instance" and ev[1] == "i1"]
    assert len(marks) == 1
    assert marks[0][3]["phase"] == "instance"
    assert "not present" in marks[0][3]["reason"]
    # i1's claim released — only i0's session stands
    assert runner.budget.sessions_used == 1
    assert chain(result) == [("running", r.EVENT_ABORT, "aborted")]
    assert "sleep" not in [ev[0] for ev in e.reporter.events]


def test_preflight_batch_reruns_each_window():
    """AC4 ("before every batch"): a new quota window is a new batch —
    the batch pre-flight (None) re-runs on the resume; the
    per-instance leg (each id) runs before every session."""
    e = Env()
    calls = []

    def pf(inst):
        calls.append(inst)

    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   preflight=pf,
                   config=r.BudgetConfig(max_sessions_per_day=1))
    result = runner.run()
    assert result.final_state == "done"
    assert result.backoffs == 1
    # window 1: batch(None) + i0; window 2 (after the reset): batch(None)
    # + i1
    assert calls == [None, "i0", None, "i1"]


def test_preflight_failure_is_retry_eligible_on_resume():
    """AC2: ``preflight_failed`` is a retry-class state (an infra
    state that burned no session — the re-run is free; the check
    re-runs before every batch and every instance on resume)."""
    recs = [{"instance_id": "i0", "status": "preflight_failed",
             "meta": {}, "ts": 1.0}]
    to_run, skipped = r.resume_queue(["i0", "i1"], recs)
    assert to_run == ["i0", "i1"]
    assert skipped == {}


def test_preflight_failure_resume_cycle(tmp_path):
    """The infra-blip story: run 1's *batch* pre-flight fails (the
    daemon is down) → the file has only the batch marker (no session
    ran — nothing burned); the resume re-runs *both* instances; run 2
    (the infra is fixed — no pre-flight hook) completes; a resume
    after run 2 re-runs nothing."""
    p = str(tmp_path / "run-1.jsonl")
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)

    def pf(inst):
        raise r.PreflightFailed("docker_daemon", "daemon down")

    runner = e.make(["i0", "i1"],
                   {"i0": "resolved", "i1": "resolved"},
                   reporter=ckpt, preflight=pf)
    result = runner.run()
    ckpt.close()
    assert result.final_state == "aborted"
    assert e.calls == []  # nothing burned
    records, dropped = r.load_checkpoint(p)
    assert dropped is False
    assert [rec["instance_id"] for rec in records] == [None]
    to_run, skipped = r.resume_queue(["i0", "i1"], records)
    assert to_run == ["i0", "i1"]  # both re-run — the marker consumes
    assert skipped == {}           # no queue slot
    # run 2 (the infra is fixed — no pre-flight hook)
    e2 = Env()
    ckpt2 = r.JsonlCheckpoint(p, clock=e2.clock)
    r2 = e2.make(to_run, {"i0": "resolved", "i1": "resolved"},
                 reporter=ckpt2)
    assert r2.run().final_state == "done"
    ckpt2.close()
    recs2, _ = r.load_checkpoint(p)
    assert [rec["instance_id"] for rec in recs2] == [None, "i0", "i1"]
    to_run3, _ = r.resume_queue(["i0", "i1"], recs2)
    assert to_run3 == []


# ---------------------------------------------------------------------------
# WP3b (issue #15) — the CLI startup model gate
# ---------------------------------------------------------------------------


def _write_json(tmp_path, obj, name="models.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


def test_cli_gate_free_model_passes(tmp_path, capsys):
    p = _write_json(tmp_path, _gate_fixture())
    assert r.main(["--model", "swe-2-high", "--models-json", p]) == 0
    out = capsys.readouterr().out
    assert "model gate: swe-2-high = Free" in out
    assert "swe-2-high, swe-2-max, swe-2-medium" in out


def test_cli_gate_paid_model_refused(tmp_path, capsys):
    p = _write_json(tmp_path, _gate_fixture())
    assert r.main(["--model", "swe-1-7", "--models-json", p]) == 2
    err = capsys.readouterr().err
    assert "refused" in err and "swe-1-7" in err


def test_cli_gate_shape_change_refused(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text('{"families": "nope"}')
    assert r.main(["--model", "swe-2-high", "--models-json", str(p)]) == 2
    err = capsys.readouterr().err
    assert "shape change" in err


def test_cli_gate_paid_override(tmp_path, capsys):
    p = _write_json(tmp_path, _gate_fixture())
    assert r.main(["--model", "swe-1-7", "--allow-paid",
                   "--token-budget", "1000", "--models-json", p]) == 0
    out = capsys.readouterr().out
    assert "model gate: swe-1-7 = Low cost" in out
    assert "paid override" in out


def test_cli_gate_paid_override_without_budget(tmp_path, capsys):
    p = _write_json(tmp_path, _gate_fixture())
    assert r.main(["--model", "swe-1-7", "--allow-paid",
                   "--models-json", p]) == 2
    assert "token budget" in capsys.readouterr().err


def test_cli_gate_live_devin_bin(tmp_path, capsys):
    """The live path: --devin-bin points at the WP2 mock (which
    speaks `models list --format json`) — the gate runs the real
    subprocess, zero quota."""
    w = _make_wrapper(tmp_path, f"\"{sys.executable}\" \"{MOCK_PATH}\"")
    assert r.main(["--model", "swe-2-high", "--devin-bin", w]) == 0
    assert r.main(["--model", "swe-1-7", "--devin-bin", w]) == 2


# ---------------------------------------------------------------------------
# WP3b review round 1 (PR #30) — review-finding regressions
# ---------------------------------------------------------------------------


def test_checkpoint_resume_torn_tail_repaired(tmp_path):
    """Review round 1 [blocker]: a resumed run must not append onto the
    torn tail. The file has a complete line + a torn fragment (no
    newline); the fresh :class:`~runner.JsonlCheckpoint` (the resume
    writer) truncates the fragment on open, so two appended records
    are clean *mid-file* lines on the next load (no
    ``RunnerProtocolError``), and the resume decision respected the
    torn ``quota_limited`` drop (i1 re-ran — exactly once)."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write('{"instance_id": "i1", "status": "quota_lim')  # the torn tail
    # the resume decision (the reader half)
    records, dropped = r.load_checkpoint(p)
    assert dropped is True
    assert [rec["instance_id"] for rec in records] == ["i0"]
    to_run, skipped = r.resume_queue(["i0", "i1", "i2"], records)
    assert to_run == ["i1", "i2"]
    # the writer half: open (repair), append two records, close
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(to_run, lambda inst: FakeSession(e.registry,
                                             "resolved" if inst == "i2"
                                             else "no_patch",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    # the next load is clean — no glued mid-file line, nothing dropped
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [(rec["instance_id"], rec["status"]) for rec in recs2] == [
        ("i0", "resolved"), ("i1", "no_patch"), ("i2", "resolved")]
    # and a resume after the resumed run re-runs nothing
    to_run3, _ = r.resume_queue(["i0", "i1", "i2"], recs2)
    assert to_run3 == []


def test_checkpoint_resume_torn_tail_complete_json_no_newline(tmp_path):
    """Review round 1 [blocker], the second shape: a kill after the
    JSON is complete but before the ``"\\n"`` — the record is
    *complete* (the reader keeps it) and the writer must complete it
    (newline) rather than truncate it, so the appended record is a
    clean line and the completed record is not silently lost (a lost
    ``resolved`` record would re-run a finished instance — a pass@1
    violation)."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write('{"instance_id": "i1", "status": "resolved", "meta": {}, "ts": 2.0}')
    # the reader half keeps the complete (un-newlined) record
    records, dropped = r.load_checkpoint(p)
    assert dropped is False
    assert [rec["instance_id"] for rec in records] == ["i0", "i1"]
    to_run, _ = r.resume_queue(["i0", "i1", "i2"], records)
    assert to_run == ["i2"]  # both resolved records stand — no re-run
    # the writer half completes the record instead of truncating it
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i2"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1", "i2"]
    assert [rec["status"] for rec in recs2] == ["resolved"] * 3
    # the raw file is clean: three newline-terminated lines, no glue
    raw = open(p, encoding="utf-8").read()
    assert raw.endswith("\n") and raw.count("\n") == 3


def test_checkpoint_resume_mid_file_corruption_refuses_repair(tmp_path):
    """The repair is fail-loud like the reader: a mid-file corruption
    (fsync'd — not a kill artifact) refuses the open, not a silent
    truncation."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write("GARBAGE-NOT-JSON\n")
        f.write('{"instance_id": "i2", "status": "timeout", "meta": {}, "ts": 3.0}\n')
    with pytest.raises(r.RunnerProtocolError):
        r.JsonlCheckpoint(p)


def test_resume_queue_non_dict_record_is_typed():
    """A non-dict record (a direct-API caller with a malformed list) is
    a typed contract error, not a bare AttributeError."""
    with pytest.raises(r.RunnerProtocolError, match="not a dict"):
        r.resume_queue(["i0"], ["not-a-dict"])


def test_gate_inf_token_budget_refused():
    """The paid override's token budget must be finite: ``inf`` is not
    a budget (an unbounded token budget is not a budget)."""
    fx = _gate_fixture()
    for bad in (float("inf"), float("nan")):
        g = r.gate_cost_tier(fx, "swe-1-7", allow_paid=True,
                            token_budget=bad)
        assert not g.allowed, bad
        assert "token budget" in g.reason
    # a large (but finite) int budget is still valid
    g = r.gate_cost_tier(fx, "swe-1-7", allow_paid=True,
                         token_budget=10**12)
    assert g.allowed


def test_fetch_models_list_none_stdout_fail_closed(tmp_path):
    """A stubbed ``run`` returning ``stdout=None`` (a non-str/bytes
    seam value) is a fail-closed refusal, not a traceback. The stub
    signature matches the call convention (``capture_output=`` /
    ``timeout=`` as keywords — the round-3 finding: a positional
    ``run(cmd, timeout_s)`` raised TypeError on the call itself, which
    the outer wrap swallowed, and the test never reached the ``stdout``
    handling it claimed to pin); ``match="not valid JSON"`` pins the
    stdout path specifically."""
    def run(cmd, capture_output=None, timeout=None):
        return types.SimpleNamespace(returncode=0, stdout=None,
                                    stderr=b"")
    with pytest.raises(r.ModelGateError, match="not valid JSON"):
        r.fetch_models_list("whatever", run=run)


def test_cli_gate_flags_without_model_refused(capsys):
    """The gate flags are inert without --model — the CLI refuses them
    there (an explicit contract: a silent no-op that "validated"
    nothing is a footgun)."""
    for flags in (["--allow-paid"], ["--token-budget", "1000"],
                 ["--devin-bin", "somedevin"]):
        assert r.main(flags) == 2, flags
        assert "--model" in capsys.readouterr().err
    # --models-json alone is refused too (it names the gate's input)
    assert r.main(["--models-json", "x.json"]) == 2


def test_next_quota_window_wall_clock_alignment():
    """The Runner's default clock is wall time (``time.time``): the
    backoff target is a true UTC midnight in epoch seconds (review
    finding — the WP3a default ``time.monotonic`` would align the
    sleep to a boot-dependent phase, up to ~24 h off the real quota
    reset; WP3b's per-window pre-flight makes the alignment
    load-bearing)."""
    import inspect
    import time as _t
    now = _t.time()
    w = r.next_quota_window(now)
    assert w > now and w % 86400 == 0 and w - now < 86400
    sig = inspect.signature(r.Runner.__init__)
    assert sig.parameters["clock"].default is _t.time


def test_preflight_wall_excluded_from_session_wall():
    """The *session* wall (the budget ``record_wall`` charge) excludes
    the zero-quota pre-flight time (a pre-flight consumes no session
    quota); the pre-flight wall is recorded in the failure mark's meta
    only."""
    e = Env()

    def pf(inst):
        e.clock.advance(5.0)  # the pre-flight consumes 5 s of wall

    runner = e.make(["i0"], {"i0": "resolved"}, preflight=pf,
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved", clock=e.clock, hold_s=2.0))
    runner.run()
    ev = [ev for ev in e.reporter.events if ev[0] == "instance"][0]
    assert ev[3]["wall_s"] == 2.0  # the session wall, not 2.0 + 5.0
    assert runner.budget.wall_used_s == 2.0


# ---------------------------------------------------------------------------
# WP3b review round 2 (PR #30, commit ab6a436) — review-finding regressions
# ---------------------------------------------------------------------------


def test_checkpoint_resume_newline_terminated_non_record_last_line_repaired(tmp_path):
    """Review round 2 [minor]: a newline-terminated *non-record* last
    line (the reader's contract blesses it as a droppable torn tail —
    test-pinned by ``test_load_checkpoint_last_line_wrong_shape_dropped``)
    must also be removed by the *write-side* repair, or the first
    appended record glues it into a mid-file line →
    ``RunnerProtocolError`` on the next load (the round-1 blocker's
    failure signature, new input class)."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write("GARBAGE-NOT-JSON\n")  # newline-terminated non-record last line
    # the reader half drops it
    records, dropped = r.load_checkpoint(p)
    assert dropped is True
    assert [rec["instance_id"] for rec in records] == ["i0"]
    # the writer half truncates it (no early return on the trailing \n)
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i1"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    # the next load is clean — the glued line did not happen
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [(rec["instance_id"], rec["status"]) for rec in recs2] == [
        ("i0", "resolved"), ("i1", "resolved")]
    # the raw file is clean: two newline-terminated lines, no garbage
    raw = open(p, encoding="utf-8").read()
    assert "GARBAGE" not in raw and raw.count("\n") == 2


def test_checkpoint_resume_blank_last_line_repaired(tmp_path):
    """A trailing blank line (newline-terminated empty last line) is
    dropped by the reader and truncated by the repair — the appended
    record must not glue onto it either."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write("\n")  # a trailing blank line
    records, dropped = r.load_checkpoint(p)
    assert [rec["instance_id"] for rec in records] == ["i0"]
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i1"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1"]


def test_fetch_models_list_missing_stdout_attr_fail_closed():
    """A stubbed ``run`` returning an object *without* a ``stdout``
    attribute is a fail-closed typed refusal (the round-2 finding — a
    leaked AttributeError would traceback in ``main()`` instead of
    exiting 2). The stub signature matches the call convention (the
    round-3 fix — see the sibling test); ``match="not valid JSON"``
    pins the ``getattr(proc, 'stdout', None)`` path specifically."""
    def run(cmd, capture_output=None, timeout=None):
        return types.SimpleNamespace(returncode=0)  # no .stdout / .stderr
    with pytest.raises(r.ModelGateError, match="not valid JSON"):
        r.fetch_models_list("whatever", run=run)


def test_cli_models_json_and_devin_bin_mutually_exclusive(capsys):
    """The gate's input is the file OR the live binary — the
    combination is refused (``--models-json`` would silently win; a
    silently inert ``--devin-bin`` is the same footgun class as the
    flags-without-``--model`` refusal)."""
    assert r.main(["--model", "swe-1-7", "--models-json", "x.json",
                  "--devin-bin", "somedevin"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_wall_clock_step_back_clamps_to_zero():
    """A backward wall-clock step (NTP / VM snapshot) never records a
    negative wall: the measurement sites clamp to 0.0 (a negative
    ``wall_s`` would be subtracted from the budget's ``wall_used_s``).
    A negative ``hold_s`` simulates the step."""
    e = Env()
    runner = e.make(["i0"], {"i0": "resolved"},
                   factory=lambda inst: FakeSession(e.registry,
                                                  "resolved",
                                                  clock=e.clock,
                                                  hold_s=-10.0))
    runner.run()
    ev = [ev for ev in e.reporter.events if ev[0] == "instance"][0]
    assert ev[3]["wall_s"] == 0.0  # clamped
    assert runner.budget.wall_used_s == 0.0  # nothing subtracted


# ---------------------------------------------------------------------------
# WP3b review round 3 (PR #30, commit 604890b) — review-finding regressions
# ---------------------------------------------------------------------------


def test_checkpoint_resume_non_ascii_prefix_byte_boundary(tmp_path):
    """Review round 3 [minor]: the repair's ``boundary`` is a *character*
    offset (text-mode read) while ``f.truncate`` takes *bytes* — with a
    non-ASCII record in the preserved prefix (the reader's contract
    accepts literal non-ASCII records; the writer's own records are
    ASCII via ``ensure_ascii``), truncating at the character offset
    cuts *inside* the last clean record, which the next open drops →
    a finished instance re-runs (the pass@1 violation this code exists
    to prevent)."""
    p = str(tmp_path / "run-1.jsonl")
    # Literal (non-escaped) CJK in the file: 3 chars = 9 bytes, so the
    # character and byte offsets of the prefix differ by 6 — the exact
    # shape the old ``f.truncate(boundary)`` corrupted.
    rec0 = ('{"instance_id": "i0", "status": "resolved", '
           '"meta": {"reason": "\u65e5\u672c\u8a9e"}, "ts": 1.0}')
    with open(p, "wb") as f:
        f.write(rec0.encode("utf-8") + b"\n")
        f.write(b'{"instance_id": "i1", "status": "quota_lim')  # the torn tail
    # the reader half drops the torn tail (the CJK record stands)
    records, dropped = r.load_checkpoint(p)
    assert dropped is True
    assert [rec["instance_id"] for rec in records] == ["i0"]
    # the writer half must preserve the CJK record byte-for-byte
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i1"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    # the next load is clean — the last clean record was not torn
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1"]
    assert recs2[0]["meta"]["reason"] == "日本語"
    raw = open(p, "rb").read()
    assert rec0.encode("utf-8") + b"\n" in raw  # intact, byte-for-byte


def test_checkpoint_resume_torn_tail_mid_multibyte_sequence_dropped(tmp_path):
    """A foreign torn tail ending mid-multibyte-sequence is a
    droppable tail, not a bare UnicodeDecodeError: the reader's strict
    per-line decode fails on the torn last line (the historical
    round-3 bug was that a whole-file ``errors="replace"`` decode
    masked it; f5eca02 moved the drop to the strict-decode torn-tail
    branch — ``UnicodeDecodeError`` on the last line → dropped) and
    the repair truncates it byte-exactly."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write(b'{"instance_id": "i1", "status": "quo')
        f.write(b"\xe6\x97")  # the first 2 bytes of a 3-byte CJK char — torn
    records, dropped = r.load_checkpoint(p)  # no UnicodeDecodeError
    assert dropped is True
    assert [rec["instance_id"] for rec in records] == ["i0"]
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i1"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1"]


def test_fetch_models_list_missing_returncode_fail_closed():
    """A stubbed ``run`` returning an object without a ``returncode``
    attribute is a fail-closed typed refusal (the round-3 finding — a
    bare ``proc.returncode`` in the f-string is the same leak class
    round 2 fixed for ``stdout``): ``rc`` defaults to 1 (unverified —
    fail-closed) and the message uses it, not a leaked AttributeError."""
    def run(cmd, capture_output=None, timeout=None):
        return types.SimpleNamespace(stdout="[]")  # no .returncode / .stderr
    with pytest.raises(r.ModelGateError, match="fail-closed.*1"):
        r.fetch_models_list("whatever", run=run)


# ---------------------------------------------------------------------------
# WP3b review round 4 (PR #30, commit 99de3bf) — review-finding regressions
# ---------------------------------------------------------------------------


def test_preflight_default_image_for_matches_wp0_pin():
    """The shipped default ``image_for`` matches the project's own WP0
    pin (README: ``swebench/sweb.eval.x86_64.<owner>_1776_<repo>-<PR>:
    latest``) — the ``swebench/`` namespace and the Docker-Hub escape
    of ``__`` (→ ``_1776_``) — or ``docker image inspect`` resolves to
    ``docker.io/library/sweb.eval.…`` and the leg fails on every
    instance (the round-4 finding)."""
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1024,
                    run=_fake_run(None, 0, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    pf("django__django-12345")
    inspect_cmds = [s for s in seen if "image inspect" in s]
    assert len(inspect_cmds) == 1
    assert inspect_cmds[0] == (
        "FAKEDOCKER image inspect "
        "swebench/sweb.eval.x86_64.django_1776_django-12345"
    ), inspect_cmds


def test_checkpoint_resume_mid_file_invalid_utf8_in_string_is_typed(tmp_path):
    """Review round 4 [minor]: an invalid UTF-8 byte *inside a string
    field of a mid-file (fsync'd) line* must fail loud with a typed
    error — the round-3 ``errors="replace"`` decode masked it as a
    U+FFFD character, a valid-but-unknown record the resume logic
    treats as stale → the finished instance silently re-runs (a
    pass@1 violation where the strict decode at least crashed loud)."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"instance_id": "i0", "status": "resolved", '
                b'"meta": {"reason": "a\xffb"}, "ts": 1.0}\n')
        f.write(b'{"instance_id": "i2", "status": "timeout", "meta": {}, "ts": 3.0}\n')
    with pytest.raises(r.RunnerProtocolError, match="invalid UTF-8"):
        r.load_checkpoint(p)
    # the writer-side repair fails loud too (before touching the file)
    with pytest.raises(r.RunnerProtocolError, match="invalid UTF-8"):
        r.JsonlCheckpoint(p)


def test_checkpoint_resume_torn_tail_invalid_utf8_newline_terminated(tmp_path):
    """A torn *last* line containing an invalid UTF-8 byte (even
    newline-terminated) is a droppable tail for the reader and is
    truncated by the write-side repair — the byte-exact (binary)
    boundary keeps the clean prefix intact."""
    p = str(tmp_path / "run-1.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"instance_id": "i0", "status": "resolved", "meta": {}, "ts": 1.0}\n')
        f.write(b'{"instance_id": "i1", "status": "quo\xff\n')
    records, dropped = r.load_checkpoint(p)
    assert dropped is True
    assert [rec["instance_id"] for rec in records] == ["i0"]
    e = Env()
    ckpt = r.JsonlCheckpoint(p, clock=e.clock)
    r.Runner(["i1"], lambda inst: FakeSession(e.registry, "resolved",
                                             clock=e.clock),
             clock=e.clock, sleeper=e.sleeper, reporter=ckpt).run()
    ckpt.close()
    recs2, dropped2 = r.load_checkpoint(p)
    assert dropped2 is False
    assert [rec["instance_id"] for rec in recs2] == ["i0", "i1"]


# ---------------------------------------------------------------------------
# WP3c (issue #16) — post-eval image/container pruning (PLAN §7)
# ---------------------------------------------------------------------------


class DockerFixture:
    """A stateful docker double (issue #16 "testable by: docker
    fixture"): an in-memory image store (name → layers) with a
    **layer-refcount model** (a layer is dropped from the store only
    when no remaining image references it — the docker image store's
    semantics; a ``rmi`` removes only the image's *unique* layers, the
    shared base/env layers are retained — issue #16 AC2) + a
    container table (id → [image, status]). Honors the daemon-side
    ``status=exited`` / ``ancestor=<image>`` filters and the ``rmi``
    "in use by a container" rule (a container — even an *exited* one
    — keeps the image "in use"; the ordering the pruner must get
    right). Records every joined command; failures inject per command
    substring (rc / exception)."""

    def __init__(self) -> None:
        self.images: dict = {}      # name -> [layers]
        self.layers: dict = {}     # layer -> {image names} (refcount)
        self.containers: dict = {}  # id -> [image, status]
        self.commands: list = []
        self.fail_rc: dict = {}    # command substring -> rc
        self.fail_exc: dict = {}   # command substring -> Exception
        self._next = 0

    def add_image(self, name: str, layers) -> None:
        self.images[name] = list(layers)
        for l in layers:
            self.layers.setdefault(l, set()).add(name)

    def add_container(self, image: str, status: str = "exited") -> str:
        self._next += 1
        cid = "c%08x" % self._next
        self.containers[cid] = [image, status]
        return cid

    def _result(self, rc: int, stdout: bytes = b"",
               stderr: bytes = b"") -> "types.SimpleNamespace":
        return types.SimpleNamespace(returncode=rc, stdout=stdout,
                                    stderr=stderr)

    def run(self, cmd: list, timeout_s: float):
        joined = " ".join(cmd)
        self.commands.append(joined)
        for sub, exc in self.fail_exc.items():
            if sub in joined:
                raise exc
        for sub, rc in self.fail_rc.items():
            if sub in joined:
                return self._result(rc, b"", b"fixture-boom")
        if len(cmd) >= 2 and cmd[1] == "ps":
            img = None
            for i, a in enumerate(cmd):
                if a == "--filter" and i + 1 < len(cmd) \
                        and cmd[i + 1].startswith("ancestor="):
                    img = cmd[i + 1][len("ancestor="):]
            out = []
            for cid in sorted(self.containers):
                cimg, status = self.containers[cid]
                if status != "exited":
                    continue  # the status=exited filter (daemon-side)
                if img is not None and cimg != img:
                    continue  # the ancestor filter (daemon-side)
                out.append(cid)
            body = ("\n".join(out) + "\n").encode() if out else b""
            return self._result(0, body, b"")
        if len(cmd) >= 3 and cmd[1] == "rm":
            cid = cmd[2]
            if cid in self.containers:
                del self.containers[cid]
                return self._result(0, b"", b"")
            return self._result(1, b"", b"no such container")
        if len(cmd) >= 3 and cmd[1] == "rmi":
            name = cmd[2]
            if name not in self.images:
                return self._result(1, b"", b"No such image")
            if any(ci[0] == name for ci in self.containers.values()):
                # a container (even an exited one) keeps the image in
                # use — the ordering the pruner must get right
                return self._result(1, b"",
                                   b"image is in use by a container")
            for layer in self.images[name]:
                self.layers[layer].discard(name)
            del self.images[name]
            # the refcount model: a layer is dropped from the store
            # only when no remaining image references it (AC2)
            self.layers = {l: refs for l, refs in self.layers.items()
                          if refs}
            return self._result(0, ("Untagged: " + name).encode(), b"")
        return self._result(1, b"", b"unknown subcommand")


def _wp3c_fixture() -> "tuple":
    """The WP3c docker-fixture setup (the layer model): the instance
    image is built on the base + env — it shares their layers; only
    ``i1`` is instance-unique. Returns ``(fixture, base, env,
    instance_image, instance_id)``."""
    fx = DockerFixture()
    base = "swebench/sweb.base.x86_64.ubuntu2204_py311:latest"
    env = "swebench/sweb.env.x86_64.testbed:latest"
    inst = "swebench/sweb.eval.x86_64.django_1776_django-12345"
    fx.add_image(base, ["b1", "b2"])
    fx.add_image(env, ["b1", "b2", "e1", "e2"])
    fx.add_image(inst, ["b1", "b2", "e1", "e2", "i1"])
    return fx, base, env, inst, "django__django-12345"


def test_pruner_removes_instance_image_and_exited_containers():
    """AC1: ``docker rmi sweb.eval.x86_64.<instance_id>`` + remove the
    exited containers. The fixture holds the base image, an env image,
    the instance image, the instance's two exited containers (the
    agent's + the eval's fresh one), a *running* container of the base
    image and an exited base-image container. The pruner removes the
    two instance containers + the instance image, touches nothing
    else, and the ``rmi`` (the exact WP0-pinned reference, the
    ``__`` → ``_1776_`` escape) lands **after** the container removal
    (a container, even an exited one, keeps the image "in use")."""
    fx, base, env, inst, iid = _wp3c_fixture()
    agent_c = fx.add_container(inst)  # the agent's container (exited)
    eval_c = fx.add_container(inst)  # the eval's fresh container (exited)
    running_c = fx.add_container(base, status="running")
    base_c = fx.add_container(base, status="exited")
    p = r.Pruner("FAKEDOCKER", run=fx.run)
    out = p.prune(iid)
    # AC1 — the instance image + the exited containers are removed
    assert out["image"] == inst
    assert out["image_removed"] is True
    assert sorted(out["containers_removed"]) == sorted([agent_c, eval_c])
    assert out["containers_failed"] == {} and out["errors"] == []
    assert inst not in fx.images
    assert agent_c not in fx.containers and eval_c not in fx.containers
    # the running container is untouched (never forced — the AC is to
    # remove *exited* containers)
    assert fx.containers[running_c][1] == "running"
    # the base image + its (exited) container are untouched
    assert base in fx.images and env in fx.images
    assert base_c in fx.containers
    assert not any(c.startswith("FAKEDOCKER rmi " + base)
                   for c in fx.commands)
    assert not any(c.startswith("FAKEDOCKER rmi " + env)
                   for c in fx.commands)
    assert not any(c == f"FAKEDOCKER rm {base_c}" for c in fx.commands)
    # the exact rmi (AC1's `docker rmi sweb.eval.x86_64.<instance_id>`,
    # the WP0 pin incl. the __ → _1776_ escape)
    assert f"FAKEDOCKER rmi {inst}" in fx.commands
    # the daemon-side filters are exact
    assert fx.commands[0] == (
        "FAKEDOCKER ps -a --filter status=exited "
        "--filter ancestor=" + inst + " --format " + "{{.ID}}"
    )
    # the ordering: every container rm before the rmi
    rmi_idx = fx.commands.index(f"FAKEDOCKER rmi {inst}")
    assert min(i for i, c in enumerate(fx.commands)
               if c == f"FAKEDOCKER rm {agent_c}"
               or c == f"FAKEDOCKER rm {eval_c}") < rmi_idx
    # the outcome is recorded (history + last)
    assert p.prunes == [out] and p.last_prune is out


def test_pruner_base_env_layers_retained():
    """AC2: ``docker rmi`` removes only the image's *unique* layers —
    the shared base/env layers are reference-counted and retained
    (the fixture's refcount model: a layer drops only when no
    remaining image references it). The instance image shares
    ``b1`` / ``b2`` (base) + ``e1`` / ``e2`` (env); only ``i1`` is
    instance-unique. After the prune: ``i1`` drops, base + env
    layers + the base/env images stand."""
    fx, base, env, inst, iid = _wp3c_fixture()
    r.Pruner("FAKEDOCKER", run=fx.run).prune(iid)
    assert "i1" not in fx.layers  # the instance-unique layer is removed
    assert {"b1", "b2", "e1", "e2"} <= set(fx.layers)  # base + env retained
    assert base in fx.images and env in fx.images  # never rmied


def test_pruner_running_container_blocks_rmi():
    """AC1's no-force corner: a *running* container of the instance
    image keeps the image "in use" — the pruner removes the *exited*
    one, the ``rmi`` fails (the fixture models the docker refusal),
    and the failure is **recorded, not raised** (the run continues;
    the next window's pre-flight ``disk`` leg is the safety net)."""
    fx = DockerFixture()
    inst = "swebench/sweb.eval.x86_64.x_1776_x-1"
    fx.add_image(inst, ["b1", "i1"])
    running_c = fx.add_container(inst, status="running")
    exited_c = fx.add_container(inst)
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("x__x-1")
    assert out["containers_removed"] == [exited_c]
    assert fx.containers[running_c][1] == "running"  # never touched
    assert out["image_removed"] is False  # the rmi failed (in use)
    assert out["errors"] and "in use" in " ".join(out["errors"])
    assert inst in fx.images  # still there — not forced


def test_pruner_default_image_for_matches_preflight():
    """AC1 ↔ AC3 cross-consistency: the pruner's default ``image_for``
    is the shared WP0 pin — the **same** reference the pre-flight's
    ``docker_image`` leg inspects (the pre-flight checks what the
    pruner removes; a drift would have the pre-flight bless an image
    the pruner never prunes — or vice versa)."""
    seen = []
    pf = r.Preflight("FAKEDEVIN", "FAKEDOCKER", disk_path="/",
                    largest_image_bytes=1,
                    run=_fake_run(None, 0, seen),
                    disk_usage_fn=lambda p: _ok_disk())
    pf("django__django-12345")
    inspect_target = [s for s in seen if "image inspect" in s][0].split()[-1]
    fx = DockerFixture()
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("django__django-12345")
    assert out["image"] == inspect_target
    assert out["image"] == "swebench/sweb.eval.x86_64." \
        "django_1776_django-12345"


def test_pruner_docker_failures_recorded_not_fatal():
    """The best-effort discipline: every docker operational failure is
    recorded in the outcome, never raised (disk pressure is a
    pre-flight concern — the next window's ``disk`` leg is the safety
    net). Each case: no exception out of ``prune``."""
    # (1) the container list fails (rc) — the rmi still runs (the
    # steps are independent best-effort)
    fx = DockerFixture()
    inst = "swebench/sweb.eval.x86_64.a_1776_a-1"
    fx.add_image(inst, ["i1"])
    fx.fail_rc["FAKEDOCKER ps"] = 1
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("a__a-1")
    assert out["errors"] and "container list" in " ".join(out["errors"])
    assert out["image_removed"] is True  # the rmi ran anyway
    # (2) one container rm fails — the next proceeds
    fx = DockerFixture()
    fx.add_image(inst, ["i1"])
    c1 = fx.add_container(inst)
    c2 = fx.add_container(inst)
    fx.fail_rc[f"FAKEDOCKER rm {c1}"] = 1
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("a__a-1")
    assert out["containers_removed"] == [c2]  # the next one proceeds
    assert c1 in out["containers_failed"]
    # the cascade: the failed rm leaves the container in place, so the
    # rmi legitimately fails (the image is still "in use" — realistic
    # docker semantics) — recorded, not fatal
    assert out["image_removed"] is False
    assert out["errors"] and "in use" in " ".join(out["errors"])
    # (3) the rmi fails (rc) — recorded
    fx = DockerFixture()
    fx.add_image(inst, ["i1"])
    fx.fail_rc["FAKEDOCKER rmi"] = 1
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("a__a-1")
    assert out["image_removed"] is False
    assert out["errors"]
    # (4) a missing image (an idempotent double-prune) — recorded,
    # not fatal
    fx = DockerFixture()  # the image never existed
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("a__a-1")
    assert out["image_removed"] is False
    assert out["errors"] and "No such image" in " ".join(out["errors"])
    # (5) a hung daemon (a timeout on the rmi) — recorded, not raised
    fx = DockerFixture()
    fx.add_image(inst, ["i1"])
    fx.fail_exc["FAKEDOCKER rmi"] = TimeoutError("daemon hung")
    out = r.Pruner("FAKEDOCKER", run=fx.run).prune("a__a-1")
    assert out["image_removed"] is False
    assert out["errors"] and "TimeoutError" in " ".join(out["errors"])


def test_pruner_timeout_is_passed_and_validated():
    """Every docker command is bounded by ``timeout_s`` (a hung daemon
    is a recorded error, not a stall — the run spans days); the
    validation mirrors the pre-flight's (a number > 0; a bool is
    refused)."""
    seen = []
    fx = DockerFixture()

    def run(cmd, timeout_s):
        seen.append(timeout_s)
        return fx.run(cmd, timeout_s)

    p = r.Pruner("FAKEDOCKER", run=run, timeout_s=7.5)
    p.prune("a__a-1")
    assert len(seen) == 2  # ps + rmi (no containers to rm)
    assert set(seen) == {7.5}
    # the default is bounded (a hung daemon never stalls the run)
    p2 = r.Pruner("FAKEDOCKER", run=run)
    p2.prune("b__b-1")
    assert p2.timeout_s > 0
    # the validation (the house style — bool refused uniformly)
    with pytest.raises(r.RunnerConfigError):
        r.Pruner(timeout_s=0)
    with pytest.raises(r.RunnerConfigError):
        r.Pruner(timeout_s=True)
    with pytest.raises(r.RunnerConfigError):
        r.Pruner(timeout_s=-1.0)


# ---------------------------------------------------------------------------
# WP3c (issue #16) — the runner integration
# ---------------------------------------------------------------------------


def _prune_env() -> "tuple":
    """A WP3c runner test environment: an :class:`Env`, a stateful
    docker fixture, and a :class:`~runner.Pruner` over it, with the
    images pre-created through the pruner's own mapping (the shared
    WP0 pin)."""
    e = Env()
    fx = DockerFixture()
    pruner = r.Pruner("FAKEDOCKER", run=fx.run)
    return e, fx, pruner


def _precreate(fx, pruner, ids) -> None:
    for i in ids:
        fx.add_image(pruner.image_for(i), ["i1"])


def test_runner_prunes_after_evaluated_instances():
    """AC1 (the trigger matrix): the full never-re-run class triggers
    exactly one prune each, in completion order — ``resolved`` /
    ``failed_test`` are the "evaluated" core (the harness verdicts);
    ``timeout`` / ``no_patch`` / ``agent_error`` end before a harness
    verdict but are never re-run, so their images are dead weight
    (PLAN §7). A spy asserts the ordering: the prune happens *after*
    the instance's mark — the state machine handled the status first.
    The ``agent_error`` here is a *single* one (the 2-consecutive
    abort does not fire)."""
    e, fx, pruner = _prune_env()
    statuses = {"i0": "resolved", "i1": "failed_test", "i2": "timeout",
               "i3": "no_patch", "i4": "agent_error"}
    _precreate(fx, pruner, statuses)
    pruned = []
    real = pruner.prune

    def spy(iid):
        # at prune time the instance's mark is already recorded — the
        # state machine handled the status first (the ordering)
        marks = [v for v in e.reporter.events
                if v[0] == "instance" and v[1] == iid]
        assert len(marks) == 1, (iid, e.reporter.events)
        pruned.append(iid)
        return real(iid)

    pruner.prune = spy
    runner = e.make(list(statuses), statuses, pruner=pruner)
    result = runner.run()
    assert result.final_state == "done"
    assert pruned == ["i0", "i1", "i2", "i3", "i4"]  # completion order
    assert len(pruner.prunes) == 5
    for o in pruner.prunes:
        assert o["image_removed"] is True  # the images were present
        assert o["errors"] == []


def test_runner_no_prune_for_retry_statuses():
    """The retry class is **not** pruned: ``quota_limited`` (the batch
    stops), ``eval_error`` and ``preflight_failed`` (the run aborts)
    all re-run on resume, and the resume's pre-flight ``docker_image``
    leg requires the image present — so their images are retained
    (zero prunes; the images still there)."""
    e, fx, pruner = _prune_env()
    ids = ["i5", "i6", "i7"]
    _precreate(fx, pruner, ids)

    def pf(inst):
        if inst == "i7":
            raise r.PreflightFailed("docker_image", "not present")

    runner = e.make(ids,
                   {"i5": "quota_limited", "i6": "eval_error",
                    "i7": "eval_error"},
                   pruner=pruner, preflight=pf)
    result = runner.run()
    assert result.final_state == "aborted"  # i7's pre-flight failure
    assert [s for _, s in result.results] == [
        "quota_limited", "eval_error", "preflight_failed"]
    assert pruner.prunes == []  # nothing pruned
    for i in ids:  # the images stand (the resume can find them)
        assert pruner.image_for(i) in fx.images


def test_runner_broken_pruner_is_fatal():
    """A pruner that *raises* is a contract bug (the injected pruner
    is broken — the real :class:`~runner.Pruner` records operational
    failures instead) → a host-crash-class fatal: the run re-raises,
    and ``run_finished`` is *not* reported (the run did not finish —
    the same discipline as a broken session)."""
    e = Env()

    class Broken:
        def __init__(self) -> None:
            self.prunes = []

        def prune(self, iid):
            raise RuntimeError("pruner bug")

    runner = e.make(["i0"], {"i0": "resolved"}, pruner=Broken())
    with pytest.raises(RuntimeError, match="pruner bug"):
        runner.run()
    assert not any(ev[0] == "run_finished" for ev in e.reporter.events)


def test_runner_prune_failure_not_fatal():
    """A docker operational failure (here: every ``ps`` + ``rmi``
    fails) is recorded in the outcome, **not fatal** — the run
    completes (the hygiene step never re-opens a state-machine
    decision; disk pressure is the next window's pre-flight ``disk``
    leg's job)."""
    e, fx, pruner = _prune_env()
    fx.fail_rc["FAKEDOCKER ps"] = 1
    fx.fail_rc["FAKEDOCKER rmi"] = 1
    runner = e.make(["i0", "i1"], {"i0": "resolved", "i1": "resolved"},
                   pruner=pruner)
    result = runner.run()
    assert result.final_state == "done"  # the run is unaffected
    assert len(pruner.prunes) == 2
    assert all(o["image_removed"] is False for o in pruner.prunes)
    assert all(o["errors"] for o in pruner.prunes)


def test_runner_prune_under_n_gt1():
    """A granted N>1: the pruning is per-instance (the pruner is
    GIL-safe — list appends, like the default reporter), every
    never-re-run instance is pruned exactly once."""
    e, fx, pruner = _prune_env()
    _precreate(fx, pruner, ["i0", "i1"])
    barrier = threading.Barrier(2, timeout=10)
    runner = e.make(["i0", "i1"], {"i0": "resolved", "i1": "resolved"},
                   concurrency=2, i_know_about_quota=True, pruner=pruner,
                   factory=lambda inst: FakeSession(
                       e.registry, "resolved", barrier=barrier))
    result = runner.run()
    assert result.final_state == "done"
    assert sorted(o["instance_id"] for o in pruner.prunes) == ["i0", "i1"]
    assert all(o["image_removed"] is True for o in pruner.prunes)


# ---------------------------------------------------------------------------
# WP3c (issue #16) — AC3: the pre-flight disk check is sized 2 × the
# largest expected image (implemented in WP3b's Preflight; pinned here)
# ---------------------------------------------------------------------------


def test_preflight_disk_sizing_two_x_largest_image():
    """AC3: the pre-flight ``disk`` leg's floor is exactly
    ``2 × the largest expected image`` (PLAN §7; the boundary
    behavior — failing below, passing at — is pinned by
    ``test_preflight_stub_failure_legs``). Together with the pruning
    (which keeps the disk near the floor), the 2× floor holds across
    the 500."""
    pf = r.Preflight(largest_image_bytes=512 * 1024)
    assert pf.min_free_bytes == 2 * 512 * 1024
    pf2 = r.Preflight(largest_image_bytes=3.5)
    assert pf2.min_free_bytes == 7.0  # a float sizing is exact
