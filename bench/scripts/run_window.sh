#!/usr/bin/env bash
# WP6c pre-flight E5 (issue #44) — ONE operator window of the 500-instance
# run (#23).
#
# A 10–50-day sequential run is one invocation per quota window, not one
# long-lived process: the operator starts a window, the harness stops at the
# daily cap (exit 42), and the next window resumes from the checkpoint
# (never re-running an instance whose outcome is decided — the pass@1
# semantics, PLAN §2.5). This script exists so that loop is *mechanical and
# logged*, and so that a failure is a hard stop instead of a retry:
#
#   0   the run is complete (the queue has no terminal gap)
#   42  the window cap was reached — resume in the next window, same command
#   4   the run FAILED — STOP. Do not loop-retry: a repeated failure burns a
#       day's quota on a broken environment, and the record is not written,
#       so nothing is learnable from a second attempt until a human reads
#       the log (live_pilot's own failure-path contract, issue #22 round 19)
#   2   configuration error (also a stop: fix the invocation)
#   3   the live-run flag gate refused the invocation (BENCH_LIVE)
#   5   the window's artifacts could not be made durable (B4) — STOP: an
#       unverified copy is the Pilot B artifact-loss failure mode (#22)
#
# It never retries, never backgrounds the process, and never edits the
# checkpoint: it runs the harness exactly once, tees the output to a
# per-window log, prints the interpretation of the exit code, and exits with
# the code it got (so `run_window.sh && ...` composes).
#
# Usage — every argument is passed through to live_pilot.py:
#   scripts/run_window.sh --pilot c --mode container --run-id full500 \
#       --workdir ./full-run --resume
#
# This script SUPPLIES ``--stop-at-window-cap`` (issue #44 E5): the WP3
# runner's default is to sleep to the next UTC window inside the process,
# which would leave this script running for days, never printing the
# resume line, never archiving. Set WP6C_KEEP_RUNNING=1 for that
# (in-process) model.
#
# Environment (all optional):
#   PYTHON              the interpreter (default: python3)
#   LIVE_PILOT          the harness path (default: ../live_pilot.py)
#   LOG_DIR             window logs (default: <workdir>/logs)
#   WP6C_KEEP_RUNNING   don't add --stop-at-window-cap (the WP6b
#                       in-process sleep model: one long-lived process)
#   WP6C_ARCHIVE_DEST   a path off the run's own disk: every window ends by
#                       archiving its artifacts there and VERIFYING the copy
#                       byte-for-byte (scripts/archive_run.py); a failed
#                       verification exits 5 (issue #44 B4)

set -uo pipefail

die() {
	echo "run_window: $*" >&2
	exit 2
}

PYTHON="${PYTHON:-python3}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="${LIVE_PILOT:-$SELF_DIR/../live_pilot.py}"
[ -n "$BASH_VERSION" ] || die "run with bash"
[ -x "$PYTHON" ] || [ "$(command -v "$PYTHON" 2>/dev/null)" ] ||
	die "no interpreter at PYTHON=$PYTHON"
[ -f "$HARNESS" ] || die "no harness at $HARNESS"

# ---- the effective invocation ------------------------------------------
#
# 1. ``--flag=value`` is normalised to ``--flag value`` (argparse accepts
#    both forms, and the scan below — and the harness — must see the same
#    thing the operator typed).
# 2. the window model is THIS script's whole reason to exist, so it
#    supplies ``--stop-at-window-cap`` itself: without it the WP3 runner
#    sleeps to the next UTC window INSIDE the process, this script never
#    returns, exit 42 never happens and the per-window archive below never
#    runs. An operator who wants the in-process model (a long-lived
#    screen session, the WP6b contract) sets WP6C_KEEP_RUNNING=1.
EFF=()
for a in "$@"; do
	case "$a" in
	--workdir=* | --run-id=* | --instances=* | --budget-file=*)
		EFF+=("${a%%=*}" "${a#*=}")
		;;
	*) EFF+=("$a") ;;
	esac
done
HAS_STOP_CAP=0
for a in ${EFF[@]+"${EFF[@]}"}; do
	[ "$a" = "--stop-at-window-cap" ] && HAS_STOP_CAP=1
done
# WP6C_KEEP_RUNNING is a flag, not a string: "0"/"false"/"no" mean "do
# not", exactly as on the command line everywhere else in this repo
KEEP_RUNNING=0
case "${WP6C_KEEP_RUNNING:-}" in
"" | 0 | [Ff]alse | [Nn]o) ;;
*) KEEP_RUNNING=1 ;;
esac
ADDED_STOP_CAP=0
if [ "$HAS_STOP_CAP" = 0 ] && [ "$KEEP_RUNNING" = 0 ]; then
	EFF+=(--stop-at-window-cap)
	HAS_STOP_CAP=1
	ADDED_STOP_CAP=1
fi

# how to print an invocation back at the operator: any argument holding
# whitespace is single-quoted, so the resume command below is the command
# they can paste (a workdir path with a space would otherwise print a
# line that runs a different one)
printable() {
	local out="" a
	for a in "$@"; do
		case "$a" in
		# quote the empty (it would vanish) and anything holding a
		# character the shell would act on — the hint's whole job is to
		# be pasted, and `foo&bar` / `we"ird` / `$x` need no space to
		# stop being the argument they print as (issue #44 review r2)
		"") a="''" ;;
		*[!A-Za-z0-9_@%+=:,./-]*)
			a="'${a//\'/\'\\\'\'}'" ;;
		esac
		out+="$a "
	done
	printf '%s' "${out% }"
}

