"""Pytest path shim + the ``BENCH_LIVE`` gate for the flat bench/ layout.

bench/ holds top-level modules (``acp_client.py``, ``mock_devin.py``,
``runner.py``, ``evaluate.py``, ``report.py``) rather than a package; this
makes ``import acp_client`` work no matter how pytest is invoked (``pytest
bench/`` from the repo root, or ``cd bench && pytest``).

Quota gate (issue #20, PLAN.md §6): tests marked ``@pytest.mark.bench_live``
drive the real ``devin`` binary (quota). They are excluded by default — CI
never consumes quota — and run only when ``BENCH_LIVE=1``.
"""

import os
import sys
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "bench_live: drives the real `devin` binary (consumes quota); "
        "skipped unless BENCH_LIVE=1 (PLAN §6 — CI never consumes quota)",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("BENCH_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="BENCH_LIVE not set — real-Devin tests are excluded by "
        "default (CI never consumes quota; PLAN §6)"
    )
    for item in items:
        if "bench_live" in item.keywords:
            item.add_marker(skip_live)
