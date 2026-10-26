"""Pytest path shim for the flat bench/ layout.

bench/ holds top-level modules (``acp_client.py``, later ``adapter.py``
etc.) rather than a package; this makes ``import acp_client`` work no
matter how pytest is invoked (``pytest bench/`` from the repo root, or
``cd bench && pytest``).
"""

import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))
