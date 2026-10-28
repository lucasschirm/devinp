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
"""

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
    env = dict(os.environ)
    env.pop("BENCH_LIVE", None)
    env.pop("PYTEST_ADDOPTS", None)
    if with_flag:
        env["BENCH_LIVE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_bench_live_excluded_by_default(tmp_path: Path):
    """No ``BENCH_LIVE`` → the ``bench_live`` item is skipped: CI (which
    never sets the flag) cannot consume quota."""
    (tmp_path / "conftest.py").write_text(_SCRATCH_CONFTEST)
    (tmp_path / "test_live.py").write_text(_LIVE_TEST)
    r = _run_scratch_pytest(tmp_path, with_flag=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 skipped" in r.stdout, r.stdout
    assert "passed" not in r.stdout, r.stdout


def test_bench_live_runs_when_flag_set(tmp_path: Path):
    """``BENCH_LIVE=1`` → the ``bench_live`` item runs (opt-in path):
    the flag is what gates the real-Devin tests."""
    (tmp_path / "conftest.py").write_text(_SCRATCH_CONFTEST)
    (tmp_path / "test_live.py").write_text(_LIVE_TEST)
    r = _run_scratch_pytest(tmp_path, with_flag=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout, r.stdout
