"""CI (issue #20, PLAN §6) — the ``BENCH_LIVE`` gate.

Real-Devin tests (``@pytest.mark.bench_live``) consume quota and are
excluded by default — **CI never consumes quota**. The gate lives in
``bench/conftest.py`` (register the marker + skip ``bench_live`` items
unless ``BENCH_LIVE=1``); these tests prove the actual gate code, not a
re-implementation, in a zero-quota way: a subprocess ``pytest`` over a
scratch root (a ``conftest.py`` re-exporting the *real* hooks from
``bench/conftest.py`` via importlib + a ``test_live.py`` carrying the
``bench_live`` marker) runs with and without the flag.

Acceptance mapping (issue #20 AC3 — "real-Devin tests remain behind
``BENCH_LIVE=1`` (excluded by default)"):

* default (no ``BENCH_LIVE``) → the marked item is **skipped**, nothing
  real-Devin runs → :func:`test_bench_live_excluded_by_default`
* ``BENCH_LIVE=1`` → the marked item **runs** (its body proves the flag
  reached the process; the body itself is quota-free) →
  :func:`test_bench_live_runs_when_flag_set`
* marker **registration** is load-bearing: with ``pytest_configure``
  removed and ``--strict-markers`` on, the unregistered mark is a
  collection error — the suite fails if the registration half of the
  gate goes missing (verified by re-running the scratch scenario without
  it) → :func:`test_marker_registration_is_load_bearing` (and the two
  tests above run under ``--strict-markers`` too, so a missing
  registration would turn the *positive* path red as well)
* the gate is only as strong as the marker discipline — a real-Devin
  test that *omits* the marker would run unconditionally in CI. The
  static guard below makes that omission fail the suite instead →
  :func:`test_unmarked_real_devin_tests_are_detected`
"""

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_BENCH_CONFT = Path(__file__).resolve().parent.parent / "conftest.py"

# The scratch root's conftest re-exports the REAL hooks from
# bench/conftest.py (loaded by path under a distinct module name, so the
# outer pytest's conftest module is never clobbered).
_SCRATCH_CONFTEST = textwrap.dedent(
    f"""\
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "bench_conftest_gate", {str(_BENCH_CONFT)!r}
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    def pytest_configure(config):
        _mod.pytest_configure(config)

    def pytest_collection_modifyitems(config, items):
        _mod.pytest_collection_modifyitems(config, items)
    """
)

# The negative-control variant: the collection hook only — NO
# pytest_configure, i.e. no marker registration.
_SCRATCH_CONFTEST_NO_REGISTRATION = textwrap.dedent(
    f"""\
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "bench_conftest_gate", {str(_BENCH_CONFT)!r}
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    def pytest_collection_modifyitems(config, items):
        _mod.pytest_collection_modifyitems(config, items)
    """
)

# The scratch live test: a stand-in for a real-Devin test. Its body is
# quota-free (it only proves the gate let it through with the flag set).
_LIVE_TEST = textwrap.dedent(
    """\
    import os

    import pytest

    @pytest.mark.bench_live
    def test_bench_live_runs_only_with_flag():
        assert os.environ.get("BENCH_LIVE") == "1"
    """
)


def _run_scratch_pytest(root: Path, with_flag: bool) -> subprocess.CompletedProcess:
    # The env scrub drops BENCH_LIVE and every PYTEST_* variable (not just
    # PYTEST_ADDOPTS): PYTEST_PLUGINS / PYTEST_DISABLE_PLUGIN_AUTOLOAD etc.
    # inherited from the outer run would otherwise leak into the
    # subprocess and could fail it for reasons unrelated to the gate.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.pop("BENCH_LIVE", None)
    if with_flag:
        env["BENCH_LIVE"] = "1"
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "-p", "no:cacheprovider",
            # --strict-markers: an unregistered bench_live mark (i.e. the
            # pytest_configure half of the gate missing) is a hard error,
            # so these tests also detect a lost registration.
            "--strict-markers",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _make_scratch_root(tmp_path: Path, conftest: str) -> None:
    (tmp_path / "conftest.py").write_text(conftest)
    (tmp_path / "test_live.py").write_text(_LIVE_TEST)