# the workdir/run id are parsed out of the args purely to place the log
# and the archive — the harness remains the only reader of the run itself
WORKDIR=""
RUN_ID=""
prev=""
for a in ${EFF[@]+"${EFF[@]}"}; do
	case "$prev" in
	--workdir) WORKDIR="$a" ;;
	--run-id) RUN_ID="$a" ;;
	esac
	prev="$a"
done
case "${1:-}" in
-h | --help)
	# the whole header, whatever length it grows to (a fixed line range
	# silently truncates the moment somebody documents another exit code)
	tail -n +2 "${BASH_SOURCE[0]}" |
		sed -n '/^set -uo pipefail/q; s/^# \{0,1\}//p'
	exit 0
	;;
esac
[ -n "$WORKDIR" ] || die "pass --workdir <dir> (the window log goes there)"

LOG_DIR_DEFAULT=""
if [ -z "${LOG_DIR:-}" ]; then
	LOG_DIR_DEFAULT=1
	LOG_DIR="$WORKDIR/logs"
fi
mkdir -p "$LOG_DIR" || die "cannot create the log dir $LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/window-$STAMP.log"
# a same-second restart must not clobber the previous window's log
n=1
while [ -e "$LOG" ]; do
	LOG="$LOG_DIR/window-$STAMP.$n.log"
	n=$((n + 1))
done

# what the run looks like from the outside = what the harness actually
# receives: the banner states the model of the EFFECTIVE argv (an operator
# who sets WP6C_KEEP_RUNNING *and* passes the cap flag gets the cap, so
# saying "in-process sleep" there would describe a run that is not
# happening — issue #44 E5, reviewed)
ARGS="$(printable ${EFF[@]+"${EFF[@]}"})"
echo "run_window: $PYTHON $HARNESS $ARGS"
echo "run_window: log $LOG"
if [ "$HAS_STOP_CAP" = 1 ]; then
	echo "run_window: window model: one window, stop at the cap (exit 42)"
	if [ "$ADDED_STOP_CAP" = 1 ]; then
		echo "              (added --stop-at-window-cap)"
	elif [ "$KEEP_RUNNING" = 1 ]; then
		echo "              (WP6C_KEEP_RUNNING is set but the arguments"
		echo "              ask for the cap: the arguments win, so this"
		echo "              window DOES end at the cap and IS archived)"
	fi
else
	echo "run_window: window model: in-process sleep between windows"
	echo "              (WP6C_KEEP_RUNNING=1: this process stays alive"
	echo "              across UTC days — WP6b's contract; no exit 42,"
	echo "              so no per-window archive either)"
fi
echo "run_window: ----"

# one invocation, no retry, the exit code preserved through the tee
# (PIPESTATUS, not $? — the operator needs the harness' code, not tee's)
"$PYTHON" "$HARNESS" ${EFF[@]+"${EFF[@]}"} 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}

echo "run_window: ----"
case "$rc" in
0)
	echo "run_window: exit 0 — the run reported COMPLETION. Verify the"
	echo "              artifacts (scripts/verify_submission.py) before"
	echo "              packaging them (scripts/package_submission.py)."
	;;
42)
	echo "run_window: exit 42 — WINDOW CAP reached (the daily budget is"
	echo "              spent; the checkpoint holds every outcome)."
	echo "              Next window, same command:"
	echo "                $0 $ARGS"
	echo "              Status any time (read-only):"
	echo "                $PYTHON $SELF_DIR/run_status.py "
	echo "                    --workdir $(printable "$WORKDIR")"
	;;
4)
	echo "run_window: exit 4 — RUN FAILED. **STOP.** Do not loop-retry:"
	echo "              no verdict record was written and a repeated"
	echo "              attempt spends another session on the same"
	echo "              failure. Read $LOG, root-cause the failure, then"
	echo "              resume deliberately (--resume keeps every decided"
	echo "              outcome)."
	;;
3)
	echo "run_window: exit 3 — the live-run gate refused this invocation"
	echo "              (BENCH_LIVE is not set: CI never consumes quota)."
	;;
2)
	echo "run_window: exit 2 — configuration error; fix the invocation"
	echo "              before any session is spent."
	;;
*)
	echo "run_window: unexpected exit $rc — treat it as a failure: STOP,"
	echo "              read $LOG."
	;;
esac

# ---- B4: make this window's artifacts durable (issue #44) --------------
#
# After EVERY exit path, including the failure ones: the checkpoint of a run
# that died is precisely the evidence you cannot afford to lose, and Pilot
# B's was deleted after its merge because nothing copied it off the
# workspace (#22). The copy is re-read and re-hashed before this script
# reports success — a backup nobody verified is a rumour.
if [ -n "${WP6C_ARCHIVE_DEST:-}" ]; then
	ARCH=("$PYTHON" "$SELF_DIR/archive_run.py" --workdir "$WORKDIR"
		--dest "$WP6C_ARCHIVE_DEST")
	[ -n "$RUN_ID" ] && ARCH+=(--run-id "$RUN_ID")
	if ! "${ARCH[@]}"; then
		echo "run_window: STOP — this window's artifacts are NOT durable." >&2
		echo "              The run itself is intact; fix the destination and" >&2
		echo "              re-run archive_run.py before starting the next" >&2
		echo "              window (which exits 5 here for exactly that)." >&2
		exit 5
	fi
fi
exit "$rc"