def test_bench_live_excluded_by_default(tmp_path: Path):
    """No ``BENCH_LIVE`` → the ``bench_live`` item is skipped: CI (which
    never sets the flag) cannot consume quota."""
    _make_scratch_root(tmp_path, _SCRATCH_CONFTEST)
    r = _run_scratch_pytest(tmp_path, with_flag=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 skipped" in r.stdout, r.stdout
    assert "passed" not in r.stdout, r.stdout


def test_bench_live_runs_when_flag_set(tmp_path: Path):
    """``BENCH_LIVE=1`` → the ``bench_live`` item runs (opt-in path):
    the flag is what gates the real-Devin tests."""
    _make_scratch_root(tmp_path, _SCRATCH_CONFTEST)
    r = _run_scratch_pytest(tmp_path, with_flag=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout, r.stdout


def test_marker_registration_is_load_bearing(tmp_path: Path):
    """Negative control: with ``pytest_configure`` removed from the
    scratch conftest, the *unregistered* mark must make the strict-markers
    run fail — so a lost registration half of the gate is detectable
    (without ``--strict-markers`` it would only warn and the skip would
    still silently work, hiding the regression)."""
    _make_scratch_root(tmp_path, _SCRATCH_CONFTEST_NO_REGISTRATION)
    r = _run_scratch_pytest(tmp_path, with_flag=True)
    assert r.returncode != 0, "unregistered bench_live mark must error under --strict-markers"
    assert "bench_live" in (r.stdout + r.stderr), r.stdout + r.stderr


# ---------------------------------------------------------------------------
# Static guard: the gate only skips MARKED tests — an unmarked real-Devin
# test would run unconditionally in CI. Fail the suite on the omission.
# ---------------------------------------------------------------------------

# Exact command strings of the real `devin` binary's quota surface
# (PLAN §6: the real-Devin tests are the ones that spawn it). A bare
# "devin" string (e.g. the predictions-file model_name_or_path in the
# WP4 evaluate tests) is NOT a signal — only a subprocess argv[0] or a
# shutil.which() lookup is.
_REAL_DEVIN_CMDS = frozenset(
    {"devin acp", "devin models list", "devin auth status"}
)


def _is_subprocess_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "Popen", "check_output", "check_call"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )


def _is_which_devin_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "shutil"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "devin"
    )


def _real_devin_signals(fn: ast.FunctionDef) -> set:
    """String evidence that *this function* drives the real ``devin``
    binary: the exact command strings, a bare ``devin`` as the
    subprocess command (argv[0]), or a ``shutil.which("devin")`` lookup."""
    hits = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _REAL_DEVIN_CMDS
        ):
            hits.add(node.value)
        elif isinstance(node, ast.Call):
            if _is_which_devin_call(node):
                hits.add('shutil.which("devin")')
            elif _is_subprocess_call(node) and node.args:
                first = node.args[0]
                if (
                    isinstance(first, (ast.List, ast.Tuple))
                    and first.elts
                    and isinstance(first.elts[0], ast.Constant)
                    and first.elts[0].value == "devin"
                ):
                    hits.add('subprocess argv[0] == "devin"')
    return hits


def _has_bench_live_mark(fn: ast.FunctionDef) -> bool:
    for dec in fn.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "mark"
            and isinstance(dec.func.value, ast.Name)
            and dec.func.value.id == "pytest"
            and bool(dec.args)
            and isinstance(dec.args[0], ast.Name)
            and dec.args[0].id == "bench_live"
        ):
            return True
    return False


def test_unmarked_real_devin_tests_are_detected():
    """The gate skips only opted-in (``bench_live``) items — a real-Devin
    test that *omits* the marker runs unconditionally in CI and consumes
    quota. This static guard makes the omission fail the suite: every
    test function whose body names the real ``devin`` binary (the exact
    command strings, a ``subprocess`` argv[0], or a ``shutil.which``
    lookup — NOT the ``"devin"`` model name or the ``mock_devin.py``
    double) must carry ``@pytest.mark.bench_live``. Zero quota: pure AST
    over the test sources."""
    failures = []
    for py in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        tree = ast.parse(py.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            signals = _real_devin_signals(fn)
            if signals and not _has_bench_live_mark(fn):
                failures.append(f"{py.name}::{fn.name} {sorted(signals)}")
    assert not failures, (
        "unmarked real-Devin tests (they would run in CI and consume "
        "quota — mark them @pytest.mark.bench_live): " + "; ".join(failures)
    )
